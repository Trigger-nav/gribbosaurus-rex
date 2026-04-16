import streamlit as st
import requests
import pandas as pd
import plotly.express as px

# ----------------------------
# PAGE SETUP
# ----------------------------
st.set_page_config(layout="wide")
st.title("🦖 Gribbosaurus Rex Dashboard")

# ----------------------------
# API CONFIG
# ----------------------------
API_URL = "http://127.0.0.1:8000/wind"

# ----------------------------
# FETCH DATA SAFELY
# ----------------------------
@st.cache_data(ttl=60)
def fetch_data():
    try:
        response = requests.get(API_URL, timeout=10)
        response.raise_for_status()
        return response.json()["points"]

    except Exception:
        return None


data = fetch_data()

if data is None:
    st.error("❌ API not running or not responding")
    st.stop()

df = pd.DataFrame(data)

# ----------------------------
# DATA VALIDATION
# ----------------------------
required_cols = {"lat", "lon", "speed", "direction"}

if not required_cols.issubset(df.columns):
    st.error("❌ API returned unexpected data format")
    st.write(df)
    st.stop()

# ----------------------------
# MAP VISUALISATION
# ----------------------------
st.subheader("🌬 Wind Field")

fig = px.scatter_mapbox(
    df,
    lat="lat",
    lon="lon",
    color="direction",
    size="speed",
    color_continuous_scale="Turbo",
    zoom=6,
    height=650,
    title="🦖 Gribbosaurus Rex Wind Field"
)

fig.update_layout(mapbox_style="open-street-map")

st.plotly_chart(fig, use_container_width=True)

# ----------------------------
# RAW DATA VIEW
# ----------------------------
st.subheader("📊 Raw Output")
st.dataframe(df)

# ----------------------------
# OPTIONAL REFRESH INFO
# ----------------------------
st.caption("Auto-refresh every 60 seconds (cached)")