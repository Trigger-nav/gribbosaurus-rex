"""Registry of GRIB fetchers, keyed by the model names used in race configs."""

from __future__ import annotations

from gribbosaurus_rex.fetch.base import BaseFetcher
from gribbosaurus_rex.fetch.ecmwf_open import AifsFetcher, EcmwfOpenFetcher
from gribbosaurus_rex.fetch.gfs import GfsFetcher
from gribbosaurus_rex.fetch.icon import IconEuFetcher
from gribbosaurus_rex.fetch.meteofrance import (
    AromeAntillesFetcher,
    AromeFranceFetcher,
    ArpegeFetcher,
    ArpegeGlobalFetcher,
)

FETCHERS: dict[str, type[BaseFetcher]] = {
    "ifs": EcmwfOpenFetcher,
    "aifs": AifsFetcher,
    "gfs": GfsFetcher,
    "icon_eu": IconEuFetcher,
    # Météo-France high-res tier
    "mf_arome": AromeFranceFetcher,
    "mf_arpege": ArpegeFetcher,
    "mf_arpege_global": ArpegeGlobalFetcher,
    "mf_arome_antilles": AromeAntillesFetcher,
}

_instances: dict[str, BaseFetcher] = {}


def get_fetcher(name: str) -> BaseFetcher:
    if name not in FETCHERS:
        raise ValueError(f"Unknown model '{name}'. Known: {sorted(FETCHERS)}")
    if name not in _instances:
        _instances[name] = FETCHERS[name]()
    return _instances[name]
