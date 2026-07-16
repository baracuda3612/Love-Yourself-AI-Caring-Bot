# Love Yourself — Pre-MVP Code Audit Findings

## Status

Living audit document. Updated after each audit area.

This file tracks factual code audit findings against the Pre-MVP Product Contract.

The goal is not to brainstorm new features and not to refactor the system.  
The goal is to identify what must be fixed, frozen, removed, or verified before the first beta.

---

## Source of truth

1. `product contract.txt` — baseline product contract.
2. Founder decisions in this file — override baseline when newer.
3. Area audit findings — factual code observations, not product decisions.
4. Audit discussions — context only, already distilled into findings below.

---

## Audit rules

- Do not treat this file as a product brainstorm.
- Do not add features unless they are accepted as founder decisions.
- Findings must map to the Product Contract or accepted audit decisions.
- Each finding must have severity, status, current behavior, expected behavior, and minimal fix.
- Coach prompt has been rewritten (all 7 sections, integration pass done, PR #246 open) and is now in scope — see "Coach / Orchestrator Integration Findings" below.
- Old product documents are not source of truth unless explicitly referenced by `product contract.txt`.
- If a finding is unclear, mark it as `UNCLEAR` and specify what file/function must be inspected next.

---

## Severity levels

### BLOCKER

Will break beta, trust, privacy, core delivery, or core lifecycle.

### P1

Must be fixed before first beta, but not immediately catastrophic.

### P2

Useful after first users; should not block beta.

### FROZEN

Existing or legacy component that should not be developed now. It may stay if it does not affect core MVP behavior.

### OK

Matches current product contract.

### UNCLEAR

Insufficient evidence. Needs inspection of another file or code path.

---

# New Founder Decisions

## FD-01 — Default opt-in next 7-day plan after completion

Status: accepted  
Priority: P1  
Area: Completion / Retention / Lifecycle

### Overrides previous baseline

Old flow:

```text
completion report → user chooses next plan
````

New flow:

```text
completion report → next 7-day plan is already prepared by default
```

### Decision

After a user completes a 7-working-day plan, the system automatically creates the next 7-working-day plan with the same DAY time and the same work_days.

The completion report is not the end of the relationship. It is a bridge between two plans.

The default next plan is always the same 7-day rhythm.

### Default behavior

* create the next SHORT / 7-working-day plan automatically;
* reuse the same DAY delivery time;
* reuse the same work_days;
* do not ask the user to choose another 7-day plan;
* do not ask for evening time;
* do not collect new onboarding data;
* do not push the user toward 14-day format by default.

### User control

User can still:

* stop / cancel;
* pause;
* change time;
* choose 14-day format;
* write Coach.

### User-facing framing

Example:

```text
Я підготував наступний тиждень у тому ж ритмі.
Перший таск прийде [date] о [HH:MM].

Якщо хочеш змінити час або зупинитись — просто скажи.
```

### Rationale

The gap after completion is a high-risk churn point.

Default continuation reduces friction while preserving user agency.

The user does not need to recommit to the same behavior they already accepted.

This is not coercion because opt-out, pause, cancel, change time, and switch-to-14 remain available.

### Code implications

* completion handler creates next SHORT plan automatically;
* next plan starts on the next valid workday;
* next plan uses the existing DAY time and existing work_days;
* no new onboarding;
* no duration choice;
* no evening time;
* report copy changes from “choose next plan” to “next week is ready”;
* 14-day becomes an optional switch, not the default CTA;
* no mini-onboarding is needed for default 7-day continuation.

---

## FD-02 — Remove the two-hour completion trigger

Status: accepted  
Priority: P1  
Area: Completion / Scheduler / Lifecycle

### Decision

The two-hour completion trigger has no valid MVP role and must be removed.

There are only two completion paths:

```text
user completes/skips the last task before expiry
→ trigger completion report quickly, ideally within 1–2 minutes
```

```text
user does not press completed/skipped
→ task expires at the end of its window
→ completion buttons are removed
→ next-day cron finalizes the plan and sends the completion report
```

Do not use a two-hour delay as lifecycle logic. At that point the task window is still open, the user may still act, and the report metrics may change afterward.

### Code implications

* remove `_maybe_schedule_plan_completion` as a two-hour report trigger;
* `task_complete` / `task_skip` handle the fast active-action path;
* expiry closes unanswered task buttons;
* `check_plan_completions` remains the no-action safety path;
* the current plan must be finalized before its completion report is sent.

---

## FD-03 — MORNING / DAY / EVENING are internal tags only

Status: accepted  
Priority: P1  
Area: Onboarding / Scheduling / User-facing terminology

### Decision

`MORNING`, `DAY`, and `EVENING` are internal technical tags attached to a concrete HH:MM time chosen by the user.

They are not user-facing product concepts and must not appear in user-facing questions, messages, buttons, reports, or plan descriptions.

The user sees:

* the actual time, for example `14:30`;
* natural language when needed;
* the logical plan day number only when useful.

The user must not see or choose an internal slot name. In particular, output such as `День 3 · День` must be removed: the first value is a plan day number, while the second is a leaked internal `DAY` tag.

`MORNING` remains frozen legacy metadata and must not be used by the MVP onboarding or first-plan flow.

---

## FD-04 — Onboarding is a deterministic mechanism-sale script

Status: accepted  
Priority: P1  
Area: Onboarding / Activation

### Decision

Core onboarding is a **deterministic scripted funnel, not an AI conversation**. It
sells the **mechanism** (timely interception at the user's own low-energy moment),
not exercises or content. No demo exercise in the base version.

Why a script beats a human/AI here: the funnel must be consistent, measurable, free
of extra questions, and guarantee the privacy promise. Human craft goes into writing
the script once — not improvising each run. A different pitch per run makes the
primary activation metric unmeasurable and puts trust surfaces at risk.

### Structure

```text
recognition
→ mechanism
→ setup
→ concrete promise + control + privacy
```

1. **recognition** — mirror the user's own chosen moment back with specificity;
   no invented psychological conclusions.
2. **mechanism** — one causal line: right moment → ready action → fewer decisions.
3. **setup** — collect one concrete HH:MM time + work_days (trigger setup, not a
   questionnaire).
4. **concrete promise + control + privacy** — exact first-task date/time,
   skip/change/stop, and a direct privacy statement.

### Accepted rules

* Script, not AI, in the core funnel.
* Privacy answer is **approved fixed text only**; AI must never improvise the answer
  to "what does my employer see?".
* Competence is created by **specific mirroring** of the user's chosen moment — not
  by demo, and not by unproven borrowed proof (e.g. do NOT claim "HR already vetted
  the product").
* Safety stated directly: user is in control; individual answers/completion are not
  shared with the company/HR.
* Known recurring questions → a short **approved FAQ (scripted)** that grows from
  real onboarding logs. Do not hardcode a fixed "objection count".
* AI (Coach) may pick up non-standard questions **only after onboarding completes** —
  never inside the critical funnel, never for privacy. Safety/crisis handling is a
  **separate guard**, not Coach improvisation.
* One selected moment per day is an existing product invariant (contract 2.4 /
  t23_t24) — restated, **not a new decision**. Evening only in the 14-day format.
* First real task arrives today **only when the chosen time is still ahead**;
  otherwise the next work day. No artificial "task now".

### Operating hypothesis — value in the first 2 minutes (NOT a proven decision)

The first 2 minutes deliver value not as an "exercise effect" but as three other
values: **recognition** ("they understood my specific moment"), **relief** ("I don't
have to remember or choose"), **predictability** ("I know exactly when and what will
happen"). This is the activation bet to validate in beta.

### Explicitly NOT decisions (A/B or rejected — keep out of contract)

* preview of tomorrow's real task message during onboarding — future A/B hypothesis;
* delivering a task "now" when the chosen moment has already passed — risks teaching
  a pull model; hypothesis only;
* borrowed "HR vetted this" proof — unproven, potential trust hit; rejected.

### Note on framing

The proof → promise → plan shape is used as a working structure only. No external
framework attribution is recorded (no verified primary source for a named "PPP"
framework; only a related "Proof > Promise" principle exists).

### Code implications

* onboarding writes DAY time + work_days and transitions to first-plan creation
  deterministically;
* confirmation copy uses the real first-step schedule (see ONB-08);
* first-plan creation must survive removal of `IDLE_ONBOARDED` (see ONB-07).

### Non-linear onboarding risks and acceptance constraints

These are product-system risks, not requests for additional MVP features. They must
be checked when the script and onboarding implementation are reviewed:

* **Funnel-length risk:** more mechanism explanation can increase understanding while
  reducing completion. The script must remain short enough that the user reaches time
  selection before the sales explanation becomes a new source of friction.
* **Mirroring risk:** specific recognition can create trust, but an invented cause,
  diagnosis, or emotional claim can feel manipulative or intrusive. Mirror only what
  the user actually selected or stated.
* **Promise/reliability coupling:** onboarding copy cannot create durable activation
  if the first task misses the promised moment. Successful plan creation and first-task
  delivery at the confirmed schedule are part of the onboarding acceptance path, not
  a separate downstream concern.
* **Privacy-truth coupling:** the fixed privacy statement is valid only if actual data
  access, reporting, logs, and HR/company surfaces match it. The statement must be
  verified against the implemented data flow before beta; copy must not promise more
  than the system guarantees.
* **Off-script input risk:** because AI is excluded from the critical funnel, arbitrary
  text during onboarding must not strand the user or silently reset progress. It needs
  deterministic handling: approved FAQ where matched, separate crisis guard where
  applicable, and a safe return to the current onboarding step.
* **Control-path risk:** back, edit, restart, stop, and time/work-day correction paths
  must preserve a comprehensible state and must not create duplicate plans.
* **Measurement risk:** onboarding completion alone can hide a broken activation loop.
  Review the observable chain: onboarding started -> time/work_days saved -> plan
  created -> first task delivered at the confirmed moment -> user responded.

The script is therefore necessary but not sufficient: its promise, persisted setup,
state transition, and first real delivery form one activation system.

---

# Scheduler Findings

## SCH-01 — Re-engagement job active

Severity: BLOCKER
Status: confirmed
Area: Scheduler / Background jobs

### Problem

Silent re-engagement job is active.

The scheduler still runs logic for:

```text
silent_2_days
silent_5_days
```

This sends messages outside the agreed scheduled plan flow.

### Contract

Re-engagement is OFF for MVP.

Only two message sources are allowed:

```text
1. scheduled delivery at agreed time
2. reactive response when user writes first
```

### Why it matters

Unexpected proactive messages are a trust break, especially in B2B2C context where the user may already be cautious because HR invited them.

It can feel like surveillance or pressure.

### Fix

Disable `silent_check` job.

Keep the function frozen if needed, but do not register it in scheduler for MVP.

Expected behavior:

```text
silent re-engagement code may exist
but no cron / interval job should trigger it
```

### Founder decision needed

No.

C12 / re-engagement OFF is already accepted.

---

## SCH-02 — SCHEDULE_ADJUSTMENT job active

Severity: P1
Status: confirmed
Area: Scheduler / FSM legacy

### Problem

Scheduler still checks for stuck `SCHEDULE_ADJUSTMENT` state.

This state is no longer part of the target MVP FSM.

Time changes should be handled through deterministic runtime tools, not a separate state tunnel.

### Contract

`SCHEDULE_ADJUSTMENT` is removed / replaced by tools.

Expected flow:

```text
user asks to change time
→ tool collects/validates time
→ tool updates future steps
→ confirmation only after success
```

### Why it matters

A dead state can create:

* unreachable transitions;
* stuck users;
* duplicate logic;
* mismatch between Coach/tools/FSM;
* confusing recovery behavior.

### Fix

Disable `stuck_schedule_adj_check` job.

Keep the function temporarily if needed for migration, but it should not run in MVP.

### Founder decision needed

No.

C_state is already accepted.

---

## SCH-03 — Fast completion after last task action missing

Severity: P1
Status: confirmed
Area: Lifecycle Completion, discovered during Scheduler audit

### Problem

After the user presses `completed` or `skipped` on the last task of the plan, the completion report should arrive quickly.

Expected delay:

```text
1–2 minutes after last completed/skipped action
```

Current suspected behavior:

```text
completion scheduling is triggered mainly after delivery / fallback,
not necessarily after task_complete / task_skip callback
```

### Contract

Active user path:

```text
user presses completed/skipped on last task
→ system closes plan loop quickly
→ completion report arrives in 1–2 minutes
→ next 7-day plan is prepared by default
```

Fallback path:

```text
user does not press anything
→ task window closes / task expires
→ completion buttons are removed
→ next-day cron finalizes the plan and sends the report
```

### Why it matters

A user who actively completed or skipped the last task should not wait like an inactive user.

This is the moment where the behavior loop closes and continuation can happen.

If the report is delayed too long, the user may leave before seeing progress or the next prepared plan.

### Fix

In `task_complete` and `task_skip` handler:

1. update step status;
2. check if this is the last step of the current plan;
3. if yes, trigger completion report with short delay;
4. continue into the default next 7-day plan according to FD-01.

Remove the 2-hour delivery-based completion trigger. Use expiry + next-day cron for the no-action path.

### Founder decision needed

No.

Fast completion after active last action is accepted as P1.

---

## SCH-04 — daily_pulse disabled

Severity: OK
Status: confirmed
Area: Scheduler / Background jobs

### Finding

`daily_pulse` is disabled.

Daily pulse job should remain off for MVP.

### Contract

Daily pulse is frozen/off.

Pushes without clear user value increase mute/block risk.

### Fix

No fix needed.

Keep disabled.

---

## SCH-05 — pulse snapshots frozen

Severity: OK / FROZEN
Status: confirmed
Area: Scheduler / Plan snapshots

### Finding

SHORT does not receive pulse snapshots.

This matches current MVP logic.

### Contract

First 7-day plan does not need pulse snapshot.

Pulse snapshot for longer formats can remain frozen.

### Fix

Do not touch before beta unless it breaks SHORT plan.

---

## SCH-06 — adaptation call path must be checked

Severity: P1 check
Status: pending
Area: Scheduler / Adaptation / Frozen components

### Problem

Scheduler does not clearly call adaptation, but comments imply adaptation may mutate step content after scheduling.

### Contract

Adaptation layer is fully frozen on MVP.

Important:

```text
frozen does not mean "do not improve"
frozen means "must not trigger"
```

The system must not automatically change:

* plan sequence;
* exercise content;
* delivery time;
* user path.

### Why it matters

Unexpected adaptation can look like surveillance or hidden personalization.

It can also break the promise that the sequence is prepared in advance and not based on hidden profiling.

### Fix

Verify call path for:

* `adaptation_executor`
* `plan_adaptations`
* adaptation-related jobs
* any mutation of plan steps after generation

Expected result:

```text
adaptation code may exist
but no MVP path should trigger it
```

### Founder decision needed

No.

C7 is already accepted: adaptation does not trigger at all on MVP.

---

# Lifecycle Completion Findings

## Lifecycle Completion Area — Audit Round 2026-07-03

Status: completed and accepted

### Files inspected

* `app/telegram.py`
* `app/scheduler.py`
* `app/orchestrator.py`
* `app/plan_runtime/tools.py`
* `app/plan_finalization.py`
* `app/plan_completion/report.py`
* `app/plan_completion/cta.py`
* `app/api.py`

### Summary

Task status writes work, but lifecycle completion is not wired correctly. The two-hour trigger can send a report while the old plan is still active. Fast completion from the task callbacks is missing, automatic default continuation is not connected, and the report/CTA still implement the old plan-choice flow.

### Accepted findings

#### LIF-01 — Completed/skipped status writes work

Severity: OK  
Status: confirmed

`handle_task_completed` and `handle_task_skipped` persist canonical step status, legacy compatibility fields, telemetry, and commit successfully.

#### LIF-02 — Last-task detection and fast completion trigger are missing

Severity: P1  
Status: confirmed

The callbacks do not detect the final task and do not trigger completion within 1–2 minutes. They only update the step and send a task-level acknowledgement.

#### LIF-03 — Completion report is sent before plan finalization

Severity: BLOCKER  
Status: confirmed

Current default path:

```text
last task delivered
→ scheduler creates a +2 hour completion job
→ plan_end_date is still 23:59:59
→ _auto_complete_plan_if_needed exits without finalizing
→ _trigger_plan_completion still sends the report
```

The user can receive a completed-plan report while the plan and user are still `ACTIVE`. The existing follow-up helper cannot run correctly from that state because it requires a finished/idle state.

Minimal fix: remove the two-hour trigger and ensure the old plan is actually finalized before its completion report is sent.

#### LIF-04 — Existing follow-up SHORT functionality is not connected

Severity: P1  
Status: confirmed

`create_followup_plan(user_id, "SHORT")` already supports a 7-working-day continuation with the stored DAY time, existing work_days, no new onboarding, and no evening time. The completion flow never calls it.

Minimal fix: make the orchestrator invoke the existing default continuation functionality as part of the completed-plan lifecycle defined by FD-01.

#### LIF-05 — Telegram completion CTA is dead

Severity: BLOCKER  
Status: confirmed

The Telegram completion report creates `start_plan:` callback data, but no matching Telegram callback handler exists. A user can press the main continuation button and get no deterministic action.

#### LIF-06 — Completion report copy and CTA use the old lifecycle

Severity: P1  
Status: confirmed

The current report asks the user to choose/repeat/change a plan and can expose legacy duration logic. It does not reflect FD-01 default continuation.

Minimal fix: rewrite the report and CTA around automatic 7-day continuation, while preserving pause, cancel, time change, and optional switch to 14 days.

#### LIF-07 — No-action completion belongs to expiry + cron

Severity: P1  
Status: confirmed

If the user does not press completed/skipped, the task must remain actionable until its expiry window closes. Expiry removes the buttons. The next-day cron then finalizes the plan and sends the report. A two-hour delivery timer must not produce the report while the task is still actionable.

#### LIF-08 — Completion report is not gated by completion rate

Severity: OK  
Status: confirmed

Completion rate changes report metrics/copy but does not prevent low-completion users from receiving closure.

#### LIF-09 — Completion may finalize the wrong plan

Severity: P1  
Status: confirmed

`_trigger_plan_completion(user_id, plan_id)` does receive an explicit `plan_id`, but `_auto_complete_plan_if_needed` finalizes the latest active plan by `created_at` (`active_plans[0]`), not that `plan_id`, while the report is built for the passed `plan_id`. If they differ, it finalizes plan A and reports on plan B. Narrow trigger (multiple active plans) — the same code already logs `"Multiple active plans found"`.

Minimal fix: finalize by explicit `plan_id`, not latest-by-`created_at`.

#### LIF-10 — Follow-up helper cannot be wired naively

Severity: P1  
Status: confirmed

`create_followup_plan` raises unless `current_state` is in `_FOLLOWUP_STATES` (IDLE_FINISHED, IDLE_DROPPED, IDLE_PLAN_ABORTED). Per LIF-03 the state is still `ACTIVE` at report time, so calling it directly from the completion path raises — the ordering (finalize → IDLE_FINISHED → create) must be fixed first. Two more facts on the same helper: it returns `{"status", "plan_type"}` with no `plan_id` and no first-task date (so the report cannot state the next task's date without a follow-up query), and its `day_time` falls back to `"14:00"` when the DAY slot is missing, unlike `create_first_plan`, which raises.

#### LIF-11 — No idempotency key on automatic next-plan creation

Severity: P1  
Status: confirmed

The report has an idempotency guard (`plan_completion_sent` event). Next-plan creation has no equivalent. Once FD-01 is wired, the +2h delivery timer and the 10:30 UTC cron (or a future button) can each call `create_followup_plan`, producing a duplicate SHORT plan or `ActivePlanExistsError`.

Minimal fix: guard creation with a plan-scoped key, e.g. a `next_plan_created_for:{plan_id}` event.

### Required MVP work

* remove the two-hour completion trigger;
* add last-task detection to completed/skipped callbacks;
* trigger the report quickly after active completion/skip;
* keep unanswered buttons active only until expiry, then remove them;
* use next-day cron for the no-action path;
* finalize the old plan before sending its report;
* connect the existing automatic SHORT continuation in the orchestrator;
* rewrite completion report copy and CTA for FD-01;
* replace the dead `start_plan:` callbacks;
* finalize the completing plan by explicit `plan_id`, not latest-by-`created_at`;
* add an idempotency key for automatic continuation to prevent duplicate plans;
* add tests for completed, skipped, expiry, cron, report ordering, and automatic continuation.

### Scope boundary

This audit records required behavior and confirmed code gaps. Exact transaction ordering, function signatures, and implementation architecture belong to a separate implementation plan.

---

# Onboarding Findings

## Onboarding Area — Audit Round 2026-07-04

Status: current implementation inspected; onboarding itself remains TODO

### Files inspected

* `app/telegram.py`
* `app/orchestrator.py`
* `app/workers/mock_workers.py`
* `app/db.py`
* `app/time_slots.py`
* `app/plan_runtime/tools.py`
* `app/plan_drafts/service.py`
* `app/plan_finalization.py`
* `app/ux/task_notification.py`
* `app/ux/plan_messages.py`
* `app/api.py`
* `app/fsm/states.py`
* `app/fsm/guards.py`

### Summary

The real MVP onboarding flow is not implemented. `/start` creates a user in `ONBOARDING:START`, but subsequent messages are routed to a mock onboarding worker that does not collect data, change state, or create the first plan. Backend support for a SHORT first plan exists, but DAY time, work_days, time recommendation/range, and the first-task confirmation are not wired.

### Findings

#### ONB-01 — Real onboarding flow is missing

Severity: BLOCKER  
Status: confirmed

Current code:

```text
/start
→ create user in ONBOARDING:START
→ next user message calls mock_onboarding_agent
→ static test response
→ no data write
→ no state transition
→ no first plan
```

The user remains stuck in onboarding.

#### ONB-02 — User-selected time and work_days are not collected

Severity: BLOCKER  
Status: confirmed

`UserProfile` receives defaults (`DAY=14:00`, Monday–Friday), but no onboarding code asks for or stores the user's actual chosen time or work_days. Defaults are not evidence of user consent or preference.

Contract:

```text
collect one concrete delivery time
→ collect work_days / weekend work preference
→ save both
```

#### ONB-03 — Internal slot tags leak into user-facing surfaces

Severity: P1  
Status: confirmed

Examples:

* task delivery maps internal tags to `Ранок / День / Вечір` and can render `День 3 · День`;
* completion report logic can produce `Ранок виявився твоїм часом`;
* web report exposes a dominant slot label;
* legacy time-slot surfaces model all three slots instead of one user-selected HH:MM.

Contract:

Internal slot tags remain available for storage, generation, and scheduling only. Remove all user-facing references according to FD-03.

#### ONB-04 — Recommended and allowed onboarding time range is not defined consistently

Severity: UNCLEAR  
Status: needs verification

Current code contains conflicting behavior:

* onboarding has no time recommendation or validation because it is a mock;
* generic HH:MM parsing accepts every valid clock time from `00:00` to `23:59`;
* `change_day_time` therefore accepts a value such as `03:00`;
* legacy schedule-adjustment UI offers DAY options from `12:00` to `17:59`;
* schedule-adjustment text claims `06:00–23:59`, while its actual slot inference rejects times before `12:00`.

The current product contract requires one daytime time but does not define one authoritative numeric recommendation and hard allowed range.

Minimal fix:

Do not implement a range by inference. First record a founder decision, then use one shared validator for onboarding, change-time tools, API, and scheduling.

Founder decision needed:

Yes. Define:

1. the recommended time or recommended options shown during onboarding;
2. the earliest and latest allowed DAY time;
3. whether a user may override the recommendation inside a broader safe range;
4. whether the same range applies when changing time later.

#### ONB-05 — First-plan backend already avoids duration and evening collection

Severity: OK  
Status: confirmed

`create_first_plan` always creates `SHORT`, requires the stored DAY time, and passes `evening_time=None`. The first plan does not need a duration choice or evening time.

#### ONB-06 — First-task date/time confirmation is missing

Severity: P1  
Status: confirmed

There is no post-onboarding confirmation containing the real first delivery date and HH:MM, such as:

```text
Перший таск прийде [сьогодні/завтра] о [HH:MM].
```

The existing generic first-plan reply does not expose the calculated activation anchor.

#### ONB-07 — create_first_plan depends on legacy IDLE_ONBOARDED state

Severity: P1  
Status: confirmed

`create_first_plan` raises unless `user.current_state == "IDLE_ONBOARDED"` (`tools.py:77`). The target FSM (C_state) removes `IDLE_ONBOARDED` and routes onboarding directly to `ACTIVE` via first-plan creation. If the legacy state is removed without updating this tool, first-plan creation breaks.

Minimal fix: when `IDLE_ONBOARDED` is removed, update the allowed entry state of `create_first_plan` to the onboarding-complete state that leads into plan creation.

#### ONB-08 — First-task confirmation must use the finalized step, not the anchor

Severity: P1  
Status: confirmed

The activation anchor computes today/tomorrow, but `finalize_plan` then shifts the first task to the next work day via `next_active_date`. A confirmation built from the anchor can say "tomorrow" while the real first `AIPlanStep.scheduled_for` is a later work day.

Minimal fix: build the confirmation copy from the actual first step's `scheduled_for` after finalization. Complements ONB-06 (confirmation missing) by fixing the data source.

### Backlog items to add

* implement onboarding for exactly one concrete HH:MM time and work_days;
* do not ask for duration, MORNING, or evening time;
* remove user-facing MORNING / DAY / EVENING terminology;
* obtain founder decision on recommended and allowed DAY time range;
* apply the decided range through one shared validator;
* create the first SHORT plan automatically after onboarding data is saved;
* confirm the actual first-task date and time;
* build the confirmation from the finalized first step's `scheduled_for`, not the activation anchor (ONB-08);
* when removing `IDLE_ONBOARDED`, update the `create_first_plan` entry-state guard (ONB-07);
* implement onboarding as the FD-04 deterministic mechanism-sale script (recognition → mechanism → setup → promise + control + privacy), with an approved privacy text and approved FAQ;
* keep the core script short and review funnel drop-off before and at time selection;
* verify the approved privacy statement against actual data access, reporting, logs, and HR/company surfaces before beta;
* handle arbitrary onboarding text deterministically without losing the current step, with approved FAQ matching and the separate crisis guard;
* test back/edit/restart/stop paths and ensure retries cannot create duplicate plans;
* instrument the full activation chain: onboarding start, setup saved, plan created, first task delivered at the confirmed moment, and first response;
* add onboarding tests for state transition, persistence, forbidden questions, range validation, first-plan creation, and confirmation copy.

---

# Coach / Orchestrator Integration Findings

## Coach / Orchestrator Integration Area — Audit Round 2026-07-16

Status: Coach prompt rewrite complete, findings below discovered during that
review (moved here from Coach-session working notes so the broader MVP
audit has one source of truth).

### Context

All 7 sections of `COACH_SYSTEM_PROMPT` were rewritten and an integration
pass synced `COACH_TOOLS`, the context builder (`_context_message`/
`_compose_messages`), `orchestrator.py`, and the Product Map to a single
target contract. PR #246 (`refactor/t5-8c-prompt-refactor` → `main`) is
open, not merged. The findings below are runtime/tool-level gaps
discovered while writing and verifying that prompt against the actual
code — not prompt-text issues.

### Files inspected

* `app/workers/coach_agent.py`
* `app/orchestrator.py`
* `app/plan_runtime/tools.py`
* `app/plan_pause.py`
* `app/scheduler.py`

### Findings

#### COACH-01 — record_evening_time has no runtime validation

Severity: P1
Status: confirmed

`record_evening_time(hhmm)` does not check `user.current_state`, does not
check `evening_slot_collected == False` before writing, and
`_validate_hhmm()` only checks the `NN:NN` shape — it accepts `99:99`.
The "first-time collection only" constraint currently exists only as
prompt text (Section 7), not as a runtime guard.

Minimal fix: add state check + `evening_slot_collected` check + a real
HH:MM range validator to `record_evening_time()`.

Founder decision needed: No — enforcement of an already-agreed contract.

#### COACH-02 — get_plan_status paused-state display bug — RESOLVED

Severity: was P1
Status: RESOLVED (commit `83370f8`, "fix(T5.8C): report paused delivery in plan status")

Was: `orchestrator.py::_execute_plan_tool`, in the `get_plan_status`
branch, checked only `result.get("plan_active")` (True/False) and always
rendered `"📋 Стан: активний план"` when true — never read
`result["state"]` to distinguish `ACTIVE` from `ACTIVE_PAUSED`.

Fix shipped: new `_format_plan_status()` helper branches on
`result.get("state") == "ACTIVE_PAUSED"` and renders "доставка вправ
призупинена" vs "доставка вправ активна". Covered by new tests in
`tests/test_orchestrator.py`.

#### COACH-03 — create_followup_plan silently defaults a missing plan_type

Severity: P1
Status: confirmed

`_build_tool_registry()`'s lambda for `create_followup_plan` does
`args.get("plan_type", "SHORT")` — if the model or a parser returns
empty arguments, the system silently creates a SHORT plan without
explicit user consent for that specific choice, even though `plan_type`
is a required field in the `COACH_TOOLS` schema.

Minimal fix: fail closed (raise / reject) instead of defaulting when
`plan_type` is absent.

Founder decision needed: No.

#### COACH-04 — pause/resume does not reschedule deliveries missed during pause

Severity: P1
Status: confirmed (code-verified)

`plan_pause.py::pause_plan()` docstring states: "Does NOT rewrite or
reschedule any plan steps." `resume_plan()` only flips `is_paused`/
`current_state`. `scheduler.py:113` gates delivery on
`current_state == "ACTIVE"` — any step whose `scheduled_for` falls
inside the pause window is silently skipped when its job fires, and
nothing reschedules it after resume. It is lost, not deferred.

Contract: Conceptual Map §10 says resume continues "з того самого
місця" (day 3→4) — the current implementation does not fully honor
this for steps that were due during the pause window.

Minimal fix: on pause, mark/skip-forward steps inside the pause window;
on resume, reschedule remaining steps from the next valid workday.

Founder decision needed: No — already implied by the existing
Conceptual Map contract (§10).

#### COACH-05 — change_evening_time: Coach cannot reliably disambiguate day vs evening

Severity: P2
Status: confirmed

Coach's runtime context does not currently expose whether the user is
on a 7-day (SHORT) or 14-day (MEDIUM) sequence. When the user says
"change my time" without specifying day/evening, Coach has no reliable
signal to pick between `change_day_time` and `change_evening_time`.

Minimal fix: expose current plan format (SHORT/MEDIUM) in Coach's
runtime context.

Founder decision needed: No.

#### COACH-06 — State-filtered tool registration — RESOLVED

Severity: was P1 (architecture improvement)
Status: RESOLVED 2026-07-16

Previously: all 8 tools were sent to the model on every Coach call
regardless of `current_state`; the FSM×tool table in the prompt was the
only enforcement (text, not code).

Resolution: `_coach_tools_for_state()` + `_TOOL_NAMES_BY_STATE`
implemented in `coach_agent.py`. `coach_agent()` now includes `tools=`
in the API call only when the current state allows at least one tool.
Verified with a parametrized test covering all live and dead states.

#### COACH-07 — create_first_plan legacy guard (cross-reference, not a new finding)

Severity: P1
Status: confirmed — same issue as ONB-07 above, tracked there

`create_first_plan()` in `plan_runtime/tools.py:77` still hard-requires
`user.current_state == "IDLE_ONBOARDED"`. Coach can no longer call this
tool at all (removed from `COACH_TOOLS`/registry/reply-template in the
prompt rewrite — onboarding is expected to create the first plan
deterministically per FD-04). The fix belongs entirely to the onboarding
rewrite already tracked as **ONB-07**; no separate action needed here.

#### COACH-08 — P0: delivered exercise context missing from Coach runtime

Severity: P0 (pre-production)
Status: confirmed

Coach's prompt (Exercise Explanation Boundary) is written to explain the
current exercise only from instructions available in
`current_exercise_context` — but the runtime does not populate or send
this field to Coach at all. `short_term_history` doesn't carry it either
(scheduled exercise messages aren't appended to Redis session history).
With the current runtime, Coach has no way to answer "how do I do this
exercise?" without inventing steps, which violates its own
anti-hallucination rule.

Minimal fix: add a structured `current_exercise_context` to the Coach
payload rather than relying on `short_term_history`, built from the
latest relevant delivered `AIPlanStep` and its trusted
`ContentLibrary.content_payload.display` data:

```json
{
  "title": "Дихання",
  "steps": [
    "Вдих — повільно, на 4 рахунки.",
    "Затримай подих на 7.",
    "Видих — повільно, на 8.",
    "Повтори 4 рази."
  ],
  "duration_label": "30–60 сек"
}
```

`delivered_today` must be evaluated in the user's local timezone. In
the 14-day format (two exercises possible on the same working day), use
the most recently delivered exercise; if a later workflow needs Coach
to distinguish both explicitly, expand to `delivered_exercises_today` —
do not silently guess which one the user means. If nothing was
delivered today: `{"current_exercise_context": null}`, not invented
data. Coach may clarify/repeat only the supplied `title`/`steps`/
duration — no variations, no added steps.

Tests needed: latest delivered exercise included for `ACTIVE`;
`display.steps`/`duration_label` preserved exactly; future/pending/
skipped/canceled/unrelated exercises not exposed as current; missing
content produces `null`, not invented data; context reaches
`_compose_messages()` before the user message.

**Related P0 — v5 delivery rendering (separate bug, same root data):**
the v5 content library stores instructions under
`content_payload.display.title` / `.steps` / `.duration_label`, but
`plan_finalization._build_step_title()` reads root `content_payload.title`,
`plan_finalization._build_step_description()` reads root `description`/
`text`/`instructions`, and `format_task_notification()` reads root
`instructions` and root duration fields. The content loader preserves
the nested `display` object without flattening it — the current
delivery path may fail to render `display.steps` in the actual Telegram
exercise notification the user receives. Fix this first: if the user
doesn't receive `display.title`/`display.steps`/`display.duration_label`,
the primary daily product loop is broken independently of Coach. The
user and Coach must end up reading the same trusted exercise data.

Founder decision needed: No — already implied by the existing
exercise-explanation contract.

#### COACH-09 — Architecture decision: Bounded Tool-Result Loop

Severity: P1 (before first external MVP user)
Status: accepted as target architecture, not implemented

Current: Coach is a one-shot command dispatcher — it returns a tool
call, the orchestrator executes it, and a hardcoded Ukrainian template
is sent back. Coach never sees the result and cannot phrase a
context-aware, tone-consistent reply. Concrete evidence that this class
of drift is real, not hypothetical: COACH-02 above — a hardcoded
template rendered "active" for a paused sequence until it was fixed
directly in the template. This general risk (a canned string silently
diverging from runtime truth) remains even after that specific instance
is resolved.

Target: bounded two-step loop. Coach makes at most one tool call;
runtime executes it; a structured result
(`{"status": "success"/"error", "facts": {...}}`) is returned to Coach;
tools are disabled on the second call; Coach writes one natural reply
from the facts; deterministic templates remain only as a fallback if
the second LLM call fails.

Estimate: 2-4h minimal; ~1 day with full test coverage of all 8 tool
results.

Founder decision needed: implicitly yes (architecture investment before
first external user) — already agreed as P1, not P0.

#### COACH-10 — Product question escalation flow (backlog)

Severity: backlog (not blocking)
Status: not implemented; current Coach behavior is safe in the meantime

If the Product Map and runtime context don't contain a factual answer,
Coach currently says it doesn't have that detail and stops there —
there is no actual escalation channel (support contact, DB queue, or an
`escalate_product_question` tool) for these questions to go anywhere.

Founder decision needed: Yes — where escalated questions go, who
answers them, automatic vs. user-confirmed escalation, response-time
expectation shown to the user, whether unresolved questions get stored
for future Product Map updates.

Possible implementation options: a deterministic support contact/button;
a support queue persisted in the database; an
`escalate_product_question` runtime tool available to Coach; an admin
notification with a later human reply flow.

Required Coach behavior until implemented (already in the prompt): say
it doesn't have the detail; never guess/approximate/invent an answer;
never claim a question was sent/escalated/reported unless a real
escalation action actually completed and returned success.

Future tool contract, if implemented: minimum input
`{"question": "...", "relevant_context": "..."}`; result should
distinguish accepted-for-human-review / already-answered-by-existing-source /
failed-to-submit.

Status: backlog. Not required for current prompt work, but must be
resolved before Coach is instructed to offer product escalation as an
available user action.

#### COACH-11 — switch_plan_format tool needed (target architecture, not implemented)

Severity: P1 (confirmed founder decision, target architecture)
Status: confirmed as target 2026-07-16, not implemented

Switching between 7-day and 14-day format from an ACTIVE/ACTIVE_PAUSED
sequence currently requires two separate steps (`cancel_plan`, then
`create_followup_plan` from `IDLE_PLAN_ABORTED`) — real friction for a
"let's try the other format" request.

Decision: build a dedicated atomic `switch_plan_format(plan_type)` tool
— available from `ACTIVE`/`ACTIVE_PAUSED`, single user confirmation,
atomically ends the old sequence and creates the new one (collecting
evening time first if switching to 14-day), old period isn't cancelled
until the new one is guaranteed creatable.

Note: the Product Map (`conceptual_map.md`/`conceptual_map_en.md`)
already describes this as "one user-confirmed action" — this is
intentional target-contract documentation, not a shipped capability.
Until the tool exists, Coach can only offer the two-step path.

#### COACH-12 — Lifecycle friction: format switch immediately after FD-01 auto-continuation

Severity: P2 (edge case, not blocking)
Status: identified, not resolved

Once FD-01 auto-continuation is wired, if the system already
auto-created a new 7-day ACTIVE sequence right after completion and the
user then asks to switch to 14 days, the just-created sequence must be
cancelled first — same friction as COACH-11, worth resolving together
with `switch_plan_format`.

### Required work before beta

* fix remaining COACH-01, COACH-03, COACH-04, COACH-05 (runtime
  validation/scheduling gaps — COACH-02 already resolved);
* implement Bounded Tool-Result Loop (COACH-09) before first external
  user;
* fix P0 delivered exercise context (COACH-08) before ACTIVE-state
  exercise explanation ships to real users;
* decide and implement product question escalation (COACH-10) before
  Coach is instructed to offer escalation as an available action;
* implement `switch_plan_format` (COACH-11/COACH-12) as part of closing
  FD-01 lifecycle work;
* merge PR #246 after review.

---

# Miscellaneous Findings

> Standalone findings that don't yet belong to an audited area above.
> File each one under its proper area (with a proper Audit Round) once
> that area gets its own audit pass. Do not leave them here permanently
> if a matching area already exists — check first.

## MISC-01 — Company-level timezone model for B2B onboarding

Status: product decision confirmed (2026-07-15), not implemented
Future area: Company / B2B Onboarding (not yet audited as its own area)

Decision: timezone is NOT collected per individual user in MVP — this
is a B2B2C product, so the company sets the timezone context at
company-onboarding time, not the employee.

Model: `organization.default_timezone` / `organization.available_offices`.
One office → timezone applied automatically, user isn't asked. Multiple
offices → office/timezone comes from roster or invite link, or one
question with office buttons if unknown. Per-user override in Settings
for travel (no automatic geolocation/travel detection in MVP).

Current code fact: every new user is silently defaulted to
`Europe/Kyiv`; timezone is never actually collected anywhere. Not a
correctness bug today (single-timezone launch), but will misroute
delivery the moment a company outside that timezone signs up.

Move to a proper "Company / B2B Onboarding" audit round when that area
is inspected.

## MISC-02 — Time Picker UX (Phase 3, not MVP)

Status: product decision confirmed (2026-07-15), not implemented
Future area: Delivery UX / Choice Prompts Infrastructure (not yet
audited as its own area)

Decision: buttons-first UX for time selection — preset time buttons +
"інший час" opens a deterministic hour → 15-minute picker (2 taps, no
free-text HH:MM, no LLM involved). Same reusable keyboard component
across onboarding, first evening-time collection, and later time
changes. Button selection counts as confirmation (no redundant "точно?"
follow-up).

Requires (not built): a new `show_time_picker(target)` Coach tool
(`target`: DAY / EVENING / UNSPECIFIED), a callback schema like
`time:day:15:00`, callbacks that call runtime tools directly without an
LLM round-trip.

Explicitly deferred — not MVP. Coach currently uses a natural-language
fallback (Section 7 Time Arguments) instead.

Move to a proper "Delivery UX" audit round when that area is inspected.
