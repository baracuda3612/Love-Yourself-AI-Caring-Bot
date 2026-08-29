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
        "occurrence": "on_demand_exercise_requests",
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

    messages = next(
        row for row in rows if ":messages:" in row["Namespace template"]
    )
    assert int(messages["TTL seconds"]) == 24 * 60 * 60
    assert "At most 20 Coach-context messages" in messages["Failure semantics"]
    assert "each payload carries `created_at`" in messages["Failure semantics"]
    assert "every read/append prunes entries" in messages["Failure semantics"]


def test_on_demand_response_window_starts_only_after_delivery() -> None:
    source = LEDGER_PATH.read_text(encoding="utf-8")

    assert "no response deadline before confirmed delivery" in source
    assert "`expires_at = delivered_at + INTERVAL '30 minutes'`" in source
    assert "`delivered`, `completed`, `skipped`, and `expired` require both" in source
    assert "`pending_delivery` and `delivery_failed` require both null" in source
    assert "`pending_delivery` follows separate retry/terminal-failure rules" in source


def test_on_demand_contract_preserves_audit_identity_surface_and_feedback_guard() -> None:
    source = LEDGER_PATH.read_text(encoding="utf-8")

    assert "`on_demand_exercise_requests`" in source
    assert "`entry_surface IN ('command_menu','coach')`" in source
    assert "`status IN ('pending_delivery','delivered')`" in source
    assert "`uq_on_demand_exercise_requests_source_operation`" in source
    assert "`ux_feedback_exercise_on_demand`" in source
    assert "Unique `(user_id, on_demand_request_id)`" in source


def test_required_deferrals_are_explicit_and_owned() -> None:
    required = {
        "pool_unification",
        "broad_index_tuning",
        "resource_efficiency_hardening",
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


def test_privacy_content_and_sealed_correction_constraints_are_exact() -> None:
    source = LEDGER_PATH.read_text(encoding="utf-8")

    assert "`expires_at = created_at + INTERVAL '90 days'`" in source
    assert "`created_at > now() - INTERVAL '90 days'`" in source
    assert "composite `MATCH FULL` FK" in source
    assert "equivalent both-null/both-non-null check" in source
    assert "`uq_aggregate_sealed_cell_revision`" in source
    assert "unique non-null `supersedes_record_id` prevents two replacement branches" in source
