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

PAGE_SIZE = 15  # the API returns at most 15 episodes per request
MIN_SPLIT = dt.timedelta(hours=1)


def get_page(start, end, offset):
    """One API call. Returns None when the API rejects offset > 0 (400)."""
    r = request("GET", KARMA_URL,
                params={"start": iso(start), "end": iso(end), "offset": offset})
    if r.status_code == 400 and offset > 0:
        log(f"  offset={offset} rejected (400): {r.text[:150]!r}")
        return None
    r.raise_for_status()
    return r.json()


def fetch_window(start, end):
    """Fetch every episode in [start, end].

    A page smaller than PAGE_SIZE means we have everything. A full page is paged
    through with offset if the API allows it; if it doesn't, the window is split in
    half and each half is fetched on its own, so no episodes are lost to the cap.
    """
    first = get_page(start, end, 0)
    if len(first) < PAGE_SIZE:
        return first
    out, offset = list(first), len(first)
    while True:
        page = get_page(start, end, offset)
        if page is None:  # can't page further -> split the window
            if end - start > MIN_SPLIT:
                mid = start + (end - start) / 2
                log(f"  splitting {iso(start)} -> {iso(end)} at {iso(mid)}")
                return fetch_window(start, mid) + fetch_window(mid, end)
            log(f"  WARNING: {iso(start)} -> {iso(end)} is full and can't be split further")
            return out
        out.extend(page)
        if len(page) < PAGE_SIZE:
            return out
        offset += len(page)


def fetch_episodes(start, end):
    seen, out = set(), []
    for e in fetch_window(start, end):
        if e["_id"] not in seen:  # windows share a boundary; dedupe
            seen.add(e["_id"])
            out.append(e)
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

FRIBB_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
_pmdb_debug_left = 3  # log the first few raw PMDB lookup responses to diagnose misses


def parse_tmdb(entry):
    """Fribb entry -> (tmdb_id, media_type) or None. Values are an int or a list."""
    tm = entry.get("themoviedb_id")
    if not isinstance(tm, dict):
        return None
    for media_type in ("tv", "movie"):
        v = tm.get(media_type)
        if isinstance(v, list):
            v = v[0] if v else None
        if v:
            return int(v), media_type
    return None


def load_fribb():
    """Community anime ID mapping: AniList/MAL id -> TMDB id."""
    try:
        r = request("GET", FRIBB_URL)
        r.raise_for_status()
        by_anilist, by_mal = {}, {}
        for e in r.json():
            tm = parse_tmdb(e)
            if not tm:
                continue
            if e.get("anilist_id"):
                by_anilist[e["anilist_id"]] = tm
            if e.get("mal_id"):
                by_mal[e["mal_id"]] = tm
        log(f"Fribb mapping loaded: {len(by_anilist)} AniList ids with a TMDB id")
        return by_anilist, by_mal
    except Exception as exc:  # fall back to PMDB lookup only
        log(f"WARNING: could not load Fribb mapping ({exc}); using PMDB lookup only")
        return {}, {}


def pmdb_lookup(ep):
    global _pmdb_debug_left
    for id_type, field in (("anilist", "anilistId"), ("mal", "malId")):
        value = ep.get(field)
        if not value:
            continue
        r = request("GET", f"{PMDB}/mappings/lookup", headers=HEADERS,
                    params={"id_type": id_type, "id_value": value})
        time.sleep(PAUSE)
        if _pmdb_debug_left > 0:
            _pmdb_debug_left -= 1
            log(f"  PMDB lookup {id_type}={value} -> {r.status_code}: {r.text[:300]}")
        if r.status_code != 200:
            continue
        results = r.json().get("results") or []
        if results and results[0].get("tmdb_id"):
            res = results[0]
            return int(res["tmdb_id"]), res.get("media_type") or "tv"
    return None


def lookup_tmdb(ep, fribb):
    by_anilist, by_mal = fribb
    found = by_anilist.get(ep.get("anilistId")) or by_mal.get(ep.get("malId"))
    return found or pmdb_lookup(ep)


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

    fribb = load_fribb()
    targets, seen_tmdb, unmapped = [], set(), []
    for ep in titles:
        found = lookup_tmdb(ep, fribb)
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
