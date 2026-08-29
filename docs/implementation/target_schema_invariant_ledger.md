# Love Yourself target schema and invariant ledger

**Contract owner:** `WP-01.2 — Define the target schema and invariant ledger`

**Physical starting point:** Alembic revision `20260827_schema_baseline`

**Contract scope:** target design only; no DDL in WP-01.2

This document is the canonical target contract for the forward migrations owned
by later work packages. The Alembic baseline remains the exact physical starting
schema, including legacy drift. This ledger does not authorize a migration,
modify the founder testnet, or make current ORM models target truth.

The words **authority**, **snapshot**, and **cache** are intentionally distinct:

* an authority is the one durable fact that operations must lock and change;
* a snapshot is immutable historical evidence captured by the authoritative
  operation and is never re-resolved as current truth;
* a cache or session key is disposable, has a TTL, and cannot grant access or
  recover a durable lifecycle fact.

## Sole database authority ledger

Each mechanism below has exactly one database authority. Owned child rows or an
immutable terminal snapshot do not become a parallel writer.

| Mechanism | Sole database authority | Durable fact | Forbidden competing authority | Implementation owner |
|---|---|---|---|---|
| content | `content_library` | An append-only `(exercise_id, content_version)` record owns released instructions, metadata, review gate, and media references. | JSON files, plan-step text, Redis, and delivery payloads cannot be consulted as current catalogue truth. | WP-03.1 |
| lifecycle | `ai_plans` aggregate root | `ai_plans.status` owns plan lifecycle and `ai_plan_steps.step_status` owns child execution; onboarding progress applies only before a plan exists. | `users.current_state`, `user_profiles.is_paused`, `users.plan_end_date`, Redis FSM state, and scheduler jobs cannot describe plan lifecycle. | WP-01.3 |
| deployment | `deployments` | One row owns launch identity, environment, operational controls, dates, timezone policy, and pinned notice version. | Configuration labels, a user column, invitation tokens, and event properties cannot redefine a deployment. | WP-01.4 schema; WP-04.1 behavior |
| entitlement | `access_entitlements` | Grant and revocation chronology owns sponsored access for one deployment/access identity. | Telegram identity, enrollment, invitation state, SSO success, roster omission, and Redis cannot independently grant access. | WP-01.4 schema; WP-04.1 behavior |
| occurrence | `on_demand_exercise_requests` | One request row owns the independent on-demand lifecycle, accepted entry surface, and selected content/presentation snapshot. | Plans, plan steps, FSM, scheduler plan jobs, events, and Redis cannot stand in for the request. | WP-06.1 (table intentionally absent until then) |
| events | `user_events` | One allow-listed envelope per stable source operation owns personal event history. | `plan_execution_windows`, `task_stats`, `failure_signals`, logs, and aggregate rows cannot manufacture personal events. | WP-01.4 schema; WP-07.1 instrumentation |
| feedback | `feedback_events` | One source-typed record owns the submitted value and, when allowed, exact user wording. | Coach chat, event properties, aggregate dimensions, email, and issue trackers cannot be the capture authority. | WP-01.4 schema; WP-03.4/WP-05.2 behavior |
| notice acknowledgement | `notice_acknowledgements` | User, deployment, exact notice version, acknowledgement time, and source operation own the fact. | Current policy text, deployment default, onboarding state, and logs cannot imply acknowledgement. | WP-01.4 schema; WP-04.2 behavior |
| report grants | `report_access_grants` | A hashed, scoped, expiring, revocable grant row owns bearer access. | A signed `plan_id`, URL possession, Redis, and current secret configuration cannot grant indefinite access. | WP-01.4 schema; WP-04.2 behavior |
| aggregates | `aggregate_records` | The table owns both bounded personal contributions and their one-way immutable sealed cells, distinguished by `record_kind`. | Ad-hoc counters, `task_stats`, event recounts during deletion, Redis, spreadsheets, and dashboards cannot become aggregate truth. | WP-01.4 ingestion primitive; WP-07.3 sealing |

Two boundaries prevent apparent duplication:

1. `ai_plan_steps` are owned children of the `ai_plans` aggregate, not another
   user-lifecycle root. `current_mode` is calculated by one function.
2. `aggregate_records(record_kind='contribution')` rows are bounded personal
   inputs. Sealing creates immutable `record_kind='sealed_cell'` rows in the
   same authority and never copies a user, event, or operation join key into
   the sealed row.

## Target table ledger

`RETAIN`, `RESHAPE`, `ADD`, `ADD LATER`, and `REMOVE` describe the target, not
actions in this package. Every structural change below requires its owning
forward migration and compatibility tests.

### Identity, profile, and conversation

