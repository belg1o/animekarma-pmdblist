"""AnimeKarmaList source: shows with episode karma above a threshold, ranked by karma."""
from __future__ import annotations

import datetime as dt

from .. import core
from ..core import Item

URL = "https://animekarmalist.com/api/episodes"
PAGE_SIZE = 15  # episodes per page; `offset` is the zero-based PAGE number, not an item offset


def _iso(d: dt.datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch_window(start: dt.datetime, end: dt.datetime) -> list[dict]:
    """Every episode in [start, end], page by page (sorted by karma, 15 per page)."""
    out = []
    for page_no in range(100):  # hard cap
        r = core.request("GET", URL, params={"start": _iso(start), "end": _iso(end),
                                             "offset": page_no})
        if r.status_code == 400 and page_no > 0:  # "offset value invalid" = past the last page
            break
        r.raise_for_status()
        page = r.json()
        out.extend(page)
        if len(page) < PAGE_SIZE:  # short or empty page = last page
            break
    return out


def fetch(min_karma: int = 200, window_days: float = 7) -> list[Item]:
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=window_days)
    core.log(f"[karma] window: {_iso(start)} -> {_iso(end)}, karma > {min_karma}")

    seen, episodes = set(), []
    for e in _fetch_window(start, end):
        if e["_id"] not in seen:  # safety net against repeated pages
            seen.add(e["_id"])
            episodes.append(e)

    picked, shows = [], set()
    for e in sorted((e for e in episodes if (e.get("karma") or 0) > min_karma),
                    key=lambda e: e["karma"], reverse=True):
        key = e.get("anilistId") or e.get("malId") or e["_id"]
        if key not in shows:  # one entry per show (a show can have 2 episodes in the window)
            shows.add(key)
            picked.append(e)
    core.log(f"[karma] {len(episodes)} episodes fetched, {len(picked)} shows above threshold")
    return [Item(name=e["name"], anilist=e.get("anilistId"), mal=e.get("malId"),
                 note=f"karma {e['karma']}", raw={"karma": e["karma"]}) for e in picked]
