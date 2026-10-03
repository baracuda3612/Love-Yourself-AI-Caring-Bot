"""WP-03.1 exact protocol, asset integrity, and fail-closed payload contracts."""
from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.content_library import (
    DEFAULT_SEED_PATH, ROOT, EXERCISE_IDS, FIELDS, REQUIRED_MEDIA,
    ContentValidationError, gif_metadata, is_eligible, load_content_library,
    selected_content, validate_record,
)
from app.db import ContentLibrary
from app.ux.task_notification import format_task_notification


@pytest.fixture
def records():
    return json.loads(DEFAULT_SEED_PATH.read_text())["inventory"]


def row(record):
    return ContentLibrary(exercise_id=record['id'], legacy_record=False,
        **{key: deepcopy(record[key]) for key in FIELDS})


def test_exact_fd10_copy_and_requirements(records):
    source = (ROOT / 'docs/audit/pre_mvp_code_audit_findings.md').read_text()
    section = source.split('### Accepted exercise catalogue')[1].split('### Target record shape')[0]
    assert len(records) == 9
    assert {r['id'] for r in records} == EXERCISE_IDS
    for record in records:
        line = next(line for line in section.splitlines() if line.startswith('| `'+record['id']+'`') and '**' in line)
        fields = [part.strip() for part in line.strip('|').split('|')]
        copy = fields[1].split('<br>')
        assert record['display']['title'] == copy[0].strip('*')
        assert record['display']['steps'] == copy[1:]
        assert record['duration_seconds'] == int(fields[2].split()[0])
        assert record['mechanic'] == fields[3].strip('`')
        assert record['modality'] == fields[4].strip('`')
        requirements_line = next(line for line in section.split('### Accepted requirements matrix')[1].splitlines() if line.startswith('| `'+record['id']+'`'))
        import re
        assert list(record['requirements'].values()) == [re.findall(r'`([^`]+)`',part) for part in requirements_line.strip('|').split('|')[1:]]
        assert record['content_version'] == 1 and record['cooldown_days'] == 1
        assert record['review_status'] == 'unreviewed'
        assert record['review_required'] == (record['id'] == 'cold_water_face')
        validate_record(record)


def test_eight_eligible_and_five_switch_without_cold_water(records):
    eligible = [r for r in records if is_eligible(row(r))]
    assert len(eligible) == 8
    assert len([r for r in eligible if r['mechanic'] == 'switch']) == 5
    assert 'cold_water_face' not in {r['id'] for r in eligible}
    assert all(is_eligible(row(r)) for r in records if r['media'] is None)


def test_approved_bytes_and_timings(records):
    expected = {
        'pmr_fist': ('afbc74c57134ef46ea46d18c9a163ba1b1fb8ec6447847553ba575e5588a75a9', (640,640,12900)),
        'breathing_sigh': ('2af25310553ca95613538f91d5b74b00fedee4d13c577d7d0124eb47892ffb2f', (320,320,10720)),
        'cold_water_face': ('a808a46206ed10b75558070114a33f2f20ec1aebb0fdb9472025f96824bc12bd', (320,320,6580)),
    }
    for record in records:
        if record['media']:
            data = (ROOT / record['media']['path']).read_bytes()
            assert len(data) < 1_000_000
            assert sha256(data).hexdigest() == expected[record['id']][0]
            assert gif_metadata(data) == expected[record['id']][1]
    assert len({r['media']['sha256'] for r in records if r['media']}) == 3