| Table | Disposition and target columns | Foreign keys and deletion |
|---|---|---|
| `users` | `RESHAPE`: keep `id`, unique `tg_id`, account creation metadata; add immutable `first_seen_at`; keep a user timezone override only if explicitly set. Remove lifecycle/access/activity summaries. | Root personal record. Personal children cascade only through the tested WP-04.2 deletion service. |
| `user_profiles` | `RESHAPE`: one row per user; keep allow-listed onboarding preferences such as display/name preference, communication style, Coach persona, `daily_time_slots`, and `active_days`. | `user_id -> users.id ON DELETE CASCADE`; unique `user_id`. |
| `onboarding_progress` | `ADD`: `user_id`, optional `deployment_enrollment_id`, allow-listed `stage`, `started_at`, `completed_at`, `updated_at`. It owns setup progress only before a plan exists. | `user_id -> users.id ON DELETE CASCADE`; enrollment uses `ON DELETE RESTRICT`; one row per user. |
| `chat_history` | `RETAIN`: `id`, `user_id`, `role`, `text`, `created_at`, `expires_at`. PostgreSQL is durable conversation truth and enforces the retention deadline. | `user_id -> users.id ON DELETE CASCADE`. |
| `user_facts` | `REMOVE`: the speculative fact store, including `MEDICAL`, has no accepted production writer or retention contract. | WP-04.2 verifies deployed emptiness/use and removes it before real employee data. |
| `user_daily_logs` | `REMOVE`: mood/stress/energy/note fields are not an accepted MVP mechanism. | WP-04.2 verifies deployed emptiness/use and removes it before real employee data. |

### Versioned content and plan aggregate

| Table | Disposition and target columns | Foreign keys and deletion |
|---|---|---|
| `content_library` | `RESHAPE`: composite identity `(exercise_id, content_version)`; exact title/steps/duration, `mechanic`, modality, structured requirements, optional media/alt text, `review_required`, `review_status`, review evidence, `is_active`, and timestamps. Released content fields are append-only; controlled review/activation fields may change. | Referenced versions use `ON DELETE RESTRICT`; a new instruction set is a new version, never an in-place rewrite. |
| `plan_drafts` | `RETAIN/DEFER`: remains a pre-activation artifact. Its simplification and removal of duplicated `draft_data` are owned by WP-03.2, not WP-01.2. | `user_id -> users.id ON DELETE CASCADE`. |
| `plan_draft_steps` | `RETAIN/DEFER`: remains owned by its draft until WP-03.2 locks the final builder record shape. It is never current content or lifecycle truth. | `draft_id -> plan_drafts.id ON DELETE CASCADE`; target content identity becomes a composite FK when WP-03.2 reshapes it. |
| `ai_plans` | `RESHAPE`: `id`, `user_id`, `cycle_number`, `status`, `activated_at`, `abandoned_at`, immutable plan parameters such as `total_days`, and creation timestamps. | `user_id -> users.id ON DELETE CASCADE`. One current plan per user is mandatory. |
| `ai_plan_days` | `RESHAPE`: `id`, `plan_id`, `day_number`, optional immutable generation snapshot. Completion is calculated from child steps. | `plan_id -> ai_plans.id ON DELETE CASCADE`; unique `(plan_id, day_number)`. |
| `ai_plan_steps` | `RESHAPE`: `id`, `day_id`, `exercise_id`, `content_version`, immutable content/mechanic snapshot, `order_in_day`, `time_slot`, `scheduled_for`, `expires_at`, `step_status`, terminal timestamp, and version for conditional updates. | `day_id -> ai_plan_days.id ON DELETE CASCADE`; `(exercise_id, content_version) -> content_library` with `ON DELETE RESTRICT`. |
| `exercise_deliveries` | `ADD LATER`: retry/reconciliation record with exactly one of `plan_step_id` or `on_demand_request_id`, stable source operation, attempt, delivery state, Telegram message identity, and immutable presentation snapshot. | Added by WP-03.4 for scheduled delivery; occurrence FK is added by WP-06.1. No delivery row owns step/request lifecycle. |

### Deployment, identity, entitlement, and enrolment

