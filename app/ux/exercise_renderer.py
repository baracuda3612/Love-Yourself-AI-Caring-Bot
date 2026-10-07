"""Small, pure Telegram renderer. Never shorten the released instructions."""
from html import escape

from app.exercise_presentation import ExercisePresentation

TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
NEUTRAL_PREVIEW = 'Пауза'
STATUS_LABELS = {
    'available': 'Доступна', 'pending': 'Доступна', 'scheduled': 'Доступна',
    'delivered': 'Доступна', 'completed': 'Виконано ✓', 'skipped': 'Пропущено',
    'expired': 'Час дії завершився', 'canceled': 'Скасовано',
}


class PresentationTooLong(ValueError):
    """The complete presentation exceeds a channel limit; no partial send."""


def telegram_length(text: str) -> int:
    """Conservative Unicode bound, including non-BMP characters as two units."""
    return len(text.encode('utf-16-le')) // 2


def render_exercise(presentation: ExercisePresentation, *, caption: bool = False, scheduled: bool = True) -> str:
    if not presentation.title or not presentation.steps or any(not step.strip() for step in presentation.steps):
        raise ValueError('complete exercise text required')
    prefix = [NEUTRAL_PREVIEW, ''] if scheduled else []
    raw = [*prefix, presentation.title]
    html = [*prefix, f'<b>{escape(presentation.title)}</b>']
    if presentation.duration_label:
        raw.append(presentation.duration_label)
        html.append(escape(presentation.duration_label))
    raw.append(''); html.append('')
    for index, step in enumerate(presentation.steps, 1):
        raw.append(f'{index}. {step}')
        html.append(f'{index}. {escape(step)}')
    status = STATUS_LABELS[presentation.status]
    if presentation.action_deadline and presentation.status in ('available', 'pending', 'scheduled', 'delivered'):
        status = f'Доступна до {presentation.action_deadline:%d.%m %H:%M}'
    raw += ['', status]; html += ['', escape(status)]
    if telegram_length('\n'.join(raw)) > (CAPTION_LIMIT if caption else TEXT_LIMIT):
        raise PresentationTooLong('caption_too_long' if caption else 'text_too_long')
    return '\n'.join(html)
