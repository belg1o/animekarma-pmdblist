"""Kuroiru source (https://kuroiru.co/app): recently aired, weekly popular, daily trending.

`airingList.airing[].time` is the *next scheduled airing* for weekly anime, so the previous
broadcast is reconstructed as time - 7 days (an approximation for irregular schedules).
The Recently Released panel adds 3000 seconds to that estimate and includes rows
where floor((next airing - now + 3000) / 86400) is 3, 4, 5 or 6 and lastep > 0.
The daily trend feed refreshes every 6 hours (00/06/12/18 UTC) and is wiped at 00:00 UTC while
keeping the last 6 hours: it covers 6h at 00:00, 12h at 06:00, 18h at 12:00 and a full 24h at
18:00 UTC. Sync it shortly after 18:00 UTC.
"""
from __future__ import annotations

import html
import math
import re
import time
from datetime import datetime, timezone
from json import JSONDecoder

from .. import core
from ..core import Item

URL = "https://kuroiru.co/app"
TREND_URL = "https://kuroiru.co/data/public/quick/trendtoday.json"
WEEK_SECONDS = 7 * 24 * 60 * 60
RECENT_DELAY_SECONDS = 3000  # Kuroiru's client-side Recently Released panel
RECENT_WINDOW_SECONDS = 4 * 24 * 60 * 60
BROWSER = {"User-Agent": "Mozilla/5.0"}


def parse_airing_list(text: str) -> dict:
    match = re.search(r"\b(?:var|let|const)\s+airingList\s*=\s*", text)
    if not match:
        raise ValueError("Couldn't find the airingList assignment in /app")
    data, _ = JSONDecoder().raw_decode(text[match.end():].lstrip())
    if not isinstance(data, dict) or not isinstance(data.get("airing"), list):
        raise ValueError("Expected airingList to contain an 'airing' array")
    return data


def _iso_utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _meets_minimums(anime: dict, min_members: int | None, min_score: float | None) -> bool:
    """Enabled minimums are inclusive; missing or invalid values cannot meet them."""
    for field, minimum in (("members", min_members), ("score", min_score)):
        if minimum is None:
            continue
        raw = anime.get(field)
        if isinstance(raw, bool):
            return False
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(value) or value < minimum:
            return False
    return True


def recent_rows(data: dict, now: float, hours: float | None = None, *,
                min_members: int | None = None, min_score: float | None = None) -> list[dict]:
    results = []
    for anime in data["airing"]:
        if not isinstance(anime, dict):
            continue
        try:
            next_airing = float(anime["time"])
            episode = int(anime.get("lastep") or 0)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if episode <= 0 or not math.isfinite(next_airing):
            continue
        previous = next_airing - WEEK_SECONDS  # a weekly schedule is assumed
        if hours is None:
            # Match the provider's panel rather than imposing our own age cutoff.
            day_bucket = math.floor((next_airing - now + RECENT_DELAY_SECONDS) / 86400)
            if not 2 < day_bucket < 7:
                continue
            released = previous + RECENT_DELAY_SECONDS
        else:
            released = previous
            if not 0 <= (now - released) / 3600 <= hours:
                continue
        age_hours = (now - released) / 3600
        if not _meets_minimums(anime, min_members, min_score):
            continue
        malid = anime.get("malid")
        results.append({
            "id": malid,
            "title": html.unescape(anime.get("title") or ""),
            "episode": episode,
            "total_episodes": anime.get("totalep"),
            "estimated_released_at": _iso_utc(released),
            "next_airing_at": _iso_utc(next_airing),
            "hours_ago": round(age_hours, 2),
            "score": anime.get("score"),
            "members": anime.get("members"),
            "url": f"https://kuroiru.co/anime/{malid}" if malid is not None else None,
        })
    results.sort(key=lambda row: row["estimated_released_at"], reverse=True)
    return results


def _ranked(rows: list[dict], top: int, *,
            min_members: int | None = None, min_score: float | None = None) -> list[dict]:
    """One row per MAL id, most views first; filter before taking the top rows."""
    seen, out = set(), []
    for anime in rows:
        if not isinstance(anime, dict):
            continue
        if not _meets_minimums(anime, min_members, min_score):
            continue
        malid = anime.get("malid")
        try:
            hits = int(anime.get("hits") or 0)
        except (TypeError, ValueError):
            continue
        if malid is None or malid in seen:
            continue
        seen.add(malid)
        out.append({"id": malid, "title": html.unescape(anime.get("title") or ""), "hits": hits,
                    "change": anime.get("change"), "members": anime.get("members"),
                    "score": anime.get("score"), "url": f"https://kuroiru.co/anime/{malid}"})
    out.sort(key=lambda row: row["hits"], reverse=True)
    return out[:top]


def _items(rows: list[dict], note) -> list[Item]:
    return [Item(name=r["title"], mal=r["id"], note=note(r), raw=r) for r in rows]


class Kuroiru:
    """Fetches /app once per run and serves all three lists from it."""

    def __init__(self):
        self._text = None
        self._data = None

    @property
    def text(self) -> str:
        if self._text is None:
            r = core.request("GET", URL, headers=BROWSER)
            r.raise_for_status()
            self._text = r.text
        return self._text

    @property
    def data(self) -> dict:
        if self._data is None:
            self._data = parse_airing_list(self.text)
        return self._data

    def recent(self, hours: float | None = None, *,
               min_members: int | None = None, min_score: float | None = None) -> list[Item]:
        rows = recent_rows(self.data, time.time(), hours,
                           min_members=min_members, min_score=min_score)
        return _items(rows, lambda r: f"ep {r['episode']}, {r['hours_ago']:g}h ago")

    def popular(self, top: int = 30, *,
                min_members: int | None = None, min_score: float | None = None) -> list[Item]:
        """Weekly most-viewed ranking: `airingList.popular`, ordered by hits."""
        popular = self.data.get("popular")
        if not isinstance(popular, list):
            raise ValueError("airingList has no 'popular' array; keys seen: " + ", ".join(self.data))
        return _items(_ranked(popular, top, min_members=min_members, min_score=min_score),
                      lambda r: f"{r['hits']:,} weekly hits")

    def trending(self, top: int = 30, *,
                 min_members: int | None = None, min_score: float | None = None) -> list[Item]:
        """Daily most-viewed ranking from trendtoday.json."""
        r = core.request("GET", TREND_URL, headers=BROWSER)
        r.raise_for_status()
        return _items(_ranked(r.json(), top, min_members=min_members, min_score=min_score),
                      lambda r: f"{r['hits']:,} daily hits")

    def print_vars(self) -> None:
        """Print every `var/let/const name = [...]|{...}` on the page that parses as JSON."""
        found = 0
        for m in re.finditer(r"\b(?:var|let|const)\s+(\w+)\s*=\s*(?=[\[{])", self.text):
            try:
                data, _ = JSONDecoder().raw_decode(self.text[m.end():])
            except ValueError:
                continue  # a JS object literal, not pure JSON
            found += 1
            if isinstance(data, dict):
                desc = "object, keys: " + ", ".join(
                    f"{k}[{len(v)}]" if isinstance(v, list) else k for k, v in list(data.items())[:12])
            elif isinstance(data, list):
                sample = data[0] if data else None
                desc = f"array of {len(data)}" + (
                    f", item keys: {', '.join(sample)}" if isinstance(sample, dict) else "")
            else:
                desc = type(data).__name__
            print(f"{m.group(1)}: {desc}")
        if not found:
            print("No JSON-looking variable assignments found.")
