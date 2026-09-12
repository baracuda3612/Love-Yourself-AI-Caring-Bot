# WP-02.2 authoritative lifecycle service manifest

This manifest records the single live lifecycle authority after WP-02.2. A
deterministic surface may gather command arguments and present the result, but
it may not re-decide ownership, aggregate state, or mutation outcome outside
`app.lifecycle`.

| Live surface or retired entrance | Authoritative method | Preserved contract | External work or later owner |
|---|---|---|---|
| Coach current-plan status | `read_lifecycle_status()` | Re-reads the enabled user and current plan, then derives mode, current day, completed work, remaining work, and remaining deliveries. | Presentation stays in the runtime tool; no mutation. |
| Coach follow-up activation | `activate_plan()` | Locks the entitled user, enforces no-current-plan/history rules, creates one SHORT/MEDIUM aggregate, and replays the same receipt to the same plan. | `reconcile_plan_schedule` runs after commit. Target generation remains WP-03.2; deterministic onboarding remains WP-04.3. |
| Former public `plan_drafts.service.create_plan()` entrance | `activate_plan()` | The builder/finalizer survives only as `create_plan_for_lifecycle()`, an internal helper called by the service. | It emits no Telegram message and performs no scheduler call. |
| Telegram complete/skip callbacks | `transition_owned_plan_step()` | Resolves the Telegram actor, checks entitlement and step ownership, locks the aggregate, and permits one terminal winner. | Callback presentation remains explicit after commit. Scheduled callback reconciliation remains WP-03.4. |
| Scheduler delivery state | `transition_plan_step(..., target_status="delivered")` | Re-checks the enabled owner and active plan before recording delivered. | The actual delivery/retry protocol remains WP-03.4. |
| Scheduler expiry and former independent ignored-task scan | `expire_plan_step()` | Expiry and canonical `task_ignored` event share one source operation and transaction. | `remove_step_keyboard` is retried until confirmed; feature-wide telemetry remains WP-07.1. |
| Former `plan_guards.validate_step_action()` | `transition_owned_plan_step()` / `transition_plan_step()` | Stored state is checked only after the service lock; no pre-read guard can authorize a stale callback. | Module deleted. |
| Former `plan_pause.pause_plan()` / `resume_plan()` | `transition_current_plan()` | Active↔paused status changes are locked and idempotent. | Detailed rescheduling/re-anchoring is returned as `deferred` to WP-02.3. |
| Coach cancellation | `abandon_current_plan()` | Atomically abandons the current plan and terminalizes every open child step; replay returns the same canceled-step targets. | `cancel_step_jobs` is verified after commit. Deterministic cancel UX/follow-up remains WP-02.3. |
| Coach day/evening change and `/user/time-slots` | `change_delivery_time()` | Validates the owned lifecycle context, persists the slot and future step times, records a receipt, and does not overwrite a later value on replay. | Active jobs reconcile after commit; paused reconciliation and semantic collection UX remain WP-02.3. API authentication/enrollment remains WP-04.1. |
| Medium-plan evening preference collection | `record_evening_time_preference()` | Records the collected preference and its replay result under the same entitlement/receipt boundary. | Plan-aware collection UX and semantic validation remain WP-02.3. |
| Plan-format request | `request_plan_format_switch()` | Validates the enabled owner, current plan, source ID, and target format, but returns `applied=False`. | Actual format-switch behavior belongs to WP-02.3. |
| Runtime/cron completion detection | `complete_current_plan_if_ready()` | Completes only the exact active owned aggregate when every child is terminal; replay exposes the same completion-report intent. | Completion report delivery is explicit. Durable obligation and automatic same-format successor remain WP-03.5. |
| Continuation hand-off | `prepare_continuation()` | Validates the exact owned completed source plan and returns its format plus stable operation identity. | No successor is created before WP-03.5. |

## Enforcement evidence

* Runtime search finds lifecycle status assignments only in `app.lifecycle`;
  plan construction remains inside the internal finalizer.
* Runtime search finds `update_user_time_slots()` calls only inside
  `app.lifecycle`.
* `app.plan_guards`, `app.plan_pause`, the ambient activation side-effect helper,
  and the separate ignored-task scanner are absent.
* Focused tests retain these reachability assertions so later packages cannot
  silently add a competing mutation entrance.

The physical legacy columns listed in
`docs/implementation/wp_01_3_lifecycle_compatibility_manifest.md` remain inert
until WP-08.1. APScheduler storage remains outside Alembic ownership and is an
effect target, never lifecycle authority.
