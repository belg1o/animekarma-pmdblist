"""Cached airing-aware planning; cron ticks only contact sources when work is due."""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import core, jobs
from .sources.kuroiru import (Kuroiru, WEEK_SECONDS, RECENT_DELAY_SECONDS,
                              RECENT_WINDOW_SECONDS, _meets_minimums)

DAY = 86400
VERSION = 2


@dataclass
class Policy:
    delay_minutes: float = 50
    batch_minutes: int = 30
    refresh_hours: float = 24
    karma_hours: int = 6
    popular_hours: int = 12
    retry_minutes: float = 30
    trending_time: str = "18:15"


def configured_jobs(names: list[str]) -> list[str]:
    return names or [j for j in jobs.JOBS if any(jobs.list_id(j, b) for b in jobs.BACKENDS)]


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def cron(timestamp: float) -> str:
    dt = datetime.fromtimestamp(timestamp, timezone.utc)
    return f"{dt.minute} {dt.hour} * * {(dt.weekday() + 1) % 7}"


def build_plan(data: dict, names: list[str], opts: jobs.Options, policy: Policy,
               now: float) -> dict:
    """Only predict the feed's actual next airing, never invent future weekly episodes.

    Retain the preceding day for catch-up after a refresh. Daily checks cover feed
    corrections, newly qualifying titles, finales absent from the feed and delays.
    """
    start, end = now - DAY, now + 8 * DAY
    slots: dict[tuple[str, int], set[str]] = {}

    def add(job, timestamp, reason, *, batch=False):
        quantum = policy.batch_minutes * 60 if batch else 60
        timestamp = math.ceil(timestamp / quantum) * quantum
        if start <= timestamp <= end:
            slots.setdefault((job, timestamp), set()).add(reason)

    eligible = []
    if "recent" in names:
        for anime in data["airing"]:
            if not isinstance(anime, dict) or anime.get("malid") is None:
                continue
            if not _meets_minimums(anime, opts.min_members, opts.min_score):
                continue
            try:
                airing = float(anime["time"])
                episode = int(anime.get("lastep") or 0)
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if not math.isfinite(airing) or not start - WEEK_SECONDS <= airing <= end:
                continue
            title = html.unescape(anime.get("title") or str(anime["malid"]))
            eligible.append({"mal": anime["malid"], "title": title, "next_airing_at": iso(airing)})
            # Include premieres (lastep=0): they will enter recent after their first airing.
            provider_delay = RECENT_DELAY_SECONDS if opts.hours is None else 0
            window = RECENT_WINDOW_SECONDS if opts.hours is None else opts.hours * 3600
            # Provider entry is strict: the title appears just after its 50-minute offset.
            delay = max(policy.delay_minutes * 60, provider_delay + 1)
            add("recent", airing + delay, f"airing: {title}", batch=True)
            # The source uses an inclusive window; schedule removal just after its edge.
            for release in ([airing - WEEK_SECONDS, airing] if episode > 0 else [airing]):
                if episode > 0 and release == airing - WEEK_SECONDS:
                    add("recent", release + delay,
                        f"airing catch-up: {title}", batch=True)
                reason = "leaves Kuroiru recent panel" if opts.hours is None else "look-back expires"
                add("recent", release + provider_delay + window + 1,
                    f"{reason}: {title}", batch=True)

    day = math.floor(start / DAY) * DAY
    trend_hour, trend_minute = map(int, policy.trending_time.split(":"))
    while day <= end:
        if "recent" in names:
            add("recent", day + 6 * 3600 + 30 * 60, "daily discovery / delayed feed fallback")
        for job, interval in (("karma", policy.karma_hours), ("popular", policy.popular_hours)):
            if job in names:
                for hour in range(0, 24, interval):
                    add(job, day + hour * 3600 + 30 * 60, "periodic ranking / threshold check")
        if "trending" in names:
            add("trending", day + trend_hour * 3600 + trend_minute * 60, "daily trend snapshot")
        day += DAY

    events = [{"job": job, "at": at, "at_utc": iso(at), "cron_utc": cron(at),
               "reasons": sorted(reasons)} for (job, at), reasons in sorted(
                   slots.items(), key=lambda pair: (pair[0][1], pair[0][0]))]
    return {"generated_at": now, "valid_until": end, "jobs": names,
            "eligible_airing": eligible, "events": events,
            "policy": asdict(policy), "filters": asdict(opts)}


def fetch_plan(names, opts, policy, now, source):
    # Karma/trending-only schedules do not require the airing feed.
    data = source.data if "recent" in names else {"airing": []}
    return build_plan(data, names, opts, policy, now)


