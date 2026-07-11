from xai_enroller.ledger import Ledger
from xai_enroller.models import JobStatus


def test_ledger_persists_only_redacted_terminal_fields(tmp_path):
    path = tmp_path / "ledger.db"
    ledger = Ledger(path, b"salt")
    job_id = ledger.start("source", attempt=1)
    ledger.finish(job_id, JobStatus.SINK_FAILED, "sink_failed", "receipt")
    raw = path.read_bytes()
    for secret in [
        "sso-token",
        "device-code",
        "https://accounts.x.ai",
        "access-token",
        "refresh-token",
        "id-token",
        "person@example.com",
    ]:
        assert secret.encode() not in raw
    row = ledger.get(job_id)
    assert row["status"] == "sink_failed"
    assert row["reason_code"] == "sink_failed"
    assert row["sink_receipt_fingerprint"] == "receipt"
    assert "source" not in repr(row["source_fingerprint"])


def test_ledger_recovers_pending_jobs(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db", b"salt")
    job_id = ledger.start("source", attempt=1)
    ledger.recover_pending()
    assert ledger.get(job_id)["status"] == JobStatus.CANCELLED.value
