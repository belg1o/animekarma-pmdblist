"""Create the list backends enabled by the configured API keys."""
from __future__ import annotations

from ..core import Settings, State
from .mdblist import MDBList
from .pmdb import PMDB


def configured(settings: Settings, state: State) -> dict[str, PMDB | MDBList]:
    backends = {}
    if settings.pmdb_key:
        backends[PMDB.name] = PMDB(settings, state)
    if settings.mdblist_key:
        backends[MDBList.name] = MDBList(settings, state)
    return backends
