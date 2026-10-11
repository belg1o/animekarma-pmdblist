"""Wires sources to backends: which jobs exist, where their list ids come from, how a run goes."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass

from . import core
from .backends import configured
from .ids import Mapper
from .sources import animekarma
from .sources.kuroiru import Kuroiru

JOBS = ("karma", "recent", "popular", "trending")
BACKENDS = ("pmdb", "mdblist")


@dataclass
class Options:
    min_karma: int = 200
    window_days: float = 7
    hours: float | None = None  # default: Kuroiru's Recently Released panel rules
    top: int = 30
    extract_only: bool = False
    min_members: int | None = None
    min_score: float | None = None


# job name -> how to fetch its items. Add a source here to add a list.
SOURCES = {
    "karma": lambda o, k: animekarma.fetch(o.min_karma, o.window_days),
    "recent": lambda o, k: k.recent(o.hours, min_members=o.min_members, min_score=o.min_score),
    "popular": lambda o, k: k.popular(o.top, min_members=o.min_members, min_score=o.min_score),
    "trending": lambda o, k: k.trending(o.top, min_members=o.min_members, min_score=o.min_score),
}


def list_id(job: str, backend: str) -> str | None:
    """Env var holding the list id for this job on this backend (karma keeps its old names)."""
    if backend == "pmdb":
        names = ["PMDB_LIST_ID_KARMA", "PMDB_LIST_ID"] if job == "karma" \
            else [f"PMDB_LIST_ID_{job.upper()}"]
    else:
        names = [f"MDBLIST_LIST_ID_{job.upper()}"]
    return next((os.environ[n] for n in names if os.getenv(n)), None)


def check_distinct_list_ids() -> None:
    """Refuse to run if two jobs target the same list on one backend (they would wipe each other)."""
    for backend in BACKENDS:
        seen = {}
        for job in JOBS:
            lid = list_id(job, backend)
            if not lid:
                continue
            if lid in seen:
                raise SystemExit(
                    f"REFUSING TO RUN: the {seen[lid]} and {job} jobs point at the same "
                    f"{backend} list ({lid}). Each job replaces its list's entire contents, "
                    "so every job needs its own list.")
            seen[lid] = job


def run(names: list[str], opts: Options, settings: core.Settings, *,
        results: dict[str, int] | None = None, kuroiru: Kuroiru | None = None) -> int:
    """Run the named jobs (default: every job that has a list id). Returns a process exit code."""
    names = names or (list(JOBS) if opts.extract_only else
                      [j for j in JOBS if any(list_id(j, b) for b in BACKENDS)])
    if not names:
        core.log("No jobs to run: set PMDB_LIST_ID_* and/or MDBLIST_LIST_ID_* (see env.example).")
        return 1
    kuroiru = kuroiru or Kuroiru()

    if opts.extract_only:  # print what the sources return; touch nothing
        out = {n: [asdict(i) for i in SOURCES[n](opts, kuroiru)] for n in names}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    check_distinct_list_ids()
    state = core.State(settings)
    backends = configured(settings, state)
    mapper = Mapper(state, [b.resolve for b in backends.values()])

    rc, ran = 0, 0
    for name in names:
        if results is not None:
            results[name] = 1  # only mark complete after all configured backends succeed
        targets_for = {b: list_id(name, b) for b in BACKENDS if list_id(name, b)}
        if not targets_for:
            core.log(f"[{name}] no list id set (PMDB_LIST_ID_* / MDBLIST_LIST_ID_*); skipping.")
            continue
        try:
            items = SOURCES[name](opts, kuroiru)
        except Exception as exc:  # one broken source must not stop the other jobs
            core.log(f"[{name}] source failed: {exc}")
            rc = 1
            continue
        core.log(f"[{name}] {len(items)} candidates")
        targets = mapper.build_targets(items)
        job_rc = 0
        for backend, lid in targets_for.items():
            if backend not in backends:
                core.log(f"[{name}] {backend} list id is set but its API key is not; skipping.")
                rc = 1
                job_rc = 1
                continue
            ran += 1
            result = backends[backend].sync(lid, targets, f"{name}:{backend}")
            rc |= result
            job_rc |= result
        if results is not None:
            results[name] = job_rc
    return rc if ran else 1
