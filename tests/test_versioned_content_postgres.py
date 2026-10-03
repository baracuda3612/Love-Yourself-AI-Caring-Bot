"""Opt-in WP-03.1 rehearsal. Creates and removes its own disposable databases."""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import psycopg2
from psycopg2 import sql
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.content_library import (
    ContentValidationError, DEFAULT_SEED_PATH, ROOT, eligible_catalogue,
    load_content_library, record_payload, selected_content, validate_record,
)
from app.db import AIPlanDay, AIPlanStep, ContentLibrary, User, UserProfile
from app.plan_drafts.plan_builder_v5 import get_default_builder
from app.plan_drafts.service import _persist_v5_draft
from app.plan_finalization import FinalizationError, finalize_plan
from app.lifecycle import _activation_receipt_status
from app.telemetry import write_event_operation
from app.ux.task_notification import format_task_notification
from scripts.test_migrations import _run_alembic

pytestmark = pytest.mark.skipif(os.environ.get('WP03_1_POSTGRES_REHEARSAL') != '1', reason='explicit disposable PostgreSQL rehearsal required')
ADMIN = 'postgresql://love_yourself_test:love_yourself_test@127.0.0.1:55432/love_yourself_test'


@pytest.fixture(scope='module')
def migrated_engine():
    name = 'ly_wp031_' + uuid4().hex
    url = ADMIN.rsplit('/',1)[0]+'/'+name
    admin = psycopg2.connect(ADMIN); admin.autocommit = True
    try:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        _run_alembic(url, '20260827_schema_baseline')
        connection = psycopg2.connect(url)
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO content_library (id,content_version,internal_name,category,difficulty,energy_cost,logic_tags,content_payload,is_active) VALUES ('old-content',1,'old','old',1,'low','{}','{}',true)")
        connection.commit()
        _run_alembic(url, '20260905_event_privacy')
        # The founder-confirmed zero-user cutover refuses an unexpected user.
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO users (tg_id,timezone) VALUES (990031000,'UTC')")
        connection.commit()
        failure = _run_alembic(url, 'head', check=False)
        assert failure.returncode != 0 and 'requires zero users' in failure.stderr
        with connection.cursor() as cursor:
            cursor.execute('SELECT version_num FROM alembic_version')
            assert cursor.fetchone()[0] == '20260905_event_privacy'
            cursor.execute("SELECT id FROM content_library")
            assert cursor.fetchall() == [('old-content',)]
            cursor.execute("DELETE FROM users WHERE tg_id=990031000")
        connection.commit()
        _run_alembic(url, 'head'); _run_alembic(url, 'head')
        with connection.cursor() as cursor:
            cursor.execute('SELECT count(*) FROM content_library')
            assert cursor.fetchone()[0] == 0
            cursor.execute("SELECT to_regclass('public.legacy_content_library')")
            assert cursor.fetchone()[0] is None
        connection.close()
        engine = create_engine(url)
        with Session(engine) as db:
            assert load_content_library(db) == 9
            db.commit()
        yield engine
        engine.dispose()
    finally:
        with admin.cursor() as cursor:
            cursor.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()', (name,))
            cursor.execute(sql.SQL('DROP DATABASE IF EXISTS {}').format(sql.Identifier(name)))
        admin.close()


@pytest.fixture
def db(migrated_engine):
    connection = migrated_engine.connect(); tx = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close(); tx.rollback(); connection.close()


def test_repeat_seed_conflicts_append_and_latest_gating(db, tmp_path):
    assert load_content_library(db) == 0
    records = json.loads(DEFAULT_SEED_PATH.read_text())
    records['inventory'][2]['display']['steps'][0] += ' changed'
    path=tmp_path/'conflict.json'; path.write_text(json.dumps(records))
    with pytest.raises(ContentValidationError, match='conflict'):
        load_content_library(db,path)
    assert len(eligible_catalogue(db)) == 8
    records['inventory'][2]['content_version'] = 2
    path.write_text(json.dumps(records))
    assert load_content_library(db,path) == 1
    assert load_content_library(db,path) == 0
    assert db.get(ContentLibrary,('tactile_surface',1)).display['steps'][0] != db.get(ContentLibrary,('tactile_surface',2)).display['steps'][0]
    assert next(r for r in eligible_catalogue(db) if r['id']=='tactile_surface')['content_version'] == 2
    db.get(ContentLibrary,('tactile_surface',2)).is_active = False; db.flush()
    assert 'tactile_surface' not in {r['id'] for r in eligible_catalogue(db)}
    assert selected_content(db,'tactile_surface',1)['content_version'] == 1


def make_draft(db):
    user=User(tg_id=991031001,timezone='Europe/Kyiv',is_active=True)
    db.add(user); db.flush()
    db.add(UserProfile(user_id=user.id,daily_time_slots={'DAY':'14:00'}, active_days=['MON','TUE','WED','THU','FRI']))
    db.flush()
    draft=get_default_builder(db).build('SHORT',user_id=str(user.id),day_time='14:00')
    assert all(s.content_version==1 and s.exercise_id!='cold_water_face' for s in draft.steps)
    record=_persist_v5_draft(db,user.id,draft); db.flush()
    return user,record


