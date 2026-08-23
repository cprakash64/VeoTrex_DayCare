import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class SourceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    authority: str
    rule: str
    title: str
    url: HttpUrl
    supplement: str
    verified_on: date


class AgeGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    label: str
    precedence: int = Field(ge=0)


class RatioTier(BaseModel):
    model_config = ConfigDict(extra="forbid")
    staff_count: int = Field(gt=0)
    max_children: int = Field(gt=0)


class RatioRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    type: Literal["STAFF_TO_CHILD_RATIO"]
    age_group_id: str
    tiers: list[RatioTier] = Field(min_length=1)
    eligible_staff_categories: list[str] = Field(min_length=1)
    parameters: dict[str, str | int | bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def tiers_are_strictly_increasing(self) -> "RatioRule":
        pairs = [(tier.staff_count, tier.max_children) for tier in self.tiers]
        if pairs != sorted(set(pairs)):
            raise ValueError("ratio tiers must be unique and strictly increasing")
        return self


class RetentionRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_type: str
    duration_days: int | None = Field(default=None, gt=0)
    status: Literal["DEFINED", "UNRESOLVED"]


class PolicyPack(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    jurisdiction: str = Field(pattern=r"^[A-Z]{2}-[A-Z]{2}$")
    policy_version: str
    effective_date: date
    enabled: bool
    disclaimer: str
    sources: list[SourceReference] = Field(min_length=1)
    age_groups: list[AgeGroup] = Field(min_length=1)
    staff_qualification_categories: list[str] = Field(min_length=1)
    ratio_rules: list[RatioRule] = Field(min_length=1)
    retention_requirements: list[RetentionRequirement]
    attendance_release_rules: list[dict[str, object]]

    @model_validator(mode="after")
    def references_are_consistent(self) -> "PolicyPack":
        age_ids = [group.id for group in self.age_groups]
        if len(age_ids) != len(set(age_ids)):
            raise ValueError("age group identifiers must be unique")
        unknown = {rule.age_group_id for rule in self.ratio_rules} - set(age_ids)
        if unknown:
            raise ValueError(f"ratio rules reference unknown age groups: {sorted(unknown)}")
        categories = set(self.staff_qualification_categories)
        for rule in self.ratio_rules:
            if not set(rule.eligible_staff_categories) <= categories:
                raise ValueError(f"rule {rule.id} references unknown staff categories")
        return self

    def canonical_content(self) -> bytes:
        """Return deterministic UTF-8 JSON for integrity checks and persistence."""
        serialized = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return serialized.encode("utf-8")

    def content_sha256(self) -> str:
        return hashlib.sha256(self.canonical_content()).hexdigest()


def load_policy_pack(path: Path) -> PolicyPack:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return PolicyPack.model_validate(raw)