@pytest.mark.parametrize('change', [
    lambda r: r.update(content_version=0),
    lambda r: r.update(weight=1),
    lambda r: r['display'].update(steps=[]),
    lambda r: r.update(media=None),
    lambda r: r['media'].update(exercise_id='pmr_fist'),
    lambda r: r['media'].update(content_version=2),
    lambda r: r['media'].update(asset_version=0),
    lambda r: r['media'].update(alt_text=''),
    lambda r: r['media'].update(sha256='0'*64),
    lambda r: r['media'].update(path='resource/assets/content_library/media/missing.gif'),
    lambda r: r['media']['approval'].update(status='unapproved'),
])
def test_invalid_protocol_or_asset_rejected_before_writes(records, change, tmp_path):
    change(records[0])
    seed = tmp_path / 'seed.json'
    seed.write_text(json.dumps({'schema_version':1,'inventory':records}))
    db = Mock(); db.get.return_value = None
    with pytest.raises(ContentValidationError):
        load_content_library(db, seed)
    db.add.assert_not_called()
    db.flush.assert_not_called()


def test_medical_review_must_cover_matching_protocol_and_asset(records):
    cold = next(r for r in records if r['id'] == 'cold_water_face')
    cold['review_status'] = 'approved'
    assert not is_eligible(row(cold))
    cold['review_evidence'] = dict(exercise_id='cold_water_face',content_version=1,
        reviewer='disposable test clinician',qualification='disposable test qualification',
        approved_on='2026-10-03',reference='disposable test review',media_sha256=cold['media']['sha256'])
    assert is_eligible(row(cold))
    cold['review_evidence']['content_version'] = 2
    assert not is_eligible(row(cold))
    cold['review_required'] = False
    assert not is_eligible(row(cold))


def test_text_read_and_renderer_survive_gif_io_failure(records, monkeypatch):
    protocol = records[0]
    db = Mock(); db.execute.return_value.scalar_one_or_none.return_value = row(protocol)
    # No asset I/O is required after eligible content has already been selected.
    monkeypatch.setattr('pathlib.Path.read_bytes', lambda self: (_ for _ in ()).throw(OSError('media unavailable')))
    payload = selected_content(db, protocol['id'], 1)
    assert payload['display'] == protocol['display']
    step = SimpleNamespace(exercise_id=protocol['id'], content_version=1,
        content_snapshot=payload, title='unused', time_slot='DAY')
    text = format_task_notification(db, step, None, 1, 1, 1)
    assert payload['display']['title'] in text
    assert all(s in text for s in payload['display']['steps'])
    assert payload['display']['duration_label'] in text
    with pytest.raises(ContentValidationError):
        selected_content(db, protocol['id'], 1, lock=True)


def test_exact_read_refuses_inactive_and_medically_gated(records):
    for protocol in [records[0], next(r for r in records if r['id']=='cold_water_face')]:
        content = row(protocol)
        if protocol['id'] != 'cold_water_face':
            content.is_active = False
        db = Mock(); db.execute.return_value.scalar_one_or_none.return_value = content
        with pytest.raises(ContentValidationError):
            selected_content(db, protocol['id'], 1)


def test_missing_attached_optional_media_is_rejected(records):
    optional=next(r for r in records if r['id']=='tactile_surface')
    optional['media']=deepcopy(records[0]['media'])
    optional['media'].update(exercise_id='tactile_surface',role='illustrative',path='resource/assets/content_library/media/missing.gif')
    optional['media']['approval'].update(exercise_id='tactile_surface')
    with pytest.raises(ContentValidationError,match='missing'):
        validate_record(optional)


def test_new_protocol_cannot_inherit_old_media_approval(records):
    record=records[0]
    record['content_version']=2
    with pytest.raises(ContentValidationError,match='version'):
        validate_record(record)


def test_late_validation_error_leaves_database_unmodified(records,tmp_path):
    records[-1]['display']['steps']=[]
    path=tmp_path/'late-error.json'; path.write_text(json.dumps({'schema_version':1,'inventory':records}))
    db=Mock(); db.get.return_value=None; db.execute.return_value.scalars.return_value=[]
    with pytest.raises(ContentValidationError):
        load_content_library(db,path)
    db.add.assert_not_called(); db.flush.assert_not_called()



def test_outer_media_version_change_does_not_transfer_approval(records):
    record=records[0]
    record['content_version']=2
    record['media']['content_version']=2
    with pytest.raises(ContentValidationError,match='approval must cover'):
        validate_record(record)
