# Exercise Delivery — Product Contract

Status: design contract (2026-07-22). This is the *contract* the renderer
rewrite must satisfy, not an audit finding. Findings (DEL-01…DEL-08) check
the code against it.

## Why this document exists

The exercise notification is the single, daily, primary product touchpoint.
Coach, reports, and settings are secondary — this message is the product
the user sees every working day. It must be executed at 5/5: not cringe,
concise, convenient, and it must make the action feel doable *now*
(Prompt + Ability), without leaning on motivation.

## Channel-neutral content contract

Content is channel-neutral; Telegram/Slack only render it differently and
never define it.

```
title · duration_label
1. ordered step
2. ordered step
3. ...
[Виконано] [Пропустити]
```

Example:

```
Дихання · 30–60 сек

1. Вдихни повільно на 4 рахунки.
2. Затримай подих на 7.
3. Повільно видихни на 8.
4. Повтори 4 рази.

[Виконано] [Пропустити]
```

Ordering rationale: title says what it is → duration removes effort
uncertainty → steps let the user start without a further hop → buttons
capture the result after the action.

**First line must be self-sufficient** (title · duration): it is what the
user sees in the push preview. Never rely on the preview showing the full
text or being enabled at all (`Margin of Safety`).

## MUST contain

* the action as a tiny, immediately-doable instruction (≤ a few steps to
  hold in mind);
* the concrete steps (currently broken — DEL-01: renderer reads legacy
  root fields, not v5 `display.*`);
* the duration label (sets the Ability expectation "це швидко");
* two responses, one tap: Виконано / Пропустити.

## MUST NOT contain

Each item pushes the action further from the prompt or imports a
compliance/pressure frame:

* progress counters (`День 3`, `1 з 2`), mechanic/slot/internal IDs;
* "чому це працює" rationale — **removed 100%** (before OR after; not
  optional-after either);
* any explanation of why the system chose this exercise;
* streaks, discipline scores, comparison to others;
* motivational intro / persona voice;
* a separate "коли завершиш, натисни кнопку" line;
* decorative frames and emoji spam;
* a Coach message fired immediately after delivery.

## On tap — edit the same message, do not send a new one

Use `editMessageText` / `editMessageReplyMarkup`. One durable message, not
a growing chat. Buttons change with state.

* **Виконано** → remove Done/Skip, show a short durable `Виконано ✓`
  status, and surface the **optional feedback tap** ("Допомогло?"). This
  is the FD-05 completed-only effect signal, integrated into this same
  edit-flow.
* **Пропустити** → remove buttons, show `Пропущено`. **No feedback button**
  (a skip cannot rate an effect — FD-05).
* **Expired** → remove buttons, no judgmental copy.
* **Canceled** → remove buttons, close the action.
* **Paused** → an already-delivered exercise stays actionable until its
  expiry (DEL-04).

A toast may accompany but must not be the only confirmation; the durable
in-message status is the real closure (`Model 4` — clean sense of a
finished step).

## Channel capabilities and limits

* **Bot messages** (Telegram/Slack): formatted text + inline buttons only.
  No custom OS-tray action buttons — Done/Skip require opening the chat.
  True act-from-notification needs a native app; do not promise it for
  Telegram/Slack beta.
* **Rich "cards"**: not a native bot-message capability. Possible only via
  a server-rendered image (heavy; loses selectable/accessible text; push
  may show only "Photo") or a native app. Not MVP → on Telegram the
  message is text, so progress counters would read as raw text = cringe →
  **cut for MVP**.
* **Telegram Mini App** is real (HTML/JS UI, timers, haptics, result back
  to the bot, opened via an inline button). But for a 30-second exercise a
  Mini App adds a launch + tap → lowers Ability (`Inversion`: turning 30 s
  of breathing into "opening a wellness app" kills the tiny behavior). A
  guided player is justified **only** where interaction makes the action
  *easier*, not prettier — realistically only breathing (tempo circle /
  countdown). Post-beta, selective, channel-neutral (Mini App in Telegram,
  button in Slack, reusable in a future native app). Every exercise must
  remain doable without it (`Margin of Safety`).