| Table | Disposition and target columns | Foreign keys and deletion |
|---|---|---|
| `organizations` | `ADD`: internal organization identity and allow-listed commercial/support metadata. | Referenced deployment history uses `ON DELETE RESTRICT`. |
| `deployments` | `ADD`: organization, opaque deployment key, immutable environment, `starts_at`, `ends_at`, `renewal_due_at`, independent `enrollment_open` and `delivery_enabled`, timezone mode/default, pinned notice version, launch denominator snapshot, champion/support references, and timestamps. | `organization_id -> organizations.id`; `notice_version_id -> privacy_notice_versions.id`; both `ON DELETE RESTRICT`. |
| `deployment_roster_versions` | `ADD`: deployment, monotonically increasing version, explicit `import_mode`, source/as-of time, validation/apply chronology, counts, and current marker. Raw uploads are not behavioral data. | `deployment_id -> deployments.id ON DELETE RESTRICT`. |
| `deployment_roster_entries` | `ADD`: normalized entry for an accepted roster version, linked access identity, explicit delta action when applicable, and validation metadata. | FKs to roster version and access identity use `ON DELETE RESTRICT`; unique within a roster version. |
| `access_identities` | `ADD`: normalized restricted identity, verification provider, verification chronology, and purpose-limited metadata. It is kept separate from behavior stores. | No FK from behavior/events to corporate identity. |
| `access_entitlements` | `ADD`: deployment, access identity, `granted_at`, nullable `revoked_at`, source, granting/confirming roster version, and audit timestamps. | FKs to deployment/identity/roster version use `ON DELETE RESTRICT`. |
| `deployment_invitations` | `ADD`: entitlement, unique token digest, issued/expires/redeemed/revoked chronology, token version, and source operation. Raw tokens are never stored. | `entitlement_id -> access_entitlements.id ON DELETE RESTRICT`; redemption does not turn the token into access authority. |
| `deployment_enrollments` | `ADD`: user, deployment, entitlement, `enrolled_at`, nullable `ended_at`, ended reason, and immutable attribution source. | FKs use `ON DELETE RESTRICT` except user deletion, which is handled by the privacy service; one active enrollment per user. |

`deployments` intentionally does not collapse lifecycle into one enum. Whether
enrolment is open, delivery is enabled, and the commercial period is current
are orthogonal facts. `users` intentionally has no permanent `deployment_id`;
historical event attribution is immutable and changing employer opens a new
enrolment instead of rewriting history.

### Events, feedback, privacy, reports, and aggregates

| Table | Disposition and target columns | Foreign keys and deletion |
|---|---|---|
| `event_catalog` | `ADD`: `(event_name, event_schema_version)`, event kind, allowed property schema, and activation chronology. | `user_events` references the composite key; catalogue rows are never deleted while referenced. |
| `user_events` | `RESHAPE`: canonical `event_id`, catalogue identity, `occurred_at`, `recorded_at`, `event_source`, `source_operation_id`, nullable personal/deployment/plan/step/content linkage, immutable attribution snapshots, and allow-listed `properties`. | FKs to user, organization, deployment, plan, step, and composite content version. Personal events are removed through user deletion; historical attribution is never rewritten. |
| `feedback_events` | `ADD`: `user_id`, `source`, stable source operation, explicit target FKs, `value`, optional reason/category, authoritative source message, optional extracted text, bounded context, and `created_at`. | Target columns reference plan step, `on_demand_request_id`, or chat message rather than a polymorphic unvalidated ID. User deletion cascades. |
| `privacy_notice_versions` | `ADD`: immutable version, publication time, content digest/location, current marker, and retirement time. | Deployment and acknowledgement references use `ON DELETE RESTRICT`. |
| `notice_acknowledgements` | `ADD`: user, deployment, notice version, `acknowledged_at`, and stable source operation. | FKs to user/deployment/notice; user deletion cascades, other references restrict. |
| `report_access_grants` | `ADD`: user, plan, purpose, unique token digest, token version, issued/expires/revoked chronology, and revocation reason. | User and plan deletion invalidate/remove grants; no token contains personal fields. |
| `aggregate_records` | `ADD`: `record_kind`, metric/schema identity, bounded period and dimensions, numeric value/counts, and timestamps. A contribution has personal `user_id` plus stable source operation and bounded retention; a sealed cell has neither and has immutable seal/gate evidence. | Contribution `user_id -> users.id ON DELETE CASCADE`; sealed cells have no user/event/operation FK or reversible join key. |
| `on_demand_exercise_requests` | `ADD LATER`: `user_id`, `exercise_id`, `content_version`, immutable `presentation_snapshot`, status chronology, `entry_surface IN ('command_menu','coach')`, unique `source_operation_id`, `requested_at`, nullable `delivered_at`, `expires_at` set only after confirmed delivery, nullable `responded_at`, restricted Telegram delivery references, exact `delivery_variant`, and bounded operational fields. Pending delivery uses its own retry/terminal-failure lifecycle. | Added only in WP-06.1; event, feedback, and delivery FKs are added with it and no placeholder rows are invented earlier. No plan/day/step/execution FK is allowed. |

### Legacy table disposition

