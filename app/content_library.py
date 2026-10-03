"""Versioned release authority. JSON is a reviewed seed, never a runtime catalogue."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import re
import struct

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.db import ContentLibrary

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SEED_PATH = ROOT / 'resource/assets/content_library/tasks/burnout_combined_content_library.json'
MANIFEST_PATH = ROOT / 'resource/assets/content_library/media/manifest.json'
EXERCISE_IDS = frozenset(('breathing_sigh', 'pmr_fist', 'tactile_surface', 'visual_distance', 'auditory_sound', 'cold_water_face', 'brain_dump', 'one_thing', 'first_step_tomorrow'))
REQUIRED_MEDIA = frozenset(('breathing_sigh', 'pmr_fist', 'cold_water_face'))
FIELDS = ('content_version', 'is_active', 'mechanic', 'modality', 'requirements', 'duration_seconds', 'cooldown_days', 'review_required', 'review_status', 'review_evidence', 'display', 'media')


class ContentValidationError(ValueError):
    """Invalid release, missing resource, or conflict with an existing version."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ContentValidationError(reason)


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def gif_metadata(data: bytes) -> tuple[int, int, int]:
    """Read GIF dimensions and frame delays without transforming approved bytes."""
    _require(data[:6] in (b'GIF87a', b'GIF89a'), 'resource is not a GIF')
    try:
        width, height = struct.unpack_from('<HH', data, 6)
        packed = data[10]
        pos = 13 + (3 * 2 ** ((packed & 7) + 1) if packed & 128 else 0)
        delay, duration, frames = 0, 0, 0
        def blocks(offset):
            while data[offset]:
                offset += 1 + data[offset]
            return offset + 1
        while data[pos] != 0x3B:
            marker = data[pos]; pos += 1
            if marker == 0x21:
                label = data[pos]; pos += 1
                if label == 0xF9:
                    _require(data[pos] == 4, 'invalid GIF control block')
                    delay = struct.unpack_from('<H', data, pos + 2)[0] * 10
                pos = blocks(pos)
            elif marker == 0x2C:
                packed = data[pos + 8]; pos += 9
                if packed & 128:
                    pos += 3 * 2 ** ((packed & 7) + 1)
                pos += 1  # LZW code size
                pos = blocks(pos)
                duration += delay; delay = 0; frames += 1
            else:
                raise ContentValidationError('invalid GIF block')
        _require(frames > 0 and duration > 0, 'GIF must contain timed frames')
        return width, height, duration
    except (IndexError, struct.error) as exc:
        raise ContentValidationError('truncated GIF') from exc


def validate_media(media: dict | None, exercise_id: str, version: int, *, check_files: bool = True, asset_root: Path = ROOT) -> None:
    if media is None:
        _require(exercise_id not in REQUIRED_MEDIA, 'required instructional media missing')
        return
    _require(isinstance(media, dict), 'media must be an object')
    _require(media.get('exercise_id') == exercise_id and type(media.get('content_version')) is int and media.get('content_version') == version, 'wrong media exercise/version')
    _require(type(media.get('asset_version')) is int and media['asset_version'] > 0, 'invalid asset version')
    _require(_text(media.get('revision')) and _text(media.get('alt_text')), 'media revision/alt text missing')
    _require(media.get('role') == ('instructional' if exercise_id in REQUIRED_MEDIA else 'illustrative'), 'wrong media role')
    _require(isinstance(media.get('sha256'), str) and re.fullmatch(r'[0-9a-f]{64}', media['sha256']) is not None, 'invalid media digest')
    approval = media.get('approval')
    _require(isinstance(approval, dict) and approval.get('status') == 'approved', 'media not approved')
    _require(all(approval.get(key) == media[key] for key in ('exercise_id', 'content_version', 'asset_version', 'sha256')), 'approval must cover exact exercise/content/asset/digest')
    _require(all(_text(approval.get(key)) for key in ('approver', 'approved_on', 'evidence', 'mobile_review')), 'media approval evidence incomplete')
    try:
        date.fromisoformat(approval['approved_on'])
    except ValueError as exc:
        raise ContentValidationError('invalid approval date') from exc
    _require(all(type(media.get(key)) is int and media[key] > 0 for key in ('width', 'height', 'loop_duration_ms')), 'invalid media dimensions/timing')
    _require(_text(media.get('path')), 'media path missing')
    path = (asset_root / media['path']).resolve()
    allowed = (asset_root / 'resource/assets/content_library/media').resolve()
    _require(path.is_relative_to(allowed) and path.suffix == '.gif', 'media path outside packaged assets')
    if check_files:
        _require(path.is_file(), 'packaged GIF missing')
        data = path.read_bytes()
        _require(sha256(data).hexdigest() == media['sha256'], 'media integrity mismatch')
        _require(gif_metadata(data) == (media.get('width'), media.get('height'), media.get('loop_duration_ms')), 'media dimensions/timing mismatch')