## Timing / anchor — reasoned MVP stance + OPEN beta learning goal

This is a real failure surface, not dismissed: a mistimed push = an
unexecuted exercise = a retention break. It cannot be fully solved without
data, so MVP does cheap hedges and instruments the rest.

**The core error to avoid:** treating the saved delivery time as a moment
the user is *guaranteed able* to act. `14:00` does not mean no call, no
incident, no manager overhead (`Map ≠ Territory`). A clock time is a
scheduled prompt, not a Fogg anchor.

**Onboarding question (reframed).** Do NOT ask "коли в тебе вільна
хвилинка" — a "free minute" self-selects to leisure (evening at home),
which is the wrong target. This product inserts a quality recovery break
*into* a demanding workday for A-players. So: first explain the product's
purpose (recovery during the grind, not rest after it), then ask
specifically — **"у другій половині робочого дня, коли найкраще отримувати
сповіщення?"** Tying the moment to the goal is Fogg's *Impact* criterion:
it yields a meaningful proto-anchor, not an arbitrary time. (Afternoon is a
reasonable, testable prior — fatigue accumulates through the day.) The
user then picks a concrete `HH:MM`: technically unchanged, behaviorally
far better framed. This makes the anchor *better, not perfect* — it is
still self-reported, and the day can still block it; the window and
no-guilt rules below are the backstop.

**Delivery-time contract:**

1. the time is the *most likely convenient moment*, not a deadline;
2. the exercise stays actionable until the end of the local day (already
   true: `expires_at` = local 23:59) — frame it as a window in copy, not
   a "now or fail" command;
3. a skip is never punished;
4. the time is easy to change;
5. the system never infers anything about the user from skips/ignores.

**Company-level anchor (recommended context only).** At company onboarding
ask: is there a stable daily/standup, when is lunch, which hours are
reliably bad, is there a genuinely protected short break. A real shared
rhythm can become a *recommended* default (e.g. standup ends ~10:15 →
suggest 10:20). But the user must confirm or override — never a forced
"wellness minute," which would make the product a KPI. Guard against
`Principal-Agent`: HR may say "lunch 13:00" while managers routinely book
13:00. This fits the company-level context model (MISC-01).

**Not now:** calendar integration; real-activity tracking; automatic
free-moment detection; behavioral time-adaptation from skip/ignore;
mandatory company-wide time. **"Нагадати через 30 хв"** is deliberately
excluded from MVP but is the **top post-beta candidate** — it directly
answers "good exercise, impossible right now" — because it is a new
scheduler lifecycle (duplicates, expiry, idempotency), to build only if
timing friction is confirmed.

**Beta measurement (to separate "bad exercise" from "good exercise, bad
moment"):** delivery→completed latency; completed/skipped/ignored rates;
differences by chosen hour; and a direct user question — "час зазвичай був
зручний, чи вправа приходила невчасно?".

Also watch **weekly rhythm**: personal energy varies across the week
independent of interruptions (fresh Monday after a weekend vs a depleted
Friday), so a fixed time cannot match daily energy by definition — this is
`Map ≠ Territory` even before black-swan meetings. The fixed time is
tolerated not because it is optimal any given day but because a *reliable,
repeated* cue is what builds the habit (consistency > per-day optimality)
and skips are never punished. If skips cluster by weekday in aggregate,
that is a candidate signal for a future *light* touch — never per-user
inference. Observe, do not build.

## Content quality — separate round

Instruction wording materially changes exercise efficacy and is out of
scope here. Example already found: `Холодна вода` currently reads as the
weak "splash and feel the difference" version, not the dive-reflex version
(submerge face + breath-hold ~10 s) that actually triggers the response.
Several exercises also carry 4 steps vs Fogg's <3 guidance — the renderer
must not truncate; whether each step is needed is a content question.
These belong to a dedicated **Content Library** audit round (not yet done).