| Current table | Target disposition | Boundary |
|---|---|---|
| `plan_instances`, `plan_execution_windows` | `REMOVE` after event/lifecycle readers switch. | They cannot remain an event-cycle authority. WP-01.4/WP-07.1 switch readers; cleanup is WP-08.1. |
| `task_stats`, `failure_signals` | `REMOVE` after canonical events/aggregates replace consumers. | Inferred failure/hidden-score mechanisms are not accepted target facts. Cleanup is WP-08.1. |
| `ai_plan_versions` | `REMOVE` with the dead adaptation subsystem. | WP-02.1 removes the writer; WP-08.1 may perform harmless physical cleanup after row/use verification. |
| duplicate draft indexes and unused duplicate enum families | `REMOVE` only in an owning forward migration. | Physical drift remains truthful until then; WP-01.2 does not rewrite the baseline. |
| `apscheduler_jobs` | `EXTERNAL` and unchanged. | APScheduler SQLAlchemyJobStore remains the sole owner; Alembic never adopts the table. |

## Enum and controlled-vocabulary ledger

PostgreSQL enums are reserved for small stable vocabularies. Event names and
onboarding stages use catalogues/checks because they evolve independently.
Legacy enum families are not reused just because they already exist.

| Type | Target values | Owner |
|---|---|---|
| `plan_status` | `active`, `paused`, `completed`, `abandoned` | WP-01.3 |
| `plan_step_status` | `pending`, `delivered`, `completed`, `skipped`, `expired`, `canceled` | WP-01.3 |
| `content_mechanic` | `switch`, `unload` | WP-03.1 |
| `content_review_status` | `unreviewed`, `approved`, `rejected` | WP-03.1 |
| `deployment_environment` | `testnet`, `production` | WP-01.4 |
| `deployment_timezone_mode` | `single`, `distributed` | WP-01.4 |
| `roster_import_mode` | `full_snapshot`, `delta` | WP-01.4 |
| `feedback_source` | `exercise_efficacy`, `coach_quality`, `product_feedback` | WP-01.4 |
| `feedback_category` | `bug`, `confusion`, `feature_request`, `content`, `coach`, `other` | WP-01.4 |
| `report_grant_purpose` | `completion_report`, `pulse_report` | WP-01.4 |
| `event_kind` | `user_behavior`, `operational`, `access_control` | WP-01.4 |
| `aggregate_record_kind` | `contribution`, `sealed_cell` | WP-01.4 |
| `on_demand_status` | `pending_delivery`, `delivered`, `completed`, `skipped`, `expired`, `delivery_failed`, `canceled` | WP-06.1 |

Invitation, entitlement, enrolment, report-grant, and deployment availability
are calculated from explicit timestamps/booleans. They are not synchronized
status enums.

## Constraint ledger

Names are target names. Owning migrations may adjust only a name needed for a
PostgreSQL collision; predicates and semantics require a reviewed contract
change.

### Partial unique constraints and idempotency

| Constraint/index | Exact invariant | Owner |
|---|---|---|
| `ux_ai_plans_one_current_per_user` | Unique `ai_plans(user_id)` where `status IN ('active','paused')`. | WP-01.3 |
| `ux_deployment_roster_versions_one_current` | Unique `deployment_roster_versions(deployment_id)` where `is_current`. | WP-04.1 |
| `ux_access_entitlements_one_open` | Unique `(deployment_id, access_identity_id)` where `revoked_at IS NULL`. | WP-04.1 |
| `ux_deployment_enrollments_one_active_user` | Unique `deployment_enrollments(user_id)` where `ended_at IS NULL`. | WP-04.1 |
| `ux_privacy_notice_versions_one_current` | Unique constant/current marker where `is_current`. | WP-04.2 |
| `ux_feedback_exercise_plan_step` | Unique `(user_id, plan_step_id)` where `source='exercise_efficacy' AND plan_step_id IS NOT NULL`. | WP-03.4 |
| `ux_feedback_exercise_on_demand` | Unique `(user_id, on_demand_request_id)` where `source='exercise_efficacy' AND on_demand_request_id IS NOT NULL`. | WP-06.1 |
| `ux_feedback_coach_message` | Unique `(user_id, coach_message_id)` where `source='coach_quality'`. | WP-05.2 |
| `ux_on_demand_exercise_requests_one_open` | Unique `on_demand_exercise_requests(user_id)` where `status IN ('pending_delivery','delivered')`. | WP-06.1 |
| `uq_on_demand_exercise_requests_source_operation` | Unique `on_demand_exercise_requests(source_operation_id)`. | WP-06.1 |
| `ux_exercise_deliveries_one_sent` | Unique source occurrence where delivery state is successful; retries remain separate attempts. | WP-03.4/WP-06.2 |
| `uq_user_events_source_operation` | Unique `(event_source, source_operation_id, event_name)`. | WP-01.4 |
| `uq_feedback_events_source_operation` | Unique `(source, source_operation_id)`. | WP-01.4 |
| `uq_notice_ack_source_operation` | Unique `source_operation_id` and unique `(user_id, deployment_id, notice_version_id)`. | WP-01.4 |
| `uq_report_access_grants_token_digest` | Unique token digest. | WP-01.4 |
| `uq_aggregate_contribution_source` | Unique source operation where `record_kind='contribution'`. | WP-01.4 |
| `uq_aggregate_sealed_cell_revision` | Unique metric/schema/period/dimension/revision key where `record_kind='sealed_cell'`; `revision=1` is the original cell. A correction appends the next revision with `supersedes_record_id` pointing to the prior leaf, and unique non-null `supersedes_record_id` prevents two replacement branches. | WP-07.3 |

