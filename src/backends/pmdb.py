"""PublicMetaDB backend: list sync (one request per item) and an id-lookup resolver."""
from __future__ import annotations

import json
import time

from .. import core
from ..core import Settings, State, Target

API = "https://publicmetadb.com/api/external"


def _item_id(item: dict) -> str:
    for key in ("list_item_id", "item_id", "itemId", "id"):
        if item.get(key) is not None:
            return str(item[key])
    raise RuntimeError(f"Can't find an item id in list item: {json.dumps(item)[:300]}")


class PMDB:
    name = "pmdb"

    def __init__(self, settings: Settings, state: State):
        self.settings, self.state = settings, state
        self.headers = {"Authorization": f"Bearer {settings.pmdb_key}"}

    # -- id lookup (fallback for ids Fribb doesn't know) --

    def resolve(self, item):
        for id_type in ("anilist", "mal"):
            value = getattr(item, id_type)
            if not value:
                continue
            r = core.request("GET", f"{API}/mappings/lookup", headers=self.headers,
                             params={"id_type": id_type, "id_value": value})
            time.sleep(self.settings.pause)
            if r.status_code == 200:
                results = r.json().get("results") or []
                if results and results[0].get("tmdb_id"):
                    res = results[0]
                    return int(res["tmdb_id"]), res.get("media_type") or "tv"
            elif r.status_code != 404:  # 404 just means "no mapping"
                core.log(f"  PMDB lookup {id_type}={value} -> {r.status_code}: {r.text[:200]}")
        return None

    # -- list contents --

    def _list_items(self, list_id: str) -> list[dict]:
        items, page, seen = [], 1, set()
        while True:
            r = core.request("GET", f"{API}/lists/{list_id}/items", headers=self.headers,
                             params={"page": page, "perPage": 500})
            r.raise_for_status()
            data = r.json()
            batch = data if isinstance(data, list) else (data.get("items") or data.get("results") or [])
            fresh = [i for i in batch if _item_id(i) not in seen]
            if not fresh:
                break
            seen.update(_item_id(i) for i in fresh)
            items.extend(fresh)
            if len(batch) < 500:
                break
            page += 1
        return items

    @staticmethod
    def _sequence(items: list[dict]):
        """List contents as [(media_type, tmdb_id)] in insertion order (PMDB orders by creation)."""
        ordered = sorted(items, key=lambda i: i.get("created") or "")  # stable if no timestamps
        return [(i.get("media_type"), i.get("tmdb_id")) for i in ordered]

    def sync(self, list_id: str, targets: list[Target], label: str) -> int:
        """Make the list match `targets` exactly, in order. Skips all contact if unchanged."""
        if not targets:
            core.log(f"[{label}] nothing to add; leaving the list untouched.")
            return 1
        seq = core.wanted(targets)
        if self.state.fast_skip(self.name, list_id, seq):
            core.log(f"[{label}] unchanged since the last sync; skipped without contacting PMDB.")
            return 0
        existing = self._list_items(list_id)
        if self._sequence(existing) == seq:
            core.log(f"[{label}] unchanged ({len(seq)} items, same order); nothing to do.")
            if not self.settings.dry_run:
                self.state.record_synced(self.name, list_id, seq)
            return 0
        if self.settings.dry_run:
            core.log(f"[{label}] DRY RUN: would replace {len(existing)} items with {len(seq)}.")
            return 0

        core.log(f"[{label}] changed; replacing {len(existing)} items with {len(seq)}")
        failures, pause = 0, self.settings.pause
        for it in existing:
            r = core.request("DELETE", f"{API}/lists/{list_id}/items/{_item_id(it)}",
                             headers=self.headers)
            if not core.ok(r):
                failures += 1
                core.log(f"  delete failed {r.status_code}: {r.text[:200]}")
            time.sleep(pause)
        for t in targets:  # sequential on purpose: insertion order = list order
            r = core.request("POST", f"{API}/lists/{list_id}/items", headers=self.headers,
                             json={"tmdb_id": t.tmdb, "media_type": t.media_type})
            if not core.ok(r):
                failures += 1
                core.log(f"  add failed for {t.name}: {r.status_code} {r.text[:200]}")
            time.sleep(pause)
        core.log(f"[{label}] done with {failures} failure(s)")
        if not failures:
            self.state.record_synced(self.name, list_id, seq)
        return 1 if failures else 0
