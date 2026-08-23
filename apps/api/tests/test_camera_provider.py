from veotrex_api.camera_provider import (
    CameraCapabilities,
    CameraCapability,
    CapabilityLimit,
)


def test_capabilities_distinguish_audio_direction_and_optional_features() -> None:
    capabilities = CameraCapabilities(
        supported=frozenset(
            {
                CameraCapability.LIVE_VIDEO,
                CameraCapability.RECEIVE_AUDIO,
                CameraCapability.MOTION_EVENTS,
            }
        ),
        limitations=(CapabilityLimit(CameraCapability.LIVE_VIDEO, "max_session_seconds", 600),),
    )

    assert capabilities.supports(CameraCapability.RECEIVE_AUDIO)
    assert not capabilities.supports(CameraCapability.SEND_AUDIO)
    assert not capabilities.supports(CameraCapability.SNAPSHOT)
    assert not capabilities.supports(CameraCapability.HISTORICAL_CLIP)
    assert capabilities.limitations[0].value == 600
