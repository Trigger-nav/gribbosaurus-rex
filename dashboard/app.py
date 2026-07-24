import math
import os
from urllib.parse import quote

import pandas as pd
import pydeck as pdk
import requests
import streamlit as st

# local default; the server deployment points this at the service port
API_URL = os.environ.get("GRIBBO_API_URL", "http://127.0.0.1:8000")
MS_TO_KN = 1.943844  # internals are SI; knots is a display convention

# Source identity colors: dark-surface categorical slots (validated set),
# assigned in fixed order — station/speed labels stay in text tokens.
SOURCE_COLORS = {
    "windycator": [57, 135, 229],   # blue
    "yacht": [25, 158, 112],        # aqua
    "metar": [201, 133, 0],         # yellow
    "ndbc": [144, 133, 233],        # violet
    "openmeteo": [213, 81, 129],    # magenta
    "test": [110, 110, 110],
}
COVERAGE_COLOR = [217, 89, 38]      # orange — model domain outlines
RACE_COLOR = [195, 194, 183]        # text-secondary — race area outline


def _arrow_icon(rgb):
    """Arrow SVG (pointing up) as a data-URL icon dict for IconLayer."""
    color = f"rgb({rgb[0]},{rgb[1]},{rgb[2]})"
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' width='48' height='48' "
        "viewBox='0 0 48 48'>"
        f"<g stroke='{color}' stroke-width='5' fill='none' "
        "stroke-linecap='round' stroke-linejoin='round'>"
        "<path d='M24 4 L24 42'/>"
        "<path d='M24 4 L14 17'/><path d='M24 4 L34 17'/>"
        "</g></svg>")
    return {"url": "data:image/svg+xml;charset=utf-8," + quote(svg),
            "width": 48, "height": 48, "anchorX": 24, "anchorY": 24}


ARROW_ICONS = {src: _arrow_icon(rgb) for src, rgb in SOURCE_COLORS.items()}
ARROW_FALLBACK = _arrow_icon([160, 160, 160])


DARK_STYLE = "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json"


def _rect_path(b):
    return [[b["lon_min"], b["lat_min"]], [b["lon_max"], b["lat_min"]],
            [b["lon_max"], b["lat_max"]], [b["lon_min"], b["lat_max"]],
            [b["lon_min"], b["lat_min"]]]


def _outline_layer(b, color, name):
    """Rectangle OUTLINE (explicitly unfilled polygon)."""
    return pdk.Layer(
        "PolygonLayer", data=[{"polygon": _rect_path(b), "name": name}],
        get_polygon="polygon", stroked=True, filled=False,
        get_line_color=color, line_width_min_pixels=2, pickable=False)


def _zoom_for(b):
    span = max(b["lon_max"] - b["lon_min"],
               (b["lat_max"] - b["lat_min"]) * 1.4, 1e-6)
    return max(4.5, min(10.5, math.log2(360.0 / span) - 0.5))

st.set_page_config(page_title="Gribbosaurus Rex", page_icon="🦖", layout="wide")
st.title("🦖 Gribbosaurus Rex")


def api(path, **params):
    r = requests.get(f"{API_URL}{path}", params=params, timeout=60)
    r.raise_for_status()
    return r.json()


# ------------------------------------------------------------- race selector
try:
    RACES = api("/races")
except requests.RequestException as e:
    st.error(f"API not reachable at {API_URL} — start it with "
             f"`python -m gribbosaurus_rex serve`  ({e})")
    st.stop()

race_names = [r["name"] for r in RACES]
# default the selector to the Channel (override with GRIBBO_DEFAULT_RACE)
_default_race = os.environ.get("GRIBBO_DEFAULT_RACE", "english-channel")
_default_idx = race_names.index(_default_race) if _default_race in race_names else 0
race = st.sidebar.selectbox("Race area", race_names, index=_default_idx)
race_cfg = next(r for r in RACES if r["name"] == race)
st.sidebar.caption(race_cfg["description"])
_b = race_cfg["bbox"]
_focus = ((_b["lat_min"] + _b["lat_max"]) / 2,
          (_b["lon_min"] + _b["lon_max"]) / 2)


# ---------------------------------------------------------------- model runs
st.header("Model runs")

status = api("/models/status")
st.caption(f"Fleet fetch domain · checked {status['time']}")

cols = st.columns(max(len(status["models"]), 1))
for col, m in zip(cols, status["models"]):
    with col:
        fresh = "🟢" if m["up_to_date"] else ("🟡" if m["latest_cycle"] else "🔴")
        st.metric(
            label=f"{fresh} {m['model'].upper()}",
            value=(m["latest_cycle"] or "no data")[:16],
            delta=(f"{m['cycle_age_hours']}h old · {m['size_mb']}MB"
                   if m["latest_cycle"] else "never fetched"),
            delta_color="off",
        )
        st.caption(
            f"{m['resolution']}  \n"
            f"next run {m['next_cycle'][11:16]}Z, "
            f"expected ~{m['next_expected_at'][11:16]}Z")

