# 🦖 Gribbosaurus Rex

A multi-model marine weather blending and confidence system for offshore racing, routing, and tactical decision support.

## 🧠 Concept

Instead of relying on a single forecast model, Gribbosaurus Rex:

1. Ingests multiple weather models:
   - ECMWF IFS
   - ECMWF AIFS
   - UKV (optional)

2. Compares them to real-world observations

3. Computes dynamic confidence scores

4. Produces a **blended probabilistic wind field**

---

## 🌬️ Features

- Multi-model forecast ingestion (ECMWF / AIFS / UKV-ready)
- Observation-based model scoring
- Confidence-weighted wind blending
- Vector wind processing (u/v components)
- Live interactive dashboard (Streamlit)

---

## 🧭 Output

- Wind speed & direction field
- Model confidence maps
- Blended forecast surface

---

## 🚀 Run dashboard

```bash
streamlit run dashboard/app.py