### Foreign-key, range, and state checks

| Constraint | Target rule |
|---|---|
| Content identity | `content_version > 0`; plan steps and event content identity use the composite content FK; referenced content versions cannot be deleted. |
| Content eligibility | Select only `is_active AND (NOT review_required OR review_status='approved')`; required-review content cannot be released by a different code path. |
| Plan structure | `cycle_number > 0`, `total_days IN (7,14)` for the active P1 format, unique `(user_id, cycle_number)`, unique `(plan_id, day_number)`, `day_number BETWEEN 1 AND total_days`, and unique `(day_id, order_in_day)`. Cross-table day bounds are enforced in the authoritative creation service plus migration tests. |
| Plan chronology | `activated_at` is present for current/completed/abandoned plans; terminal timestamps cannot precede activation. Completion time is calculated from the last terminal step fact. |
| Step state | `step_status` is the only execution state; `completed` requires a completion timestamp; other terminal states require their terminal timestamp; terminal states cannot transition again. |
| Step scheduling | `expires_at > scheduled_for` when both exist; `time_slot IN ('DAY','EVENING')` for new P1 rows; mechanic is `switch` or `unload`. |
| Conversation | `role IN ('user','assistant')`; `created_at` and `expires_at` are non-null; `expires_at = created_at + INTERVAL '90 days'`. PostgreSQL reads also require both `created_at > now() - INTERVAL '90 days'` and `expires_at > now()` so a malformed legacy row cannot extend retention; each cached message carries the same cutoff, while the disposable Redis key may expire earlier. |
| Deployment | `starts_at < ends_at` when both exist; counts are non-negative; `single` timezone mode requires a valid default timezone; environment is immutable. |
| Entitlement/invitation | `revoked_at >= granted_at`; invitation `expires_at > issued_at`; redeemed/revoked times cannot precede issuance; token digest is non-empty and raw token is absent. |
| Enrollment | `ended_at > enrolled_at` when ended; enrollment deployment must equal its entitlement deployment, enforced by a composite FK/unique key. |
| Event envelope | `recorded_at >= occurred_at`; at least one allowed subject/operation linkage required by the catalogue; properties validate against the catalogue and exclude free text/identity/diagnostic fields. |
| Event content linkage | `plan_step_id` is an integer FK; nullable `(exercise_id, content_version)` uses a composite `MATCH FULL` FK (or an equivalent both-null/both-non-null check), so half-populated content identity is invalid; legacy mixed `step_id` receives no new writes and is removed after compatibility. |
| Feedback | Source-specific check requires exactly its valid target/value shape; exercise feedback follows a completed plan-step or on-demand occurrence and permits at most one efficacy submission per `(user, occurrence)`, Coach quality targets an assistant message, and product feedback preserves explicit source wording. |
| Notice acknowledgement | Acknowledged notice version must be the deployment-pinned version unless a recorded re-acknowledgement operation changes the binding. |
| Report grant | `expires_at > issued_at`; `revoked_at >= issued_at`; active state is calculated; deletion/secret-version retirement revokes access. |
| Aggregate contribution | Contribution rows require `user_id` and source operation, forbid seal fields, contain no raw text, and follow personal retention/deletion. |
| Aggregate sealed cell | Sealed rows forbid user/event/operation identifiers, require `sealed_at` and gate evidence, are immutable, and use only approved coarse dimensions. An original cell has `revision=1` and no predecessor; a correction is an append-only next revision whose `supersedes_record_id` references the prior leaf with the same logical cell key. Readers select the leaf revision; unique logical-key/revision and unique predecessor constraints plus a locked append prevent forks without mutating the replaced row. Company-summary cells require the centralized 100-eligible/50-contributor gate. |
| On-demand occurrence | Exactly one content/version snapshot; `entry_surface IN ('command_menu','coach')`; no response deadline before confirmed delivery; `(delivered_at IS NULL) = (expires_at IS NULL)` and `expires_at = delivered_at + INTERVAL '30 minutes'` when present. `delivered`, `completed`, `skipped`, and `expired` require both delivery timestamps; `pending_delivery` and `delivery_failed` require both null. `completed`/`skipped` require `responded_at <= expires_at`; `delivered`/`expired`/`delivery_failed` forbid `responded_at`. Cancellation may occur before or after delivery but must preserve paired timestamp nullability. No plan/day/step/execution FK is allowed; `pending_delivery` follows separate retry/terminal-failure rules and cannot expire as user inactivity. |

