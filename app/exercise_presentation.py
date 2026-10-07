"""Exact DB content facts, independent of channel, selection and lifecycle writes."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.content_library import ContentValidationError, selected_content


@dataclass(frozen=True)
class ExerciseMedia:
    exercise_id: str
    content_version: int
    asset_version: int
    revision: str
    path: str
    sha256: str
    role: str
    alt_text: str
    width: int
    height: int
    loop_duration_ms: int


@dataclass(frozen=True)
class ExercisePresentation:
    exercise_id: str | None
    content_version: int | None
    title: str
    duration_label: str
    steps: tuple[str, ...]
    media: ExerciseMedia | None
    status: str
    action_deadline: datetime | None
    available_actions: tuple[str, ...]

    def to_payload(self) -> dict:
        payload = asdict(self)
        payload['steps'] = list(self.steps)
        payload['available_actions'] = list(self.available_actions)
        payload['action_deadline'] = (
            self.action_deadline.isoformat() if self.action_deadline else None
        )
        return payload


def exercise_presentation(
    db: Session, exercise_id: str, content_version: int, *,
    status: str = 'available', action_deadline: datetime | None = None,
    available_actions: tuple[str, ...] = ('complete', 'skip'),
) -> ExercisePresentation:
    """Shared exact-version entrance; callers own selection and action identity."""
    content = selected_content(db, exercise_id, content_version)
    display = content['display']
    media = content['media']
    return ExercisePresentation(
        exercise_id=exercise_id, content_version=content_version,
        title=display['title'], duration_label=display['duration_label'],
        steps=tuple(display['steps']),
        media=ExerciseMedia(**{key: media[key] for key in ExerciseMedia.__dataclass_fields__}) if media else None,
        status=status, action_deadline=action_deadline,
        available_actions=tuple(available_actions),
    )


def step_presentation(db: Session, step, *, user_timezone=None) -> ExercisePresentation:
    """Read facts for an existing scheduled step; never mutate its lifecycle."""
    status = getattr(step, 'step_status', None) or 'scheduled'
    deadline = getattr(step, 'expires_at', None)
    if deadline is not None:
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if user_timezone is not None:
            deadline = deadline.astimezone(user_timezone)
    actions = ('complete', 'skip') if status in ('pending', 'scheduled', 'delivered') else ()
    if deadline is not None and deadline <= datetime.now(timezone.utc):
        actions = ()
    if step.exercise_id:
        version = getattr(step, 'content_version', None)
        if version is None:
            raise ContentValidationError('selected content version missing')
        return exercise_presentation(
            db, step.exercise_id, version, status=status,
            action_deadline=deadline, available_actions=actions,
        )
    # Compatibility only for historical rows without any catalogue identity.
    if not getattr(step, 'title', None) or not getattr(step, 'description', None):
        raise ContentValidationError('historical exercise text missing')
    return ExercisePresentation(
        None, None, step.title, '', (step.description,), None,
        status, deadline, actions,
    )


def current_exercise_context(db: Session, user_id: int, plan_id: int, *, user_timezone=None) -> dict | None:
    """Only an actually delivered message on the user's named current plan."""
    from app.db import AIPlan, AIPlanDay, AIPlanStep, PlanLifecycleOperation

    step = (
        db.query(AIPlanStep)
        .join(AIPlanDay, AIPlanStep.day_id == AIPlanDay.id)
        .join(AIPlan, AIPlanDay.plan_id == AIPlan.id)
        .join(PlanLifecycleOperation, PlanLifecycleOperation.plan_step_id == AIPlanStep.id)
        .filter(
            AIPlan.user_id == user_id, AIPlan.id == plan_id,
            AIPlanStep.tg_message_id.isnot(None),
            PlanLifecycleOperation.user_id == user_id,
            PlanLifecycleOperation.plan_id == plan_id,
            PlanLifecycleOperation.operation == 'step_delivered',
            PlanLifecycleOperation.result_status == 'delivered',
        )
        .order_by(PlanLifecycleOperation.created_at.desc(), AIPlanStep.id.desc())
        .first()
    )
    if step is None:
        return None
    try:
        return step_presentation(db, step, user_timezone=user_timezone).to_payload()
    except ContentValidationError:
        # A gated record must not become Coach instructions through history.
        return None
