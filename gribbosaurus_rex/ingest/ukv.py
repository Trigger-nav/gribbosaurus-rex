"""DEPRECATED — the hardcoded UKV placeholder returned constants.

Free UKV GRIBs need a Met Office DataHub key; until that's added, the
high-res regional slot is filled by DWD ICON-EU
(gribbosaurus_rex/fetch/icon.py)."""

raise ImportError(
    "gribbosaurus_rex.ingest.ukv is deprecated — use gribbosaurus_rex.fetch")