## High-value index ledger

These indexes follow accepted query paths or enforce target integrity. Anything
else waits for production-shaped data and `EXPLAIN`; the current JSON GIN and
duplicate single-column indexes are not copied automatically.

| Index | Query/invariant served | Owner |
|---|---|---|
| `ux_ai_plans_one_current_per_user` | Current-plan lookup and concurrency guard. | WP-01.3 |
| `ix_ai_plans_user_created` on `(user_id, created_at DESC)` | Plan history/latest plan. | WP-01.3 |
| `uq_ai_plan_days_plan_day` on `(plan_id, day_number)` | Ordered plan-day load. | WP-01.3 |
| `uq_ai_plan_steps_day_order` on `(day_id, order_in_day)` | Deterministic ordered delivery. | WP-01.3 |
| `ix_ai_plan_steps_due` on `(scheduled_for, id)` where `step_status='pending'` | Scheduler due-work scan. | WP-03.4 |
| `ix_ai_plan_steps_expiry` on `(expires_at, id)` where status is `pending` or `delivered` | Expiry/restart reconciliation. | WP-03.4 |
| `ix_chat_history_user_recent` on `(user_id, created_at DESC, id DESC)` | PostgreSQL fallback and bounded Coach context. | WP-04.2 |
| `ix_chat_history_retention` on `(expires_at, id)` | Retention deletion batches. | WP-04.2 |
| `ix_content_library_eligible` on `(mechanic, exercise_id, content_version)` with the release predicate | Deterministic eligible-catalogue selection. | WP-03.1 |
| `ix_user_events_user_time` on `(user_id, occurred_at DESC, event_id)` | Personal timeline, retention, and reconciliation. | WP-01.4 |
| `ix_user_events_plan_time` on `(plan_id, occurred_at, event_id)` | Cycle/plan metrics. | WP-01.4 |
| `ix_user_events_step_time` on `(plan_step_id, occurred_at, event_id)` | Delivery-to-response reconciliation. | WP-01.4 |
| `ix_user_events_deployment_time` on `(deployment_id, occurred_at, event_id)` | Deployment funnel and bounded retention queries. | WP-01.4 |
| `ix_invitations_token_digest` unique | Constant-path token redemption. | WP-01.4 |
| `ix_invitations_expiry` on `(expires_at, id)` where unredeemed/unrevoked | Expiry cleanup. | WP-04.1 |
| `ix_entitlements_identity_current` on `(access_identity_id, deployment_id)` where not revoked | Authorization and reconciliation. | WP-04.1 |
| `ix_enrollments_user_current` on `(user_id)` where not ended | Returning-user authorization. | WP-04.1 |
| `ix_report_grants_expiry` on `(expires_at, id)` where not revoked | Access check cleanup/revocation. | WP-04.2 |
| `ix_feedback_user_time` on `(user_id, created_at DESC)` | Personal export/deletion and product review. | WP-01.4 |
| `ix_aggregate_records_cell` on metric/schema/period/dimension key | Idempotent contribution/seal and approved report reads. | WP-01.4/WP-07.3 |
| `ix_on_demand_exercise_requests_user_time` on `(user_id, requested_at DESC)` | Current/history request lookup. | WP-06.1 |
| `ix_on_demand_exercise_requests_expiry` on `(expires_at, id)` where `status='delivered'` | Expiry/restart reconciliation. | WP-06.1 |

## Derived-field classification ledger

Only the three classifications below are permitted:

* `calculate` — do not persist the value; derive it from named authority;
* `immutable_snapshot` — capture it in the authoritative operation and never
  silently refresh it;
* `remove` — no accepted target use remains.

