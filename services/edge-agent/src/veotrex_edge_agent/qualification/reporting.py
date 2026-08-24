from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from veotrex_edge_agent.qualification.models import (
    CameraQualificationResult,
    EngineeringTargets,
)

SENSITIVE_KEY = re.compile(
    r"token|authorization|password|secret|ring_account|provider_device|provider_component",
    re.IGNORECASE,
)
JWT_LIKE = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}"
)
RTSP_USERINFO = re.compile(r"rtsps?://[^/@\s]+@", re.IGNORECASE)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if SENSITIVE_KEY.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return JWT_LIKE.sub("[REDACTED]", RTSP_USERINFO.sub("rtsps://[REDACTED]@", value))
    return value


def build_report(
    results: list[CameraQualificationResult],
    environment: dict[str, Any],
    *,
    targets: EngineeringTargets | None = None,
    incomplete: bool = False,
) -> dict[str, Any]:
    configured_targets = targets or EngineeringTargets()
    report = {
        "schema_version": "1.0",
        "stage": "1D-A",
        "qualification_only": True,
        "incomplete": incomplete or any(not result.complete for result in results),
        "environment": environment,
        "engineering_targets": asdict(configured_targets),
        "cameras": [result.safe_dict() for result in results],
        "privacy": {"video_retained": False, "audio_retained": False},
    }
    return cast(dict[str, Any], redact(report))


def write_json_report(report: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(redact(report), indent=2, sort_keys=True) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=".qualification-", dir=output.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def human_summary(report: dict[str, Any]) -> str:
    lines = [
        "VeoTrex Ring stream qualification (Stage 1D-A)",
        f"Incomplete: {'yes' if report.get('incomplete') else 'no'}",
    ]
    for camera in report.get("cameras", []):
        continuity = camera["continuity"]
        lines.append(
            f"{camera['label']}: {camera['decision']}; "
            f"availability={continuity['media_availability_percent']:.3f}%; "
            f"p99_gap_ms={continuity['p99_gap_ms']}"
        )
    lines.append("Passing this engineering gate is not regulatory certification.")
    return "\n".join(lines)
