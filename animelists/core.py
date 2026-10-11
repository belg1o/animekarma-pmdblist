"""Shared plumbing: settings, HTTP, the local state file, and the small data types."""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

MAP_TTL_DAYS, MISS_TTL_DAYS = 30, 3  # how long resolved / unresolved ids stay cached
SECTIONS = ("lists", "map", "miss")


def log(msg: str) -> None:
    print(msg, flush=True)


def _flag(name: str) -> bool:
    return os.getenv(name, "").lower() in ("1", "true", "yes")


# ---------- settings ----------

@dataclass
class Settings:
    dry_run: bool = False       # read-only: report what would change
    force: bool = False         # ignore the local cache this run
    verify_days: float = 7      # re-check a live list at least this often
    pause: float = 0.1          # between API calls (PMDB allows 300 requests / 10 s per IP)
    state_file: str = "sync_state.json"
    pmdb_key: str | None = None
    mdblist_key: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        default_state = Path(__file__).resolve().parent.parent / "sync_state.json"
        return cls(
            dry_run=_flag("DRY_RUN"),
            force=_flag("FORCE"),
            verify_days=float(os.getenv("VERIFY_DAYS", "7")),
            state_file=os.getenv("STATE_FILE") or str(default_state),
            pmdb_key=os.getenv("PMDB_API_KEY"),
            mdblist_key=os.getenv("MDBLIST_API_KEY"),
        )


# ---------- data types ----------

@dataclass
class Item:
    """What a source yields: a title with whatever anime ids it knows."""
    name: str
    anilist: int | None = None
    mal: int | None = None
    note: str = ""
    raw: dict = field(default_factory=dict)  # extra source fields, shown by --extract-only


@dataclass
class Target:
    """A title resolved to TMDB, ready to go on a list."""
    tmdb: int
    media_type: str  # "tv" | "movie"
    name: str
    note: str = ""


def wanted(targets: list[Target]) -> list[tuple[str, int]]:
    return [(t.media_type, t.tmdb) for t in targets]


# ---------- HTTP ----------

_session = requests.Session()
_session.headers["User-Agent"] = "animelists/1.0"


def request(method: str, url: str, **kw):
    """HTTP with retry on 429 / 5xx. Stops with a clear message on a Cloudflare challenge."""
    r = None
    for attempt in range(6):
        r = _session.request(method, url, timeout=30, **kw)
        if r.status_code == 403 and "Just a moment" in r.text[:600]:
            raise SystemExit(
                f"BLOCKED: {url} returned a Cloudflare challenge page (403), not an API "
                "response. The site is refusing requests from this IP (datacenter IPs such "
                "as GitHub-hosted runners are often challenged). Run from a home connection "
                "or ask the site to allow this traffic.")
        if r.status_code == 429 or r.status_code >= 500:
            try:
                wait = float(r.headers.get("Retry-After", ""))
            except ValueError:
                wait = 2 ** attempt
            if wait > 120:  # e.g. a daily quota that resets at 00:00 UTC
                raise SystemExit(f"RATE LIMITED: {url} asks to retry in {wait:.0f}s; "
                                 "giving up for this run.")
            log(f"  {r.status_code} from {url}, retrying in {wait:.0f}s")
            time.sleep(wait)
            continue
        return r
    return r


def ok(r) -> bool:
    return r is not None and 200 <= r.status_code < 300


# ---------- local state: last synced contents + id mapping cache ----------

class State:
    """sync_state.json. Lets a run skip contacting a service when nothing changed, and caches ids."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.data = self._read()
        for section in SECTIONS:
            self.data.setdefault(section, {})

    def _read(self) -> dict:
        try:
            with open(self.settings.state_file, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self) -> None:
        """Merge our view into whatever is on disk (another run may have written), then save."""
        disk, now = self._read(), time.time()
        for section in SECTIONS:
            disk.setdefault(section, {}).update(self.data[section])
        disk["map"] = {k: v for k, v in disk["map"].items()
                       if now - v.get("t", 0) < MAP_TTL_DAYS * 86400}
        disk["miss"] = {k: t for k, t in disk["miss"].items() if now - t < MISS_TTL_DAYS * 86400}
        path = self.settings.state_file
        try:
            with open(path + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(disk, fh, indent=1)
            os.replace(path + ".tmp", path)
        except OSError as exc:
            log(f"WARNING: could not save {path}: {exc}")

    # -- last synced contents --

    @staticmethod
    def _fingerprint(seq) -> str:
        return hashlib.sha1(json.dumps(seq).encode()).hexdigest()

    def fast_skip(self, service: str, list_id: str, seq) -> bool:
        """True if exactly this sequence was synced (and verified) recently: no need to pull."""
        s = self.settings
        if s.force or s.dry_run:
            return False
        rec = self.data["lists"].get(f"{service}:{list_id}")
        return bool(rec and rec.get("fp") == self._fingerprint(seq)
                    and time.time() - rec.get("verified", 0) < s.verify_days * 86400)

    def record_synced(self, service: str, list_id: str, seq) -> None:
        self.data["lists"][f"{service}:{list_id}"] = {
            "fp": self._fingerprint(seq), "verified": time.time()}
        self.save()

    # -- id mapping cache --

    def cached_mapping(self, keys):
        if self.settings.force:
            return None
        now = time.time()
        for k in keys:
            hit = self.data["map"].get(k)
            if hit and now - hit.get("t", 0) < MAP_TTL_DAYS * 86400:
                return hit["tmdb"], hit["mt"]
        return None

    def recently_missed(self, keys) -> bool:
        if self.settings.force or not keys:
            return False
        now = time.time()
        return all(now - self.data["miss"].get(k, 0) < MISS_TTL_DAYS * 86400 for k in keys)

    def remember(self, keys, found) -> None:
        now = time.time()
        for k in keys:
            if found:
                self.data["map"][k] = {"tmdb": found[0], "mt": found[1], "t": now}
                self.data["miss"].pop(k, None)
            else:
                self.data["miss"][k] = now
