# Prompt Review — TODO (відкриті задачі між сесіями)

## Section 4 — Context & Memory Use (переписати повністю)

Section 1.6 "Memory & Continuity" видалена — її зміст переноситься сюди.

Переписати Section 4 під фактичний runtime. Поля які реально є:
- `short_term_history`
- `current_state`
- `temporal_context`
- `completion_context`
- `schedule_adjustment_context`

Поля яких немає і не повинно бути:
- `profile_snapshot` — видалено
- "long-term memory" — не існує
- "as if you remember" у широкому сенсі — видалити цю framing

## Section 2.6 — Unified Persona (перевірити після видалення 1.6)

Після видалення 1.6 перевірити Section 2.6 "Unified Persona":
- Там є "one consistent voice / one mind / one presence" — це persona consistency, не memory.
- Ця частина залишається в 2.6, вона правильно там.
- Але якщо є будь-які рядки про "you remember" або "context you received" — прибрати або перенести в Section 4.

## Архітектурне рішення — ONBOARDING блок видалено з Coach промпту (Section 2.1)

Видалено повністю блок `### ONBOARDING` (states `IDLE_NEW`, `ONBOARDING:*`) із Section 2.1
Internal System Map.

**Рішення:** Coach не повинен містити жодної логіки про онбординг.

**Чому:**
- Coach не існує в онбордингу — це інший флоу, інший агент/механіка.
- Архітектура має гарантувати, що Coach не викликається до завершення онбордингу.
  Промпт не повинен страхувати архітектуру — якщо Coach отримає `IDLE_NEW` чи
  `ONBOARDING:*`, це сигнал що щось зламалось вище за рівнем оркестрації, а не
  привід інструктувати Coach як це обробляти.
- **Inversion-перевірка:** як зламати онбординг? Дати Coach-у почати пояснювати
  продукт, розпитувати стан, пропонувати план — поки система ще збирає базові
  дані про користувача. Сам факт наявності цього блоку в промпті — це відкритий
  шлях для такого зламу (модель бачить інструкцію "як говорити в онбордингу" і
  може застосувати її навіть коли не повинна).

**Наслідок:** якщо в майбутньому Coach дійсно має брати участь у якійсь частині
онбордингу (наприклад, "м'яка" передача голосу між онбордингом і Coach) — це
окреме архітектурне рішення, яке потребує власного product-рев'ю, а не рядок
у Internal System Map.

**Статус:** рішення прийняте, зафіксоване. Не повертатись до цього без нового
product-обговорення.

## FSM / Coach Prompt Decision: IDLE_ONBOARDED and IDLE_DROPPED

### IDLE_ONBOARDED

Removed from the Coach-facing prompt.

Reason:
`IDLE_ONBOARDED` is no longer a real conversational state in the V4 product flow.

Earlier, it made sense because onboarding could finish before a plan was selected
or created. The user could remain "onboarded but without a plan" and the Coach
needed to help them choose what to start.

In V4, onboarding is expected to end with the first 7-day plan being created
automatically. The user should not enter a free-form Coach conversation between
onboarding completion and the first plan. Therefore this state should not be
described as a Coach behavior mode.

Decision:
Do not include `IDLE_ONBOARDED` in the Coach-facing state map.
Keep any remaining backend/FSM cleanup as a separate architecture task.

### IDLE_DROPPED

Removed from the Coach-facing prompt.

Reason:
`IDLE_DROPPED` does not currently have a defined deterministic user-facing flow.

It appears to represent passive abandonment / background expiry / stale active
plan cleanup, but the current runtime does not provide a stable mechanism that
naturally moves users into this state. Explicit user cancellation already maps
to `IDLE_PLAN_ABORTED`, and natural completion maps to `IDLE_FINISHED`.

Without a clear entry rule, describing `IDLE_DROPPED` in the Coach prompt would
create behavior for a state that the product has not actually defined.

Decision:
Do not include `IDLE_DROPPED` in the Coach-facing state map for V4.
Revisit if smart notifications or deterministic abandonment logic are
implemented later, for example inactivity timeout or stale plan cleanup.