def activate(db,user,draft):
    return finalize_plan(db,user.id,draft,
        activation_time_utc=datetime(2026,10,3,8,tzinfo=timezone.utc),
        source_operation_id='wp031:test-activation',
        activation_receipt_status=_activation_receipt_status('SHORT','14:00',None))


def test_builder_activation_renderer_and_event_use_exact_version(db):
    user,draft=make_draft(db); result=activate(db,user,draft)
    step=db.execute(select(AIPlanStep).join(AIPlanDay).where(AIPlanDay.plan_id==result.plan.id).order_by(AIPlanStep.id)).scalars().first()
    before=deepcopy(step.content_snapshot)
    assert step.title == before['display']['title']
    assert step.description == '\n'.join(before['display']['steps'])
    # A future release must not change the old plan or event content identity.
    content=db.get(ContentLibrary,(step.exercise_id,1))
    values=record_payload(content); values['content_version']=2
    if values['media']:
        values['media']['content_version']=2
    values['display']['title']='new version'
    db.add(ContentLibrary(exercise_id=values.pop('id'),  **values)); db.flush()
    event=write_event_operation(db,user_id=user.id,event_name='task_completed',event_source='wp031',source_operation_id='wp031:event',plan_step_id=step.id,properties={'day_number':1})
    assert event.event.content_version==1 and event.event.exercise_id==step.exercise_id
    message=format_task_notification(db,step,None,1,1,1)
    assert before['display']['title'] in message and 'new version' not in message
    assert all(s in message for s in before['display']['steps'])
    content.is_active=False; db.flush(); db.refresh(step)
    assert step.content_snapshot==before
    with pytest.raises(ContentValidationError):
        format_task_notification(db,step,None,1,1,1)
    with pytest.raises(DBAPIError,match='immutable'):
        # Canonical events cannot be relinked to newer content.
        with db.begin_nested():
            db.execute(text("UPDATE user_events SET content_version=2 WHERE event_id=:id"),{'id':event.event.event_id})


def test_activation_rechecks_gate(db):
    user,draft=make_draft(db)
    selected=draft.steps[0]
    db.get(ContentLibrary,(selected.exercise_id,selected.content_version)).is_active=False; db.flush()
    with pytest.raises(FinalizationError,match='draft_content_unavailable'):
        activate(db,user,draft)


def test_db_rejects_in_place_content_and_referenced_deletion(db):
    for query in [
        "UPDATE content_library SET display='{}' WHERE exercise_id='tactile_surface'",
        "UPDATE content_library SET duration_seconds=21 WHERE exercise_id='tactile_surface'",
        "UPDATE content_library SET media='{}' WHERE exercise_id='breathing_sigh'",
        "DELETE FROM content_library WHERE exercise_id='tactile_surface'",
    ]:
        with pytest.raises(DBAPIError):
            with db.begin_nested():
                db.execute(text(query))
    assert db.get(ContentLibrary,('tactile_surface',1)).duration_seconds==20


def test_downgrade_is_refused_without_losing_versions(migrated_engine):
    result=subprocess.run([sys.executable,'-m','alembic','downgrade','20260905_event_privacy'],
        env={**os.environ,'DATABASE_URL':migrated_engine.url.render_as_string(hide_password=False)},capture_output=True,text=True)
    assert result.returncode!=0 and 'forward repair' in result.stderr
    with migrated_engine.connect() as connection:
        assert connection.execute(text('SELECT version_num FROM alembic_version')).scalar_one()=='20261003_content_library'
        assert connection.execute(text('SELECT count(*) FROM content_library')).scalar_one()==9


def test_medical_gate_approves_only_matching_version(db):
    cold=db.get(ContentLibrary,('cold_water_face',1))
    cold.review_status='approved'
    cold.review_evidence=dict(exercise_id='cold_water_face',content_version=2,
        reviewer='disposable test clinician',qualification='disposable test qualification',
        approved_on='2026-10-03',reference='disposable test review',media_sha256=cold.media['sha256'])
    db.flush()
    assert 'cold_water_face' not in {r['id'] for r in eligible_catalogue(db)}
    cold.review_evidence={**cold.review_evidence,'content_version':1}; db.flush()
    assert len(eligible_catalogue(db))==9
    assert selected_content(db,'cold_water_face',1)['review_status']=='approved'
    future=record_payload(cold)
    future.update(content_version=2,review_status='unreviewed',review_evidence=None)
    # Keep deliberately stale media-version evidence: v2 cannot inherit v1 approval.
    db.add(ContentLibrary(exercise_id=future.pop('id'),**future)); db.flush()
    assert 'cold_water_face' not in {r['id'] for r in eligible_catalogue(db)}
    with pytest.raises(ContentValidationError):
        selected_content(db,'cold_water_face',2)


