"""MDBList backend: list sync with bulk add/remove, plus a MAL-id resolver."""
from __future__ import annotations

import time

from .. import core
from ..core import Settings, State, Target

API = "https://api.mdblist.com"
CHUNK = 500  # items per add/remove request


class MDBList:
    name = "mdblist"

    def __init__(self, settings: Settings, state: State):
        self.settings, self.state = settings, state

    def _params(self, **extra):
        return {"apikey": self.settings.mdblist_key, **extra}

    def _get(self, path: str, **params):
        return core.request("GET", f"{API}{path}", params=self._params(**params))

    # -- id resolving (fallback for titles Fribb doesn't know) --

    def resolve(self, item):
        """MAL id -> (tmdb_id, media_type) via /mal/{show|movie}/{id}."""
        if not item.mal:
            return None
        for media_type in ("show", "movie"):
            r = self._get(f"/mal/{media_type}/{item.mal}")
            time.sleep(self.settings.pause)
            if r.status_code == 200:
                tmdb = (r.json().get("ids") or {}).get("tmdb")
                if tmdb:
                    return int(tmdb), ("tv" if media_type == "show" else "movie")
            elif r.status_code != 404:
                core.log(f"  MDBList resolve mal={item.mal} ({media_type}) -> "
                         f"{r.status_code}: {r.text[:200]}")
        return None

    # -- list contents --

    @staticmethod
    def _tmdb_of(item: dict):
        return item.get("tmdb_id") or item.get("tmdb") or item.get("id")

    def _items(self, list_id: str):
        """Current list as [(media_type, tmdb_id)] in rank order."""
        rows, seen, offset, limit = [], set(), 0, 1000
        while True:
            r = self._get(f"/lists/{list_id}/items", limit=limit, offset=offset)
            r.raise_for_status()
            data = r.json()
            batch = [("tv" if kind == "shows" else "movie", i)
                     for kind in ("shows", "movies") for i in (data.get(kind) or [])]
            fresh = [(mt, i) for mt, i in batch if (mt, self._tmdb_of(i)) not in seen]
            if not fresh:
                break
            for mt, i in fresh:
                seen.add((mt, self._tmdb_of(i)))
                rank = i.get("rank") if i.get("rank") is not None else 10 ** 9
                rows.append((rank, mt, self._tmdb_of(i)))
            if len(batch) < limit:
                break
            offset += len(batch)
        rows.sort(key=lambda row: row[0])
        return [(mt, tmdb) for _, mt, tmdb in rows]

    @staticmethod
    def _body(pairs):
        body = {}
        for mt, tmdb in pairs:
            body.setdefault("shows" if mt == "tv" else "movies", []).append({"tmdb": tmdb})
        return body

    def _modify(self, list_id: str, action: str, pairs) -> bool:
        r = core.request("POST", f"{API}/lists/{list_id}/items/{action}",
                         params=self._params(), json=self._body(pairs))
        time.sleep(self.settings.pause)
        if not core.ok(r):
            core.log(f"  {action} failed {r.status_code}: {r.text[:200]}")
            return False
        try:
            res = r.json()
        except ValueError:
            res = {}
        nf = res.get("not_found") or res.get("notfound") or {}
        missing = sum(v for v in nf.values() if isinstance(v, int))
        if action == "add" and missing:
            core.log(f"  WARNING: MDBList didn't recognise {missing} item(s) in this batch")
        return True

    @staticmethod
    def _batches(seq):
        """Consecutive same-type runs (movies and shows are separate arrays), chunked."""
        batch, kind = [], None
        for pair in seq:
            if batch and (pair[0] != kind or len(batch) >= CHUNK):
                yield batch
                batch = []
            kind = pair[0]
            batch.append(pair)
        if batch:
            yield batch

    def sync(self, list_id: str, targets: list[Target], label: str) -> int:
        """Make the static list match `targets` exactly, in order. Skips all contact if unchanged."""
        if not targets:
            core.log(f"[{label}] nothing to add; leaving the list untouched.")
            return 1
        seq = core.wanted(targets)
        if self.state.fast_skip(self.name, list_id, seq):
            core.log(f"[{label}] unchanged since the last sync; skipped without contacting MDBList.")
            return 0
        existing = self._items(list_id)
        if existing == seq:
            core.log(f"[{label}] unchanged ({len(seq)} items, same order); nothing to do.")
            if not self.settings.dry_run:
                self.state.record_synced(self.name, list_id, seq)
            return 0
        if self.settings.dry_run:
            core.log(f"[{label}] DRY RUN: would replace {len(existing)} items with {len(seq)}.")
            return 0

        core.log(f"[{label}] changed; replacing {len(existing)} items with {len(seq)}")
        failures = 0
        for i in range(0, len(existing), CHUNK):
            failures += not self._modify(list_id, "remove", existing[i:i + CHUNK])
        for batch in self._batches(seq):  # sequential, so insertion order = list order
            failures += not self._modify(list_id, "add", batch)

        after = self._items(list_id)  # self-check: did MDBList keep our order?
        if after != seq:
            diff = next((i for i, (a, b) in enumerate(zip(after, seq)) if a != b),
                        min(len(after), len(seq)))
            core.log(f"[{label}] WARNING: list now has {len(after)} items vs {len(seq)} sent; "
                     f"first difference at position {diff + 1}: got {after[diff:diff + 1]}, "
                     f"expected {seq[diff:diff + 1]}")
        core.log(f"[{label}] done with {failures} failure(s)")
        if not failures and after == seq:
            self.state.record_synced(self.name, list_id, seq)
        return 1 if (failures or after != seq) else 0
