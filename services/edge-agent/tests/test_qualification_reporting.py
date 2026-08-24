import json
from pathlib import Path
from uuid import UUID

from veotrex_edge_agent.qualification.metrics import continuity_summary
from veotrex_edge_agent.qualification.models import (
    CameraQualificationResult,
    QualificationDecision,
    QualificationMode,
    SessionClass,
    SessionResult,
)
from veotrex_edge_agent.qualification.reporting import (
    build_report,
    human_summary,
    redact,
    write_json_report,
)


def sample_result() -> CameraQualificationResult:
    sessions = [
        SessionResult(1, 0.0, first_media_at=0.1, last_media_at=1.0, ended_at=1.1),
        SessionResult(2, 1.0, first_media_at=1.1, last_media_at=2.0, ended_at=2.1),
    ]
    return CameraQualificationResult(
        UUID("00000000-0000-0000-0000-000000000001"),
        "camera-01",
        QualificationMode.TRANSPORT,
        SessionClass.LINE_POWERED_60_SECONDS,
        True,
        sessions,
        continuity_summary(sessions),
        QualificationDecision.MEETS_STREAM_TARGET,
    )


def test_token_and_provider_identity_redaction() -> None:
    token = "abcdefghijkl.mnopqrstuvwxyz.abcdefghijkl"  # noqa: S105 - redaction fixture
    value = redact(
        {
            "access_token": token,
            "provider_device_id": "ring-123",
            "message": f"failed {token}",
            "url": f"rtsps://x:{token}@example.invalid/a",
        }
    )
    serialized = json.dumps(value)
    assert token not in serialized
    assert "ring-123" not in serialized
    assert "[REDACTED]" in serialized


def test_report_contains_only_safe_identity_and_no_media_retention(tmp_path: Path) -> None:
    report = build_report([sample_result()], {"platform": "fake"})
    output = tmp_path / "report.json"
    write_json_report(report, output)
    loaded = json.loads(output.read_text())
    assert loaded["cameras"][0]["label"] == "camera-01"
    assert loaded["privacy"] == {"audio_retained": False, "video_retained": False}
    assert "Ring" not in json.dumps(loaded)


def test_human_summary_marks_incomplete_run() -> None:
    report = build_report([sample_result()], {}, incomplete=True)
    assert "Incomplete: yes" in human_summary(report)
    assert "not regulatory certification" in human_summary(report)
