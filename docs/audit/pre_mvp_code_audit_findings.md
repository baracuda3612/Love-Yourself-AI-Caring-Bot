# Love Yourself — Pre-MVP Code Audit Findings

## Status

Living audit document. Updated after each audit area.

This file tracks factual code audit findings against the Pre-MVP Product Contract.

The goal is not to brainstorm new features and not to refactor the system.  
The goal is to identify what must be fixed, frozen, removed, or verified before the first beta.

---

## Source of truth

1. `docs/audit/product_contract.md` — baseline product contract.
2. Founder decisions in this file — override baseline when newer.
3. `resource/assets/product/conceptual_map.md` and `conceptual_map_en.md` — synchronized user-facing product facts; lifecycle changes must stay aligned with the founder decisions and contract.
4. Area audit findings — factual code observations, not product decisions.
5. Audit discussions — context only, already distilled into findings below.

---

## Audit rules

- Do not treat this file as a product brainstorm.
- Do not add features unless they are accepted as founder decisions.
- Findings must map to the Product Contract or accepted audit decisions.
- Each finding must have severity, status, current behavior, expected behavior, and minimal fix.
- Coach prompt has been rewritten (all 7 sections, integration pass done, PR #246 open) and is now in scope — see "Coach / Orchestrator Integration Findings" below.
- Old product documents are not source of truth unless explicitly referenced by `docs/audit/product_contract.md`.
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

## FD-01 — Automatic same-format continuation after completion

Status: accepted  
Priority: P1  
Area: Completion / Retention / Lifecycle

### Overrides previous baseline

Old flow:

```text
completion report → user chooses the next 7- or 14-day format
````

New flow:

```text
completed 7 days → next 7 days are already prepared
completed 14 days → next 14 days are already prepared
```

### Decision

After a user completes a 7- or 14-working-day plan, the system automatically creates the next period in the same format with the same delivery time or times and the same `work_days`.

The completion report is not the end of the relationship. It is a bridge between two plans.

The default is same-format continuation: 7→7 and 14→14.

### Default behavior

* create the next plan automatically from the completed plan's canonical format: `SHORT`→`SHORT`, `MEDIUM`→`MEDIUM`;
* reuse the same DAY delivery time and, for 14 days, the configured EVENING time;
* reuse the same work_days;
* start on the next selected working day;
* do not ask the user to reconfirm the same format;
* do not ask for evening time again when it is already configured;
* do not collect new onboarding data;
* do not push the user toward a different format by default.

### User control

User can still:

* stop / cancel;
* pause;
* change time;
* explicitly switch 7↔14 through the dedicated format-switch flow;
* write Coach.

### User-facing framing

Example:

```text
Наступні 7 або 14 робочих днів уже готові в тому самому ритмі.
Перший таск прийде [date] о [HH:MM].

Якщо хочеш змінити формат, час або зупинитись — просто скажи.
```

### Rationale

The gap after completion is a high-risk churn point.

Same-format continuation reduces friction while preserving user agency.

The user does not need to recommit to the same behavior they already accepted.

This is not coercion because opt-out, pause, cancel, change time, and switch-to-14 remain available.

### Code implications

* completion handler derives the canonical format from the completed plan and creates the same format automatically;
* next plan starts on the next valid workday;
* next plan uses the existing DAY time, existing work_days, and the existing EVENING time for `MEDIUM`;
* no new onboarding;
* no duration choice;
* no repeated evening-time collection for an existing 14-day format;
* report copy changes from “choose next plan” to “the next period is ready”;
* switching 7↔14 remains an optional explicit action, not the default CTA;
* no mini-onboarding is needed for same-format continuation.

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
→ normal path finalizes and reports on the next selected work day at the saved DAY time
→ fixed UTC cron remains a recovery safety net only
```

Do not use a two-hour delay as lifecycle logic. At that point the task window is still open, the user may still act, and the report metrics may change afterward.

### Code implications

* remove `_maybe_schedule_plan_completion` as a two-hour report trigger;
* `task_complete` / `task_skip` handle the fast active-action path;
* expiry closes unanswered task buttons;
* the normal no-action path resolves the user's timezone, next selected work day, and saved DAY time;
* `check_plan_completions` remains a fixed-UTC recovery safety net;
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

## FD-05 — No behavioral contingency in MVP; variety by randomness, feedback by explicit ask

Status: accepted (2026-07-22)
Priority: P1
Area: Plan Generation / Delivery / Retention
Resolves: PLAN-05 (from "P1 defect" to "OK by design")

### Decision

Two separate levers, set independently:

* **Content variety (stimulus unpredictability): MAX.** The exercise
  sequence is randomized so the user cannot predict tomorrow's exercise.
  This is a deliberate design property, not a bug. Its randomness derives
  only from **shown-history** (which exercises were already delivered),
  never from completion-history.
* **Behavioral contingency: ZERO.** The generated sequence does not
  change based on `completed`, `skipped`, or `ignored`. The system never
  infers *why* a user skipped and never rewards or punishes behavior with
  future content. Delivery follows the user-selected schedule.

**Terminology (corrected 2026-07-22).** Do *not* call this "variable
reward." In operant terms a variable reward is a variable *consequence of
behavior*; a random exercise sequence is not a consequence of anything the
user did, so the label is wrong and, worse, it imports the exact
TikTok/slot-machine connotation this product avoids. The accurate framing:
**content variety is intentionally non-contingent — each cycle receives a
different valid sequence, independent of completion, skip, or ignore.**
The unpredictability may still drive anticipation/engagement (real
psychology), but through novelty of a non-contingent stimulus, not through
reinforcement of behavior. These two levers are compatible precisely
because the variety is never earned or lost through the user's actions.

### Three levels of "learning" — only level 3 is excluded

"Personalization" and "learning" are not one thing. There are three
distinct levels, and the ZERO-contingency decision applies **only to
level 3**. Conflating them would wrongly forbid work that is actually
allowed.

1. **Global product learning (for everyone, equally).** Aggregate metrics
   and feedback rewrite weak exercises, retune timing, adjust the library
   or recipe. The builder gets smarter, but produces the same output for
   all users. Intended. This is the primary improvement path.
2. **Context personalization by explicit ask.** The product asks the user
   about their anchor/context (where they work, when they take breaks —
   Fogg Model 5, Anchoring) and the builder fits the exercise and time to
   that stated context. Personal, but collected by **asking, not
   inferring** — same rule as the feedback button. Ethically clean.
   Also the real answer to the fixed-14:00 prompt problem: one onboarding
   question about the user's routine lets both time and exercise fit their
   actual day, with no tracking. Deferred **only** for onboarding
   simplicity (funnel-length risk, FD-04), not for any ethical reason. A
   clean v2, not a forbidden zone.
3. **Behavioral-inference personalization.** The system guesses from
   completed/skipped/ignored what suits a user and diverges their content.
   This is the excluded lever. Deferred until real usage data, a direct
   effect signal, and critical mass exist — "not before critical mass,"
   not "not before v2." This is where the surveillance and adverse-
   selection risks live.

The dividing line for exclusion is **inference vs asking**, not
"personal vs not." Level 2 is personal and allowed because the user
states it; level 3 is forbidden because the system infers it.

### Why contingency is excluded (not deferred by accident)

* **Map ≠ territory.** `task_completed` does not prove the exercise was
  done or helped; `skip` does not prove the exercise is bad (the user may
  have been on a call). Personalizing on clicks is pseudo-precision.
* **Incentive corruption.** If `completed` unlocked "better" exercises or
  `skip` degraded the future, the system would train button-pressing and
  punish honest answers. In a B2B context this risks the product being
  perceived as employee monitoring — a fat-tail trust failure that can
  break the corporate sale even without any individual data reaching HR.
* **Adverse selection.** If future delivery depended on compliance, the
  most exhausted users would receive least support exactly when they need
  it most. Fixed-time delivery requires nothing to be "earned."
* **No statistical base.** At 10–15 users there is no basis for
  per-user personalization — only the risk of building complexity around
  noise.
* **History.** Behavioral adaptation was built in v3 (65 exercises) and
  deliberately cut as speculative. v1 ships with none by decision. The
  `ADAPTATIONS_ENABLED` mentions in code are deletion residue, not a
  pending feature.

### What MVP does keep

* **Different but neutral, non-evaluative response handling.** `completed`,
  `skipped`, and `ignored` record distinct states and telemetry (counters,
  time-to-response), and completion summaries are factual. None of this
  gates or shapes future content.
* **Optional explicit feedback ("did it help?") — after `completed` ONLY.**
  This is the *direct effect signal* that click data alone cannot provide.
  Critical constraint: the question is asked **only after `completed`**.
  After `skipped` the user did not do the exercise and cannot rate its
  effect — bolting an "оцініть вправу" prompt onto a skip is nonsensical
  and embarrassing. A skip needs a *different* question (why skipped), and
  that is a separate feature, deferred. For completed only: one optional
  tap; store `exercise_id`, answer, timestamp; change nothing in the
  current or next cycle; never send an individual answer to the company;
  use only for aggregated exercise review.

  This is a deliberate, scoped **exception to the "no new features" audit
  rule** — justified because it is architecturally trivial (no plan/state
  changes) and high-EV for product-level learning. Effort is realistically
  a few hours, not 15–20 minutes: `UserEvent.context` is already JSONB so
  likely no migration, but callback, idempotency, keyboard state, storage,
  and tests remain. It belongs to the **Delivery / Telemetry** area as a
  small standalone experiment, not to Plan Generation.

### Boundary for future work

User-level adaptation may be reconsidered only after real usage data and
the accumulated effect signal exist. Completion clicks alone are
insufficient. Product-level learning (rewriting weak exercises,
re-tuning timing globally) is Bayesian updating of the product, not
per-user profiling, and is the intended path.

### Code implications

* remove `ADAPTATIONS_ENABLED` mentions and any "pending adaptation" copy;
* disable legacy adaptation/engagement CTAs (see DEL-03);
* selection reads shown-history for variety, never completion-history;
* build the optional feedback capture; wire it to storage only;
* drop the `weight` field for now (PLAN-06) — difficulty grading is a
  post-data hypothesis, not an MVP dial.

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
→ next period in the same 7- or 14-day format is prepared by default
```

Fallback path:

```text
user does not press anything
→ task window closes / task expires
→ completion buttons are removed
→ next selected work day at the saved DAY time finalizes and sends the report
→ fixed UTC cron is recovery only
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
4. continue into the same 7- or 14-day format according to FD-01.

Remove the 2-hour delivery-based completion trigger. Use expiry + the user's next selected work day and DAY time for the normal no-action path; keep the fixed UTC cron as recovery only.

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

## Lifecycle Completion Area — Audit Round 2026-07-03, revalidated 2026-07-16

Status: completed, revalidated against the current Product Map and MVP contract

### Files inspected

* `app/telegram.py`
* `app/scheduler.py`
* `app/orchestrator.py`
* `app/plan_runtime/tools.py`
* `app/plan_finalization.py`
* `app/plan_completion/report.py`
* `app/plan_completion/cta.py`
* `app/plan_completion/metrics.py`
* `app/plan_completion/timeline.py`
* `app/plan_completion/tokens.py`
* `app/api.py`
* `app/templates/completion_report.html`
* completion-related tests in `tests/`

### Summary

Task status writes and local-day plan scheduling work, but the completion lifecycle is not wired correctly. The two-hour trigger can send a report while the old plan is still active. Fast completion from task callbacks is missing, the no-action path ignores the user's DAY time and selected work days, automatic same-format continuation is not connected, and the report/CTA still implement the old duration/load/focus lifecycle. Report delivery is also not durable enough to guarantee the Product Map promise that every completed period receives a summary.

### Accepted findings

#### LIF-01 — Completed/skipped status writes work

Severity: OK  
Status: confirmed

`handle_task_completed` and `handle_task_skipped` persist canonical step status, legacy compatibility fields, telemetry, and commit successfully.

#### LIF-02 — Last-task detection and fast completion trigger are missing

Severity: P1
Status: confirmed

The callbacks do not detect the final task and do not trigger completion within 1–2 minutes. They only update the step and send a task-level acknowledgement.

For a 14-day period, “last task” must mean that no other non-terminal step remains in the plan, not merely that the clicked exercise is last by time or order. Completion must not fire while the second exercise for that working day is still actionable.

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

#### LIF-04 — Automatic same-format continuation is not connected

Severity: P1  
Status: confirmed

`create_followup_plan` already supports both canonical formats and reuses stored schedule data, but the completion flow never calls it. The target contract is now same-format continuation: `SHORT`→`SHORT` and `MEDIUM`→`MEDIUM`, with the same `work_days`, DAY time, and configured EVENING time for 14 days.

Minimal fix: after finalizing the explicit completed plan, derive its canonical format and atomically create the same-format successor before confirming that the next period is ready.

#### LIF-05 — Telegram completion CTA is dead

Severity: BLOCKER  
Status: confirmed

The Telegram completion report creates `start_plan:` callback data, but no matching Telegram callback handler exists. A user can press a visible button and get no deterministic action. Under FD-01 these legacy next-plan buttons should be removed rather than repaired as the continuation mechanism; format switching is a separate explicit flow.

#### LIF-06 — Completion report copy and CTA use the old lifecycle

Severity: P1  
Status: confirmed

The current report asks the user to choose/repeat/change a plan and exposes legacy 21/30-day, load, focus, adaptation, persona, and internal slot logic. It also contains unsupported interpretations such as `Ранок виявився твоїм часом`, `Ти тримав ритм навіть коли було складно`, and `Це більше ніж більшість`.

Minimal fix: replace the legacy recommendation engine with a factual 7/14-day summary and confirmation of automatic same-format continuation. Preserve pause, cancel, time change, and explicit 7↔14 switching as separate controls.

#### LIF-07 — No-action completion belongs to expiry + cron

Severity: P1  
Status: confirmed

If the user does not press completed/skipped, the task must remain actionable until its expiry window closes. Expiry removes the buttons. The normal path then finalizes and reports on the next selected work day at the saved DAY time; the fixed UTC cron is recovery only. A two-hour delivery timer must not produce the report while the task is still actionable.

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

`create_followup_plan` raises unless `current_state` is in `_FOLLOWUP_STATES` (IDLE_FINISHED, IDLE_DROPPED, IDLE_PLAN_ABORTED). Per LIF-03 the state may still be `ACTIVE` at report time, so calling it directly from the current path raises. It also returns no `plan_id` or first-task date, and silently falls back to `14:00` when DAY time is missing. The target operation therefore needs explicit ordering and a completion-specific result: finalize old plan → create same-format successor → obtain its first delivery → send factual confirmation.

#### LIF-11 — No idempotency key on automatic next-plan creation

Severity: P1  
Status: confirmed

The report has an idempotency guard (`plan_completion_sent` event). Next-plan creation has no equivalent. Once FD-01 is wired, concurrent action, cron, retry, or message-entry paths could each attempt same-format continuation, producing duplicate plans or `ActivePlanExistsError`.

Minimal fix: guard creation with a plan-scoped key or unique parent-plan relationship, e.g. `next_plan_created_for:{completed_plan_id}`.

#### LIF-12 — No-action completion does not use the user's DAY time or work_days

Severity: P1
Status: confirmed

`check_plan_completions` runs once at 10:30 UTC and immediately finalizes every expired active plan it finds. It does not schedule the user-facing report for the user's saved DAY time and does not move delivery to the next selected working day. A Friday completion can therefore surface on Saturday, and users in different timezones receive it at unrelated local times.

Minimal fix: treat the fixed UTC cron as detection/recovery only. Resolve the next selected work day and saved DAY time in the user's timezone for the normal no-action path.

#### LIF-13 — Completion report delivery is not durable or atomic

Severity: BLOCKER
Status: confirmed

`_auto_complete_plan_if_needed` may create a fire-and-forget send task before the caller commits completion. If no event loop exists, it skips the send. After the plan is committed as completed, the cron no longer selects it because the cron queries only active plans. Metrics failure returns without a fallback report or retry. The `plan_completion_sent` check is also a non-atomic read-before-send guard, so concurrent paths can duplicate the message; success is recorded only after Telegram delivery. Normal Telegram send failures reschedule themselves every 30 minutes with no attempt cap, while the failure notice runs only when the submitted coroutine raises rather than when the send returns `None`.

Minimal fix: persist a plan-scoped completion workflow/outbox state in the same transaction as finalization, then process it idempotently through explicit `pending → sent` states. A report-generation failure must retain a retryable pending record rather than silently return.

#### LIF-14 — Web report misstates completed days for 14-day plans

Severity: P1
Status: confirmed

The web report calculates `completed_days = round(completion_rate * total_days)`, but a 14-day plan can contain two exercises per day. Exercise completion rate is not the same as completed-day count. Completing one of two exercises on every day displays `7 з 14 днів`, even though the user interacted on all 14 days.

Minimal fix: derive day-level counts from the existing timeline or label the metric honestly as completed exercises instead of completed days.

#### LIF-15 — Completion tests are stale and currently broken

Severity: P1
Status: confirmed

Scheduler tests explicitly assert the deprecated +2-hour behavior. Report and CTA tests still use 21/30-day, load/focus/adaptation contracts and currently fail because their `CompletionMetrics` fixtures omit newer required fields. No test covers the target end-to-end invariants: explicit-plan finalization before report, fast completed/skipped path, local expiry path, same-format continuation, or plan-scoped idempotency.

Minimal fix: replace legacy assertions with 7-day and 14-day lifecycle tests derived from FD-01/FD-02 and the Product Map. Do not merely repair the old fixtures to keep testing obsolete behavior.

#### LIF-16 — Plan end boundary respects timezone and selected work days

Severity: OK
Status: confirmed

`finalize_plan` maps logical plan days onto real dates from the user's selected `work_days`, schedules each step in the user's timezone, sets each `expires_at` to local `23:59:59`, and derives `plan_end_date` from the last actually scheduled step rather than by adding calendar days. This calculation is correct. LIF-12 concerns the later report-delivery path, not this boundary.

#### LIF-17 — Expiry, keyboard removal, and ignored telemetry are not one lifecycle event

Severity: P1
Status: confirmed

`expire_overdue_steps` checks hourly at minute `:05`, marks steps `expired`, and then removes Telegram keyboards. Depending on timezone offset, a dead keyboard can remain visible for up to roughly one hour after local expiry even though `validate_step_action` rejects the click. Separately, `check_ignored_tasks` logs `task_ignored` at 08:00 UTC using a sliding 24-hour window rather than the step's local expiry. User-facing closure, canonical step state, and ignored telemetry can therefore happen at different times.

Minimal fix: make local expiry the single source event for terminal state and ignored telemetry, and remove the keyboard within the accepted post-midnight UX window. Final telemetry schema details belong to the later telemetry audit.

#### LIF-18 — Report sending is an ambient side effect of `_auto_complete_plan_if_needed`

Severity: P1
Status: confirmed

`_auto_complete_plan_if_needed` decides whether to send the completion report based on whether a running event loop happens to exist in the caller's context: it calls `asyncio.get_running_loop()` and, on success, fires `asyncio.create_task(send_plan_completion_message(...))`; on `RuntimeError` it logs and skips. Its three callers rely on opposite behavior. `handle_incoming_message` is `async`, so the loop exists and the hidden task sends the report. `_trigger_plan_completion` and `check_plan_completions` assume the function does not send and submit `send_plan_completion_message` themselves via `_submit_coroutine`.

Today this does not double-send for one incidental reason: `scheduler` is a `BackgroundScheduler`, so its jobs run in worker threads with no running loop and the hidden `create_task` is skipped. Completion correctness therefore depends on the APScheduler executor type, not on the completion logic. Switching to `AsyncIOScheduler` — an otherwise innocuous cleanup — gives both scheduler paths a running loop and produces two completion reports per completed plan, guarded only by the non-atomic `plan_completion_sent` read-before-send described in LIF-13.

The plan id also differs between the two senders: the hidden task uses `plan.id` from `active_plans[0]`, while the explicit senders pass the caller's `plan_id` (see LIF-09).

Minimal fix: remove sending from `_auto_complete_plan_if_needed` entirely. Finalization must only finalize and record durable outbox state (LIF-13); delivery must be an explicit, separately invoked step for every caller.

#### LIF-19 — The plan-to-plan seam is undefined and mechanically biased toward same-day collision

Severity: P1 (target-behavior design gap)
Status: confirmed; founder-flagged 2026-07-22

Context: this is the seam where one plan ends and the FD-01 same-format
successor begins — specifically the timing of the completion report
relative to the new plan's first exercise. Automatic continuation is not
wired yet (LIF-04: `create_followup_plan` is only reachable as a Coach
tool, never from the completion path), so the seam does not exist in code
today. But the mechanical pieces already in place bias the *target* seam
toward exactly the "дибілізм" to avoid. Recorded now so the continuation
is built with a defined seam, not an emergent one.

**Mechanical facts (verified):**

* `create_plan`/`finalize_plan` anchor a new plan at `activation_time_utc
  = now` (`service.py`), and map logical day 1 via
  `next_active_date(anchor_date)`, which is **inclusive** (`>= from_date`,
  `active_days.py:72`). So a successor created on a working day gets
  **day 1 = the same day**.
* Consequence by path:
  * **Active completion** (target fast path, LIF-02): user presses
    completed on the last task at, say, 14:30 while DAY time is 14:00.
    Successor day 1 = today, scheduled_for = today 14:00 — **already in
    the past**. The first new exercise either fires immediately (user gets
    a fresh task seconds after the "план завершено" report) or is silently
    missed. Either is broken.
  * **No-action / ignored**: last task expires 23:59 local; the cron
    finalizes on a later day; a successor created then still anchors day 1
    to that same day, and the report timing is already wrong per LIF-12.
* The report is supposed to state the first task's date/time (FD-01 copy:
  "Перший таск прийде [date] о [HH:MM]"), but `create_followup_plan`
  returns no `plan_id` and no first-step datetime (LIF-10), so the report
  cannot currently state a true, consistent next-task moment.