def validate_record(record: dict, *, check_files: bool = True, asset_root: Path = ROOT) -> None:
    _require(isinstance(record, dict) and set(record) == {'id', *FIELDS}, 'invalid content fields')
    eid, version = record['id'], record['content_version']
    _require(_text(eid) and eid in EXERCISE_IDS, 'unknown exercise ID')
    _require(type(version) is int and version > 0, 'invalid content version')
    _require(type(record['is_active']) is bool and type(record['review_required']) is bool, 'invalid gate flags')
    _require(record['mechanic'] in ('switch', 'unload') and record['modality'] in ('breathing','muscle','tactile','visual','auditory','thermal','writing'), 'invalid mechanic/modality')
    _require(type(record['duration_seconds']) is int and record['duration_seconds'] > 0, 'invalid duration')
    _require(type(record['cooldown_days']) is int and record['cooldown_days'] >= 0, 'invalid cooldown')
    _require(record['review_status'] in ('unreviewed', 'approved', 'rejected'), 'invalid review status')
    _require(eid != 'cold_water_face' or record['review_required'], 'DG-02 cannot be disabled')
    requirements = record['requirements']
    _require(isinstance(requirements, dict) and set(requirements) == {'capabilities', 'environment', 'friction'}, 'invalid requirements structure')
    _require(all(isinstance(values, list) and all(_text(v) for v in values) and len(values) == len(set(values)) for values in requirements.values()), 'invalid requirements values')
    display = record['display']
    _require(isinstance(display, dict) and set(display) == {'title', 'steps', 'duration_label'}, 'invalid display fields')
    _require(_text(display['title']) and _text(display['duration_label']) and isinstance(display['steps'], list) and len(display['steps']) > 0 and all(_text(s) for s in display['steps']), 'complete text required')
    _require(record['review_evidence'] is None or isinstance(record['review_evidence'], dict), 'invalid review evidence')
    _require(record['media'] is None or isinstance(record['media'], dict), 'media must be an object')
    if record['review_required'] and record['review_status'] == 'approved':
        evidence = record['review_evidence'] or {}
        _require(evidence.get('exercise_id') == eid and evidence.get('content_version') == version, 'review must match exact version')
        _require(all(_text(evidence.get(k)) for k in ('reviewer', 'qualification', 'approved_on', 'reference')), 'qualified review evidence required')
        _require(evidence.get('media_sha256') == (record['media'] or {}).get('sha256') and evidence.get('media_sha256') is not None, 'medical review must cover matching media')
    validate_media(record['media'], eid, version, check_files=check_files, asset_root=asset_root)


def record_payload(content: ContentLibrary) -> dict:
    return deepcopy({'id': content.exercise_id, **{key: getattr(content, key) for key in FIELDS}})


def load_content_library(db: Session, source_path: str | Path = DEFAULT_SEED_PATH, *, asset_root: Path = ROOT, manifest_path: Path = MANIFEST_PATH) -> int:
    """Validate the whole release before writing. Identical versions are no-ops.

    Transaction/commit belongs to the caller. Never updates an existing version.
    """
    data = json.loads(Path(source_path).read_text(encoding='utf-8'))
    _require(set(data) == {'schema_version', 'inventory'} and data['schema_version'] == 1, 'invalid seed schema')
    records = data['inventory']
    _require(isinstance(records, list) and len(records) == 9 and all(isinstance(r, dict) and _text(r.get('id')) for r in records) and {r.get('id') for r in records} == EXERCISE_IDS, 'seed must contain exactly nine independent exercises')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))['assets']
    seen_paths, seen_digests = set(), set()
    pending = []
    for record in records:
        validate_record(record, asset_root=asset_root)
        media = record['media']
        if media:
            _require(media in manifest, 'asset approval/identity absent from manifest')
            _require(media['path'] not in seen_paths and media['sha256'] not in seen_digests, 'shared/generic media rejected')
            seen_paths.add(media['path']); seen_digests.add(media['sha256'])
            shared = db.execute(select(ContentLibrary).where(ContentLibrary.exercise_id != record['id'], ContentLibrary.media.is_not(None))).scalars()
            _require(all(row.media is None or (row.media.get('sha256') != media['sha256'] and row.media.get('path') != media['path']) for row in shared), 'media belongs to another exercise')
        existing = db.get(ContentLibrary, (record['id'], record['content_version']))
        if existing:
            _require(not existing.legacy_record and record_payload(existing) == record, 'content version conflict; publish a new version')
        else:
            pending.append(record)
    for record in pending:
        db.add(ContentLibrary(exercise_id=record['id'], legacy_record=False, **{key: deepcopy(record[key]) for key in FIELDS}))
    db.flush()
    return len(pending)


def is_eligible(content: ContentLibrary, *, check_files: bool = True) -> bool:
    if content.legacy_record or not content.is_active:
        return False
    if content.review_required and content.review_status != 'approved':
        return False
    try:
        validate_record(record_payload(content), check_files=check_files)
    except (ContentValidationError, OSError):
        return False
    return True


def eligible_catalogue(db: Session, mechanic: str | None = None) -> list[dict]:
    """Latest released version per ID; never resurrect an older gated version."""
    latest = select(ContentLibrary.exercise_id, func.max(ContentLibrary.content_version).label('version')).where(ContentLibrary.legacy_record.is_(False)).group_by(ContentLibrary.exercise_id).subquery()
    query = select(ContentLibrary).join(latest, (ContentLibrary.exercise_id == latest.c.exercise_id) & (ContentLibrary.content_version == latest.c.version)).order_by(ContentLibrary.exercise_id)
    if mechanic is not None:
        query = query.where(ContentLibrary.mechanic == mechanic)
    return [record_payload(row) for row in db.execute(query).scalars() if is_eligible(row)]


def selected_content(db: Session, exercise_id: str, version: int, *, lock: bool = False) -> dict:
    """Read an exact already selected protocol with text independent of GIF I/O.

    Activation locks the control fields and verifies packaged media. Presentation
    can retain the text when an approved file later cannot be loaded/delivered.
    """
    query = select(ContentLibrary).where(ContentLibrary.exercise_id == exercise_id, ContentLibrary.content_version == version)
    if lock:
        query = query.with_for_update(read=True)
    content = db.execute(query.execution_options(populate_existing=True)).scalar_one_or_none()
    _require(content is not None and is_eligible(content, check_files=lock), 'selected content is unavailable')
    return record_payload(content)
