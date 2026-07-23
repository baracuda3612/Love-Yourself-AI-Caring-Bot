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
3. `docs/audit/delivery_contract.md` — accepted product contract for the daily exercise touchpoint (adopted by FD-06).
4. `resource/assets/product/conceptual_map.md` and `conceptual_map_en.md` — synchronized user-facing product facts; lifecycle changes must stay aligned with the founder decisions and contract.
5. Area audit findings — factual code observations, not product decisions.
6. Audit discussions — context only, already distilled into findings below.

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

## FD-06 — Exercise Delivery Contract adopted

Status: accepted (2026-07-22)
Priority: P1
Area: Delivery / product touchpoint

The daily exercise notification is the single primary product touchpoint.
Its product contract is `docs/audit/delivery_contract.md`, adopted here as
an authoritative source of truth (see Source of truth #3). The delivery
findings (DEL-01…DEL-08) check the code against it.

Key accepted points (full text in the contract):

* channel-neutral shape `title · duration · ordered steps · Done/Skip`;
  first line self-sufficient for the push preview;
* no rationale in the message — **not before and not after** (removed
  100%); no counters, mechanic/slot/internal IDs, persona copy, or a Coach
  message right after delivery;
* on tap, **edit the same message** (not a new one); buttons change with
  state; after `Виконано` show the optional completed-only feedback tap;
  after `Пропустити` show no feedback;
* Mini App / rich cards deferred (post-beta, selective, only where
  interaction makes the action *easier*);
* delivery timing / anchor is a reasoned MVP stance + open beta learning
  goal — the onboarding time question is purpose-framed and scoped to the
  workday, the time is a likely-good moment not a deadline, company anchor
  is recommended-only, and no delivery time/content adapts automatically
  in MVP.

---

## FD-07 — Store three explicit feedback signals in one database table

Status: accepted (2026-07-23)
Priority: P1 for beta learning
Area: Feedback / Coach / Delivery / Telemetry

### Decision

The beta collects three different feedback signals. They answer different
product questions but share one storage model:

1. `exercise_efficacy` — optional one-tap feedback after an exercise was
   marked completed (already accepted under FD-05/FD-06);
2. `coach_quality` — thumbs up/down attached to free-form Coach responses,
   with a short reason after thumbs down;
3. `product_feedback` — feedback the user explicitly asks to submit about
   Love Yourself, captured through natural conversation and a dedicated
   `capture_product_feedback` runtime tool.

All three are stored in one `feedback_events` table with a `source` field.
The database is the source of truth. Beta scope is capture only: no email
notification, support inbox, admin Telegram channel, dashboard, automatic
triage workflow, or response-time promise is required.

### Explicit-intent boundary

`capture_product_feedback` is available only when the user clearly asks to
submit or pass feedback to the product/development team. A direct request that
already contains the feedback is sufficient consent.

Examples that allow capture:

```text
Хочу залишити відгук про продукт: ...
Передай розробникам, що ...
Запиши це як фідбек: ...
```

A complaint, question, frustration, or negative phrase alone is not consent
to store product feedback. For example, "я не розумію, коли приходять вправи"
is first a product-support question: Coach answers it from the Product Map.
It is captured only if the user also clearly asks for it to be submitted.

Do not require the feedback topic to be absent from the Product Map. The map
determines whether Coach can answer a factual question; it does not restrict
what the user may criticize or submit as feedback.

If the user expresses intent to leave feedback but has not said what should be
submitted, ask one brief question. Do not infer feedback text or submit ambient
conversation automatically.

### Classification and fidelity

Classification happens only after explicit submission intent is established.
It may sort feedback into a small enum such as `bug`, `confusion`,
`feature_request`, `content`, `coach`, or `other`, but classification never
acts as the trigger.

Store the exact source message and any model-extracted submitted text
separately. The original user wording is authoritative; a category or summary
must not replace it. Do not attach the full conversation by default.

`product_feedback` is not COACH-10 escalation:

* feedback records an opinion and does not promise a human response;
* escalation asks a human to resolve an unanswered product question and may
  require a response path;
* they may share infrastructure later, but their user-facing contracts remain
  distinct.

### Storage shape

Minimum shared fields:

```text
id
user_id
source                     # exercise_efficacy / coach_quality / product_feedback
source_entity_id           # exercise, Coach message, or source Telegram message
value                      # source-specific rating/answer
reason                     # optional structured reason
feedback_text              # optional submitted text
category                   # optional post-intent classification
context                    # bounded JSON: plan/exercise/prompt/model/state identifiers
created_at
```

Do not make feedback company-facing. Company reporting remains anonymized and
aggregated under the existing privacy contract. Feedback does not change the
current or future exercise sequence for the individual user.

### Source-specific interaction rules

* `exercise_efficacy`: show only after `completed`, never after `skipped`;
  one optional tap, no plan adaptation.
* `coach_quality`: attach only to free-form Coach responses, not scheduled
  exercises or deterministic tool confirmations. A negative rating may expose
  a compact reason row (`incorrect`, `irrelevant`, `tone`, `too_long`,
  `other`). One user/message pair must be idempotent and may update its latest
  rating rather than create duplicates.
* `product_feedback`: no slash command is required. Natural-language explicit
  intent invokes the tool. Confirm storage only after the database write
  succeeds; do not promise that a person will reply.

### Deferred deliberately

* outbound email to `hello@loveyourselfua.com`;
* admin/support Telegram channel;
* feedback dashboard or issue-tracker integration;
* automatic alerts and triage;
* automatic prompt, exercise, timing, or per-user plan changes based on
  feedback.

Feedback is reviewed in batches during product iteration. If volume later
makes manual database review a bottleneck, notifications or a review surface
can be added without changing the capture contract.

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

Collect the one user-confirmed delivery time using the purpose-framed,
workday-scoped onboarding question defined in `delivery_contract.md`
(FD-06) — not a generic "when are you free" prompt.

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

Severity: P2 (consistency cleanup)
Status: founder decision made (2026-07-22) — no hard range; reconcile legacy claims

Current code contains conflicting behavior:

* onboarding has no time recommendation or validation because it is a mock;
* generic HH:MM parsing accepts every valid clock time from `00:00` to `23:59`;
* `change_day_time` therefore accepts a value such as `03:00`;
* legacy schedule-adjustment UI offers DAY options from `12:00` to `17:59`;
* schedule-adjustment text claims `06:00–23:59`, while its actual slot inference rejects times before `12:00`.

The current product contract requires one daytime time but does not define one authoritative numeric recommendation and hard allowed range.

Minimal fix:

Do not implement a range by inference. Use one shared validator for onboarding, change-time tools, API, and scheduling — but the founder decision below sets what that validator enforces.

Founder decision (2026-07-22):

**No artificial hard range for MVP.** Allowed DAY time = any valid clock
time up to `23:59`; the end-of-local-day expiry window already exists and
handles late choices. Rationale: with 0 users this is a hypothesis, not a
constraint to guess — give people freedom and let aggregate data reveal
which hours actually work (good times survive, bad timing fades). Steering
toward the productive window is done **softly**, by the purpose-framed
onboarding question (FD-06 / `delivery_contract.md`), not by a hard numeric
gate — the two would be redundant, and the soft version preserves freedom
while still keeping most users out of the leisure-time slot. Remaining
cleanup is consistency only: remove the contradictory legacy range claims
(`12:00–17:59` UI vs `06:00–23:59` text vs `<12:00` rejection) so one
shared validator agrees with "any time, end-of-day window."

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
* reconcile the contradictory legacy time-range claims (`12:00–17:59` UI vs
  `06:00–23:59` text vs `<12:00` rejection) into **one shared validator**
  that enforces the ONB-04 founder decision: any valid time up to `23:59`,
  no artificial hard range (soft steer via the onboarding question only);
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
* add onboarding tests for state transition, persistence, forbidden questions, time validation (any time up to `23:59` accepted; one shared validator), first-plan creation, and confirmation copy.

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
path is dormant today, but adding one later would violate FD-06: **rationale
is not rendered before or after the exercise in MVP** (removed 100%).

Minimal fix: render only trusted display content in the exercise body and
keep internal slot, mechanic, task index, and rationale out of the message
entirely (per FD-06 / `delivery_contract.md`).

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

Expected MVP behavior (per FD-06 / `delivery_contract.md`): persist the
canonical result, then **edit the same delivered message** — do not send a
new one. Specifically:

* remove the Done/Skip buttons;
* show a durable in-message status `Виконано` / `Пропущено`;
* after `Виконано`, show the optional completed-only feedback tap
  (`Допомогло?`);
* after `Пропустити`, show **no** feedback control;
* do **not** send any additional engagement/adaptation message, and do not
  activate the random catalog or an adaptation CTA.

The exact acknowledgement copy can be polished later; disabling unsupported
behavior does not need a new product decision.

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
* assert that internal IDs/slots/mechanics and rationale are absent
  (before AND after the action, per FD-06);
* assert the two callback IDs and ownership/idempotency behavior;
* assert on-tap **edit-in-place** of the same message (no new message) with
  the correct keyboard replacement and durable status;
* assert the feedback tap appears **only** after `Виконано`, never after
  `Пропустити`;
* cover active, paused, canceled, completed, skipped, and expired button
  states, including that `expired`/`canceled` remove the keyboard and
  `paused` leaves an already-delivered exercise actionable;
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
* implement on-tap **same-message state transition** (edit, not new
  message) with changing buttons and durable status (DEL-03, FD-06);
* implement the **completed-only feedback callback** and persist its result
  (`exercise_id`, answer, timestamp); wired to storage only (FD-05/FD-06);
* align pause/cancel keyboard behavior with the Product Map (DEL-04);
* add bounded delivery retry/idempotency (DEL-05);
* replace stale callback tests and add end-to-end renderer coverage
  (DEL-06).

The full delivery shape and copy rules are defined in
`docs/audit/delivery_contract.md` (FD-06); this list is the code work to
satisfy it.

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

# Runtime Tools Findings

## Runtime Tools Area — Audit Round 2026-07-22, full-matrix revalidation 2026-07-23

Status: completed; findings recorded only, no runtime fixes applied.
**First pass was too narrow** — it only compliance-checked the evening-time
rule the founder flagged, not the area. The revalidation takes the whole
tool matrix (Coach state→tools + prompt rules) and checks every tool through
schema, registration, backend guard, side effects, result handling, and tests.

Method: for each of the 8 Coach-callable runtime tools — what states/rules does the Coach
side declare, what does the backend actually enforce, do the side effects
fulfil the declared result, and can the user-facing reply be trusted?

### Files inspected

* `app/plan_runtime/tools.py` — all 8 Coach-callable runtime functions
* `app/orchestrator.py` — tool registry, `_execute_plan_tool`, result/error
  formatting, context builder, and evening cascade
* `app/workers/coach_agent.py` — prompt contract, schemas, state→tools matrix,
  `_coach_tools_for_state`, and API call
* `app/time_slots.py` — validation and future-step recomputation
* `app/plan_drafts/service.py` — first/follow-up plan invariants
* `app/plan_finalization.py` — activation and scheduling side effects
* `app/plan_pause.py`, `app/plan_guards.py`, and `app/scheduler.py`
* `app/session_memory.py` — pending 14-day action
* runtime-tool, Coach schema/filter, orchestrator, pause/resume, and status tests
* `conceptual_map.md`, `conceptual_map_en.md`, and the Coach system prompt

### Summary — the root problem

The outer command boundary is materially better than it was before the Coach
rewrite:

* only allowlisted tools are registered;
* tools are filtered before the API call by `current_state`;
* schemas are strict, reject extra properties, and require tool arguments;
* unknown DB fields are not dumped directly to the user;
* pause, resume, and cancel have backend state checks.

But the full path still has four systemic gaps:

1. tool gating is state-based while several preconditions are plan-type- or
   pending-flow-based, and prompt prose is the only bridge;
2. some tools commit product state before external scheduler side effects,
   then return success even when those side effects failed or never happened;
3. the Coach never receives the tool result, so hardcoded Ukrainian templates
   become a second, drifting source of user-facing truth;
4. Product Map promises one atomic 7↔14 format switch, but no such tool exists.

### Per-tool matrix (Coach declaration vs backend)

| Tool | Coach offers in states | Backend guard | Aligned? |
|---|---|---|---|
| `create_followup_plan` | `IDLE_PLAN_ABORTED` only | FINISHED, DROPPED, ABORTED + silent defaults | **mismatch** → RT-06 |
| `record_evening_time` | `IDLE_PLAN_ABORTED` plus pending 14-day flow | no state/pending guard; format-only time validation | **mismatch** → RT-02 / RT-10 |
| `change_day_time` | ACTIVE, ACTIVE_PAUSED, ABORTED | no state guard | partial/paused false-result risk → RT-08 / RT-12 |
| `change_evening_time` | ACTIVE, ACTIVE_PAUSED, ABORTED plus configured evening | no state/plan-format guard | false success for 7-day → RT-01 / RT-12 |
| `pause_plan` | ACTIVE | `ACTIVE` | state aligns; promised preservation does not → RT-13 |
| `resume_plan` | ACTIVE_PAUSED | `ACTIVE_PAUSED` + profile flag | state aligns; “next remaining day” does not → RT-13 |
| `cancel_plan` | ACTIVE, ACTIVE_PAUSED + extra consent | state only | consent is model-governed; cleanup incomplete → RT-13 |
| `get_plan_status` | ACTIVE, ACTIVE_PAUSED, ABORTED | any state (read-only) | mostly aligned; final-day facts can be wrong → RT-14 |
| plan switch 7↔14 | — | — | **does not exist** → RT-07 |

The prompt table and `_TOOL_NAMES_BY_STATE` match each other exactly. The
remaining mismatches are between that declaration and runtime truth, not
between the two copies of the Coach-side matrix.

### Reverse audit — Product Map promise to execution path

This pass also starts from the user-facing Product Map rather than the existing
tool list. A declared capability does not automatically require a Coach tool:
deterministic lifecycle operations, Telegram callbacks, and supplied runtime
context are separate execution mechanisms.

| Product Map capability | Correct execution owner | Current status | Missing Coach tool? |
|---|---|---|---|
| Create the first 7 working days after setup | deterministic onboarding completion | backend flow missing/incomplete; ONB-01/ONB-07 | No |
| Automatically continue 7→7 or 14→14 after completion | deterministic completion lifecycle | not implemented; LIF-04/FD-01 | No |
| Pause delivery | `pause_plan` | tool exists; preservation semantics incomplete (RT-13) | No |
| Resume with the next remaining day | `resume_plan` | tool exists; re-anchoring incomplete (RT-13) | No |
| Permanently cancel the current period | `cancel_plan` | tool exists; cleanup incomplete (RT-13/DEL-04) | No |
| Start a new 7- or 14-day period after cancellation | `create_followup_plan` | tool exists; guards/defaults drift (RT-06) | No |
| Change daytime or configured evening delivery time | `change_day_time` / `change_evening_time` | tools exist; plan/state/reconciliation gaps (RT-01/RT-03/RT-08/RT-12) | No |
| Switch 7↔14 as one confirmed action | atomic `switch_plan_format` | no schema, backend operation, registry entry, or test | **Yes — RT-07 / COACH-11** |
| Choose the first evening time when entering 14 days | `record_evening_time` plus pending creation/switch flow | tool exists; pending-flow guard incomplete (RT-02) | No |
| See current period status/progress | `get_plan_status`; completion report for closed periods | tool exists; final-day calculation gap (RT-14) | No |
| Mark the delivered exercise Done or Skip | Telegram callback buttons | implemented outside Coach tools | No |
| Treat no button response by local end of day as not completed | expiry/lifecycle job | deterministic delivery lifecycle, not a conversation action | No |
| Explain how to perform today's exercise | `current_exercise_context` supplied to Coach | context gap tracked under COACH-08 | No |
| Send the completion summary and prepare the next period | completion/report lifecycle | lifecycle gaps already tracked | No |
| Direct an unanswered factual question to support | deterministic support contact or escalation flow | no real path; COACH-10 backlog | Maybe later; not currently offered as an action |

Therefore the only missing Coach action required by the current Product Map is
`switch_plan_format`. `create_first_plan`, automatic continuation, completion,
Done/Skip, expiry, and exercise-context retrieval must not be converted into
LLM tools merely because they are missing or incomplete elsewhere.

The Product Map says the user chooses working days during setup, but does not
promise that working days can be changed during an active period. Likewise it
does not promise user-controlled timezone changes, one-off snoozing, replaying
an exercise, marking Done/Skip through free text, or retrieving arbitrary
historical reports through the Coach. Possible future tools such as
`change_work_days`, `change_timezone`, `snooze_current_exercise`,
`record_exercise_result`, or `get_latest_completion_summary` would expand the
product contract rather than close a current implementation gap.

Of those candidates, `change_work_days` is the strongest post-MVP possibility:
Fogg's Ability/Routine-fit lens says a changed work schedule can make every
prompt mistimed. But current users can skip, pause, or change time, and there is
no usage evidence yet that runtime work-day editing is a bottleneck. Keep it as
a decision/data question rather than adding another scheduling mutation before
the core loop is reliable.

The Fogg working paper is an analytical lens, not the MVP source of truth. Its
recommendations to let users choose exercises, generate tiny variants, or adapt
content after behavior conflict with the approved Product Map and the frozen
adaptation decision. They do not justify additional runtime tools in this
round.

### Consent boundary

Natural-language intent and consent are interpreted by the Coach. The backend
can validate tool name, state, arguments, and product invariants, but it does
not independently know whether a sentence was genuine confirmation.
Consequently, cancellation's additional confirmation requirement is enforced
by prompt/model behavior, not a durable confirmation token or callback.

This is not automatically a separate architecture bug for an AI-first MVP,
but it is a real control boundary. It requires scenario/evaluation coverage.
If deterministic proof of irreversible consent is later required, cancellation
needs a confirmation button/token; adding another prose rule will not create
backend enforcement.

### Findings

#### RT-01 — `change_evening_time` gives a false success for 7-day users

Severity: P1
Status: confirmed

`change_evening_time` (`tools.py:222-247`) calls `update_user_time_slots`
with `{"EVENING": hhmm}` and reschedules future steps. It never checks
`evening_slot_collected` or whether the active plan is MEDIUM. For a SHORT
plan there are no evening steps, so `update_user_time_slots`
(`time_slots.py:247`, merge semantics) simply stores an EVENING value the
plan never uses and `reschedule_plan_steps` gets an empty list →
`rescheduled=0` — yet the function returns `{"status": "ok"}` and the
orchestrator replies **"✅ Вечірній час змінено"** (`orchestrator.py:1230`).
The user is told a time changed that affects nothing in their plan.

Fix (`tools.py`): guard at the top — load profile + active plan; if
`evening_slot_collected` is False, or the active plan is SHORT / has no
EVENING slot, return a soft `{"status": "no_evening_slot"}` and persist
nothing. Do not claim success.

#### RT-02 — `record_evening_time` sets the 14-day flag with no context guard

Severity: P2
Status: confirmed

`record_evening_time` (`tools.py:167-191`) persists EVENING and sets
`evening_slot_collected=True` for any caller in the gated state. The MEDIUM
auto-create only fires when `pending_action == collect_evening_time_for_medium`
(`orchestrator.py:1316`), but the persistence + flag set happen
unconditionally. If the tool mis-fires (LLM error) outside that flow, a
14-day-only flag is set for a user with no pending 14-day creation.

Fix: gate persistence to the pending-MEDIUM context — either the
orchestrator only routes `record_evening_time` when
`pending_action == collect_evening_time_for_medium`, or the tool no-ops
outside it. Lower severity than RT-01 (it stores a real user-provided
value), but it is a 14-day flag set outside the 14-day flow.

#### RT-03 — Tool gating is state-based; evening relevance is plan-type-based; only prompt prose bridges them

Severity: P1
Status: confirmed

The state→tools matrix (`coach_agent.py:652-654`) lists
`change_evening_time` under `ACTIVE` and `ACTIVE_PAUSED` unconditionally. A
user in `ACTIVE` may hold a SHORT **or** MEDIUM plan — the state does not
distinguish. The prompt notes (`coach_agent.py:658-660`) say
`change_evening_time` is "available only when an evening time is already
configured" and `record_evening_time` "only while a 14-day is pending" —
but `_coach_tools_for_state` (`coach_agent.py:962-970`) filters purely by
`current_state` and enforces neither. So the only thing stopping a 7-day
user from getting the evening tool is the LLM obeying a prompt note. Per
this audit's standing discipline, the prompt is not a guarantee.

Fix: make tool filtering plan-type-aware (needs RT-04). Drop
`change_evening_time` when the active plan is SHORT / no evening configured;
expose `record_evening_time` only when a MEDIUM creation is pending.

#### RT-04 — Coach context omits plan type, so the filter cannot be plan-type-aware

Severity: P1 (enabler for RT-03)
Status: confirmed

The context payload built in `orchestrator.py:1124-1130` carries
`current_state` but no `plan_type` / `total_days`. `_context_message`
(`coach_agent.py:738-748`) forwards only `current_time`, `current_state`,
`completion_context`. So neither the LLM nor `_coach_tools_for_state` has
the information needed to distinguish 7-day from 14-day.

Fix: add the active plan's `plan_type` (or `total_days`) and
`evening_slot_collected` to the payload at `orchestrator.py:1124`, thread
them through to `_coach_tools_for_state`, and use them in the filter (RT-03).

#### RT-05 — Dead / removed-from-Coach code inventory (delete, do not patch)

Severity: P1 (correctness of the removal plan)
Status: confirmed by grep + cross-referenced

Consolidated index of code that is **dead or already stripped from the
Coach, but still present** in the backend. Founder directive: these are to
be **removed**, not guarded/patched. Several existing findings advise
patching guards on code that is actually being deleted — that framing is
wrong and is corrected here.

**1. `create_first_plan` — dead wrapper, no callers.**
`grep` confirms zero callers anywhere (`app/plan_runtime/tools.py`): the
only references are inside its own body (error string + log). It is not a
Coach tool. ONB-07, COACH-07, and FSM-01 all describe it as "update the
`IDLE_ONBOARDED` entry guard when that state is removed" — **wrong
disposition.** You do not patch the guard of a function you are deleting.
Correct disposition: **delete `create_first_plan`**. First-plan creation
(when the real onboarding, ONB-01, is built) calls `create_plan()` in
`plan_drafts/service.py` directly — it already enforces "first plan must be
SHORT" (`service.py:51`) and `fsm/guards.py:7` already names `create_plan()`
as the transition mechanism. No wrapper needed.

**2. `IDLE_ONBOARDED` — dead state.** Never assigned in production (grep
for assignment finds none; only comparisons in the dead `create_first_plan`
guard and the FSM transition table). Its only "consumer" is the dead
wrapper above. Remove from state definitions, guards, `ONBOARDABLE_STATES`,
`IDLE_STATES`, and transition tables. Tracked as FSM-01 / ONB-07.

**3. `IDLE_DROPPED` — dead state, orphan writer.** The only writer
(`orchestrator.py:767`) has no live production caller (FSM-02); explicit
abandonment uses `IDLE_PLAN_ABORTED`. Remove the orphan writer and all
`IDLE_DROPPED` allowances; migrate any existing rows. Tracked as FSM-02.
(This corrects RT-06's earlier "add IDLE_DROPPED to the Coach matrix"
direction — the state is removed, not wired.)

**4. `SCHEDULE_ADJUSTMENT` — zombie subsystem.** The state is **never
entered** (grep for assignment finds none), yet a full machinery references
it across 8 files: a `stuck_schedule_adj_check` cron job that scans for
users in it (`scheduler.py:829`), Telegram `sched_adj_timeout` callbacks,
session-memory `schedule_adjustment_context`, orchestrator routing
(`:1381`), FSM transitions, and constants. Remove the whole subsystem, not
just the constant. Tracked as FSM-03 (and SCH-02).

**5. `MORNING` slot — frozen, unused, still carried.** Not produced by any
P1 plan (recipe is DAY/EVENING only; `schemas/planner.py:51` marks it
"frozen"), yet it persists as defaults and branches across ~7 files
(`db.py:157`, `plan_finalization.py:92`, `api.py:57/83/89`, `time_slots.py`,
`task_notification.py:9`). **Not in Codex's inventory — added here.** Lower
priority than 1–4 (it is inert, not wired to a live job), but it is a latent
user-facing leak risk (a `MORNING`/`Ранок` label could surface) and dead
weight. Decide: remove, or keep explicitly frozen with a single guarded
definition rather than scattered branches.

Distinction to keep: items 1–5 are **dead** (remove). This is different
from *orphaned-but-reachable* gaps (e.g. a live state with no Coach path) —
those are behavior gaps, not dead code, and are tracked in their own
findings.

Fix: one removal pass that deletes 1–5 together with a forward migration
for any persisted `IDLE_DROPPED` / `SCHEDULE_ADJUSTMENT` rows, and updates
ONB-07 / COACH-07 / FSM-01 wording from "patch the guard" to "remove the
wrapper."

#### RT-06 — `create_followup_plan` has stale states and fail-open defaults

Severity: P1
Status: confirmed; cross-reference COACH-03 / FSM-02

Backend `_FOLLOWUP_STATES` = {`IDLE_FINISHED`, `IDLE_DROPPED`,
`IDLE_PLAN_ABORTED`} (`tools.py:25`). But `_TOOL_NAMES_BY_STATE`
(`coach_agent.py:952`) offers `create_followup_plan` **only** in
`IDLE_PLAN_ABORTED`. Under the accepted target, the Coach matrix is the
correct direction: `IDLE_FINISHED` auto-continues through FD-01, and
`IDLE_DROPPED` is removed. Adding those rows back to the Coach would restore
the old architecture rather than reconcile it.

There are also two fail-open defaults:

* `_build_tool_registry()` uses `args.get("plan_type", "SHORT")`, silently
  creating 7 days if the required model argument is missing;
* `create_followup_plan()` uses `"14:00"` when the saved DAY time is absent,
  silently inventing a schedule instead of reporting broken setup.

Both contradict the prompt contract: the user chooses the specific format,
and saved setup is the source of truth.

Fix:

* narrow `_FOLLOWUP_STATES` to `IDLE_PLAN_ABORTED`;
* keep automatic same-format continuation as a separate deterministic
  lifecycle service, not a Coach call from `IDLE_FINISHED`;
* remove `IDLE_DROPPED` with FSM-02;
* require `args["plan_type"]` and fail closed when the saved DAY time is
  missing; do not default either decision.

#### RT-07 — No plan-switch (7↔14) capability exists

Severity: P1 before beta under the current Product Map
Status: confirmed; accepted target dependency, not implemented

The English and Ukrainian Product Maps explicitly tell the Coach that format
can be switched from 7 to 14 or 14 to 7 as **one user-confirmed action**.
`product_contract.md` also lists `switch_plan_format` among the known backend
dependencies that must close before beta. No schema, registry entry, backend
function, state row, or test exists.

The only current approximation is `cancel_plan` followed by
`create_followup_plan(other type)`. That is not equivalent:

* it requires two actions and extra friction;
* cancellation permanently ends the current sequence and suppresses its
  progress summary;
* a failure between the two actions leaves the user with no active sequence;
* first switch to 14 days needs an evening-time subflow.

This is not a quick wrapper around two existing calls. The switch must be
atomic at the data level: validate all inputs first, end the old sequence and
create the new format in one transaction, roll back to the old sequence if
creation fails, then reconcile scheduler jobs idempotently. For the first
7→14 switch, collect evening time before ending the current sequence.

Current contract gives two honest options: implement the atomic tool before
beta, or explicitly remove the capability from Product Map and the Product
Contract for MVP. Leaving the claim while calling it optional is not coherent.

#### RT-08 — `change_day_time` / `change_evening_time` have no backend state guard

Severity: P2
Status: confirmed

Neither `change_day_time` (`tools.py:194`) nor `change_evening_time`
(`tools.py:222`) checks `current_state` — they validate HH:MM and update.
All state gating for them lives in the Coach matrix. So any caller path
that bypasses the matrix (a direct call, a future entry point, an LLM
misfire the matrix didn't catch) can change delivery times in any state.
Contrast pause/resume/cancel, which all guard `current_state` in the
backend. The evening guard from RT-01 partly covers `change_evening_time`;
`change_day_time` should also assert a sane state (has a plan / follow-up
context) rather than trusting the matrix alone.

#### RT-09 — The Coach does not voice tool outcomes; a fixed template does

Severity: P1
Status: confirmed (cross-ref COACH-09)

The prompt (`coach_agent.py:669-674`) instructs the Coach to return **only**
the tool call, no user-facing text, and not to claim success. The
orchestrator then sends a fixed `_TOOL_REPLY_TEMPLATES` string
(`orchestrator.py:1226-1235`). So the Coach never sees or interprets the
actual result — the user hears a canned line. Consequences directly
relevant to this round:

* soft/failure results have no natural voice. Today only two are handled
  explicitly (`needs_evening_time`, the MEDIUM cascade); the RT-01
  `no_evening_slot` result proposed above would have **no** template and
  fall through unless the orchestrator is extended case-by-case.
* the founder's concern is the inverse risk once the Coach *is* allowed to
  respond: the backend returns one thing and the Coach, not seeing it,
  narrates something else ("схоже, система заблокувала…") and invents
  detail. The Coach is the system's face; it must speak from the real
  result, not around it.

Fix (COACH-09): a bounded tool-result loop — the tool result is returned to
the Coach, which forms one natural response grounded in the actual
outcome (success, soft, or failure), instead of the orchestrator guessing a
template per tool. This is the same Bounded Tool-Result Loop already
recorded as COACH-09; this round confirms the runtime tools depend on it,
especially once soft results (RT-01) exist.

The current templates also bypass Section 3 language adherence: every success,
soft result, and error is Ukrainian even when the user speaks English. Several
templates restore old user-facing `план` terminology, and the evening-time
question requires manual `20:30` input despite the prompt allowing natural
language. These are additional symptoms of the same missing result loop, not
reasons to grow a larger template catalog.

#### RT-10 — backend time validation accepts impossible values

Severity: P1
Status: confirmed and directly reproduced

`_validate_hhmm()` checks only `^\d{2}:\d{2}$`. Direct execution accepted all
of `24:00`, `99:99`, and `12:60`. The strict OpenAI schema correctly prevents
those values on a compliant model call, and `change_day_time` /
`change_evening_time` later pass through `time_slots._parse_time()`. But
`record_evening_time` writes the value directly after `_validate_hhmm()`, so
an invalid time can be persisted as trusted profile setup.

Expected behavior: one backend validator is authoritative for every time
entry point and enforces real 24-hour time (`00:00` through `23:59`). The
schema remains defense-in-depth, not the only semantic validation.

This validator does not choose or convert a timezone. The accepted `HH:MM`
is a local wall-clock value in the user's saved `user.timezone`.
`change_day_time` / `change_evening_time` already pass that timezone into
`compute_scheduled_for()`, which converts the result to UTC; first MEDIUM-plan
finalization does the same after `record_evening_time`. Correct collection and
validation of `user.timezone` is a separate setup contract.

Minimal fix: replace the format-only regex helper with parsing that returns a
normalized `HH:MM`, and use it in all three time tools and onboarding. Add
boundary tests for `00:00`, `23:59`, `24:00`, invalid minutes, and malformed
input.

#### RT-11 — plan creation can report success after activation side effects failed

Severity: P1
Status: confirmed

Both creation tools commit the finalized plan and `ACTIVE` state, then call
`activate_plan_side_effects()` outside that transaction. That function catches
every exception, logs it, and returns `None`; it also returns normally when the
plan/user cannot be loaded. The caller therefore always returns
`{"status": "ok"}` and the orchestrator says the new sequence was launched,
even if no exercise jobs were scheduled.

The failure can also be partial: scheduling earlier steps mutates APScheduler,
then a later exception is swallowed. The DB may say active while only part of
the sequence has jobs.

Minimal fix: make activation an idempotent, observable operation. At minimum,
return/raise a structured activation result and never claim full success when
job activation failed. The durable target should use an outbox/reconciliation
record so a committed plan can retry job activation without duplicate jobs.
Do not attempt to make a DB transaction roll back external scheduler changes.

Founder directive (2026-07-23): the silent fail-open is **not acceptable** —
this is deterministic backend code, it must not swallow a failure and report
success. Required: either (a) a bounded retry/reconciliation policy that
guarantees the jobs eventually exist, or (b) fail **closed** — surface the
failure so the plan is not reported as launched. Do not ship the current
"looks OK everywhere, user gets nothing" path. Success must mean the delivery
jobs actually exist.

#### RT-12 — time changes commit before scheduler reconciliation and fail while paused

Severity: P1
Status: confirmed; related to COACH-04 / FSM-06

`change_day_time` and `change_evening_time` first commit the profile and
rewritten `scheduled_for` timestamps, then call `reschedule_plan_steps()` in a
separate session. If rescheduling raises, the orchestrator reports failure
although the saved time and DB schedule already changed.

For `ACTIVE_PAUSED`, the prompt explicitly offers both time tools, but
`schedule_plan_step()` refuses every step because `can_deliver_tasks()` accepts
only `ACTIVE`. The tool therefore commits the new times, schedules zero jobs,
and returns `status=ok`. Resume only flips flags/state and does not rebuild the
jobs. The promised result — future delivery at the new time after resume — is
not implemented.

Minimal fix: treat schedule reconciliation as an idempotent post-commit result,
not an assumed side effect. For paused users, persist the new time but defer job
creation to the required resume re-anchoring flow. Return structured facts such
as `saved=true`, `jobs_reconciled=true/false/deferred`, and let the bounded
result loop describe only what actually happened.

#### RT-13 — state guards align for pause/resume/cancel, but their declared effects do not

Severity: P1
Status: confirmed; cross-reference COACH-04, DEL-04, and FSM-06

The backend correctly rejects pause outside `ACTIVE`, resume outside
`ACTIVE_PAUSED`, and cancel outside those two states. That part matches the
prompt. The promised consequences do not:

* pause says the remaining sequence is preserved, but due steps fire and are
  lost while delivery is gated;
* resume says delivery continues with the next remaining day, but it only
  flips `is_paused` and `current_state`; it does not re-anchor or reschedule;
* resume does not verify that an active/paused `AIPlan` actually exists;
* cancel changes plan/user state and removes future scheduler jobs, but does
  not close delivered/pending step state or remove visible Telegram keyboards
  (DEL-04).

Cancellation's additional verbal confirmation is also model-governed as noted
under Consent boundary; the backend validates state, not conversation history.

Minimal fix: do not create another set of tool-specific work items here.
Implement the accepted pause/resume re-anchoring once (COACH-04 / FSM-06), the
cancelled-step/keyboard closure once (DEL-04), and add integration tests through
the runtime tools so their advertised results become true.

#### RT-14 — `get_plan_status` can report one day remaining after the last day was delivered

Severity: P1 factual correctness
Status: confirmed

`current_day` advances when all steps for a day are delivered, not when they
are completed. On the final day, `maybe_advance_current_day()` caps the value at
`total_days`; it cannot advance to `total_days + 1`. `get_plan_status()` then
calculates:

```text
days_remaining = total_days - current_day + 1
```

Therefore, after every final-day exercise has already been delivered,
`current_day == total_days` and the tool still reports `1` day remaining until
the plan moves out of the active state. The existing test hardcodes a middle
day and does not cover this boundary.

Minimal fix: define status facts from actual scheduled days/remaining
non-terminal or not-yet-delivered steps rather than inferring all semantics
from a capped cursor. Add first-day, middle-day, final-day-before-delivery,
final-day-after-delivery, paused, and no-plan cases. Keep the structured result;
the bounded Coach result loop should verbalize it.

#### RT-15 — tests prove wiring, not the declared product outcomes

Severity: P1 test expansion
Status: confirmed

The targeted current suite passed (`44 passed` with unavailable Trio cases
excluded), but it primarily mocks each function's collaborators and asserts
that they were called. It does not cover:

* impossible but regex-valid times;
* missing `plan_type` silently becoming SHORT;
* missing DAY time silently becoming `14:00`;
* activation-side-effect failure after plan commit;
* time-change reconciliation failure or paused time changes;
* pending-action absence/expiry around first evening collection;
* actual `_execute_plan_tool()` success/soft/error/cascade behavior;
* language adherence after a tool call;
* final-day status facts;
* cancellation consent scenarios or cleanup through the runtime boundary;
* the absent format-switch contract.

The green suite is useful regression coverage for current wiring, but it is not
evidence that the prompt/tool contract is fulfilled. Add outcome-level tests
alongside the fixes rather than enlarging mocks of the current behavior.

### Recommended fix shape

Priority order:

1. **Close false-success paths (RT-11/RT-12/RT-13).** A created or changed
   sequence must have observable, retryable scheduler reconciliation; pause,
   resume, and cancellation must fulfil their existing Product Map contract.
2. **Align follow-up entry points (RT-06).** Automatic continuation is not a
   Coach tool; user-requested follow-up creation is only from
   `IDLE_PLAN_ABORTED`; remove stale states and silent defaults.
3. **Add authoritative input/context guards (RT-01/RT-02/RT-08/RT-10).**
   Invalid time, wrong plan format, wrong state, or absent pending flow must
   fail before persistence.
4. **Make filtering plan-aware (RT-03/RT-04).** The Coach should not receive
   tools that cannot be meaningful for the current plan/context.
5. **Implement the bounded tool-result loop (RT-09/COACH-09).** The Coach
   receives one safe structured result and writes one grounded reply; templates
   remain failure fallback only.
6. **Resolve the accepted format-switch contract (RT-07).** Implement the
   atomic tool before beta, or explicitly remove the promise from Product Map
   and Product Contract.
7. **Correct status facts and outcome tests (RT-14/RT-15).**

### Per-file work list

* `app/plan_runtime/tools.py`
  * `change_evening_time`: add the no-evening-slot guard (RT-01);
  * `record_evening_time`: gate persistence to the pending-MEDIUM flow (RT-02);
  * use one semantic time parser for record/change operations (RT-10);
  * add backend state/context guards to both time-change tools (RT-08);
  * narrow follow-up creation to `IDLE_PLAN_ABORTED`; require explicit
    `plan_type` and saved DAY time (RT-06);
  * return structured, factual results for activation and scheduler
    reconciliation instead of unconditional `status=ok` (RT-11/RT-12);
  * implement atomic `switch_plan_format(plan_type)` or remove its product
    promise before beta (RT-07);
  * derive status fields from real remaining/delivered steps (RT-14).
* `app/orchestrator.py`
  * `_execute_plan_tool` / cascade: handle the new `no_evening_slot` soft
    result; refuse `record_evening_time` unless
    `pending_action == collect_evening_time_for_medium` (RT-02);
  * remove the `SHORT` default in the `create_followup_plan` registry wrapper
    (RT-06 / COACH-03);
  * context builder (`:1124-1130`): add `plan_type`/`total_days` +
    `evening_slot_collected` (RT-04);
  * `_humanize_tool_error`: map the new guard errors to friendly copy;
  * tool-result handling: replace per-tool fixed templates with a bounded
    result loop so the Coach voices the real outcome in the user's language
    (RT-09/COACH-09);
  * emit telemetry for requested / soft-pending / succeeded / failed as
    distinct outcomes, not one `plan_tool_executed` event.
* `app/workers/coach_agent.py`
  * `_context_message`: forward `plan_type` / evening-configured (RT-04);
  * `_coach_tools_for_state`: filter evening tools by plan type + pending
    state (RT-03);
  * keep `IDLE_FINISHED` and removed `IDLE_DROPPED` out of the Coach tool
    matrix; prove the deterministic auto-continuation path separately (RT-06);
  * add `switch_plan_format` schema/state availability only when its backend
    atomic operation exists (RT-07);
  * on the second bounded call, disable tools and ground the answer solely in
    the safe structured result (RT-09).
* `app/plan_finalization.py` / scheduler integration:
  * make activation and rescheduling idempotent and observable; do not swallow
    activation failure while callers report success (RT-11/RT-12);
  * implement the existing pause/resume and cancel lifecycle fixes once, then
    exercise them through tool-level integration tests (RT-13).
* tests: implement the outcome coverage enumerated in RT-15, including
  invalid semantic time, plan-creation activation failure, paused time change,
  final-day status, and the full tool-result loop.

### Scope boundary

This round audits all 8 Coach-callable runtime functions and the missing
format-switch capability against their declarations. First-plan creation
belongs to the deterministic onboarding flow and is tracked separately under
ONB-07 / FSM-01. The FSM state model itself is covered by the FSM State &
Guard round below. The Bounded Tool-Result Loop architecture is owned by
COACH-09; RT-09 only confirms the runtime tools depend on it.

---

# FSM State & Guard Findings

## FSM State & Guard Area — Audit Round 2026-07-22, consistency pass 2026-07-23

Status: completed and revalidated; findings recorded only, no runtime fixes applied

### Files and paths inspected

* `app/fsm/states.py`
* `app/fsm/guards.py`
* `app/fsm/__init__.py`
* `app/db.py`
* `app/orchestrator.py`
* `app/plan_runtime/tools.py`
* `app/plan_pause.py`
* `app/plan_finalization.py`
* `app/scheduler.py`
* `app/telegram.py`
* `app/session_memory.py`
* FSM, schedule-adjustment, runtime-tool, onboarding, completion, and Coach tests
* FSM-related SQL migrations in `migrations/`

### Accepted target

Per `C_state`, FD-01, FD-04, and the accepted pause/cancel contract, the
target MVP lifecycle is:

```text
IDLE_NEW -> ONBOARDING:* -> ACTIVE
ACTIVE <-> ACTIVE_PAUSED
ACTIVE -> IDLE_FINISHED -> ACTIVE (automatic same-format continuation)
ACTIVE / ACTIVE_PAUSED -> IDLE_PLAN_ABORTED
IDLE_PLAN_ABORTED -> ACTIVE (explicit new sequence)
```

`IDLE_FINISHED` may be a short-lived technical state while completion and
automatic continuation are committed. `IDLE_ONBOARDED`, `IDLE_DROPPED`, and
`SCHEDULE_ADJUSTMENT` are not part of the target FSM. Plan format remains
plan data, not a separate FSM state.

### Summary

The three states accepted for removal are still represented across the ORM
constraint, guards, runtime branches, tool entry conditions, scheduler,
Telegram callbacks, Redis session memory, and tests. They are not equally
dead:

* `IDLE_ONBOARDED` is absent from the current onboarding path but remains a
  load-bearing precondition of `create_first_plan`;
* `IDLE_DROPPED` has an orphan writer with no production caller, but is still
  accepted by follow-up creation and transition guards;
* `SCHEDULE_ADJUSTMENT` is a zombie subsystem: the old entry dispatcher has
  no production caller, while callbacks, timeout recovery, session storage,
  and an active scheduler job remain.

The guard layer is also only partially authoritative: several core services
write `user.current_state` directly after local checks. More importantly, the
runtime does not have one enforced lifecycle aggregate. `User.current_state`,
`UserProfile.is_paused`, `AIPlan.status`, and duplicated end-date fields can
describe different realities for the same user. This has already allowed the
pause, delivery, cancellation, and completion contracts to drift apart.

### Findings

#### FSM-01 — `IDLE_ONBOARDED` is target-dead but still load-bearing

Severity: P1
Status: confirmed; cross-reference ONB-07 / COACH-07

Current behavior:

* a new Telegram user is created directly in `ONBOARDING:START`;
* the current mock onboarding branch does not persist a transition to
  `IDLE_ONBOARDED` or create a plan;
* `create_first_plan()` still rejects every state except
  `IDLE_ONBOARDED`;
* `states.py`, `guards.py`, the ORM constraint, and tests still treat the
  state as live.

This means the state is unreachable through the present onboarding
implementation, yet deleting it mechanically would remove the only backend
entry accepted for first-plan creation.

Expected behavior: deterministic onboarding saves the required setup and
creates the first SHORT sequence idempotently, ending directly in `ACTIVE`.

Minimal fix: implement the accepted onboarding-completion transaction first;
then remove `IDLE_ONBOARDED` from state definitions, guards, tool preconditions,
tests, and the DB constraint. Do not temporarily expose first-plan creation to
the Coach merely to preserve the legacy state.

#### FSM-02 — `IDLE_DROPPED` has no live entry path

Severity: P1 cleanup before beta
Status: confirmed

`_auto_drop_plan_for_new_flow()` is the only runtime writer of
`IDLE_DROPPED`, and it has no production or test caller. Explicit cancellation
uses `IDLE_PLAN_ABORTED`. Nevertheless, `IDLE_DROPPED` remains in
`PLAN_CREATION_ENTRY_STATES`, `_FOLLOWUP_STATES`, end-state guards, the ORM
constraint, migrations, and FSM tests.

Expected behavior: one user-visible stopped-sequence state,
`IDLE_PLAN_ABORTED`, covers the post-cancellation return path. There is no
background-drop behavior in the accepted MVP.

Minimal fix: remove the orphan helper and all `IDLE_DROPPED` allowances. In a
forward migration, map any existing `IDLE_DROPPED` rows to
`IDLE_PLAN_ABORTED` before replacing the constraint. Log the affected row
count; do not silently discard plan records.

#### FSM-03 — `SCHEDULE_ADJUSTMENT` is a zombie subsystem, not one dead constant

Severity: P1
Status: confirmed; expands SCH-02

The old `run_plan_tool_call()` dispatcher has no production caller, so the
normal target flow cannot enter `SCHEDULE_ADJUSTMENT`. Time changes now use
the direct `change_day_time` and `change_evening_time` runtime tools.

However, the legacy subsystem still includes:

* transition constants and guard branches;
* four orchestrator handlers plus old keyboard builders;
* Telegram `sched_task:*`, `sched_time:*`, and timeout callbacks;
* Redis context, last-active, and soft-prompt keys/methods;
* the active `stuck_schedule_adj_check` scheduler job and hard-reset path;
* two dedicated test modules.

The DB definition is internally inconsistent as well: `app/db.py` permits
`SCHEDULE_ADJUSTMENT`, but no checked SQL migration adds it. Its availability
therefore depends on whether a database was created from ORM metadata or
evolved through migrations.

Expected behavior: time changes are atomic deterministic tool operations and
do not change FSM state.

Minimal fix, in safe order:

1. remove any persisted `stuck_schedule_adj_check` job and disable its
   registration;
2. inspect/reset any existing `SCHEDULE_ADJUSTMENT` rows to their recorded
   pre-tunnel `ACTIVE` or `ACTIVE_PAUSED` state before dropping the state;
3. remove the legacy dispatcher, handlers, callbacks, Redis methods, constants,
   and tests;
4. keep and test the direct time-change tools instead.

Do not add a migration that makes the obsolete tunnel valid merely to repair
the current ORM/migration mismatch.

#### FSM-04 — `guards.py` is not the authoritative transition boundary

Severity: P1
Status: confirmed

`can_transition()` is enforced by `_commit_fsm_transition()` for the generic
`transition_signal` path and the legacy schedule-adjustment tunnel. Core
runtime paths instead assign `user.current_state` directly:

* first/follow-up creation and plan finalization -> `ACTIVE`;
* pause/resume -> `ACTIVE_PAUSED` / `ACTIVE`;
* cancellation -> `IDLE_PLAN_ABORTED`;
* completion -> `IDLE_FINISHED`;
* the orphan drop helper -> `IDLE_DROPPED`.

Those services have some local preconditions, but a transition can therefore
exist in code even if it is absent from `guards.py`. The file is a partial
validator, despite comments and the product contract treating the FSM guard as
the transition authority.

Expected behavior: deterministic services own the action, but all persisted
state changes pass through one shared transition boundary with the accepted
matrix and consistent logging.

Minimal fix: after the three legacy states are removed, introduce or reuse one
small transition helper for runtime services and replace direct assignments
incrementally. Do not build a new agent or a generic workflow engine. Keep
tool-specific business preconditions in the tools; centralize only state
validation, persistence semantics, and transition telemetry.

#### FSM-05 — prefixed onboarding states are valid in one validator and invalid in another

Severity: P1 (must be resolved with onboarding)
Status: confirmed

`is_valid_fsm_state("ONBOARDING:START")` returns `True`, and the DB constraint
accepts `ONBOARDING:%`. `_normalize_fsm_state()` instead checks exact membership
in `FSM_ALLOWED_STATES`, so every `ONBOARDING:*` value normalizes to `None`.

The downstream guard then fails open: `_guard_fsm_transition()` treats a
missing or unrecognized normalized current state as permission to accept any
valid target. Direct reproduction produced all of:

```text
None -> ACTIVE
CORRUPT -> ACTIVE
ONBOARDING:START -> ACTIVE
```

The onboarding transition therefore appears to work only because its current
state was discarded, not because the transition matrix authorized it. The
current Coach response contract does not emit `transition_signal`, which
reduces immediate reachability but does not make the guard correct.

Minimal fix: fail closed when an existing current state is absent or invalid,
make normalization use the canonical state validator, and explicitly encode
the target deterministic `ONBOARDING:* -> ACTIVE` transition. Add one
integration test for the real onboarding completion path plus invalid/NULL
source-state tests; unit-testing only `is_valid_fsm_state()` is insufficient.

Founder note (2026-07-23): scheduled to be fixed **when onboarding is built**
(ONB-01), not before. Onboarding has not been reached yet — the product is
still being built up — so the `ONBOARDING:*` validation path was never
hardened. This finding is the checklist item for that time; it is not a
standalone task to do now.

#### FSM-06 — a paused sequence can reach natural completion while still paused

Severity: P1
Status: confirmed; related to COACH-04

Pause changes the user/profile state but leaves the plan `active` and does not
move `plan_end_date`. The completion cron selects active plans whose end date
has passed without excluding `ACTIVE_PAUSED`, and `_auto_complete_plan_if_needed()`
then writes `IDLE_FINISHED`. `guards.py` also explicitly permits
`ACTIVE_PAUSED -> IDLE_FINISHED`.

This conflicts with the accepted pause meaning: future delivery stops and the
remaining sequence is preserved for resume. A sufficiently long pause can
instead finish the sequence in the background.

Minimal fix: completion candidates must exclude paused users, and the
pause/resume fix in COACH-04 must re-anchor remaining steps and the plan end
date. Remove ordinary `ACTIVE_PAUSED -> IDLE_FINISHED` from the target guard
matrix. If a special last-step edge case is desired later, encode it as an
explicit lifecycle condition rather than a blanket transition.

Mechanism note: removing `ACTIVE_PAUSED -> IDLE_FINISHED` is not a set edit.
The guard at `guards.py:45` is blanket — `{ACTIVE, ACTIVE_PAUSED} -> any
_PLAN_END_STATE`. It must be **split**: `ACTIVE` may go to `IDLE_FINISHED` or
`IDLE_PLAN_ABORTED`; `ACTIVE_PAUSED` may go to `IDLE_PLAN_ABORTED` only (cancel
from pause must still work), never `IDLE_FINISHED`. Deleting the state from a
shared set would either leave the bug or break cancel-from-pause.

#### FSM-07 — current tests and module docstrings preserve the obsolete architecture

Severity: P1 test replacement
Status: confirmed

`tests/test_fsm_states.py` passes all 8 tests, but three of those tests
explicitly assert the legacy `IDLE_ONBOARDED`, `IDLE_DROPPED`, and
`SCHEDULE_ADJUSTMENT` transitions. The green suite therefore protects the old
FSM rather than the accepted one.

The **module docstrings do the same** and must be corrected alongside the
tests: `states.py`'s header declares "Active FSM (9 state groups)" and lists
`IDLE_ONBOARDED`, `IDLE_DROPPED`, and `SCHEDULE_ADJUSTMENT` as live, and
`guards.py`'s header lists `SCHEDULE_ADJUSTMENT` as a live "time slot change"
transition and `IDLE_DROPPED` as "background expiry." Anyone reading the
module to learn the FSM is taught the dead model — a field-debugging trap.
Rewrite both headers to the target 6-state FSM when the legacy states are
removed.

The dedicated schedule-adjustment tests are already stale against other
accepted changes. With a syntactically valid test bot token and the unavailable
Trio parametrization excluded, the targeted run produced `13 passed, 7 failed`;
the failures expect the removed user-facing `MORNING` slot and old tunnel
behavior. The default test token also fails aiogram validation during
collection.

Minimal fix: replace legacy tests with a target transition-matrix suite,
direct-tool tests for time changes, onboarding-to-first-plan integration,
paused-completion prevention, and a DB-constraint migration test. Remove the
schedule-adjustment test modules together with their runtime subsystem.

#### FSM-08 — lifecycle truth is duplicated across unsynchronized fields

Severity: P1
Status: confirmed

The same lifecycle is represented independently in several places:

* `User.current_state` gates Coach tools, scheduler delivery, and step actions;
* `UserProfile.is_paused` is described in code and migration comments as the
  authoritative persistent pause flag, but the scheduler does not read it;
* `AIPlan.status` permits `active`, `completed`, `paused`, and `abandoned`;
  helpers accept `paused`, but no production writer sets a plan to `paused`;
* `User.plan_end_date` and `AIPlan.end_date` duplicate completion timing, and
  cancellation updates only the plan field, leaving the user field stale until
  a later completion check clears it.

The status vocabulary is also internally inconsistent: the unused Python
`PlanStatus` enum defines `CANCELED`, while the ORM enum and runtime use
`abandoned`. No cross-table constraint or startup reconciliation verifies that
these values form one valid aggregate.

Expected target ownership:

* `User.current_state` owns the interaction/delivery mode;
* `AIPlan.status` owns plan-record lifecycle (`active`, `completed`,
  `abandoned`);
* pause remains a delivery state, not a second plan status;
* completion timing has one authoritative source or an explicitly maintained
  projection.

Minimal fix: remove the unused `paused`/`canceled` vocabulary and preferably
the duplicate `is_paused` boolean. If compatibility requires keeping a mirror,
mark it non-authoritative and update/check it inside the same locked
transaction. Add an invariant checker that logs and blocks impossible
combinations before user-facing side effects.

#### FSM-09 — state-only checks can manufacture valid-looking states without a plan

Severity: P1
Status: confirmed; expands RT-13

Pause, resume, and cancellation validate fragments of state rather than the
whole lifecycle aggregate:

* `pause_plan` accepts `User.current_state == ACTIVE` without requiring an
  active plan;
* `resume_plan` accepts `ACTIVE_PAUSED` plus `profile.is_paused=True` without
  requiring a plan;
* `cancel_plan` accepts `ACTIVE`/`ACTIVE_PAUSED`, and if no plan is found still
  commits `IDLE_PLAN_ABORTED` and returns success.

The inverse inconsistency is also possible: an active plan paired with an idle
or paused-mirror mismatch is stranded because scheduler and Coach gating trust
`current_state`. There is no repair or explicit failure mode for either
direction.

Minimal fix: lifecycle operations must lock and load the user, profile, and
exactly one current plan, validate the combination, then apply the transition
atomically. On mismatch, fail honestly and emit reconciliation telemetry;
never report a successful pause/resume/cancel for a plan that does not exist.

#### FSM-10 — the orchestrator retains an unreachable second mutation architecture

Severity: P1 cleanup before beta
Status: confirmed

The current `coach_agent()` can return only text or a function tool call.
Nevertheless, `handle_incoming_message()` still processes legacy
`generated_plan_object`, `plan_updates`, and `transition_signal` fields that
the Coach cannot emit. Those branches can:

* abandon and replace an active plan using the old load/focus plan schema;
* apply plan adaptations that are disabled for MVP;
* directly rewrite `user.plan_end_date`;
* commit generic FSM transitions outside runtime tools.

Related preview/deep-link/action handlers remain in Telegram and orchestrator
code even though the v5 plan service creates plans deterministically and
`build_plan_draft_preview()` is frozen to return an empty string.

This is dead code today, but it preserves a conflicting lifecycle model and
creates a dangerous accidental re-entry point if a future worker begins
returning similarly named fields.

Minimal fix: remove the legacy envelope branches, persistence helpers, draft
preview/deep-link/action flow, and their tests after confirming no external
caller. The orchestrator's plan-changing surface should be deterministic
onboarding, completion/continuation, and the allowlisted runtime tools only.

#### FSM-11 — database schema history has no authoritative FSM migration

Severity: P1 before beta deployment
Status: confirmed

The ORM, SQL files, and deployed-schema bootstrap do not define one FSM:

* current SQL files variously permit `PLAN_FLOW:*`, `ADAPTATION_FLOW`,
  adaptation substates, and confirmation states;
* the ORM permits a different set and adds `SCHEDULE_ADJUSTMENT`;
* no checked migration installs the ORM's current constraint;
* startup uses `Base.metadata.create_all()`, which does not replace constraints
  on an existing database;
* `audit_startup_schema()` checks only three columns on `plan_instances`, not
  FSM constraints or lifecycle enums;
* both `users.current_state` and `ai_plans.status` are nullable, so their CHECK
  or enum declarations do not enforce a complete lifecycle value.

Consequently, behavior depends on whether a database was created from current
ORM metadata or evolved through an undocumented subset/order of raw SQL files.

Minimal fix: ship one explicit forward migration that remaps legacy rows,
installs the accepted state constraint, makes lifecycle columns non-null, and
normalizes plan-status values. Record applied migration/version state and add a
startup assertion for the expected FSM contract. Do not rely on `create_all()`
to upgrade an existing database.

#### FSM-12 — the database does not enforce one active plan per user

Severity: P1
Status: confirmed; cross-reference LIF-09 / LIF-11

`finalize_plan()` locks the user and checks for an active plan, which protects
the canonical creation path. The database itself has no partial unique
constraint for one `AIPlan(status='active')` per user, however, and legacy
writers or race paths can still leave multiple active rows.

The runtime already anticipates this invalid state: completion logs a warning
and completes only the newest active plan. Scheduler queries can meanwhile
restore or deliver steps for every active plan belonging to the same `ACTIVE`
user. One user-level FSM state cannot identify which plan is authoritative.

Minimal fix: clean any existing duplicates, add a PostgreSQL partial unique
index on active plan ownership, and retain the plan-scoped idempotency key
required by LIF-11 for automatic continuation. Treat duplicate detection as an
invariant breach, not a normal "choose latest" condition.

#### FSM-13 — lifecycle mutations are not serialized against each other

Severity: P1
Status: confirmed

Plan finalization uses row locks, but pause, resume, cancellation, and
completion do not lock the user/current plan before reading and writing their
state. Telegram messages, completion cron, and message-entry completion checks
can run concurrently.

For example, cancellation and completion can both observe `ACTIVE`; one writes
`abandoned`/`IDLE_PLAN_ABORTED`, while the other writes
`completed`/`IDLE_FINISHED` and may initiate a report. Under ordinary
read-committed transactions, the last commit wins individual fields without
preserving the intended operation-level outcome.

Minimal fix: serialize lifecycle mutations by locking the user and current plan
inside one short DB transaction, validate through the shared transition
boundary, and commit state plus plan metadata together. External Telegram and
scheduler side effects must remain outside the lock and use idempotent
outbox/reconciliation records rather than extending the DB transaction.

### Cross-domain FSM consequences already owned elsewhere

Do not create duplicate fixes for these; the consistency pass confirmed them:

* DEL-04 owns the rule that an already-delivered current-day exercise remains
  actionable through expiry while future delivery is paused;
* COACH-04 / RT-12 / RT-13 own pause/resume re-anchoring and scheduler
  reconciliation;
* LIF-03 / LIF-04 / LIF-09 / LIF-11 own factual completion, same-format
  continuation, explicit plan identity, and continuation idempotency;
* DEL-04 owns cancellation of open steps and visible Telegram keyboards.

### Verification evidence

* Current core state/tool/guard tests: `67 passed`.
* The green tests assert the current architecture, including obsolete states;
  they are not evidence of target-FSM correctness.
* Callback plus legacy schedule-adjustment revalidation:
  `14 passed, 15 failed, 23 deselected`. Failures are stale step doubles and
  removed `MORNING`/legacy tunnel assumptions, consistent with DEL-06 and
  FSM-03/FSM-07.
* Direct guard reproduction confirmed that NULL, corrupt, and prefixed
  onboarding source states all currently pass through to `ACTIVE`.

### Required MVP work and order

1. implement deterministic onboarding completion and first-plan creation
   (ONB-07 / FSM-01);
2. prevent paused plans from completing and finish pause/resume re-anchoring
   (COACH-04 / FSM-06);
3. disable the legacy schedule-adjustment scheduler job, inspect legacy rows,
   then remove the full tunnel (SCH-02 / FSM-03);
4. remove `IDLE_DROPPED` and merge any rows into `IDLE_PLAN_ABORTED` (FSM-02);
5. ship one forward migration that remaps legacy rows and replaces
   `ck_users_current_state` with the target set, makes lifecycle fields
   non-null, and normalizes plan-status vocabulary;
6. align `states.py`, `guards.py`, runtime tools/services, Coach state-tool
   filtering, and tests to that same matrix;
7. centralize state persistence behind a small shared transition boundary
   without introducing a new workflow framework (FSM-04);
8. enforce aggregate existence, one active plan per user, and locked lifecycle
   mutations (FSM-08 / FSM-09 / FSM-12 / FSM-13);
9. remove the unreachable legacy Coach mutation/draft path (FSM-10).

### What is not a finding

`IDLE_NEW` is bypassed when Telegram creates a user directly in
`ONBOARDING:START`, but it remains the accepted conceptual/default pre-onboarding
state and is not classified as dead in this round. `IDLE_FINISHED` is also not
dead: it remains the technical completion boundary until FD-01 automatic
continuation is implemented atomically.

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