**Required target seam (no-дибілізм contract):**

* successor **day 1 = next active day strictly AFTER** the completion/
  report day — never the same day. Offset the anchor by one day before
  day-mapping, or make the day-1 resolution exclusive of the completion
  date. This removes past-time day 1 and same-day double-exercise at once.
* exactly **one** seam message on the closing day: the report. It is
  delivered **first**, states the exact next-task date and time, and the
  first new task then arrives at precisely that stated moment — never
  before the report, never in the same instant.
* the report reads the successor's real first-step datetime (fix LIF-10 so
  the helper returns it), so stated and scheduled moments cannot diverge.
* both report and successor creation are guarded by plan-scoped
  idempotency (LIF-11) so cron + fast-path + any Coach path cannot
  double-create the plan or double-send the report.
* no-action path delivers the report at the user's DAY time on the next
  active day (LIF-12), consistent with the successor's first task.

Minimal framing: the seam is "close old → report with a concrete future
moment → new plan's first task lands exactly at that moment on the next
active day." One message now, one task later, never both at once.

### Required MVP work

* remove the two-hour completion trigger;
* add last-task detection to completed/skipped callbacks;
* trigger the report quickly after active completion/skip;
* keep unanswered buttons active only until expiry, then remove them;
* use the user's timezone, next selected work day, and DAY time for the normal no-action path; keep the fixed UTC cron as a safety net only;
* finalize the old plan before sending its report;
* create the next same-format plan atomically (`SHORT`→`SHORT`, `MEDIUM`→`MEDIUM`);
* rewrite completion report copy and CTA for FD-01;
* remove the dead legacy `start_plan:` continuation buttons;
* finalize the completing plan by explicit `plan_id`, not latest-by-`created_at`;
* add an idempotency key for automatic continuation to prevent duplicate plans;
* make report delivery durable and retryable after plan finalization;
* remove the event-loop-dependent hidden send from `_auto_complete_plan_if_needed` so finalization never delivers;
* fix day-level report metrics for 14-day plans;
* unify local expiry, keyboard removal, and ignored telemetry timing;
* replace stale tests with completed, skipped, expiry, timezone/work_days, report ordering, same-format continuation, and idempotency coverage.

