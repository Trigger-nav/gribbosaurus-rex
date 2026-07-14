import pandas as pd
import requests
import streamlit as st

API_URL = "http://127.0.0.1:8000"

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
race = st.sidebar.selectbox("Race area", race_names)
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

obs_rows = api("/obs", window_h=24)
if obs_rows:
    odf = pd.DataFrame(obs_rows)
    st.caption(f"{len(odf)} obs in the last 24h "
               f"({', '.join(sorted(odf['source'].unique()))})")
    st.map(odf.rename(columns={"lat": "latitude", "lon": "longitude"}),
           size=10, zoom=7)
    with st.expander("Observation table"):
        st.dataframe(odf, use_container_width=True)
else:
    st.info("No observations yet.")

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
