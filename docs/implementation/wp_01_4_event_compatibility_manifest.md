# WP-01.4 event compatibility and removal manifest

This manifest defines the bounded writer/reader switch for
`20260905_event_privacy`. Canonical event rows have a non-null `event_name`,
catalogue version, occurrence/recording chronology, source operation,
environment, properties, and explicit subject linkage. A row with
`event_name IS NULL` is legacy evidence only and is never reinterpreted as a
canonical fact.

There is no mixed-version writer interval. Stop the old application, workers,
poller, and scheduler; upgrade from `20260902_plan_lifecycle`; then start only
the build pinned to `20260905_event_privacy`. The old build cannot start against
the new head because startup requires an exact Alembic revision.

| Legacy surface | WP-01.4 behavior | Remaining consumer | Removal owner |
|---|---|---|---|
| `user_events.event_type`, `timestamp`, `plan_execution_id`, `step_id`, `context` | Existing values are retained unchanged. New writes leave every column null and use the canonical envelope only. No guessed source operation, plan/step/content/deployment linkage, or occurrence chronology is backfilled. | Inert ORM mapping and migration evidence only. | WP-07.1 verifies instrumentation/reconciliation coverage; WP-08.1 drops the columns after deployed-use evidence. |
| `user_events.id` | Renamed in place to canonical `event_id`; existing row identity is preserved. | Canonical primary key. | Retained. |
| `plan_instances`, `plan_execution_windows` | No canonical writer creates or reuses either table. Current event readers use `plan_id` and `plan_step_id`. | Inert ORM relationships and migration legacy fixtures only. | WP-07.1 verifies no missing event reader; WP-08.1 drops both tables. |
| mixed `step_id` content/plan-step identity | No new write is accepted. The writer derives integer `plan_step_id`, owning `plan_id`, `exercise_id`, and `content_version` from the authoritative plan step and catalogue row. It never creates a content stub. | Preserved only on legacy rows. | WP-08.1 after WP-07.1 reconciliation evidence. |
| `task_stats`, `failure_signals` | Canonical ingestion does not read or update mutable counters or inferred failure rows. The independent contribution is inserted in the same transaction as the personal event. | Inert ORM mapping and legacy data only. | WP-07.1 switches any remaining instrumentation assumptions; WP-08.1 drops the tables. |
| ad-hoc event properties | New writes validate keys and scalar/container types against `event_catalog`, reject free-text/identity/diagnostic fields, and bound nested payloads. Legacy `context` is not copied. | Legacy rows remain inspectable only as legacy evidence. | WP-08.1 removes legacy storage after WP-07.1. |
| `users.last_active_at` and profile telemetry counters | The canonical event operation does not maintain mutable activity/counter mirrors. | Existing runtime readers/writers outside event ingestion remain compatibility surfaces. | WP-07.1 owns instrumentation and derived-query replacement; WP-08.1 owns physical cleanup. |
| on-demand occurrence linkage | No placeholder table, unvalidated ID, event, feedback row, or Redis authority is created. | Contract only. | WP-06.1 adds `on_demand_exercise_requests` and its event/feedback FKs atomically. |

`aggregate_records(record_kind='contribution')` is personal and bounded: it has
a user and stable source operation but no event FK. A sealed cell has no user,
event, or operation join key and is immutable; WP-07.3 owns the locking,
100-eligible/50-contributor gate, correction append, and report readers. This
package does not seal production metrics or create a dashboard.

Deployment, entitlement, invitation, enrollment, notice, feedback, and report
grant tables are authorities for later operations, not claims of current
runtime behavior. WP-04.1 owns access/enrollment decisions; WP-04.2 owns notice
flow, grant validation, export, retention, and deletion; WP-03.4/WP-05.2 own
feedback capture. No Railway schema or data is changed by this package.