**Summary:**
- `IDLE_ONBOARDED` removed — technical transit state, not a conversation.
- `IDLE_DROPPED` removed — undefined entry state, not a defined product behavior.
- The Coach prompt now only describes states where the Coach has distinct,
  product-defined behavior: `IDLE_FINISHED`, `IDLE_PLAN_ABORTED`, `ACTIVE`,
  `ACTIVE_PAUSED`, `SCHEDULE_ADJUSTMENT`.

## 2026-06-18 — Product Map rewrite and provisional Coach integration

### Context

This work was done during the ongoing line-by-line review of the Coach system
prompt. It is **not a finished PR** and does not mean the full prompt review is
complete.

The purpose was to create a reliable product source of truth for practical
questions such as:

- what Love Yourself does and what value it provides;
- how the 7-day and 14-day formats work;
- why a specific exercise is shown at a specific time;
- how exercises are selected;
- what pause, cancellation, and time changes mean;
- what happens after a missed exercise;
- what the user can expect when one exercise does not feel immediately
  noticeable.

This replaces the earlier idea of writing a large FAQ or letting the Coach
improvise product explanations.

### Product Map files

Updated:

- `resource/assets/product/conceptual_map.md`
  - Ukrainian master version.
  - Written as a product grounding document rather than a ready-made user FAQ.

Added:

- `resource/assets/product/conceptual_map_en.md`
  - English equivalent intended for the Coach runtime.
  - The English version exists because the Coach system instructions are written
    in English.

Updated:

- `resource/assets/product/README.md`
  - Documents the distinction between the Ukrainian Product Map, the English
    Coach version, and the technical `product_internal_spec.md`.

### Content decisions included in both maps

- Product value is stated directly rather than framed through defensive
  disclaimers.
- The expected value includes a clearer return to work, more sustained focus,
  longer work rhythm, and less end-of-day depletion.
- The product is not described as guaranteeing an identical immediate feeling
  after every exercise.
- Long-term value is explained through regularity over weeks, using the logic
  that one action is not equivalent to a repeated practice.
- Exercise selection is described accurately:
  the sequence is generated in advance for the user using defined selection
  rules rather than being dynamically chosen from the current conversation or
  mood.
- The map explains user control over delivery time.
- Pause, cancellation, and time changes are presented as available actions.
- Missing or skipping an exercise does not rebuild the sequence.
- Exercises are optional when the user has no time, ability, desire, or suitable
  surroundings.
- The map states that exercises were reviewed with practicing psychologists.
- The document instructs the Coach to use only relevant facts, answer in the
  user's language, and not invent details missing from the map or current
  context.

### Provisional runtime integration

Changed:

- `app/workers/coach_agent.py`

The English Product Map is currently loaded from
`resource/assets/product/conceptual_map_en.md` and sent to the model as a
separate trusted system message.

Current message order:

1. `COACH_SYSTEM_PROMPT` — identity, behavior, state, tool, safety, and response
   rules.
2. `COACH_PRODUCT_MAP` — static product facts and value proposition.
3. Runtime context — current user-specific facts such as `current_state`,
   current time, and completion context when available.
4. Recent conversation history.
5. Current user message.

This is static prompt grounding, **not RAG**. The complete English map is sent
with each Coach request; no retrieval or semantic search is performed.

### Architecture decision

The complete English Product Map must be sent with **every Coach API call** as a
separate trusted system message.

It is always present, not injected only for detected product questions. This
keeps product facts, value explanations, and action consequences available
throughout the conversation without relying on intent detection or retrieval.

The Ukrainian map remains the master product document for human review. The
English map is the runtime version used by the Coach. Both versions must be
updated together whenever product facts change.

The wider system prompt review is still in progress, but this Product Map
delivery decision is accepted and should not be treated as provisional.

### Supporting code and test changes

- Added `PRODUCT_MAP_PATH` and `COACH_PRODUCT_MAP`.
- Updated the foreign-instruction scan to recognize three trusted internal
  system messages instead of two.
- Added a regression test confirming that the English Product Map appears after
  the Coach prompt and before runtime context.

Verification on 2026-06-18:

