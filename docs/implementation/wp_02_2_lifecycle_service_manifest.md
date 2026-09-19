# WP-02.2 authoritative lifecycle service manifest

This manifest records the single live lifecycle authority after WP-02.2. A
deterministic surface may gather command arguments and present the result, but
it may not re-decide ownership, aggregate state, or mutation outcome outside
`app.lifecycle`.

| Live surface or retired entrance | Authoritative method | Preserved contract | External work or later owner |
|---|---|---|---|
| Ordinary text lifecycle preflight | `require_lifecycle_entitlement()` | Re-reads the current runtime entitlement under the user lock even when no current plan exists, so an inactive sender cannot reach onboarding or Coach through the no-plan path. | Full identity, enrollment, and access UX remains WP-04.1. |
| Coach current-plan status | `read_lifecycle_status()` | Re-reads the enabled user and current plan, then derives mode, current day, completed work, remaining work, and remaining deliveries. | Presentation stays in the runtime tool; no mutation. |
| Coach follow-up activation | `activate_plan()` / `recover_current_plan_activation()` | Locks the entitled user, enforces no-current-plan/history rules, and writes a versioned receipt over canonical submitted arguments, including omitted values. Exact new-format replay rejects drift; bounded base/candidate receipt readers return the recorded plan without creating another one. Recovery reaches that plan without depending on a repeated Coach call ID. | One plan-wide `reconcile_plan_schedule` proof runs after commit. Target generation remains WP-03.2; deterministic onboarding remains WP-04.3. |
| Former public `plan_drafts.service.create_plan()` entrance | `activate_plan()` | The builder/finalizer survives only as `create_plan_for_lifecycle()`, an internal helper called by the service. | It emits no Telegram message and performs no scheduler call. |
| Telegram complete/skip callbacks | `transition_owned_plan_step()` | Resolves the Telegram actor, checks entitlement and step ownership, locks the aggregate, and permits one terminal winner. | Callback presentation remains explicit after commit. Scheduled callback reconciliation remains WP-03.4. |
| Scheduler delivery state | `transition_plan_step(..., target_status="delivered")` | Re-checks the enabled owner and active plan before recording delivered. | The actual delivery/retry protocol remains WP-03.4. |
| Scheduler expiry and former independent ignored-task scan | `expire_plan_step()` | The terminal transition and receipt remain authoritative. Expected pre-/post-catalog chronology and supported legacy/JSON-only linkage gaps return `telemetry_not_recorded` without reopening the step; unexpected invariant/storage failures still fail their candidate boundary. | `remove_step_keyboard` is retried until confirmed; feature-wide telemetry remains WP-07.1. |
| Former `plan_guards.validate_step_action()` | `transition_owned_plan_step()` / `transition_plan_step()` | Stored state is checked only after the service lock; no pre-read guard can authorize a stale callback. | Module deleted. |
| Former `plan_pause.pause_plan()` / `resume_plan()` | `transition_current_plan()` | Active↔paused status changes are locked and idempotent. | Detailed rescheduling/re-anchoring is returned as `deferred` to WP-02.3. |
| Coach cancellation | `abandon_current_plan()` | Atomically abandons the current plan and terminalizes every open child step; replay returns the same canceled-step targets. | `cancel_step_jobs` is verified after commit. Deterministic cancel UX/follow-up remains WP-02.3. |
| Coach day/evening change and `/user/time-slots` | `change_delivery_time()` | Canonicalizes accepted time once, persists the slot and future step times, and records that same value in the receipt/result. Exact replay invokes a plan-wide proof: every eligible future job is exact and every obsolete deterministic job is absent, including a stale job whose new time is already past. Superseded replay is non-success with the authoritative value. | Paused decisions are saved but explicitly `deferred`; semantic collection UX remains WP-02.3. API authentication/enrollment remains WP-04.1. |
| Medium-plan evening preference collection | `record_evening_time_preference()` | Updates only the profile default and collected flag, so it cannot silently rewrite an active or paused plan schedule. Replay propagates `applied`/`superseded` and the authoritative value. | Plan-aware collection UX and semantic validation remain WP-02.3. |
| Plan-format request | `request_plan_format_switch()` | Reserves a durable deferred receipt keyed to the recorded plan/format. Receipt-first replay returns that original decision after completion, cancellation, no-current state, or a different current plan, plus a truthful current disposition. | Actual format-switch behavior belongs to WP-02.3. |
| Runtime/cron completion detection | `complete_current_plan_if_ready()` | Completes only the exact active owned aggregate when every child is terminal; replay exposes the same completion-report intent. Concurrent report attempts serialize within one runtime. After Telegram confirms a send, an in-memory known-success guard permits receipt-only retry and prevents that runtime from resending. | Cross-runtime durable obligation and automatic same-format successor remain WP-03.5. |
| Continuation hand-off | `prepare_continuation()` | Validates the exact owned completed source plan, reserves its stable operation identity, and returns an exact replay as the existing deferred result. | No successor is created before WP-03.5. |

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
