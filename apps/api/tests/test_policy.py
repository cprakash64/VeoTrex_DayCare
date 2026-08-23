from pathlib import Path

import pytest
from pydantic import ValidationError

from veotrex_api.policy import PolicyPack, load_policy_pack

PACK = Path(__file__).parents[3] / "config/jurisdictions/US-AZ/2025-08-03.yaml"


def test_arizona_pack_represents_special_ratio_tiers_exactly() -> None:
    pack = load_policy_pack(PACK)
    rules = {rule.age_group_id: rule for rule in pack.ratio_rules}
    assert [(tier.staff_count, tier.max_children) for tier in rules["INFANT"].tiers] == [
        (1, 5),
        (2, 11),
    ]
    assert [(tier.staff_count, tier.max_children) for tier in rules["ONE_YEAR_OLD"].tiers] == [
        (1, 6),
        (2, 13),
    ]
    assert all(rule.parameters["youngest_child_governs"] for rule in pack.ratio_rules)
    assert pack.policy_version == "2025-08-03.supp-25-2"
    assert pack.jurisdiction == "US-AZ"
    assert rules["TWO_YEAR_OLD"].tiers[0].max_children == 8
    assert rules["THREE_YEAR_OLD"].tiers[0].max_children == 13
    assert rules["FOUR_YEAR_OLD"].tiers[0].max_children == 15
    assert rules["FIVE_YEAR_OLD_NON_SCHOOL_AGE"].tiers[0].max_children == 20
    assert rules["SCHOOL_AGE"].tiers[0].max_children == 20
    assert set(rules["INFANT"].eligible_staff_categories) == {
        "DIRECTOR",
        "CHILD_EDUCATOR",
        "ASSISTANT_CHILD_EDUCATOR",
    }


def test_policy_pack_rejects_unknown_age_group() -> None:
    data = load_policy_pack(PACK).model_dump(mode="json")
    data["ratio_rules"][0]["age_group_id"] = "UNKNOWN"
    with pytest.raises(ValidationError, match="unknown age groups"):
        PolicyPack.model_validate(data)


def test_policy_pack_forbids_unversioned_extra_fields() -> None:
    data = load_policy_pack(PACK).model_dump(mode="json")
    data["surprise"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PolicyPack.model_validate(data)


def test_policy_digest_is_deterministic_and_content_sensitive() -> None:
    pack = load_policy_pack(PACK)
    reparsed = PolicyPack.model_validate(pack.model_dump(mode="json"))
    assert pack.content_sha256() == reparsed.content_sha256()
    assert len(pack.content_sha256()) == 64

    changed = pack.model_dump(mode="json")
    changed["ratio_rules"][0]["tiers"][0]["max_children"] = 4
    assert PolicyPack.model_validate(changed).content_sha256() != pack.content_sha256()


def test_policy_pack_rejects_non_increasing_ratio_tiers() -> None:
    data = load_policy_pack(PACK).model_dump(mode="json")
    data["ratio_rules"][0]["tiers"] = [
        {"staff_count": 2, "max_children": 11},
        {"staff_count": 1, "max_children": 5},
    ]
    with pytest.raises(ValidationError, match="strictly increasing"):
        PolicyPack.model_validate(data)


def test_policy_pack_rejects_duplicate_age_groups() -> None:
    data = load_policy_pack(PACK).model_dump(mode="json")
    data["age_groups"][1]["id"] = data["age_groups"][0]["id"]
    with pytest.raises(ValidationError, match="identifiers must be unique"):
        PolicyPack.model_validate(data)


def test_policy_pack_rejects_unknown_staff_qualification() -> None:
    data = load_policy_pack(PACK).model_dump(mode="json")
    data["ratio_rules"][0]["eligible_staff_categories"] = ["UNKNOWN"]
    with pytest.raises(ValidationError, match="unknown staff categories"):
        PolicyPack.model_validate(data)
