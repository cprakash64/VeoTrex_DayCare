# Jurisdiction policy engine

Policy packs are immutable, schema-versioned YAML under `config/jurisdictions/<jurisdiction>/`.
Pydantic rejects unknown fields, invalid identifiers, duplicate/unsorted tiers, missing age groups,
and unknown staff qualifications. A database policy version stores the exact validated document and
content digest. `PolicyPack.content_sha256()` hashes canonical UTF-8 JSON with sorted keys and compact
separators, so equivalent validated content has a deterministic SHA-256 digest and any content change
produces a different digest. Enabling a pack will later require an authorized approval and signing
workflow.

The Arizona starter pack represents R9-5-404 as facts and tiers. Infants contain explicit `(1, 5)`
and `(2, 11)` tiers; one-year-olds contain `(1, 6)` and `(2, 13)`. Therefore the exceptional second
staff capacity is retained and cannot be incorrectly derived from a scalar ratio. Each age group has
an ordering used to select the youngest group, and every ratio rule explicitly allows only director,
child educator, or assistant child educator categories.

The source is Arizona Secretary of State, Title 9 Chapter 5, Supp. 25-2, effective August 3, 2025,
and was verified on August 23, 2026. Policy data includes a legal disclaimer. Automated checks do not
constitute legal advice or guarantee compliance; approved owners must re-verify source material.

Future detectors emit timestamped observations such as counts and age categories. A policy evaluator
will consume normalized facts plus an explicit policy-version ID and produce a deterministic decision
containing its inputs, policy identifier, rule ID, policy digest/version, and reason codes. Vision
code never decides legal conditions. Attendance/release rules are a separate collection so their semantics do not get
forced into ratio models. Retention is marked unresolved rather than guessed.

Open questions: tier extrapolation beyond listed staffing counts, mixed-age edge cases, effective-time
timezone, amendments during an event, tenant activation/overrides, source archival, authorized review,
pack signatures, and rule-evaluator conformance fixtures.