- `python3 -m py_compile app/workers/coach_agent.py` — passed.
- `pytest -q tests/test_coach_idle_finished.py -k 'not trio'` —
  9 passed, 2 deselected.
- The two excluded variants require the unavailable `trio` dependency; this is
  the existing environment issue, not a Product Map failure.

### Status

Worktree only. No PR opened. Continue reviewing the Coach system prompt before
deciding the final integration shape.

## 2026-06-18 — Product Map follow-up: value and selection framing

Three corrections were applied to both `conceptual_map.md` and
`conceptual_map_en.md`:

1. **Short-term value in Section 2**
   - Removed the per-exercise promise that it will immediately make returning
     to work feel easier.
   - Reframed immediate value as one bounded moment of switching with a clear
     beginning and end, instead of an open-ended stream of distraction.
   - Kept the long-term value statement about regular short pauses supporting
     sustained work rhythm, deeper focus, and lower end-of-day depletion.

2. **Exercise selection in Section 8**
   - Removed builder-level details such as weights, cooldowns, and library
     activation flags from the Coach-facing Product Map.
   - Reframed the value for the user: the system handles exercise selection,
     variety, daytime/evening purposes, and excessive repetition so the user
     does not have to manage those decisions.
   - Preserved the important product fact that an already-created sequence is
     not rebuilt from conversation, skips, or completed exercises.

3. **Psychologist review claim in Section 10**
   - Removed the statement that every exercise was reviewed with practicing
     psychologists because this claim is not currently supported as a verified
     product fact.

4. **Section 10 consistency correction**
   - Removed the remaining per-exercise promise that an action helps the user
     return to work with clearer focus.
   - Section 10 now describes only the observable mechanism: a short action
     moves attention to one concrete step and creates a bounded switching point
     within the working day.

Status remains worktree-only. No PR has been opened.

## 2026-06-19 — Format-switch rule and plan-status intents

Two remaining ACTIVE dependencies were completed:

1. **Product Map: changing between 7 and 14 days**
   - Added to both Ukrainian and English maps.
   - The format cannot be changed while the current 7 or 14 days are running.
   - Switching before completion requires canceling the current sequence and
     starting a new one in the desired format.

2. **`get_plan_status` intent coverage**
   - Expanded both the Section 6 instruction and the registered tool
     description.
   - Explicitly covers:
     current day, days remaining, completion progress, and current status.
   - Added natural-language examples so the model recognizes common user
     phrasings instead of guessing from conversation history.
   - Runtime verification found that the old tool result did not fully support
     the new description: it could format the first day as `Day 0 of 7`, did
     not return days remaining, and did not calculate exercise completion.
   - Updated `app/plan_runtime/tools.py::get_plan_status()` to return:
     `current_day`, `days_remaining`, `steps_total`, `steps_completed`, and
     `completion_rate`.
   - Updated the orchestrator's user-facing status reply to show the current
     day, remaining days, and completed exercises.
   - Added unit coverage for active-plan progress and the no-active-plan case.
     Verification: `PYTHONPATH=. pytest -q tests/test_plan_runtime_tools.py` —
     16 passed.

Status: implemented in the worktree. No PR opened.

## 2026-06-18 — ACTIVE PLAN follow-up tasks

### Agreed ACTIVE PLAN draft

Use this as the working version when Section 2.1 is updated:

```text
### ACTIVE PLAN

State: `ACTIVE`

A 7 or 14-day plan is currently running.
Exercises may be scheduled for the user.

Coach behavior depends on the user's intent:

- If the user asks about exercises, timing, pause, continuation, stopping,
  or what to do next:
  use the Product Map as the source of truth for how the product works,
  why this exercise is shown now, and what options are available.
  Explain how to perform a specific exercise only from instructions
  available in the current context or conversation.
  Do not invent missing product facts or exercise steps.
  When the request requires an action, follow the tool and consent rules.
  Do not change plan content, exercises, or structure.

- If the user brings up workday friction, frustration, or emotional discomfort
  without asking for plan management:
  respond as Coach support, not as plan logistics.
  Do not immediately turn the message into instructions,
  explanations, or plan management.

- If both are present:
  acknowledge the emotional context first, then answer the practical question.
```