if st.button("Check for new runs now"):
    requests.post(f"{API_URL}/fetch", timeout=600)
    st.rerun()

with st.expander("Run history"):
    runs = api("/runs")
    if runs:
        st.dataframe(pd.DataFrame(runs), use_container_width=True)
    else:
        st.info("No runs fetched yet.")

# ---------------------------------------------------------- model confidence
st.header("Model confidence")

sc = api("/scores", race=race)
if sc["latest"]:
    src = ("earned from observations" if sc["weight_source"] == "confidence"
           else "static priors — no verification data yet")
    st.caption(f"Blend weights: {src}")
    ccols = st.columns(max(len(sc["latest"]), 1))
    for col, (m, s) in zip(ccols, sorted(sc["latest"].items())):
        col.metric(label=m.upper(), value=f"{s:.2f}",
                   delta=f"blend {sc['blend_weights'].get(m, 0):.0%}",
                   delta_color="off")

    hist = pd.DataFrame(api("/scores/history", race=race))
    if not hist.empty:
        hist["time"] = pd.to_datetime(hist["time"])
        st.line_chart(hist.pivot_table(index="time", columns="model",
                                       values="score"))
else:
    st.info("No confidence scores yet — they appear once observations "
            "have been fetched and verified against model runs.")

# ------------------------------------------------------------- observations
st.header("Recent observations")

cov_models = st.multiselect(
    "Show model coverage", race_cfg["models"],
    help="Outlines each model's native domain. Global models cover the "
         "whole map and are listed below instead of drawn.")

obs_rows = api("/obs", window_h=3)
layers = []

# race area outline (always)
layers.append(_outline_layer(_b, RACE_COLOR + [200], race))

# model coverage outlines (finite domains only)
global_models = []
for m in status["models"]:
    if m["model"] not in cov_models:
        continue
    if m["domain"]:
        layers.append(_outline_layer(m["domain"], COVERAGE_COLOR + [220],
                                     f"{m['model']} domain"))
    else:
        global_models.append(m["model"])
if global_models:
    st.caption("Global coverage (whole map): "
               + ", ".join(g.upper() for g in global_models))

if obs_rows:
    odf = pd.DataFrame(obs_rows)
    latest = (odf.sort_values("time")
                 .groupby(["source", "station"], as_index=False).last())
    latest["kn"] = (latest["wind_speed_ms"] * MS_TO_KN).round(1)
    latest["gust_kn"] = (latest["gust_ms"] * MS_TO_KN).round(1)
    latest["label"] = latest["kn"].map(
        lambda v: f"{v:.0f}" if pd.notna(v) else "-")  # ASCII-safe fallback
    # explicit r/g/b columns — a column of Python lists serializes
    # unreliably through pydeck, which silently falls back to black
    rgb = latest["source"].map(
        lambda s: SOURCE_COLORS.get(s, [160, 160, 160]))
    latest["cr"] = [c[0] for c in rgb]
    latest["cg"] = [c[1] for c in rgb]
    latest["cb"] = [c[2] for c in rgb]

    # wind ARROW per station: an IconLayer arrow rotated to where the wind
    # is GOING (dir is "coming from"), sized in SCREEN PIXELS by speed —
    # constant readability at every zoom (geographic lines don't give
    # that). Icons are per-source coloured SVG data-URLs; pickable for
    # the hover tooltip.
    latest["icon_data"] = latest["source"].map(
        lambda s: ARROW_ICONS.get(s, ARROW_FALLBACK))
    # icon points up; CCW rotation a moves the tip to bearing -a, and we
    # want bearing dir+180  =>  a = 180 - dir  (mod 360)
    latest["angle"] = (180.0 - latest["wind_dir_deg"].fillna(0.0)) % 360.0
    latest["size_px"] = (16 + latest["kn"].fillna(0.0).clip(0, 45) * 0.8) \
        .round(0)
    # pre-formatted tooltip fields (raw NaN renders as 'nan' otherwise)
    latest["kn_s"] = latest["kn"].map(
        lambda v: f"{v:.1f}" if pd.notna(v) else "?")
    latest["gust_s"] = latest["gust_kn"].map(
        lambda v: f"{v:.0f}" if pd.notna(v) else "-")
    latest["dir_s"] = latest["wind_dir_deg"].map(
        lambda v: f"{v:.0f}" if pd.notna(v) else "?")

    has_dir = latest["wind_dir_deg"].notna()
    cols = ["lon", "lat", "label", "cr", "cg", "cb", "icon_data", "angle",
            "size_px", "station", "source", "kn_s", "gust_s", "dir_s", "time"]
    plot = latest[cols].copy()

    layers += [
        pdk.Layer(  # arrows for stations with direction
            "IconLayer", data=plot[has_dir.values],
            get_position="[lon, lat]", get_icon="icon_data",
            get_size="size_px", get_angle="angle",
            size_min_pixels=14, size_max_pixels=52, pickable=True),
        pdk.Layer(  # small dot for direction-less stations (pressure-only)
            "ScatterplotLayer", data=plot[(~has_dir).values],
            get_position="[lon, lat]", get_fill_color="[cr, cg, cb]",
            radius_min_pixels=4, radius_max_pixels=6, pickable=True),
        pdk.Layer(  # knots label in text ink, tucked under the arrow
            "TextLayer", data=plot, get_position="[lon, lat]",
            get_text="label", get_color=[255, 255, 255, 230], get_size=11,
            get_pixel_offset=[0, 18], get_text_anchor='"middle"',
            get_alignment_baseline='"center"'),
    ]

    n_src = ", ".join(f"{s} ({n})"
                      for s, n in latest["source"].value_counts().items())
    st.caption(f"{len(latest)} stations reporting in the last 3h — {n_src}. "
               "Arrow shows where the wind is blowing to; number is knots.")

    st.pydeck_chart(pdk.Deck(
        map_style=DARK_STYLE,
        initial_view_state=pdk.ViewState(
            latitude=(_b["lat_min"] + _b["lat_max"]) / 2,
            longitude=(_b["lon_min"] + _b["lon_max"]) / 2,
            zoom=_zoom_for(_b)),
        layers=layers,
        tooltip={"text": "{station}\n{kn_s} kn, gust {gust_s}, "
                         "from {dir_s}°\nsource: {source}\n{time}"},
    ), use_container_width=True)

    with st.expander("Observation table"):
        full = pd.DataFrame(api("/obs", window_h=24))
        full["kn"] = (full["wind_speed_ms"] * MS_TO_KN).round(1)
        st.dataframe(full, use_container_width=True)
