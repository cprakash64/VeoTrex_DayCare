from veotrex_edge_agent.qualification.environment import inspect_environment
from veotrex_edge_agent.qualification.resources import ResourceCollector, parse_tegrastats


def test_tegrastats_parser_is_bounded_to_safe_resource_fields() -> None:
    value = parse_tegrastats(
        "RAM 1024/7900MB CPU [12%@729,off] GR3D_FREQ 42% " "cpu@48.5C POM_5V_IN 4500/5000"
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