### TODO — Missing Product Information policy in Section 2.2

Add a global rule under `2.2 Role Boundaries & Scope`:

```text
### Missing Product Information

If the Product Map and current context do not contain the information needed
to answer a factual product question:

- say clearly that you do not have that detail,
- do not infer, approximate, or invent an answer,
- direct the user to product support when an escalation path is available.
```

Important:

- Do not tell the user that the Coach contacted, notified, or escalated to the
  product team unless a real escalation mechanism exists.
- Before release, decide the actual escalation path:
  a support contact, a deterministic support flow, or a future
  `escalate_product_question` tool.
- Until that path exists, the Coach should only state that the detail is not
  available rather than pretending to pass the question to someone.

### TODO — Resolve Exercise Visibility Boundary conflict

The current `Exercise Visibility Boundary` conflicts with the agreed ACTIVE
behavior.

Current boundary forbids the Coach from:

- describing step-by-step actions;
- instructing the user how to perform an exercise.

The agreed ACTIVE behavior allows the Coach to explain a specific exercise when
its original instructions are available in current context or conversation.

When reviewing `Exercise Visibility Boundary`:

- allow the Coach to clarify or repeat exercise instructions that are actually
  present in trusted runtime context or conversation;
- prohibit inventing missing steps, modifications, substitute exercises, or
  additional exercise content;
- verify whether the runtime currently provides the delivered exercise text to
  the Coach;
- if it does not, add the required exercise context before claiming that the
  Coach can explain how to perform it.

Status: open. Review these items when Section 2.2 and Exercise Visibility
Boundary are reached.

## 2026-06-18 — P0: delivered exercise context is missing from Coach runtime

### Verified current behavior

The Coach currently does **not** receive `display.steps` for the exercise that
was delivered to the user.

The delivered exercise is not available through either supported path:

1. **`short_term_history`**
   - Scheduled exercise messages are sent directly by `app.scheduler`.
   - `send_scheduled_message()` sends the Telegram message and records telemetry,
     but does not append the notification text to Redis session history.
   - It also does not create an assistant `ChatHistory` row.
   - Therefore `get_stm_history()` cannot return the delivered exercise message.

2. **Structured Coach context**
   - `build_user_context()` currently returns:
     `message_text`, `short_term_history`, `current_state`,
     `temporal_context`, and `schedule_adjustment_context`.
   - It does not return `current_exercise_context`, `exercise_id`,
     `display.steps`, or the delivered exercise text.
   - `_context_message()` narrows the Coach runtime context further and currently
     includes only current time, FSM state, and completion context when present.

### Product consequence

The agreed ACTIVE PLAN prompt says the Coach may explain how to perform the
relevant exercise using instructions available in current context or
conversation.

With the current runtime, those instructions are normally unavailable.
Shipping that prompt would create a guaranteed contract gap:

- the prompt permits and expects exercise clarification;
- the system does not provide the original exercise steps;
- the Coach must either refuse a basic support question or invent instructions.

This is a **P0 fix for T5.8 before the ACTIVE PLAN block goes to production**.
Without it, the new ACTIVE contract promises support that the runtime physically
cannot provide.

### Recommended implementation

Add a structured `current_exercise_context` to the Coach payload rather than
relying only on `short_term_history`.

Minimum payload exposed to the Coach:

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

The context should be built from the latest relevant delivered `AIPlanStep`
and its trusted `ContentLibrary.content_payload.display` data.

`delivered_today` must be evaluated in the user's local timezone. In the
14-day format, where two exercises can be delivered on the same working day,
use the most recently delivered exercise as `current_exercise_context`.
If a later workflow needs the Coach to distinguish both exercises explicitly,
expand this to `delivered_exercises_today`; do not silently guess which one the
user means.

If no exercise has actually been delivered today:

```json
{
  "current_exercise_context": null
}
```

The Coach may clarify or repeat only the supplied `title`, `steps`, and duration.
It must not create variations, add steps, alter the exercise, or infer missing
instructions.

Add tests proving:

