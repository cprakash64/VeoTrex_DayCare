from pathlib import Path

from veotrex_edge_agent.qualification.environment import element_present, inspect_environment
from veotrex_edge_agent.qualification.resources import ResourceCollector, parse_tegrastats


def test_tegrastats_parser_is_bounded_to_safe_resource_fields() -> None:
    value = parse_tegrastats(
        "RAM 1024/7900MB CPU [12%@729,off] GR3D_FREQ 42% cpu@48.5C POM_5V_IN 4500/5000"
    )
    assert value["available"] is True
    assert value["gpu"] == "42%"
    assert value["temperature_cpu"] == "48.5C"
    assert value["cpu_frequencies_utilization"] == "[12%@729,off]"
    assert "token" not in value


def test_platform_resource_collector_tolerates_optional_metrics() -> None:
    sample = ResourceCollector().sample().as_dict()
    assert sample["monotonic_seconds"] > 0
    assert "process_rss_bytes" in sample
    assert "system_cpu_percent" in sample
    assert "jetson" in sample


def test_environment_inspection_is_read_only_and_reports_plugins() -> None:
    result = inspect_environment()
    assert result["architecture"]
    assert "rtspsrc" in result["gstreamer_plugins"]
    assert "nvv4l2decoder" in result["gstreamer_plugins"]
    assert all(isinstance(value, bool) for value in result["gstreamer_plugins"].values())


def test_missing_element_error_text_is_not_reported_as_present(tmp_path: Path) -> None:
    # gst-inspect prints "No such element" and exits non-zero; only the exit status counts.
    assert element_present("/bin/true", "rtspsrc") is True
    assert element_present("/bin/false", "avdec_h264") is False
    assert element_present(str(tmp_path / "missing-gst-inspect"), "rtspsrc") is False


def test_tegrastats_parser_reads_orin_power_and_media_engines() -> None:
    value = parse_tegrastats("RAM 4545/7485MB GR3D_FREQ 38% VDD_IN 6130mW/6130mW NVDEC 115")
    assert value["power"] == "6130mW/6130mW"
    assert value["nvdec"] == "115"