| Current/target field | Classification | Source and consequence |
|---|---|---|
| `users.current_state` | `remove` | `current_mode` comes from onboarding progress plus the current `ai_plans.status`; no synchronized user FSM column. |
| `user_profiles.is_paused` | `remove` | Pause is `ai_plans.status='paused'`. |
| `users.plan_end_date` / `ai_plans.end_date` | `calculate` | Actual completion is the last authoritative terminal step/completion fact. |
| `ai_plans.current_mode` | `calculate` | One shared function returns `ONBOARDING`, `ACTIVE`, `ACTIVE_PAUSED`, or `NO_ACTIVE_PLAN`. |
| `ai_plans.current_day` | `calculate` | Derive from ordered plan days and authoritative schedule/step facts. |
| `ai_plans.duration` | `calculate` | Render the product label from immutable `total_days`. |
| `ai_plans.preferred_time_slots` | `remove` | User profile schedule plus each step's scheduled snapshot owns timing. |
| `ai_plans.focus`, `ai_plans.load`, `ai_plans.milestone_status` | `remove` | Frozen/legacy concepts are not P1 lifecycle or generation facts. |
| `ai_plan_days.is_completed`, `ai_plan_days.completed_at` | `calculate` | Derive from the day's terminal step statuses/timestamps. |
| `ai_plan_steps.is_completed`, `ai_plan_steps.skipped` | `remove` | `step_status` and its terminal timestamp are the only execution facts. |
| `ai_plan_steps.step_type`, `difficulty`, `time_of_day`, `slot_type`, `job_id` | `remove` | Legacy generation/scheduler fields are not accepted target authorities. |
| `ai_plan_steps.mechanic` | `immutable_snapshot` | Captured from the selected content version during plan creation; never re-resolved after a library change. |
| `ai_plan_steps.exercise_id`, `content_version`, content copy | `immutable_snapshot` | Exact selected content identity/copy remains historical plan evidence; the catalogue stays release authority. |
| `ai_plan_steps.scheduled_for`, `expires_at` | `immutable_snapshot` | Written together by scheduling/rescheduling; later timezone changes cannot retroactively move the deadline. |
| actual delivery variant/payload | `immutable_snapshot` | Captured on the successful `exercise_deliveries` row. |
| `users.first_seen_at` | `immutable_snapshot` | Set once from the first proven accepted interaction; unknown legacy facts stay null. |
| `users.last_active_at` | `calculate` | Maximum accepted user-authored event; no mutable activity summary. |
| event organization/deployment/plan/content attribution | `immutable_snapshot` | Captured at occurrence and never rewritten when enrollment or content changes. |
| event time-of-day bucket | `immutable_snapshot` | Captured with its timezone basis so later preference changes do not rewrite telemetry. |
| `user_profiles.pause_count` | `calculate` | Count accepted `plan_paused` events; it is telemetry, never lifecycle. |
| `user_profiles.pulse_sent_indices` | `calculate` | Use canonical delivery events/operations, not a mutable JSON cursor. |
| `user_profiles.evening_slot_collected` | `calculate` | Presence of the required allow-listed schedule value is sufficient. |
| `plan_drafts.total_steps`, `is_valid` | `calculate` | Derive from normalized draft steps/validation when WP-03.2 removes duplicated draft payloads. |
| invitation/entitlement/enrollment/report-grant active state | `calculate` | Use explicit issued/granted/enrolled, expiry/end, and revocation facts. |
| `deployment.eligible_count_at_launch` and roster/as-of binding | `immutable_snapshot` | Freeze the accepted launch denominator; later roster reconciliation cannot rewrite it. |
| feedback category/extracted text | `immutable_snapshot` | Secondary capture metadata never replaces authoritative source wording. |
| response latency | `calculate` | Derive from linked delivery and response timestamps; it is not exercise duration. |
| percentages, completion rates, streaks, counters | `calculate` | Query authoritative steps/events or approved aggregate records with explicit denominator/window. |
| `task_stats.*` mutable counters | `remove` | Canonical events and the aggregate authority replace them. |
| `plan_execution_windows` counters and hidden compensation score | `remove` | They are legacy/inferred fields and cannot drive lifecycle or telemetry. |
| sealed aggregate values and gate evidence | `immutable_snapshot` | Sealing is one-way; corrections append a versioned successor linked by `supersedes_record_id`. The old cell remains immutable, and no correction may rebuild from deleted personal data. |

## Redis namespace and TTL ledger

PostgreSQL remains durable truth. Missing, expired, or flushed Redis keys may
lose convenience or require the user to repeat a prompt, but cannot change
content eligibility, lifecycle, deployment, entitlement, events, feedback,
notice acknowledgement, report access, occurrences, or aggregates.