else:
    st.info("No observations in the last 3 hours.")

# ------------------------------------------------------------ point forecast
st.header("Point forecast — all models")

c1, c2 = st.columns(2)
lat = c1.number_input("Latitude", value=round(_focus[0], 3), format="%.3f")
lon = c2.number_input("Longitude", value=round(_focus[1], 3), format="%.3f")

MS_TO_KN = 1.943844  # internals are SI; knots is a display convention

if st.button("Get forecast"):
    data = api("/point", lat=lat, lon=lon, race=race)
    df = pd.DataFrame(data)
    df["time"] = pd.to_datetime(df["time"])
    df["wind_speed_kn"] = df["wind_speed_ms"] * MS_TO_KN

    st.subheader("Wind speed (kn)")
    st.line_chart(df.pivot_table(index="time", columns="model",
                                 values="wind_speed_kn"))
    st.subheader("Wind direction (° FROM)")
    st.line_chart(df.pivot_table(index="time", columns="model",
                                 values="wind_dir"))
    st.subheader("Pressure (hPa)")
    st.line_chart(df.pivot_table(index="time", columns="model",
                                 values="pressure"))
    with st.expander("Raw data"):
        st.dataframe(df, use_container_width=True)

# ------------------------------------------------------------ GRIB downloads
st.header("GRIB downloads")
st.caption("Latest run of each model, cropped to this race area — "
           "load straight into Expedition/Adrena/qtVlm.")

dl_cols = st.columns(max(len(race_cfg["models"]), 1))
for col, mdl in zip(dl_cols, race_cfg["models"]):
    with col:
        if st.button(f"Prepare {mdl.upper()}", key=f"prep_{mdl}"):
            r = requests.get(f"{API_URL}/grib/{mdl}",
                             params={"race": race}, timeout=300)
            if r.status_code != 200:
                st.error(r.json().get("detail", r.text))
            else:
                fname = r.headers.get("content-disposition", "").split(
                    'filename="')[-1].rstrip('"') or f"{mdl}.grib2"
                st.download_button(
                    label=f"⬇ {fname} ({len(r.content) / 1e6:.1f} MB)",
                    data=r.content, file_name=fname,
                    mime="application/octet-stream", key=f"dl_{mdl}")

# ------------------------------------------------------------- blended grid
st.header("Blended grid")

if st.button("Run blended field"):
    data = api("/grid", race=race)
    df = pd.DataFrame(data)
    df["speed_kn"] = df["speed_ms"] * MS_TO_KN
    df["uncertainty_kn"] = df["uncertainty_ms"] * MS_TO_KN
    st.map(df.rename(columns={"lat": "latitude", "lon": "longitude"}),
           size=1, zoom=7)
    st.dataframe(df, use_container_width=True)