### Verification on 2026-07-16

* `pytest -q tests/test_plan_completion_scheduler.py tests/test_plan_completion_metrics.py tests/test_orchestrator.py -k 'not trio'` → `27 passed, 2 deselected`;
* those green scheduler tests still assert the deprecated +2-hour path and therefore do not validate the target contract;
* isolated report/CTA run with test environment variables → `18 failed`, all from stale `CompletionMetrics` fixtures missing `engagement_rate`, `silent_miss_rate`, and `current_streak`;
* `tests/test_dashboard.py` could not be collected with the system interpreter because `fastapi` is unavailable there; the local `.venv` has `fastapi` but does not have `pytest`.

### Scope boundary

This audit records required behavior and confirmed code gaps. Exact transaction ordering, function signatures, and implementation architecture belong to a separate implementation plan. Report-token lifetime and company/individual access boundaries are intentionally deferred to the dedicated privacy audit.

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

# Plan Generation Findings

## Plan Generation Area — Audit Round 2026-07-18

Status: completed

### Files inspected

* `app/plan_drafts/plan_builder_v5.py`
* `app/plan_drafts/service.py`
* `app/plan_drafts/plan_types.py`
* `resource/assets/plan/plan_context_template.yaml`
* `resource/assets/content_library/tasks/burnout_combined_content_library.json`
* `app/telegram.py` (consequence of completed/skipped, for the operant frame)