- the latest delivered exercise is included for `ACTIVE`;
- `display.steps` and `duration_label` are preserved exactly;
- future, pending, skipped, canceled, or unrelated exercises are not exposed as
  the current exercise;
- missing content produces `current_exercise_context = null`, not invented data;
- the context reaches `_compose_messages()` before the user message.

### Related P0 discovered during verification: v5 delivery rendering

The v5 content library stores user instructions under:

```text
content_payload.display.title
content_payload.display.steps
content_payload.display.duration_label
```

However:

- `plan_finalization._build_step_title()` reads root `content_payload.title`;
- `plan_finalization._build_step_description()` reads root
  `description`, `text`, or `instructions`;
- `format_task_notification()` reads root `instructions` and root duration
  fields.

The content loader preserves the nested `display` object and does not flatten
it. Therefore the current delivery path may fail to render the v5
`display.steps` in the Telegram exercise notification.

Verify and fix the notification renderer to read the canonical v5
`content_payload.display` fields. This is separate from, but required alongside,
the Coach context fix: the user and the Coach must receive the same trusted
exercise instructions.

Priority order:

1. **P0 — Telegram renderer**
   Fix and test the core scheduled notification first. If the user does not
   receive `display.title`, `display.steps`, and `display.duration_label`, the
   primary daily product loop is broken independently of the Coach.

2. **P0 — `current_exercise_context`**
   Add the delivered exercise data to the Coach runtime before enabling the new
   ACTIVE PLAN prompt behavior.

Status: open, P0 before production.

## Backlog — Product question escalation flow

Design and implement a real escalation path for factual product questions that
cannot be answered from the Product Map or current runtime context.

### Product decision required

Decide:

- where escalated questions go;
- who receives and answers them;
- whether the user receives an answer in the same Telegram conversation;
- whether escalation is automatic or requires explicit user confirmation;
- what response-time expectation, if any, is shown to the user;
- whether the unresolved question should be stored for future Product Map
  updates.

Possible implementation options:

- a deterministic support contact or support button;
- a support queue persisted in the database;
- an `escalate_product_question` runtime tool available to the Coach;
- an admin notification with a later human reply flow.

### Required Coach behavior before implementation

If the Product Map and current context do not contain the requested factual
detail, the Coach must:

- say that it does not have that detail;
- avoid guessing, approximating, or inventing an answer;
- avoid claiming that the question was sent, escalated, or reported to the
  product team.

The Coach may claim successful escalation only after a real escalation action
has completed and the runtime has returned a successful result.

### Future tool contract

If implemented as a tool, the minimum input should include:

```json
{
  "question": "The user's unresolved product question",
  "relevant_context": "Minimal context required to understand the question"
}
```

The tool result should explicitly distinguish:

- accepted for human review;
- already answered by an existing source;
- failed to submit.

### Status

Backlog. Not required to continue the current line-by-line prompt review, but
must be resolved before the Coach is instructed to offer product escalation as
an available user action.

## 2026-06-19 — Remove SCHEDULE_ADJUSTMENT from Coach-facing architecture

### Decision

Remove `SCHEDULE_ADJUSTMENT` from the Coach-facing state map and do not model
time changes as a separate conversational state.

Planned interaction:

1. The user asks to change a delivery time.
2. The Coach collects a concrete time if it is missing.
3. The user confirms the requested change.
4. The Coach calls `change_day_time` or `change_evening_time`.

This should be a direct tool-call flow governed by tool descriptions, current
state permissions, and consent rules.

### Reason

A dedicated FSM state adds a separate conversation mode for an operation that
does not require one. It also creates additional Redis context, transition
logic, and recovery behavior, with a risk of leaving the user stuck inside a
time-change workflow.

The time-change behavior is already covered by:

- the `ACTIVE` and `ACTIVE_PAUSED` state permissions;
- Product Map explanations;
- `change_day_time` and `change_evening_time` tool descriptions;
- explicit confirmation rules.

### Backend cleanup TODO

- Remove `SCHEDULE_ADJUSTMENT` from FSM states, transitions, and guards.
- Remove `schedule_adjustment_context` Redis keys and session-memory methods.
- Remove schedule-adjustment last-active and soft-prompt tracking.
- Remove orchestrator branches and helper functions tied to the dedicated
  state.
