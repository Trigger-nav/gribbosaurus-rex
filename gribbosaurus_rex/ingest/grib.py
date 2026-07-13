"""DEPRECATED — this placeholder was replaced by the real fetchers in
gribbosaurus_rex/fetch/ (ECMWF IFS/AIFS open data, GFS via NOMADS,
DWD ICON-EU). Use `python -m gribbosaurus_rex fetch-once` or the
scheduler. This module will be removed."""

raise ImportError(
    "gribbosaurus_rex.ingest.grib is deprecated — use gribbosaurus_rex.fetch")
