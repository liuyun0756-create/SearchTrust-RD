import socket

from scripts.export_v22_site_candidates import candidates
from app.jobs_v22.digest import verified_request_digest


def test_site_candidates_use_real_builders_offline_and_keep_the_same_parent(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("Candidate export must not contact a provider")
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    result = candidates()
    assert result == candidates()
    assert result["purpose"] == "release_candidate_synthetic_sources_v1"
    reports = result["reports"]
    assert reports["verified"]["input_manifest"]["previous_report"]["payload_checksum"] == verified_request_digest(reports["prospect"])
    assert reports["verified"]["input_manifest"]["case_id"] == reports["prospect"]["input_manifest"]["case_id"]
    assert [len(r["actions"]) for r in reports.values()] == [1, 3, 0]
    assert any(e["value"] == 503 for e in reports["prospect"]["evidence"])
    assert any(e["value"] == 750 for e in reports["verified"]["evidence"])


def test_zero_action_report_still_has_diagnosis_and_completed_analysis_policy():
    diagnosis = candidates()["reports"]["diagnosis"]
    assert diagnosis["findings"] and diagnosis["evidence"] and diagnosis["limitations"]
    assert diagnosis["action_state"] == "insufficient_basis"
    assert diagnosis["settlement_policy"] == "one_credit_per_completed_analysis"
