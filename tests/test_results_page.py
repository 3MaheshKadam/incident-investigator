import json

from ui import results as R


def test_loaders_survive_missing_files(tmp_path):
    assert R.detection_rows(tmp_path / "none.json") == []
    assert R.pool_sweep(tmp_path) == []
    assert R.retrieval_evidence(tmp_path) == []
    assert R.poisoning_rows(tmp_path / "none.json") == []
    assert R.model_runs(tmp_path / "none") == []


def test_detection_summary_counts(tmp_path):
    p = tmp_path / "d.json"
    p.write_text(json.dumps([
        {"kind": "a", "detected": True, "ttd_samples": 3, "extra_incidents": 0},
        {"kind": "b", "detected": False, "ttd_samples": None, "extra_incidents": 1},
        {"kind": "healthy", "alerts": [], "false_alarms": 0},
    ]))
    s = R.detection_summary(R.detection_rows(p))
    assert s == {"faults": 2, "detected": 1, "healthy_runs": 1, "false_incidents": 1}


def test_committed_results_load():
    assert R.detection_summary(R.detection_rows())["faults"] > 0
    assert R.pool_sweep() and R.retrieval_evidence() and R.poisoning_rows()
