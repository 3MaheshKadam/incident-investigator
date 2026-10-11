"""Replay and approval interface.   streamlit run ui/app.py"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.data import ScenarioData
from agent.pipeline import investigate_and_propose
from agent.remediation import ActionError, ApprovalGate, SimulatedSystem
from agent.store import Store
from evals.baseline import RuleBasedLLM
from evals.run import DATA_DIR, load_manifest
from guard.guard import Guard
from ui.replay import evidence_lines, replay_steps

st.set_page_config(page_title="Incident Investigator", layout="wide")


@st.cache_resource
def get_store() -> Store:
    path = os.environ.get("STORE_PATH", "investigator.sqlite")
    return Store(path)


@st.cache_resource
def systems() -> dict[str, SimulatedSystem]:
    return {}


def gate_for(scenario_id: str) -> ApprovalGate:
    sims = systems()
    if scenario_id not in sims:
        sims[scenario_id] = SimulatedSystem.from_data(ScenarioData(DATA_DIR / scenario_id))
    return ApprovalGate(get_store(), sims[scenario_id])


store = get_store()
st.title("Incident Investigator")
st.caption("All incidents are simulated. Nothing here touches a real system.")
page = st.sidebar.radio("View", ["Run an investigation", "Replay", "Approvals", "Audit log"])

if page == "Run an investigation":
    entries = load_manifest(DATA_DIR, "all")
    sid = st.selectbox("Scenario", [e["id"] for e in entries])
    use_guard = st.checkbox("Guard on", value=True)
    configured = os.environ.get("LLM_PROVIDER", "mock").lower() != "mock"
    options = ["Rule-based baseline (no model)"] + (["Configured model"] if configured else [])
    which = st.radio("Investigator", options)
    data = ScenarioData(DATA_DIR / sid)
    st.code(data.alert)
    if st.button("Investigate"):
        if which.startswith("Rule"):
            llm = RuleBasedLLM()
        else:
            from agent.llm import make_llm

            llm = make_llm()
        with st.spinner("Investigating..."):
            out = investigate_and_propose(
                llm, data, store, gate_for(sid), guard=Guard() if use_guard else None
            )
        st.success(f"Run {out.run_id} saved ({out.result.stop_reason}, {out.result.steps} tool calls).")

elif page == "Replay":
    runs = store.list_runs()
    if not runs:
        st.info("No runs yet. Run an investigation first.")
    else:
        labels = {f"#{r['id']} {r['scenario_id']} ({r['stop_reason']})": r["id"] for r in runs}
        run = store.get_run(labels[st.selectbox("Run", list(labels))])
        steps = replay_steps(run["trace"])
        if steps:
            i = st.slider("Step", 1, len(steps), 1) - 1
            s = steps[i]
            st.subheader(f"Step {i + 1} of {len(steps)}: {s['title']}")
            st.code(s["body"])
        if run["report"]:
            st.divider()
            st.subheader("Evidence cited")
            for line in evidence_lines(run["report"]):
                st.write(f"- {line}")

elif page == "Approvals":
    pending = store.list_proposals("pending")
    st.subheader(f"{len(pending)} waiting for a decision")
    for p in pending:
        with st.container(border=True):
            st.markdown(f"**{p['action_type']}** on `{p['target']}`  (scenario {p['scenario_id']})")
            st.write(p["summary"])
            for line in p["evidence"]:
                st.write(f"- {line}")
            c1, c2, _ = st.columns([1, 1, 6])
            try:
                if c1.button("Approve", key=f"a{p['id']}"):
                    st.success(gate_for(p["scenario_id"]).approve(p["id"], "reviewer"))
                    st.rerun()
                if c2.button("Reject", key=f"r{p['id']}"):
                    gate_for(p["scenario_id"]).reject(p["id"], "reviewer")
                    st.rerun()
            except ActionError as e:
                st.error(str(e))
    st.subheader("Decided")
    for p in store.list_proposals():
        if p["status"] != "pending":
            st.write(f"#{p['id']} {p['action_type']} {p['target']}: {p['status']} - {p['result'] or ''}")

else:
    for a in store.list_audit():
        st.text(f"{a['ts']}  {a['event']:<14} {json.dumps(a['detail'])}")
