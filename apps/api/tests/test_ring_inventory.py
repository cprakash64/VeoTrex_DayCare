import httpx
import pytest
from pydantic import SecretStr

from veotrex_api.config import Settings
from veotrex_api.ring_client import RingClient, RingClientError
from veotrex_api.ring_inventory import RingInventoryDocumentError, parse_inventory_page


class Resolver:
    def resolve(self, _: str) -> SecretStr:
        return SecretStr("test-only")


def document(*, components: object = None, reverse: bool = False) -> dict[str, object]:
    included = [
        {"type": "device-status", "id": "s/opaque", "attributes": {"online": True}},
        {
            "type": "device-capabilities",
            "id": "c/opaque",
            "attributes": {
                "video": {"codecs": ["h264"]},
                "motion_detection": True,
                **({"components": components} if components is not None else {}),
            },
        },
        {
            "type": "device-configurations",
            "id": "cfg/opaque",
            "attributes": {"privacy_zones": [{"redacted": True}]},
        },
        {
            "type": "locations",
            "id": "loc/opaque",
            "attributes": {"country": "us", "state": "AZ", "address": "discard"},
        },
        {"type": "future-resource", "id": "ignored", "attributes": {"new": True}},
    ]
    if reverse:
        included.reverse()
    return {
        "data": [
            {
                "type": "devices",
                "id": "opaque/device:%2F",
                "attributes": {"name": "Infant Room (untrusted name)"},
                "relationships": {
                    "status": {"data": {"type": "device-status", "id": "s/opaque"}},
                    "capabilities": {"data": {"type": "device-capabilities", "id": "c/opaque"}},
                    "configurations": {
                        "data": {"type": "device-configurations", "id": "cfg/opaque"}
                    },
                    "location": {"data": {"type": "locations", "id": "loc/opaque"}},
                },
            }
        ],
        "included": included,
        "unknown": "allowed",
    }


def test_compound_document_resolves_by_exact_type_and_id_in_any_order() -> None:
    first = parse_inventory_page(document())
    second = parse_inventory_page(document(reverse=True))
    assert first == second
    device = first.devices[0]
    assert device.provider_device_id == "opaque/device:%2F"
    assert device.provider_online is True
    assert (device.location_country, device.location_region) == ("US", "AZ")
    assert device.components[0].component_key == "__single__"
    assert set(device.components[0].capabilities) == {"LIVE_VIDEO", "MOTION_EVENTS"}
    assert device.components[0].privacy_zones_configured is True


def test_multi_camera_component_ids_remain_opaque() -> None:
    page = parse_inventory_page(
        document(
            components=[
                {"id": "front/lens", "name": "Front", "capabilities": {"video": True}},
                {"id": "not-a-number", "name": "Back", "capabilities": {"motion": True}},
            ]
        )
    )
    assert [value.provider_component_id for value in page.devices[0].components] == [
        "front/lens",
        "not-a-number",
    ]


def test_current_components_items_and_nested_privacy_configuration() -> None:
    payload = document(
        components={
            "items": [
                {
                    "component_id": "opaque-view",
                    "component_type": "lens",
                    "component_name": "View 1",
                }
            ]
        }
    )
    assert isinstance(payload["included"], list)
    for resource in payload["included"]:
        if resource["type"] == "device-configurations":
            resource["attributes"] = {
                "motion_detection": {"motion_zones": [{"geometry": "discard"}]},
                "image_enhancements": {"privacy_zones": [{"geometry": "discard"}]},
            }
    component = parse_inventory_page(payload).devices[0].components[0]
    assert component.provider_component_id == "opaque-view"
    assert component.display_name == "View 1"
    assert component.privacy_zones_configured is True
    assert component.motion_zones_configured is True
    assert "LIVE_VIDEO" in component.capabilities


def test_conflicting_duplicate_included_resource_fails_closed() -> None:
    payload = document()
    assert isinstance(payload["included"], list)
    payload["included"].append(
        {"type": "device-status", "id": "s/opaque", "attributes": {"online": False}}
    )
    with pytest.raises(RingInventoryDocumentError, match="conflicting"):
        parse_inventory_page(payload)


async def test_inventory_read_retries_429_and_honors_retry_after(settings: Settings) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"data": [], "included": []})

    async def sleep(value: float) -> None:
        sleeps.append(value)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = RingClient(settings, Resolver(), http, sleeper=sleep, random_value=lambda: 0)
    assert await client.discover_devices(SecretStr("access")) == ()
    assert calls == 2
    assert sleeps == [2]
    await http.aclose()


async def test_inventory_does_not_retry_403_or_follow_cross_origin(settings: Settings) -> None:
    calls = 0

    def forbidden(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403)

    http = httpx.AsyncClient(transport=httpx.MockTransport(forbidden))
    client = RingClient(settings, Resolver(), http)
    with pytest.raises(RingClientError, match="forbidden"):
        await client.discover_devices(SecretStr("access"))
    assert calls == 1
    await http.aclose()

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"data": [], "included": [], "links": {"next": "https://evil.test/x"}}
            )
        )
    )
    client = RingClient(settings, Resolver(), http)
    with pytest.raises(RingClientError, match="unsafe_pagination_link"):
        await client.discover_devices(SecretStr("access"))
    await http.aclose()


async def test_multi_camera_discovery_reads_each_component_configuration(
    settings: Settings,
) -> None:
    requests: list[httpx.Request] = []
    payload = document(
        components={
            "items": [
                {"component_id": "view/one", "component_name": "One"},
                {"component_id": "view two", "component_name": "Two"},
            ]
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/devices":
            return httpx.Response(200, json=payload)
        component_id = request.url.params["component_id"]
        return httpx.Response(
            200,
            json={
                "data": {
                    "type": "component-configurations",
                    "id": f"configuration-{component_id}",
                    "attributes": {
                        "image_enhancements": {
                            "privacy_zones": [{}] if component_id == "view/one" else []
                        },
                        "motion_detection": {
                            "motion_zones": [{}] if component_id == "view two" else []
                        },
                    },
                }
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = RingClient(settings, Resolver(), http)
    devices = await client.discover_devices(SecretStr("access"))
    assert len(requests) == 3
    assert devices[0].components[0].privacy_zones_configured
    assert devices[0].components[1].motion_zones_configured
    assert requests[1].url.params["component_id"] == "view/one"
    assert requests[2].url.params["component_id"] == "view two"
    await http.aclose()