def print_plan(plan: dict, now: float, *, as_json=False) -> None:
    if as_json:
        # Escapes keep redirected JSON usable on Windows legacy code pages too.
        print(json.dumps(plan, ensure_ascii=True, indent=2))
        return
    future = [event for event in plan["events"] if event["at"] > now]
    core.log(f"UTC plan through {iso(plan['valid_until'])}; "
             f"{len(plan['eligible_airing'])} airing titles pass your filters.")
    core.log("Cron previews are weekly and become stale; use tick for automatic replanning.")
    for job in plan["jobs"]:
        count = sum(event["job"] == job and event["at"] <= now + 7 * DAY for event in future)
        core.log(f"  {job}: {count} scheduled checks over the next 7 days")
    for event in future:
        core.log(f"{event['at_utc']}  {event['job']:8}  {event['cron_utc']:12}  "
                 + "; ".join(event["reasons"]))


def _signature(names, opts, policy, settings):
    config = {"version": VERSION, "jobs": names, "options": asdict(opts), "policy": asdict(policy),
              "targets": {j: {b: jobs.list_id(j, b) for b in jobs.BACKENDS} for j in names},
              "keys_present": [bool(settings.pmdb_key), bool(settings.mdblist_key)]}
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != VERSION:
            return {}
        for section in ("last_success", "last_attempt"):
            if not isinstance(data.get(section), dict):
                return {}
            if any(not isinstance(v, (int, float)) or not math.isfinite(v)
                   for v in data[section].values()):
                return {}
        plan = data.get("plan")
        if plan:
            if not isinstance(plan["events"], list):
                return {}
            for event in plan["events"]:
                if event["job"] not in jobs.JOBS or not math.isfinite(event["at"]):
                    return {}
            if not math.isfinite(plan["generated_at"]) or not math.isfinite(plan["valid_until"]):
                return {}
        return data
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return {}


def _save(path: Path, data: dict):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


@contextmanager
def _lock(path: Path):
    """OS releases the lock on process exit, including crashes; no stale lock cleanup."""
    with path.with_name(path.name + ".lock").open("a+b") as handle:
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                if handle.read(1) == b"":
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def tick(names: list[str], opts: jobs.Options, settings: core.Settings,
         policy: Policy, state_file: str) -> int:
    names = configured_jobs(names)
    if not names:
        core.log("No scheduled jobs: configure list IDs in .env or specify jobs.")
        return 1
    jobs.check_distinct_list_ids()
    path = Path(state_file)
    try:
        with _lock(path) as acquired:
            if not acquired:
                core.log("[schedule] another tick is running; skipped.")
                return 0
            return _tick(names, opts, settings, policy, path)
    except OSError as exc:
        core.log(f"[schedule] state/lock error: {exc}")
        return 1


def _tick(names, opts, settings, policy, path):
    now = time.time()
    signature = _signature(names, opts, policy, settings)
    state = _read(path)
    if state.get("signature") != signature:
        state = {"version": VERSION, "signature": signature,
                 "last_success": {}, "last_attempt": {}}
    plan = state.get("plan")
    source = Kuroiru()
    rc = 0
    if not plan or now - plan["generated_at"] >= policy.refresh_hours * 3600:
        if now - state.get("refresh_attempt", 0) >= policy.retry_minutes * 60:
            state["refresh_attempt"] = now
            try:
                plan = fetch_plan(names, opts, policy, now, source)
                state["plan"] = plan
                core.log("[schedule] refreshed airing plan.")
            except (Exception, SystemExit) as exc:
                core.log(f"[schedule] refresh failed; retry after {policy.retry_minutes:g} minutes: {exc}")
                rc = 1
            if not settings.dry_run:
                _save(path, state)
    if not plan or plan["valid_until"] < now:
        core.log("[schedule] no valid plan; no lists were synced.")
        return 1

    due = []
    for name in names:
        last = state["last_success"].get(name)
        pending = last is None or any(e["job"] == name and last < e["at"] <= now
                                      for e in plan["events"])
        if pending and now - state["last_attempt"].get(name, 0) >= policy.retry_minutes * 60:
            due.append(name)
    if not due:
        core.log("[schedule] nothing due.")
        return rc
    core.log("[schedule] due: " + ", ".join(due))
    if settings.dry_run:
        core.log("[schedule] DRY RUN: no syncs or scheduler state updates.")
        return rc
    state["last_attempt"].update({name: now for name in due})
    _save(path, state)  # back off even if a source/backend raises or the process exits
    results = {}
    try:
        rc |= jobs.run(due, opts, settings, results=results, kuroiru=source)
    finally:
        # Use the start time so events during a long sync remain due on the next tick.
        state["last_success"].update({name: now for name, result in results.items() if result == 0})
        _save(path, state)
    return rc