- Remove Telegram callbacks and handlers that depend on the old workflow.
- Preserve day-time versus evening-time disambiguation in the tool schema.
- Preserve first-time evening collection through `record_evening_time`; do not
  confuse it with changing an existing evening time.
- Add direct tool-flow tests for:
  missing time, invalid time, explicit confirmation, day-time change,
  evening-time change, and cancellation before confirmation.

### Status

The prompt block is being removed during the current prompt review.
Backend cleanup remains open and should be completed in a separate code change
before production.

## 2026-06-19 — Section 2.2 Role Boundaries & Scope approved draft

Use the following as the working final version for Section 2.2:

```text
## 2.2 Role Boundaries & Scope

Your role is limited to:

- Love Yourself product support,
- workday emotional support.

---

### Love Yourself Product Support

You may:

- explain the user's current 7 or 14 days,
- explain exercises and how they work,
- answer questions about timing, missed days, pause, resume, cancellation,
  continuation, and available options,
- help the user understand the next relevant choice available inside
  Love Yourself.

Use the Product Map and current runtime context as the source of truth.

Do not invent product facts, personalization logic, features,
or hidden system behavior.

---

### Workday Emotional Support

You may respond when the user brings up work-related pressure, friction,
frustration, tiredness, difficulty starting, or emotional discomfort,
including patterns they notice across multiple workdays.

Keep the response connected to the user's words without labeling,
diagnosing, interpreting hidden causes, confirming self-criticism,
or turning the conversation into a session.

When the user brings up a serious or potentially harmful situation:
respond with emotional support, do not minimize the concern,
and do not redirect it into productivity or plan completion.
Do not decide the outcome for the user.
If safety or crisis rules apply, follow the dedicated safety guidance.

---

### Exercise Explanation Boundary

Use the Product Map to explain:

- how exercises work,
- why an exercise is shown at a particular time,
- that the sequence is prepared in advance and does not require the user
  to choose each exercise.

Explain how to perform the current exercise only from instructions available
in `current_exercise_context`.

Do not:

- invent missing exercise steps,
- modify or replace the current exercise,
- suggest additional exercises outside the current 7 or 14 days,
- list or expose the full exercise library.

---

### Missing Product Information

If the Product Map and current context do not contain the information needed
to answer a factual product question:

- say clearly that you do not have that detail,
- do not infer, approximate, or invent an answer,
- direct the user to product support only when a real escalation path
  is available.

Do not claim that a question was reported, forwarded, or escalated
unless that action actually occurred.

---

### Professional Guidance and Major Decisions

Do not provide professional medical, legal, financial,
career, or other specialist guidance.

Do not make major life, work, career, medical, legal,
or financial decisions for the user.

---

### Outside-Scope Requests

If the user asks for something outside this scope:

- do not perform, draft, solve, or materially assist with the outside-scope task,
- say briefly and directly that it is outside what you can help with,
- do not reinterpret an unrelated request as a wellbeing issue,
- do not force the conversation back to Love Yourself,
- mention an available in-scope form of help only when it is relevant
  to what the user said.

---

### Mixed Requests

If a message contains both an outside-scope task
and workday emotional context:

- acknowledge the emotional context,
- decline the outside-scope task,
- respond only to the part that is within scope.
```

### Section 4.8 safety boundary TODO

When reviewing Section 4.8, explicitly distinguish:

- **serious but non-crisis workday situations**:
  major conflict, fear of losing a job, potentially harmful workplace
  situations, or large work/career decisions without immediate danger;
- **safety or crisis situations**:
  self-harm, threatened harm to others, violence, immediate danger, or another
  acute safety risk.

Required priority rule:

- Section 2.2 governs serious non-crisis situations.
- Dedicated safety guidance overrides Section 2.2 whenever crisis or immediate
  safety criteria apply.
- The Coach must not remain neutral about immediate safety: it must follow the
  crisis response protocol.

Status: Section 2.2 draft approved. Section 4.8 boundary remains open.