def test_snapshot_mutation_and_partial_identity_are_rejected(db):
    user,draft=make_draft(db); result=activate(db,user,draft)
    step=db.execute(select(AIPlanStep).join(AIPlanDay).where(AIPlanDay.plan_id==result.plan.id)).scalars().first()
    for query in [
        'UPDATE ai_plan_steps SET content_version=2 WHERE id=:id',
        "UPDATE ai_plan_steps SET content_snapshot='{}' WHERE id=:id",
        "UPDATE ai_plan_steps SET description='changed' WHERE id=:id",
    ]:
        with pytest.raises(DBAPIError,match='snapshot is immutable'):
            with db.begin_nested():
                db.execute(text(query),{'id':step.id})
    with pytest.raises(DBAPIError):
        with db.begin_nested():
            db.execute(text("INSERT INTO ai_plan_steps (day_id,title,order_in_day,exercise_id,content_version,mechanic,step_status,version) VALUES (:day,'partial',9,'tactile_surface',NULL,'switch','pending',1)"),{'day':step.day_id})



def test_pause_resume_and_repeat_seed_keep_new_plan_snapshots(db):
    user,draft=make_draft(db); result=activate(db,user,draft)
    steps=db.execute(select(AIPlanStep).join(AIPlanDay).where(AIPlanDay.plan_id==result.plan.id).order_by(AIPlanStep.id)).scalars().all()
    before=[(s.id,s.exercise_id,s.content_version,deepcopy(s.content_snapshot)) for s in steps]
    from app.lifecycle import transition_current_plan
    assert transition_current_plan(db,user_id=user.id,operation='pause',source_operation_id='wp031:pause').status=='paused'
    assert transition_current_plan(db,user_id=user.id,operation='resume',source_operation_id='wp031:resume',occurred_at=datetime(2026,10,4,8,tzinfo=timezone.utc)).status=='active'
    assert load_content_library(db)==0
    db.flush()
    assert [(s.id,s.exercise_id,s.content_version,s.content_snapshot) for s in steps]==before


@pytest.mark.parametrize('same_bytes,same_path', [(True,True),(True,False),(False,True),(False,False)])
def test_released_media_paths_preserve_prior_versions(db,tmp_path,same_bytes,same_path):
    seed=json.loads(DEFAULT_SEED_PATH.read_text())
    manifest=json.loads((ROOT/'resource/assets/content_library/media/manifest.json').read_text())
    # Work only on disposable fixture copies; approved repository bytes are untouched.
    for asset in manifest['assets']:
        target=tmp_path/asset['path']; target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes((ROOT/asset['path']).read_bytes())
    future=seed['inventory'][0]
    old_path=future['media']['path']
    original=(ROOT/old_path).read_bytes()
    future['content_version']=2
    future['display']['title']+=' v2'
    media=future['media']; media['content_version']=2
    if not same_path:
        media['path']=old_path.replace('.gif','.v2.gif')
    # A trailing fixture byte changes the digest without touching approved files.
    replacement=original if same_bytes else original+b'\x00'
    (tmp_path/media['path']).write_bytes(replacement)
    media.update(sha256=sha256(replacement).hexdigest(),asset_version=2,revision='disposable-fixture-v2')
    media['approval'].update(content_version=2,asset_version=2,sha256=media['sha256'],evidence='disposable fixture approval')
    manifest['assets'].append(deepcopy(media))
    seed_path=tmp_path/'seed.json'; seed_path.write_text(json.dumps(seed))
    manifest_path=tmp_path/'manifest.json'; manifest_path.write_text(json.dumps(manifest))
    if same_path and not same_bytes:
        with pytest.raises(ContentValidationError,match='publish a new asset path'):
            load_content_library(db,seed_path,asset_root=tmp_path,manifest_path=manifest_path)
        assert db.get(ContentLibrary,('breathing_sigh',2)) is None
        assert db.query(ContentLibrary).count()==9
    else:
        assert load_content_library(db,seed_path,asset_root=tmp_path,manifest_path=manifest_path)==1
        assert load_content_library(db,seed_path,asset_root=tmp_path,manifest_path=manifest_path)==0
        validate_record(record_payload(db.get(ContentLibrary,('breathing_sigh',1))),asset_root=tmp_path)
        validate_record(record_payload(db.get(ContentLibrary,('breathing_sigh',2))),asset_root=tmp_path)
        # Even after a replacement, another exercise cannot claim the old asset.
        optional=seed['inventory'][2]
        optional['content_version']=2
        optional['media']=deepcopy(manifest['assets'][0])
        optional['media'].update(exercise_id='tactile_surface',content_version=2,role='illustrative')
        optional['media']['approval'].update(exercise_id='tactile_surface',content_version=2)
        manifest['assets'].append(deepcopy(optional['media']))
        seed_path.write_text(json.dumps(seed)); manifest_path.write_text(json.dumps(manifest))
        with pytest.raises(ContentValidationError,match='shared/generic|another exercise'):
            load_content_library(db,seed_path,asset_root=tmp_path,manifest_path=manifest_path)
        assert db.get(ContentLibrary,('tactile_surface',2)) is None
    assert (ROOT/old_path).read_bytes()==original
    assert selected_content(db,'breathing_sigh',1,lock=True)['media']['sha256']==sha256(original).hexdigest()