### Method

Findings below are not read-only inference. The builder was executed
directly against the real library and recipe for 500 synthetic users and
for repeated regenerations of the same user, and the resulting series
were measured. Simulation commands and counts are reproducible from
`get_default_builder()`.

### Summary

The builder is clean, deterministic, and correctly enforces its stated
invariants. The problem is not correctness — it is that the generated
series is **frozen per user and blind to behavior**. The same user
receives a byte-identical exercise sequence on every plan, forever, and
nothing in generation reacts to whether the user did anything. Combined
with an 8-exercise library, this makes the product's core artifact
repetitive by construction.

### Findings

#### PLAN-01 — Every regenerated plan is byte-identical for the same user

Severity: BLOCKER
Status: confirmed by execution; fix blessed as the variety mechanism (FD-05, 2026-07-22)

> **Decision note (2026-07-22).** The fix below is not just bug-removal —
> it *is* the variety mechanism of FD-05 (level "variety by randomness").
> Unfreezing the seed turns the current single frozen combination into a
> fresh random draw from ~20,480 valid sequences per plan. Full random is
> deliberately blessed. Today the system does not have "one chosen
> combination" — it has one *accidental* frozen draw nobody selected,
> which is the worst of both worlds.

`_weighted_choice` seeds `random.Random(seed_key)` with
`f"{user_id}:{day}:{slot}"`. `day` is the plan-local day number, which
restarts at 1 for every new plan, and `last_used` starts empty on every
build. The seed therefore contains nothing that distinguishes plan N from
plan N+1.

