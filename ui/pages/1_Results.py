"""Measured results: detection, performance, retrieval, poisoning, model runs.

streamlit run ui/app.py   (open "Results" in the sidebar)
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from ui import results as R

st.set_page_config(page_title="Results", layout="wide")
st.title("Measured results")
st.caption("Read from the committed result files. Nothing here is typed in by hand.")

st.header("1. Detection on real faults")
rows = R.detection_rows()
if rows:
    s = R.detection_summary(rows)
    a, b, c, d = st.columns(4)
    a.metric("Faults injected", s["faults"])
    b.metric("Detected", s["detected"])
    c.metric("Healthy runs", s["healthy_runs"])
    d.metric("False incidents", s["false_incidents"])
    st.dataframe(rows, hide_index=True, use_container_width=True)
else:
    st.info("No detection results found.")

st.header("2. Performance of the service stack")
sweep = R.pool_sweep()
if sweep:
    st.dataframe(sweep, hide_index=True, use_container_width=True)
else:
    st.info("No performance results found.")

st.header("3. Finding the right evidence")
ev = R.retrieval_evidence()
if ev:
    st.dataframe(ev, hide_index=True, use_container_width=True)
    st.subheader("Cause ranking from runbook and past incidents")
    st.dataframe(R.retrieval_hypotheses(), hide_index=True, use_container_width=True)
else:
    st.info("No retrieval results found.")

st.header("4. Poisoned incident history")
poison = R.poisoning_rows()
if poison:
    st.dataframe(poison, hide_index=True, use_container_width=True)
else:
    st.info("No poisoning results found.")

st.header("5. Model runs")
runs = R.model_runs()
if runs:
    st.write(runs)
else:
    st.info("No model evaluation has been run yet.")
