from uuid import UUID

from pydantic import SecretStr

from veotrex_edge_agent.qualification.cli import ManualPromptTokenProvider, parser
from veotrex_edge_agent.qualification.models import (
    CameraTarget,
    QualificationMode,
    SessionClass,
    SessionRequest,
)


def request() -> SessionRequest:
    return SessionRequest(
        CameraTarget(UUID(int=1), "camera-01", "private"),
        QualificationMode.TRANSPORT,
        SessionClass.BATTERY_30_SECONDS,
        1,
    )


def test_cli_accepts_camera_uuid_but_has_no_token_argument() -> None:
    command = parser()
    help_text = command.format_help()
    assert "access-token" not in help_text
    arguments = command.parse_args(["qualify-env"])
    assert arguments.command == "qualify-env"
    qualify = command.parse_args(
        [
            "qualify-ring",
            "--camera-id",
            "00000000-0000-0000-0000-000000000001",
            "--mode",
            "transport",
            "--session-class",
            "battery_30_seconds",
            "--manual-prompt",
        ]
    )
    assert not hasattr(qualify, "access_token")


def test_transport_qualification_cli_has_no_credential_or_endpoint_arguments() -> None:
    command = parser()
    arguments = command.parse_args(["qualify-transport", "--scenario", "ring-gate"])
    assert arguments.command == "qualify-transport"
    assert arguments.codec == "h264"
    transport_help = (
        next(
            action
            for action in command._subparsers._group_actions  # type: ignore[union-attr]
            if action.dest == "command"
        )
        .choices["qualify-transport"]
        .format_help()
    )
    for forbidden in ("token", "password", "url", "location", "endpoint"):
        assert f"--{forbidden}" not in transport_help
    for scenario in ("smoke", "reconnect", "stall", "renewal", "failures", "soak"):
        assert (
            command.parse_args(["qualify-transport", "--scenario", scenario]).scenario == scenario
        )


async def test_manual_provider_returns_secret_type_without_persistence() -> None:
    credential = SecretStr("fixture-secret")
    provided = await ManualPromptTokenProvider(credential).access_token_for(request())
    assert provided is credential