Measured: building SHORT three times for the same `user_id` returns the
identical sequence every time.

```text
user 101, SHORT, runs 1/2/3 — all three:
somatic_001, somatic_006, somatic_005, somatic_006,
somatic_005, somatic_001, somatic_006
```

Under FD-01 (automatic same-format continuation) this is not a cosmetic
issue: the user completes 7 days, receives "наступні 7 днів готові", and
gets exactly the same seven exercises in exactly the same order. Every
period. Indefinitely.

Minimal fix: include a stable **per-cycle discriminator** in the seed —
`plan_instance_id` or a cycle number, **not** a creation timestamp. The
contract is: **deterministic within one cycle** (re-generating the same
cycle yields the same sequence) but **different across cycles**. A
timestamp breaks the first half. Also carry `last_used` across the plan
boundary so the first days of a new cycle do not repeat the final days of
the previous one.

#### PLAN-02 — Switching SHORT→MEDIUM replays the first 7 days

Severity: P1
Status: confirmed by execution

Because the seed depends only on `user_id:day:slot`, the DAY slots of a
MEDIUM plan reproduce the SHORT plan exactly for days 1–7; only days
8–14 are new. Measured for user 101 and user 202: `MEDIUM.DAY[0:7] ==
SHORT.DAY` is `True`.

A user who explicitly upgrades to the longer format is shown a full week
of repeats before reaching anything unseen — the opposite of what an
explicit upgrade should feel like. Same fix as PLAN-01.

#### PLAN-03 — Small library: repetition within a week is accepted, not fixed by growth

Severity: was BLOCKER → **decision recorded** (2026-07-22): library not expanded for MVP