| Namespace template | Class | TTL seconds | Failure semantics | Owner |
|---|---|---:|---|---|
| `ly:{environment}:session:{user_id}:messages:v1` | `transient_session` | 86400 | At most 20 Coach-context messages; each payload carries `created_at`, and every read/append prunes entries at or beyond their individual 90-day retention deadline before returning context. The one-day key TTL deliberately keeps only recently active sessions in paid RAM; rebuild from retained `chat_history` on any miss and never prefer a partial cache as newer durable truth. | WP-04.2/WP-05.1 |
| `ly:{environment}:session:{user_id}:pending_action:v1` | `transient_session` | 3600 | A lost key repeats or safely abandons the prompt; it cannot prove an operation committed. | Owning onboarding/runtime-action package |

Any future coordination namespace must name its owning operation, version, and
short TTL in this table before use. Durable locks, access tokens, lifecycle
state, event queues, and report grants are forbidden in Redis.

Current namespaces are migration inputs, not target exceptions:

* `session:{user_id}:messages` has no TTL and must be versioned/migrated without
  treating its contents as authoritative;
* `session:{user_id}:schedule_adjustment_*` belongs to the zombie tunnel and is
  removed wholesale by WP-02.1; WP-01.2 does not repair it;
* aiogram Redis FSM keys are removed with the duplicated FSM storage in
  WP-02.1; do not flush Redis globally;
* `session:{user_id}:pending_action` moves to the versioned namespace and keeps
  its existing one-hour TTL.

## Explicit deferrals and package boundaries

| Deferred item | Decision now | Owning trigger/package |
|---|---|---|
| `pool_unification` | Keep the application and APScheduler pools separate; no schema consequence and no new pool layer. | Revisit only on observed connection pressure; DB-19/backlog. |
| `broad_index_tuning` | Add only the integrity/query-path indexes named above; do not copy duplicate/GIN/speculative indexes into target migrations. | Production-shaped `EXPLAIN` evidence; DB-13/backlog. |
| `resource_efficiency_hardening` | Do not add speculative partitioning, cold-storage infrastructure, broad query rewrites, or a cache redesign for the small beta. WP-09.2 must expose database/Redis saturation and backlog signals, and WP-09.4 must test the launch burst; only a failed launch gate promotes the smallest evidenced fix (explicit connection/socket/statement bounds, bounded/keyset batches, removal of a proven N+1 path, or bounded task fan-out). Otherwise these remain post-beta unit-economics and storage-efficiency improvements. | WP-09.2/WP-09.4 evidence; post-beta backlog when launch gates pass. |
| `scheduler_leader_election` | Preserve exactly one bot replica/scheduler writer; do not add database or Redis leadership primitives. | Multi-replica requirement; WP-09.2/DB-20. |
| `harmless_legacy_table_cleanup` | Stop legacy readers/writers first; leave inert non-sensitive tables until row/use evidence and compatibility boundaries permit removal. | WP-08.1 after WP-01.3/WP-01.4/WP-02.1 switches. |
| `plan_draft_simplification` | Do not resolve `draft_data`/step duplication in this package. | WP-03.2 builder migration. |
| `universal_outbox_or_lock_framework` | Define only stable source operations and critical reconciliation records; no generic framework. | Specific delivery/scheduling need in WP-03.4 or later. |
| `lifecycle_migration` | Contract only: no lifecycle column, enum, reader, or writer changes in WP-01.2. | WP-01.3. |
| `event_privacy_deployment_primitives` | Contract only: do not add target tables or mixed event columns in WP-01.2. | WP-01.4. |
| `content_migration` | Contract only: do not import, rewrite, or activate catalogue rows in WP-01.2. | WP-03.1. |
| `sensitive_schema_removal` | Target removal is decided, but no table is dropped without deployed-use verification and privacy deletion tests. | WP-04.2; PRIV-09/DB-10. |
| `on_demand_occurrence` | Reserve the exact authority/constraints but create no placeholder table, event rows, or Redis state. | WP-06.1. |
| `schedule_adjustment` | No fixes, enum widening, constraints, Redis cleanup, or tests in this package. | Complete tunnel deletion in WP-02.1. |

## Migration handoff rules

Every owning package must:

1. start from `20260827_schema_baseline` and use a reviewed forward Alembic
   revision;
2. use expand -> evidence-based backfill -> writer switch -> reader switch ->
   contract only when compatibility requires it;
3. never backfill an event, notice acknowledgement, first-seen time, content
   version, deployment attribution, feedback, or aggregate fact that cannot be
   proven from existing rows;
4. name every temporary legacy reader/writer and its removal owner;
5. preserve one content authority and one plan-centric lifecycle authority;
6. keep `apscheduler_jobs` outside Alembic ownership;
7. rehearse migrations on disposable/restored copies, not first against
   founder data.

WP-01.2 itself ends at this reviewed contract and its validation tests.
