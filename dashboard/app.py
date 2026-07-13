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


# ---------------------------------------------------------------- model runs
st.header("Model runs")

try:
    status = api("/models/status")
except requests.RequestException as e:
    st.error(f"API not reachable at {API_URL} — start it with "
             f"`python -m gribbosaurus_rex serve`  ({e})")
    st.stop()

st.caption(f"Race area: **{status['race']}** · checked {status['time']}")

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

# ------------------------------------------------------------ point forecast
st.header("Point forecast — all models")

c1, c2 = st.columns(2)
lat = c1.number_input("Latitude", value=39.5, format="%.3f")
lon = c2.number_input("Longitude", value=2.6, format="%.3f")

if st.button("Get forecast"):
    data = api("/point", lat=lat, lon=lon)
    df = pd.DataFrame(data)
    df["time"] = pd.to_datetime(df["time"])

    st.subheader("Wind speed (kn)")
    st.line_chart(df.pivot_table(index="time", columns="model",
                                 values="wind_speed"))
    st.subheader("Wind direction (° FROM)")
    st.line_chart(df.pivot_table(index="time", columns="model",
                                 values="wind_dir"))
    st.subheader("Pressure (hPa)")
    st.line_chart(df.pivot_table(index="time", columns="model",
                                 values="pressure"))
    with st.expander("Raw data"):
        st.dataframe(df, use_container_width=True)

# ------------------------------------------------------------- blended grid
st.header("Blended grid")

if st.button("Run blended field"):
    data = api("/grid")
    df = pd.DataFrame(data)
    st.map(df.rename(columns={"lat": "latitude", "lon": "longitude"}),
           size=1, zoom=7)
    st.dataframe(df, use_container_width=True)