> **Decision note (2026-07-22).** Original framing ("library too small,
> grow it") is rejected by the founder. The 5 `switch` exercises are the
> deliberately approved core — the previous 65 were mostly fillers and
> were cut. The product is state-change + rest, not exercise count. Adding
> exercises is itself an untested hypothesis and reintroduces fillers, so
> the library is **not expanded for MVP**.
>
> Consequence, stated honestly so it is not a hidden assumption: with 5
> exercises over 7 day-slots, within-week repetition is mathematically
> forced (pigeonhole — at least two exercises must repeat). This is
> accepted. Variety comes from **cross-plan** sequence randomness (PLAN-01
> fix), not from library size: each new period is a different random draw
> of order/frequency over the same 5 exercises. Day-to-day unpredictability
> ("never know tomorrow's exercise") holds; the content *pool* stays at 5.
> Whether a 5-exercise pool avoids week-3 habituation is a real open
> question answerable only in beta — recorded, not pre-answered.

The library holds 8 active exercises: 5 `switch`, 3 `unload`.

Measured across 500 users, SHORT (7 days, DAY slot only):

| distinct exercises in a 7-day plan | users |
|---|---|
| 5 | 177 |
| 4 | 268 |
| 3 | 54 |
| 2 | 1 |

| max repeats of a single exercise | users |
|---|---|
| 2× | 261 |
| 3× | 226 |
| 4× | 13 |

So the median user sees 4 distinct exercises across a 7-day period, and
nearly half see one exercise three or more times. For MEDIUM the EVENING
slot draws from 3 `unload` exercises across 14 evenings — measured
distinct count is 3, i.e. a forced rotation of the same three items.

Measurement note: with 5 `switch` exercises over 7 day-slots, some
within-week repetition is mathematically forced (pigeonhole). The seeding
fix (PLAN-01) does not add exercises the library lacks — it only varies
order/frequency **across** cycles. Per the decision note above, this is
accepted; library size is not increased for MVP. (Earlier reference
points about ≥7 / ≥14 exercises assumed the rejected "grow the library"
path and no longer apply.)

#### PLAN-04 — `cooldown_days: 1` permits every-other-day repetition

Severity: not a bug — tuning knob, accepted as beta hypothesis (2026-07-22)
Status: confirmed

`_is_in_cooldown` returns `(current_day - last_used[id]) <= cooldown_days`.
With `cooldown_days: 1` — the value on all 8 library items — an exercise
used on day N is blocked on day N+1 and available again on day N+2.

Combined with PLAN-03 this is what produces the 3× and 4× repeat counts
above. The cooldown mechanism works as written; the value is a **content
pacing dial**, not a defect. Given the accepted 5-exercise library
(PLAN-03), raising it too far starves the pool (`NoCandidatesError`).
Decision: keep `1` as the beta hypothesis; revisit only if usage shows
the repetition texture actually hurts.

Note the mechanism is shared across slots: `last_used` is one dict for
both DAY and EVENING, which correctly prevents the same exercise
appearing twice on the same day.

#### PLAN-05 — Generation is blind to user behavior (operant frame)

Severity: OK by design — resolved by FD-05 (2026-07-22)
Status: confirmed; product decision recorded

Requested analysis: does plan generation react to user behavior? **No,
verified structurally.** `plan_builder_v5.py` contains no reference to
step status, completion, skip, streak, or any history. Its only inputs
are `plan_type`, `user_id`, the two time strings, the recipe, and the
library. `ADAPTATIONS_ENABLED` is named in the invariant header but does
not exist as a config flag anywhere in `app/` (only in comments) — it is
deletion residue, not a disabled feedback path.

Locus note: reinforcement (in the operant sense) lives in the
response→consequence path, not in sequence generation, so the operant
question is really about the callback layer, not the builder. This finding
is filed under Plan Generation only because the question was asked about
generation. Two facts about that callback layer, recorded for accuracy:
`completed` and `skipped` record **distinct** states and telemetry (they
are not identical — they only lead to the same future next exercise); and
the completion report **arrives** for everyone regardless of completion
rate (LIF-08) but its **content** reflects actual completion (14/14 and
1/14 get different text, not identical text).

Resolution: this is not a defect. FD-05 records behavioral contingency as
intentionally excluded from MVP. Residual work is cleanup (remove
adaptation residue, disable legacy engagement CTAs), tracked under FD-05
code implications — not a new adaptation system.

#### PLAN-06 — `weight` is effectively decorative

Severity: P2
Status: confirmed; founder decision — drop for now (2026-07-22, FD-05)

Active weights span 1.2–1.5 (`1.5`×1, `1.4`×2, `1.3`×3, `1.2`×2). Across
a candidate pool of 2–5 items this is close to uniform selection; the
field implies editorial control over exercise frequency that it does not
meaningfully exert.

Founder decision: **drop the field** for MVP; selection is uniform (within
cooldown). Weighting exercise frequency by difficulty or editorial prior
is a post-data hypothesis, not an MVP dial — it should be driven by
behavioral data later, not guessed now.

#### PLAN-07 — `source_exercises` records the whole library, not the plan

Severity: P2
Status: confirmed

`source_exercises=[e.id for e in active]` stores every active exercise,
not the ones actually scheduled. It is persisted into `draft_data`
(`service.py:127`) and typed as "Which content library exercises were
used" (`plan_types.py:193`). Any later provenance or content-performance
analysis reading this field will be wrong.

Minimal fix: store the distinct `exercise_id` values actually placed in
`steps`.

#### PLAN-08 — Library `variations` is unused

Severity: P2
Status: confirmed; parked as deferred new feature (2026-07-22)

Every inventory item carries a `variations` array. **Correction
(2026-07-22):** it is no longer empty everywhere — 3 of 8 items now carry
variations (`somatic_006_v2`=3, `somatic_001_combined`=2,
`somatic_003_v2`=5), the other 5 are still empty. Regardless of fill
state, `ExerciseV5.from_library_item` does not read the field and no code
in `app/` consumes it — it remains **authored but unwired**.

> **Decision note (2026-07-22).** Not dead schema — this is the founder's
> own **unfinished** variations feature (e.g. a longer second exercise on
> the same theme for reactively-engaged users). It is a new feature, still
> at the "not yet thought through" stage, and the audit rule is "no new
> features in MVP." So it is **parked**: leave the field in place but
> unwired, no authoring guidance yet. Revisit only when the variations
> feature is actually designed. It is one candidate mechanism for level-2
> content variety later (FD-05), but not MVP.

#### PLAN-09 — Builder invariants that hold

Severity: OK
Status: confirmed

Verified correct and worth protecting with tests:

* first plan is forced to SHORT (`service.py:51`), independent of caller;
* MEDIUM refuses to fall back to the default `21:00` EVENING value unless
  `evening_slot_collected` is True — the silent-default trap is
  explicitly closed (`service.py:65-78`);
* `mechanic` is snapshotted onto the step at build time and never
  recomputed (invariant 6);
* an empty candidate pool raises `NoCandidatesError` rather than silently
  producing a short plan;
* no Focus / Load / StepType / SlotType / DifficultyLevel anywhere in the
  builder — the v5 cleanup is genuinely complete on this path.

### Required MVP work

Mandatory:

* add a stable per-cycle discriminator to the selection seed and carry
  cooldown state across cycle boundaries (PLAN-01, PLAN-02);
* fix `source_exercises` to record what was actually scheduled (PLAN-07);
* drop the `weight` field (PLAN-06);
* remove adaptation residue / `ADAPTATIONS_ENABLED` mentions and disable
  legacy engagement CTAs (FD-05, PLAN-05);
* add regression tests: same cycle re-generates identically; the next
  cycle differs; SHORT→MEDIUM does not replay days 1–7.

Consciously accepted (no work — recorded decisions, FD-05 / PLAN-03):

* five DAY exercises; within-week repetition; no behavioral adaptation;
  current `cooldown_days: 1` kept as a beta hypothesis (PLAN-04 is a tuning
  knob, not a bug — revisit only if beta shows repetition hurts).

Deferred (not MVP):

* `variations` (PLAN-08); exercise levels/unlocks; personalization;
  automatic future-cycle changes from feedback.

The optional post-`completed` feedback tap (FD-05) is a small standalone
experiment owned by the Delivery / Telemetry area, not Plan Generation.

### Scope boundary

This round covers exercise selection and series construction only.
Rendering of `display.*` belongs to the Delivery renderer round (assigned
in parallel). Scheduling of logical days onto real dates was already
confirmed correct in LIF-16 and is not re-audited here. Whether the
library's editorial content is clinically appropriate is out of scope.

---

# Delivery Renderer Findings

## Delivery Renderer Area — Audit Round 2026-07-18

Status: completed; findings recorded only, no runtime fixes applied

### Files inspected

* `app/content_library.py`
* `app/plan_finalization.py`
* `app/ux/task_notification.py`
* `app/scheduler.py`
* `app/telegram.py`
* `app/plan_guards.py`
* `app/plan_runtime/tools.py`
* `app/plan_pause.py`
* `app/ux/catalog.py`
* `resource/assets/content_library/tasks/burnout_combined_content_library.json`
* `resource/assets/ux/trigger_messages.json`
* `resource/assets/product/conceptual_map.md`
* `resource/assets/product/conceptual_map_en.md`
* delivery/task-related tests in `tests/`

### Method

The audit traced the complete user-facing path:

```text
v5 content JSON
→ ContentLibrary.content_payload
→ plan finalization / AIPlanStep snapshot
→ scheduler job payload
→ Telegram exercise message + buttons
→ complete/skip callback
→ post-click acknowledgement
→ expiry / keyboard removal
```

The current renderer was also executed directly against a real v5
library item to verify the output rather than relying only on static code
inspection.

### Summary

The core exercise message is deterministically broken for the current v5
library. All 8 active items store their user-facing fields under
`display`, while finalization and rendering still read the legacy root
schema. The resulting Telegram message can contain an internal exercise
ID, no instructions, and an unlabeled numeric duration. Buttons are
attached correctly, but their pause/cancel lifecycle is inconsistent with
the Product Map. The post-click path also activates legacy randomized
engagement/adaptation copy that is outside the accepted MVP contract.

### Findings

#### DEL-01 — V5 `display.*` is not rendered in the exercise message

Severity: BLOCKER / P0
Status: confirmed and reproduced

`load_content_library()` preserves the nested v5 payload; it does not
flatten `display`. Every current inventory item has
`display.title`, `display.steps`, and `display.duration_label`, and none
has root-level `title` or `instructions`.

The live path still reads the legacy shape in two places:

* `plan_finalization._build_step_title()` reads root `title`, then falls
  back to `ContentLibrary.internal_name`; the loader defaults that field
  to the exercise ID;
* `format_task_notification()` reads root `title`, `instructions`, and
  numeric `duration_minutes`/`duration_estimate` instead of the nested
  display object.

Direct reproduction with `somatic_004_v2` produced:

```text
━━━━━━━━━━━━━━━━━━
☀️ <b>somatic_004_v2</b>
День 1 · День · 1 з 1

⏱ 1
━━━━━━━━━━━━━━━━━━
```

The actual title `Дихання`, its four instructions, and the duration label
`30–60 сек` are all absent. This breaks the primary daily product loop.

Expected behavior: one canonical v5 display reader produces the trusted
`title`, ordered `steps`, and `duration_label` for both finalization and
Telegram rendering. Missing/invalid display content must fail visibly in
logs and tests rather than silently degrade to `Завдання` or an internal
ID. The same display data must later feed `current_exercise_context`
(cross-reference: COACH-08).

Minimal fix: introduce one structured display extractor/validator and use
it in finalization and delivery. Render the step array as concrete ordered
instructions; do not convert it into an opaque legacy `instructions`
field.

#### DEL-02 — Delivery exposes internal scheduling metadata and has a dormant pre-action rationale path

Severity: P1
Status: confirmed

The target contract is `title → steps → duration → buttons`. The current
message also renders:

```text
День {plan_day_number} · {DAY/EVENING label} · {task_index} з {task_total}
```

`DAY`/`EVENING` are internal scheduling tags under FD-03/C8, not exercise
content. The day/task counter is likewise outside the accepted delivery
shape and adds implementation framing to a deliberately small action.

`format_task_notification()` also contains a `Чому це працює` block before
the instructions. The current v5 inventory has no rationale field, so the
path is dormant today, but adding one later would silently violate the
contract that rationale belongs only in optional post-action closure.

Minimal fix: render only trusted display content in the exercise body and
keep internal slot, mechanic, task index, and rationale out of the
pre-action message.

#### DEL-03 — Legacy randomized engagement/adaptation runs after complete and skip

Severity: P1
Status: confirmed

After the callback toast, both handlers send an additional chat message
from `trigger_messages.json`. The catalog is old friend-bot/plan logic and
contains unsupported or false claims, including:

* the same exercise returning tomorrow even though the sequence rotates;
* changing difficulty or redesigning/reviewing the plan after two skips;
* internal `focus` framing and old `plan` terminology;
* pressure and judgment such as `головне не два підряд`, `не зупиняйся`,
  and personality conclusions from a streak.

After two explicit skips, the runtime also adds a `Переглянути план`
button and routes `adapt_suggest` into Coach. This is an automatic
adaptation/engagement prompt even though C7 says adaptation does not
trigger at all in MVP and C3 keeps expanded closure/reflection at P2.

Expected MVP behavior: persist the canonical result, remove the buttons,
and return one short deterministic acknowledgement. Do not activate the
random engagement catalog or an adaptation CTA. The exact acknowledgement
copy can be polished later; disabling unsupported behavior does not need a
new product decision.

#### DEL-04 — Delivered buttons conflict with pause and cancellation semantics

Severity: P1
Status: confirmed

The Product Map says exercise buttons remain available until the end of
the user's local day. `validate_step_action()` instead requires both an
active plan and `user.current_state == ACTIVE`.

Consequences:

* if the user pauses after receiving an exercise, the already-delivered
  Done/Skip buttons immediately stop working and answer `План зараз не
  активний`, although pause is defined as stopping future delivery;
* `cancel_plan()` removes scheduler jobs but does not mark delivered steps
  canceled and does not remove their Telegram keyboards. Stale buttons
  remain visible until expiry and answer the same error when pressed.

Minimal fix: make already-delivered current-day actions remain actionable
through their existing expiry when delivery is paused, unless the product
contract is explicitly changed. On cancellation, close pending/delivered
steps and remove visible keyboards immediately.

Cross-references: COACH-04 covers future deliveries lost during a pause;
LIF-17 covers the separate expiry/keyboard/ignored timing drift. This
finding covers only the delivered-message interaction surface.

#### DEL-05 — A transient Telegram send failure permanently loses the exercise

Severity: P1
Status: confirmed

`_send_message_async()` catches a Telegram exception and returns `None`.
`send_scheduled_message()` logs `task_delivery_failed`, but the APScheduler
date job has already fired. The step remains pending with a
`scheduled_for` timestamp in the past, while startup restoration selects
only future pending steps. There is no retry or recovery path inside the
two-hour late-delivery window.

Expected behavior: a bounded, idempotent retry/recovery path may re-attempt
the same step within the allowed delivery grace period, records each
failure, and never creates duplicate delivered events/messages.

#### DEL-06 — Renderer coverage is absent and callback tests are stale

Severity: P1
Status: confirmed

There is no direct regression test for `format_task_notification()` and no
test that renders the actual v5 `display.*` payload. Existing lifecycle
tests cover local expiry calculations, but not the final message body or
keyboard cleanup behavior.

The callback suite is also not executable as a reliable guard:

* its default `test-token` is rejected by the current aiogram token
  validator during collection;
* with a syntactically valid fake token, the targeted run produced
  `35 passed, 17 failed`;
* 8 asyncio failures come from stale `DummyStep` objects that do not have
  canonical `step_status`; 9 more are the unconfigured/missing `trio`
  backend.

Minimum tests before beta:

* render every active v5 item from `display.title`, ordered
  `display.steps`, and `display.duration_label`;
* assert that internal IDs/slots/mechanics and pre-action rationale are
  absent;
* assert the two callback IDs and ownership/idempotency behavior;
* cover active, paused, canceled, completed, skipped, and expired button
  states;
* cover successful delivery, bounded retry, and duplicate prevention;
* cover keyboard removal at expiry and cancellation.

#### DEL-07 — Dynamic HTML content is not escaped

Severity: P2 hardening
Status: confirmed; no current content failure

Telegram delivery uses `parse_mode="HTML"`, but title, instructions, and
duration are inserted without HTML escaping. A future content edit with
`<`, `>`, or `&` can turn a valid exercise into a Telegram parse failure,
which currently becomes the permanent loss described in DEL-05.

The 8 current display payloads contain no HTML-sensitive characters and
their core rendered content is small (the largest title/steps/duration
combination is 165 characters), so this is not the cause of the current
P0. Escape every dynamic display field and add a message-size assertion as
part of the renderer rewrite.

#### DEL-08 — Delivery mechanics that already match the contract

Severity: OK
Status: confirmed, with the limitations above

* the scheduled exercise is sent as one Telegram message with `Виконано`
  and `Пропустити` buttons attached immediately;
* callback data identifies the exact plan step;
* ownership is checked before a step is changed;
* canonical terminal states make repeated complete/skip actions
  idempotent in the runtime guard;
* `expires_at` is calculated from the user's local day;
* the expiry job marks unanswered steps expired and attempts to remove the
  keyboard.

These pieces should be preserved while fixing the content and lifecycle
gaps rather than replacing the whole delivery path.

### Required MVP work

* fix the v5 schema mismatch and use one canonical display reader
  (DEL-01);
* remove internal metadata and the pre-action rationale path from the
  message (DEL-02);
* freeze the legacy post-click engagement/adaptation layer and keep one
  deterministic acknowledgement (DEL-03);
* align pause/cancel keyboard behavior with the Product Map (DEL-04);
* add bounded delivery retry/idempotency (DEL-05);
* replace stale callback tests and add end-to-end renderer coverage
  (DEL-06).

HTML escaping and a size guard (DEL-07) are low-cost hardening and should
be included in the renderer change if possible, but they are not the
source of the current deterministic P0.

### Scope boundary

This round covers conversion of trusted exercise data into the Telegram
message and the immediate Done/Skip interaction lifecycle. Exercise
selection belongs to Plan Generation; editorial adequacy and the number
of steps per exercise belong to the later Content Library audit. General
pause rescheduling, completion reports, and Coach context are tracked in
COACH-04, Lifecycle Completion, and COACH-08 respectively.

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
