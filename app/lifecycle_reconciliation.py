"""Explicit post-commit reconciliation for lifecycle scheduler effects."""

from __future__ import annotations

from dataclasses import replace
import logging

from app.lifecycle import (
    ExternalEffect,
    ExternalEffectState,
    LifecycleResult,
)


logger = logging.getLogger(__name__)


def _failed(effect: ExternalEffect, error_code: str) -> ExternalEffect:
    return replace(
        effect,
        state=ExternalEffectState.FAILED,
        error_code=error_code,
    )


def _record_activation_event(result: LifecycleResult) -> None:
    from app.db import SessionLocal
    from app.telemetry import log_user_event

    with SessionLocal() as db:
        log_user_event(
            db,
            user_id=int(result.user_id),
            event_type="plan_activated",
            event_source="plan_finalization",
            source_operation_id=f"plan-activation:{result.plan_id}",
            plan_id=result.plan_id,
            context={"total_days": int(result.details["total_days"])},
        )
        db.commit()


def reconcile_scheduler_effects(result: LifecycleResult) -> LifecycleResult:
    """Execute retry-safe scheduler effects and return their truthful outcome."""
    from app.scheduler import (
        reconcile_cancel_plan_step_jobs,
        reconcile_plan_schedule,
        reconcile_terminal_step_keyboards,
    )

    outcomes: list[ExternalEffect] = []
    for effect in result.effects:
        if effect.kind == "record_activation_event":
            # A retry re-evaluates the event after the current schedule proof.
            continue
        if effect.state is not ExternalEffectState.PENDING:
            outcomes.append(effect)
            continue
        try:
            if effect.kind == "reconcile_plan_schedule":
                reconciliation = reconcile_plan_schedule(
                    int(effect.target_ids[0])
                    if effect.target_ids
                    else result.plan_id
                )
            elif effect.kind == "cancel_step_jobs":
                reconciliation = reconcile_cancel_plan_step_jobs(
                    list(effect.target_ids)
                )
            elif effect.kind in {"remove_step_keyboard", "remove_step_keyboards"}:
                reconciliation = reconcile_terminal_step_keyboards(
                    list(effect.target_ids)
                )
            else:
                outcomes.append(_failed(effect, "unsupported_effect"))
                continue
        except Exception as exc:
            logger.exception(
                "Lifecycle effect failed operation=%s plan=%s kind=%s",
                result.operation,
                result.plan_id,
                effect.kind,
            )
            outcomes.append(_failed(effect, type(exc).__name__))
            continue

        state = (
            ExternalEffectState.SUCCEEDED
            if not reconciliation.failed_ids
            else ExternalEffectState.FAILED
        )
        outcomes.append(
            replace(
                effect,
                state=state,
                attempted=reconciliation.attempted,
                succeeded=reconciliation.succeeded,
                error_code=(
                    None if state is ExternalEffectState.SUCCEEDED
                    else "scheduler_reconciliation_failed"
                ),
            )
        )

    activation_ready = result.operation == "activate" or (
        result.operation == "switch_plan_format"
        and result.code in {"applied", "replayed"}
        and "total_days" in result.details
    )
    if activation_ready:
        event_effect = ExternalEffect(
            kind="record_activation_event",
            target_ids=(int(result.plan_id),),
        )
        schedule_succeeded = all(
            effect.state in {
                ExternalEffectState.SUCCEEDED,
                ExternalEffectState.DEFERRED,
                ExternalEffectState.NOT_REQUIRED,
            }
            for effect in outcomes
            if effect.kind not in {"remove_step_keyboard", "remove_step_keyboards"}
        )
        if not schedule_succeeded:
            event_effect = replace(event_effect, state=ExternalEffectState.DEFERRED)
        else:
            try:
                _record_activation_event(result)
            except Exception:
                logger.exception(
                    "Activation event reconciliation failed plan=%s",
                    result.plan_id,
                )
                event_effect = _failed(event_effect, "activation_event_failed")
            else:
                event_effect = replace(event_effect, state=ExternalEffectState.SUCCEEDED)
        outcomes.append(event_effect)
    return replace(result, effects=tuple(outcomes))


__all__ = ["reconcile_scheduler_effects"]
