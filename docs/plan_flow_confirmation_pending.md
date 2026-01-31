# Plan Agent — PLAN_FLOW:CONFIRMATION_PENDING

## Preview Boundaries

Preview may read draft structure for limited, non-interactive rendering only.
It must never reason about tasks, interpret the plan, or provide task-level control.
Preview is an artifact, not a reply.

## Minimal Confirmation Contract

In PLAN_FLOW:CONFIRMATION_PENDING, the LLM returns:

{
  "reply_text": "string",
  "transition_signal": "PLAN_FLOW:FINALIZATION | PLAN_FLOW:DATA_COLLECTION | IDLE_PLAN_ABORTED | null",
  "plan_updates": object | {} | null,
  "generated_plan_object": null
}

Semantics:
- transition_signal != null → FSM transition
- plan_updates == {} → regenerate draft + preview
- plan_updates is a non-empty object → rebuild draft + preview
- plan_updates == null → no backend action
