# WP-01.3 lifecycle compatibility manifest

This manifest is the explicit expand/backfill/writer-switch/reader-switch
boundary for `20260902_plan_lifecycle`. PostgreSQL plan facts are authoritative
after the revision commits. Legacy columns remain physically present for a
bounded observation window, but are cleared and never synchronized.

There is deliberately no mixed-version write window. The old poller and
scheduler must be stopped before the revision runs: old pause/FSM writers do
not know the new authority, while the old build's exact Alembic revision check
also prevents it from restarting on the new head. Rollback is therefore a
database restore/forward-fix decision, not an old-binary restart.

| Legacy surface | WP-01.3 behavior | Remaining consumer | Removal owner |
|---|---|---|---|
| `users.current_state` | Read once to seed `onboarding_progress`, then cleared; the default is removed. The new runtime neither derives from nor writes it. | Inert ORM mapping only; the retained `SCHEDULE_ADJUSTMENT` scanner entrypoint is a no-op. | WP-02.1 removes the tunnel; WP-08.1 drops the column after deployed-use evidence. |
| `user_profiles.is_paused` | Cleared to nullable inert storage; pause/resume reads and writes only `ai_plans.status`. | Inert ORM mapping only. | WP-08.1 after WP-02.1. |
| `users.plan_end_date` | Cleared; completion requires all authoritative steps to be terminal. | Inert ORM mapping only. | WP-08.1. |
| `ai_plans.end_date` | Cleared. `plan_completion_at()` calculates the last terminal step timestamp. | Inert ORM mapping only. | WP-08.1 after report readers use calculated completion. |
| `ai_plans.current_mode` | Existing values are nulled and the default is removed. `app.lifecycle.derive_current_mode()` is the only mode derivation. | Inert ORM mapping only. | WP-08.1. |
| `ai_plans.current_day` | Cleared and made nullable; progress is calculated from ordered days and non-terminal steps. | Inert ORM mapping only. | WP-03.4 switches remaining delivery presentation, then WP-08.1 drops it. |
| `ai_plan_days.is_completed`, `completed_at` | Cleared after backfill; day terminality is calculated from child `step_status`. | Inert ORM mapping. | WP-08.1. |
| `ai_plan_steps.is_completed`, `skipped`, `completed_at` | Used only as non-contradictory backfill evidence, then cleared and stripped of defaults. New callbacks write `step_status` and `terminal_at` only. | Inert ORM mapping only. | WP-03.4 finishes delivery switch; WP-08.1 drops columns. |
| `plan_status_enum` and legacy `plan_status` enum | `ai_plans.status` is converted to the normalized `plan_status`; the prior unused type is renamed `legacy_plan_status`. | Type objects only; no authoritative column. | WP-08.1 after rollback window. |
| generic `generated_plan_object`, `plan_updates`, `transition_signal` writers | Inputs are explicitly ignored by the live orchestrator. Helper bodies remain inert to avoid beginning broad dead-subsystem deletion here. | No accepted worker can emit an authoritative mutation through them. | WP-02.1 removes the zombie mutation architecture. |
| `SCHEDULE_ADJUSTMENT` stored FSM/Redis tunnel | Its periodic job is no longer registered, its scanner is a no-op, and its stored-FSM writer raises. No plan lifecycle fact is stored in Redis. | Inert callbacks/helpers only. | WP-02.1 removes the subsystem as one bounded package. |
| admin `/spawn` structural writer | Disabled because it appended steps to an active immutable aggregate outside draft finalization. | Command entrypoint returns a diagnostic message only. | WP-03.4 may add a fixture-only delivery path that does not mutate a real plan. |
| `apscheduler_jobs` | Unchanged and outside Alembic ownership. Jobs can trigger work but never define plan or step lifecycle. | APScheduler `SQLAlchemyJobStore`. | Scheduler reconciliation work in WP-03.4; never Alembic-owned. |

Deployment ordering is:

1. stop every old application, polling, worker, and scheduler process;
2. apply the transactional expand/backfill revision;
3. start only the WP-01.3 build, which writes plan/step authority and operation
   receipts;
4. verify derived-mode, mutation, scheduler, and compatibility checks;
5. leave the listed storage inert until its named package removes the final
   consumer;
6. perform destructive contract cleanup only after that consumer evidence.

The migration aborts instead of choosing a winner for multiple current plans,
inventing an unsupported duration, inventing an activation/abandonment time, or
normalizing contradictory terminal facts. Because PostgreSQL DDL is
transactional, a failed upgrade leaves the Alembic revision at the baseline;
after evidence-based remediation, `alembic upgrade head` resumes safely.
The legacy timezone-less `ai_plans.activated_at` value is not treated as
standalone chronology evidence: migration activation comes from the recorded
timezone-aware plan start, falling back only to row creation time.
