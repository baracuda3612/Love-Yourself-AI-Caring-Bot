from __future__ import annotations

import re
from pathlib import Path


LEDGER_PATH = Path("docs/implementation/target_schema_invariant_ledger.md")


def _table_rows(heading: str) -> list[dict[str, str]]:
    lines = LEDGER_PATH.read_text(encoding="utf-8").splitlines()
    start = lines.index(heading)

    header_index = next(
        index for index in range(start + 1, len(lines)) if lines[index].startswith("|")
    )
    headers = [cell.strip() for cell in lines[header_index].strip("|").split("|")]
    rows: list[dict[str, str]] = []
    for line in lines[header_index + 2 :]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        assert len(cells) == len(headers), line
        rows.append(dict(zip(headers, cells, strict=True)))
    return rows


def test_every_required_mechanism_has_one_named_database_authority() -> None:
    expected = {
        "content": "content_library",
        "lifecycle": "ai_plans",
        "deployment": "deployments",
        "entitlement": "access_entitlements",
        "occurrence": "on_demand_requests",
        "events": "user_events",
        "feedback": "feedback_events",
        "notice acknowledgement": "notice_acknowledgements",
        "report grants": "report_access_grants",
        "aggregates": "aggregate_records",
    }
    rows = _table_rows("## Sole database authority ledger")

    assert len(rows) == len(expected)
    assert len({row["Mechanism"] for row in rows}) == len(rows)
    for row in rows:
        authority_tokens = re.findall(r"`([a-z][a-z0-9_]*)`", row["Sole database authority"])
        assert authority_tokens == [expected[row["Mechanism"]]]
        assert "redis" not in row["Sole database authority"].lower()


def test_derived_fields_use_only_the_three_accepted_classifications() -> None:
    rows = _table_rows("## Derived-field classification ledger")
    classifications = {row["Classification"].strip("`") for row in rows}
    fields = "\n".join(row["Current/target field"] for row in rows)

    assert classifications == {"calculate", "immutable_snapshot", "remove"}
    for required_field in (
        "users.current_state",
        "user_profiles.is_paused",
        "ai_plans.current_mode",
        "ai_plan_steps.is_completed",
        "users.first_seen_at",
        "event organization/deployment/plan/content attribution",
        "sealed aggregate values",
    ):
        assert required_field in fields


def test_target_redis_namespaces_are_versioned_transient_and_bounded() -> None:
    rows = _table_rows("## Redis namespace and TTL ledger")

    assert {row["Class"].strip("`") for row in rows} <= {
        "transient_session",
        "transient_coordination",
    }
    assert len(rows) == 2
    for row in rows:
        namespace = row["Namespace template"].strip("`")
        ttl_seconds = int(row["TTL seconds"])
        assert namespace.startswith("ly:{environment}:")
        assert namespace.endswith(":v1")
        assert 0 < ttl_seconds <= 90 * 24 * 60 * 60


def test_required_deferrals_are_explicit_and_owned() -> None:
    required = {
        "pool_unification",
        "broad_index_tuning",
        "scheduler_leader_election",
        "harmless_legacy_table_cleanup",
        "plan_draft_simplification",
        "universal_outbox_or_lock_framework",
        "lifecycle_migration",
        "event_privacy_deployment_primitives",
        "content_migration",
        "sensitive_schema_removal",
        "on_demand_occurrence",
        "schedule_adjustment",
    }
    rows = _table_rows("## Explicit deferrals and package boundaries")
    actual = {row["Deferred item"].strip("`") for row in rows}

    assert actual == required
    assert all(row["Owning trigger/package"] for row in rows)


def test_ledger_preserves_baseline_and_external_scheduler_ownership() -> None:
    source = LEDGER_PATH.read_text(encoding="utf-8")

    assert "target design only; no DDL in WP-01.2" in source
    assert "20260827_schema_baseline" in source
    assert "`apscheduler_jobs` | `EXTERNAL` and unchanged" in source
    assert "Complete tunnel deletion in WP-02.1" in source
    assert "no synchronized user FSM column" in source
    assert "JSON files, plan-step text, Redis" in source
