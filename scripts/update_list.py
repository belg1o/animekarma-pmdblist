#!/usr/bin/env python3
"""Rebuild a PublicMetaDB list from the top-karma episodes on animekarmalist.com.

Flow: fetch last N days of episodes -> keep karma > MIN_KARMA -> map AniList/MAL ids
to TMDB ids -> clear the PMDB list -> re-add in karma order.

Nothing is deleted until the full replacement list has been resolved.
"""
import datetime as dt
import json
import os
import sys
import time

import requests

KARMA_URL = "https://animekarmalist.com/api/episodes"
PMDB = "https://publicmetadb.com/api/external"

API_KEY = os.environ["PMDB_API_KEY"]
LIST_ID = os.environ["PMDB_LIST_ID"]
MIN_KARMA = int(os.getenv("MIN_KARMA", "200"))
WINDOW_DAYS = float(os.getenv("WINDOW_DAYS", "7"))
DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")
PAUSE = 0.1  # limit is 300 req / 10 s per IP; this keeps us far below it

HEADERS = {"Authorization": f"Bearer {API_KEY}"}
session = requests.Session()
session.headers["User-Agent"] = "pmdb-anime-karma-list/1.0"


def log(msg):
    print(msg, flush=True)


def request(method, url, **kw):
    """HTTP with retry on 429 / 5xx."""
    r = None
    for attempt in range(6):
        r = session.request(method, url, timeout=30, **kw)
        if r.status_code == 429 or r.status_code >= 500:
            try:
                wait = float(r.headers.get("Retry-After", ""))
            except ValueError:
                wait = 2 ** attempt
            log(f"  {r.status_code} from {url}, retrying in {wait:.0f}s")
            time.sleep(wait)
            continue
        return r
    return r


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- animekarmalist ----------

def fetch_episodes(start, end):
    seen, out, offset = set(), [], 0
    for _ in range(50):  # hard cap on pages
        r = request("GET", KARMA_URL,
                    params={"start": iso(start), "end": iso(end), "offset": offset})
        r.raise_for_status()
        page = r.json()
        if not page:
            break
        fresh = [e for e in page if e["_id"] not in seen]
        if not fresh:  # server ignored offset / repeated a page
            break
        seen.update(e["_id"] for e in fresh)
        out.extend(fresh)
        offset += len(page)
    return out


def pick_titles(episodes):
    eps = [e for e in episodes if (e.get("karma") or 0) > MIN_KARMA]
    eps.sort(key=lambda e: e["karma"], reverse=True)
    seen, out = set(), []
    for e in eps:  # one entry per show (a show can have 2 episodes in the window)
        key = e.get("anilistId") or e.get("malId") or e["_id"]
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


# ---------- PublicMetaDB ----------

def lookup_tmdb(ep):
    for id_type, field in (("anilist", "anilistId"), ("mal", "malId")):
        value = ep.get(field)
        if not value:
            continue
        r = request("GET", f"{PMDB}/mappings/lookup", headers=HEADERS,
                    params={"id_type": id_type, "id_value": value})
        time.sleep(PAUSE)
        if r.status_code != 200:
            continue
        results = r.json().get("results") or []
        if results and results[0].get("tmdb_id"):
            res = results[0]
            return int(res["tmdb_id"]), res.get("media_type") or "tv"
    return None


def item_id(item):
    for key in ("list_item_id", "item_id", "itemId", "id"):
        if item.get(key) is not None:
            return str(item[key])
    raise RuntimeError(f"Can't find an item id in list item: {json.dumps(item)[:300]}")


def get_list_items():
    items, page, seen = [], 1, set()
    while True:
        r = request("GET", f"{PMDB}/lists/{LIST_ID}/items", headers=HEADERS,
                    params={"page": page, "perPage": 500})
        r.raise_for_status()
        data = r.json()
        batch = data if isinstance(data, list) else (data.get("items") or data.get("results") or [])
        fresh = [i for i in batch if item_id(i) not in seen]
        if not fresh:
            break
        seen.update(item_id(i) for i in fresh)
        items.extend(fresh)
        if len(batch) < 500:
            break
        page += 1
    return items


def ok(r):
    return r is not None and 200 <= r.status_code < 300


# ---------- main ----------

def main():
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=WINDOW_DAYS)
    log(f"Window: {iso(start)} -> {iso(end)}, karma > {MIN_KARMA}")

    episodes = fetch_episodes(start, end)
    titles = pick_titles(episodes)
    log(f"{len(episodes)} episodes fetched, {len(titles)} shows above threshold")

    targets, seen_tmdb, unmapped = [], set(), []
    for ep in titles:
        found = lookup_tmdb(ep)
        if not found:
            unmapped.append(ep["name"])
            continue
        if found in seen_tmdb:  # e.g. several seasons mapping to the same TMDB show
            continue
        seen_tmdb.add(found)
        targets.append((found[0], found[1], ep["name"], ep["karma"]))

    for i, (tmdb, mt, name, karma) in enumerate(targets, 1):
        log(f"  {i:>2}. {name} (karma {karma}) -> {mt}/{tmdb}")
    for name in unmapped:
        log(f"  !! no TMDB mapping: {name}")

    if not targets:
        log("Nothing to add; leaving the list untouched.")
        return 1
    if DRY_RUN:
        existing = get_list_items()
        log(f"DRY RUN: list currently has {len(existing)} items; no changes made.")
        if existing:
            log(f"Sample existing item: {json.dumps(existing[0])[:400]}")
        return 0

    existing = get_list_items()
    log(f"Clearing {len(existing)} existing items")
    if existing:
        log(f"Sample existing item: {json.dumps(existing[0])[:400]}")
    failures = 0
    for it in existing:
        r = request("DELETE", f"{PMDB}/lists/{LIST_ID}/items/{item_id(it)}", headers=HEADERS)
        if not ok(r):
            failures += 1
            log(f"  delete failed {r.status_code}: {r.text[:200]}")
        time.sleep(PAUSE)

    log("Adding items in karma order")
    for tmdb, mt, name, _ in targets:  # sequential on purpose: insertion order = karma order
        r = request("POST", f"{PMDB}/lists/{LIST_ID}/items", headers=HEADERS,
                    json={"tmdb_id": tmdb, "media_type": mt})
        if not ok(r):
            failures += 1
            log(f"  add failed for {name}: {r.status_code} {r.text[:200]}")
        time.sleep(PAUSE)

    log(f"Done with {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())