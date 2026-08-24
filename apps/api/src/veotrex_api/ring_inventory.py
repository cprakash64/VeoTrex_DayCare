from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from veotrex_api.camera_provider import CameraCapability

MAX_PROVIDER_ID = 512


class RingInventoryDocumentError(ValueError):
    pass


class ResourceIdentifier(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = Field(min_length=1, max_length=128)
    id: str = Field(min_length=1, max_length=MAX_PROVIDER_ID)


class Relationship(BaseModel):
    model_config = ConfigDict(extra="allow")
    data: ResourceIdentifier | list[ResourceIdentifier] | None = None


class Resource(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: str = Field(min_length=1, max_length=128)
    id: str = Field(min_length=1, max_length=MAX_PROVIDER_ID)
    attributes: dict[str, Any] = Field(default_factory=dict)
    relationships: dict[str, Relationship] = Field(default_factory=dict)


class RingDevicesDocument(BaseModel):
    model_config = ConfigDict(extra="allow")
    data: Resource | list[Resource]
    included: list[Resource] = Field(default_factory=list)
    links: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NormalizedComponent:
    provider_component_id: str | None
    component_key: str
    display_name: str
    capabilities: tuple[str, ...]
    capability_details: dict[str, Any]
    privacy_zones_configured: bool
    motion_zones_configured: bool


@dataclass(frozen=True, slots=True)
class NormalizedDevice:
    provider_device_id: str
    display_name: str
    provider_online: bool | None
    location_country: str | None
    location_region: str | None
    capabilities_sha256: str
    configuration_sha256: str
    components: tuple[NormalizedComponent, ...]


@dataclass(frozen=True, slots=True)
class ParsedInventoryPage:
    devices: tuple[NormalizedDevice, ...]
    next_link: str | None


@dataclass(frozen=True, slots=True)
class NormalizedConfiguration:
    privacy_zones_configured: bool
    motion_zones_configured: bool
    configuration_sha256: str


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _linked(
    resource: Resource, name: str, included: dict[tuple[str, str], Resource]
) -> list[Resource]:
    relationship = resource.relationships.get(name)
    if relationship is None or relationship.data is None:
        return []
    identifiers = relationship.data if isinstance(relationship.data, list) else [relationship.data]
    resolved: list[Resource] = []
    for identifier in identifiers:
        related = included.get((identifier.type, identifier.id))
        if related is not None:
            resolved.append(related)
    return resolved


def _capabilities(attributes: dict[str, Any]) -> tuple[tuple[str, ...], dict[str, Any]]:
    raw = attributes.get("capabilities", attributes)
    names: set[str] = set()
    details: dict[str, Any] = {}
    if isinstance(raw, list):
        entries = {str(value): True for value in raw if isinstance(value, str)}
    elif isinstance(raw, dict):
        entries = raw
    else:
        entries = {}
    aliases = {
        "video": CameraCapability.LIVE_VIDEO.value,
        "live_video": CameraCapability.LIVE_VIDEO.value,
        "receive_audio": CameraCapability.RECEIVE_AUDIO.value,
        "audio_receive": CameraCapability.RECEIVE_AUDIO.value,
        "send_audio": CameraCapability.SEND_AUDIO.value,
        "audio_send": CameraCapability.SEND_AUDIO.value,
        "snapshot": CameraCapability.SNAPSHOT.value,
        "snapshots": CameraCapability.SNAPSHOT.value,
        "motion": CameraCapability.MOTION_EVENTS.value,
        "motion_detection": CameraCapability.MOTION_EVENTS.value,
    }
    for key, value in entries.items():
        normalized = key.lower().replace("-", "_")
        if value is False or value is None:
            continue
        mapped = aliases.get(normalized)
        if mapped:
            names.add(mapped)
        if normalized in {"video", "live_video", "audio", "codecs", "resolutions"}:
            if isinstance(value, str | int | float | bool | list | dict):
                details[normalized] = value
    return tuple(sorted(names)), details


def _configured(attributes: dict[str, Any], *keys: str) -> bool:
    for key in keys:
        value = attributes.get(key)
        if isinstance(value, list) and value:
            return True
        if isinstance(value, dict) and value:
            return True
        if value is True:
            return True
    return False


def _nested_configured(attributes: dict[str, Any], category: str, key: str) -> bool:
    nested = attributes.get(category)
    return isinstance(nested, dict) and _configured(nested, key)


def _normalize_device(
    resource: Resource, included: dict[tuple[str, str], Resource]
) -> NormalizedDevice:
    if resource.type != "devices":
        raise RingInventoryDocumentError("primary resource must have type devices")
    status = _linked(resource, "status", included)
    caps = _linked(resource, "capabilities", included)
    configs = _linked(resource, "configurations", included)
    locations = _linked(resource, "location", included)
    status_attrs = status[0].attributes if status else {}
    cap_attrs = caps[0].attributes if caps else {}
    config_attrs = configs[0].attributes if configs else {}
    location_attrs = locations[0].attributes if locations else {}

    display_name = resource.attributes.get("name") or resource.attributes.get("description")
    if not isinstance(display_name, str) or not display_name.strip():
        display_name = "Ring camera"
    display_name = display_name.strip()[:200]
    online = status_attrs.get("online")
    if not isinstance(online, bool):
        online = status_attrs.get("connected")
    if not isinstance(online, bool):
        online = None

    base_capabilities, base_details = _capabilities(cap_attrs)
    privacy = _configured(config_attrs, "privacy_zones", "privacyZones") or _nested_configured(
        config_attrs, "image_enhancements", "privacy_zones"
    )
    motion = _configured(config_attrs, "motion_zones", "motionZones") or _nested_configured(
        config_attrs, "motion_detection", "motion_zones"
    )
    raw_components = cap_attrs.get("components")
    components: list[NormalizedComponent] = []
    if isinstance(raw_components, dict) and isinstance(raw_components.get("items"), list):
        raw_components = raw_components["items"]
    elif isinstance(raw_components, dict):
        raw_components = [
            dict(value, id=key) if isinstance(value, dict) else {"id": key}
            for key, value in raw_components.items()
        ]
    if isinstance(raw_components, list):
        for index, raw_component in enumerate(raw_components):
            if not isinstance(raw_component, dict):
                raise RingInventoryDocumentError("malformed component")
            component_id = raw_component.get("component_id", raw_component.get("id"))
            if not isinstance(component_id, str) or not component_id or len(component_id) > 512:
                raise RingInventoryDocumentError("malformed component identity")
            capabilities, details = base_capabilities, base_details
            component_name = raw_component.get("component_name", raw_component.get("name"))
            if not isinstance(component_name, str) or not component_name:
                component_name = f"{display_name} component {index + 1}"
            components.append(
                NormalizedComponent(
                    component_id,
                    f"provider:{component_id}",
                    component_name[:200],
                    capabilities,
                    details,
                    privacy,
                    motion,
                )
            )
    else:
        components.append(
            NormalizedComponent(
                None, "__single__", display_name, base_capabilities, base_details, privacy, motion
            )
        )
    country = location_attrs.get("country") or location_attrs.get("country_code")
    region = location_attrs.get("state") or location_attrs.get("region")
    return NormalizedDevice(
        resource.id,
        display_name,
        online,
        country[:2].upper() if isinstance(country, str) else None,
        region[:64] if isinstance(region, str) else None,
        _stable_hash(cap_attrs),
        _stable_hash(
            {
                "privacy_zones_configured": privacy,
                "motion_zones_configured": motion,
            }
        ),
        tuple(components),
    )


def parse_inventory_page(payload: object) -> ParsedInventoryPage:
    try:
        document = RingDevicesDocument.model_validate(payload)
    except ValidationError as exc:
        raise RingInventoryDocumentError("malformed JSON:API document") from exc
    resources: dict[tuple[str, str], Resource] = {}
    for resource in document.included:
        key = (resource.type, resource.id)
        previous = resources.get(key)
        if previous is not None and previous.model_dump() != resource.model_dump():
            raise RingInventoryDocumentError("conflicting included resource")
        resources[key] = resource
    primary = document.data if isinstance(document.data, list) else [document.data]
    devices = tuple(_normalize_device(resource, resources) for resource in primary)
    next_value = document.links.get("next")
    if isinstance(next_value, dict):
        next_value = next_value.get("href")
    if next_value is not None and not isinstance(next_value, str):
        raise RingInventoryDocumentError("malformed pagination link")
    return ParsedInventoryPage(devices, next_value)


def parse_configuration_document(payload: object) -> NormalizedConfiguration:
    try:
        document = RingDevicesDocument.model_validate(payload)
    except ValidationError as exc:
        raise RingInventoryDocumentError("malformed configuration document") from exc
    values = document.data if isinstance(document.data, list) else [document.data]
    if len(values) != 1 or values[0].type not in {
        "device-configurations",
        "component-configurations",
    }:
        raise RingInventoryDocumentError("malformed configuration resource")
    attributes = values[0].attributes
    privacy = _configured(attributes, "privacy_zones", "privacyZones") or _nested_configured(
        attributes, "image_enhancements", "privacy_zones"
    )
    motion = _configured(attributes, "motion_zones", "motionZones") or _nested_configured(
        attributes, "motion_detection", "motion_zones"
    )
    return NormalizedConfiguration(
        privacy,
        motion,
        _stable_hash(
            {
                "privacy_zones_configured": privacy,
                "motion_zones_configured": motion,
            }
        ),
    )
