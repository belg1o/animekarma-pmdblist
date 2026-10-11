"""Anime id -> TMDB id: manual overrides, local cache, Fribb dataset, then backend resolvers."""
from __future__ import annotations

from . import core
from .core import Item, State, Target

FRIBB_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"

# Manual fixes for titles nothing else can map: (id_type, id) -> (TMDB id, "tv" | "movie").
OVERRIDES = {
    ("anilist", 191788): (126437, "tv"),  # Aoashi Season 2 (same TMDB show as S1)
    ("mal", 61603): (126437, "tv"),       # Aoashi Season 2
}


def parse_tmdb(entry: dict):
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


def _download_fribb():
    try:
        r = core.request("GET", FRIBB_URL)
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
        core.log(f"Fribb mapping loaded: {len(by_anilist)} AniList ids with a TMDB id")
        return by_anilist, by_mal
    except Exception as exc:  # carry on with the backend resolvers only
        core.log(f"WARNING: could not load Fribb mapping ({exc})")
        return {}, {}


class Mapper:
    """resolvers: callables Item -> (tmdb_id, media_type) | None, tried in order after Fribb."""

    def __init__(self, state: State, resolvers=()):
        self.state = state
        self.resolvers = list(resolvers)
        self._fribb = None  # downloaded lazily: only when an id isn't cached

    def resolve(self, item: Item):
        for id_type in ("anilist", "mal"):
            v = getattr(item, id_type)
            if v and (id_type, v) in OVERRIDES:
                return OVERRIDES[(id_type, v)]

        keys = [f"{t}:{getattr(item, t)}" for t in ("anilist", "mal") if getattr(item, t)]
        hit = self.state.cached_mapping(keys)
        if hit:
            return hit
        if self.state.recently_missed(keys):
            return None

        if self._fribb is None:
            self._fribb = _download_fribb()
        found = None
        for id_type, table in zip(("anilist", "mal"), self._fribb):
            v = getattr(item, id_type)
            if v and v in table:
                found = table[v]
                break
        for resolver in ([] if found else self.resolvers):
            found = resolver(item)
            if found:
                break
        self.state.remember(keys, found)
        return found

    def build_targets(self, items: list[Item]) -> list[Target]:
        """Items (already in list order) -> deduped targets. Logs what mapped and what didn't."""
        targets, seen, unmapped = [], set(), []
        for it in items:
            found = self.resolve(it)
            if not found:
                unmapped.append(it.name)
                continue
            if tuple(found) in seen:  # e.g. several seasons mapping to the same TMDB show
                continue
            seen.add(tuple(found))
            targets.append(Target(found[0], found[1], it.name, it.note))
        self.state.save()  # persist newly resolved ids
        for i, t in enumerate(targets, 1):
            core.log(f"  {i:>2}. {t.name} ({t.note}) -> {t.media_type}/{t.tmdb}")
        for name in unmapped:
            core.log(f"  !! no TMDB mapping: {name}")
        return targets
