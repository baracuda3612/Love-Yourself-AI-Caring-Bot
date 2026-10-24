"""Local restricted operator interface. Never expose exports through public HTTP."""
import argparse
import asyncio
import json
import os
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operator',required=True,help='Assigned support owner reference')
    sub=parser.add_subparsers(dest='action',required=True)
    notice=sub.add_parser('publish-notice')
    notice.add_argument('--version',required=True);notice.add_argument('--file',required=True)
    notice.add_argument('--approved-artifact-ref',required=True)
    pin=sub.add_parser('pin-notice');pin.add_argument('--deployment',type=int,required=True);pin.add_argument('--notice',type=int,required=True)
    queue=sub.add_parser('queue');queue.add_argument('--after',help='next_after cursor from the previous page')
    status=sub.add_parser('status');status.add_argument('request')
    intake=sub.add_parser('receive-email');intake.add_argument('--kind',required=True);intake.add_argument('--received-at',required=True)
    export=sub.add_parser('export');export.add_argument('request');export.add_argument('--output',required=True)
    correction=sub.add_parser('correct');correction.add_argument('request');correction.add_argument('--changes-file',required=True)
    complete=sub.add_parser('complete');complete.add_argument('request');complete.add_argument('--evidence',required=True)
    deletion=sub.add_parser('delete');deletion.add_argument('request')
    sub.add_parser('retention')
    cache=sub.add_parser('retire-legacy-cache');cache.add_argument('--cursor',type=int,default=0)
    args=parser.parse_args()
    from app.db import SessionLocal,audit_startup_schema,Deployment,PrivacyRequest,PrivacyNoticeVersion
    from app import privacy
    audit_startup_schema()
    if args.action == 'retire-legacy-cache':
        async def retire():
            from app.session_memory import SessionMemory
            cache=SessionMemory().redis
            if cache is None:raise RuntimeError('cache_unavailable')
            cursor,keys=await cache.scan(args.cursor,match='session:*',count=500)
            keys=[k for k in keys if k.endswith((':messages',':pending_action'))]
            if keys:await cache.delete(*keys)
            print(json.dumps({'next_cursor':cursor,'removed':len(keys)}))
        asyncio.run(retire());return
    with SessionLocal() as db:
        if args.action == 'publish-notice':
            row=privacy.register_notice(db,version=args.version,notice_text=Path(args.file).read_text(),location=args.approved_artifact_ref)
            db.commit();print(row.id);return
        if args.action == 'pin-notice':
            from app.access import lock_deployment
            target=lock_deployment(db,args.deployment,require_active_period=False)
            notice=db.get(PrivacyNoticeVersion,args.notice)
            if not notice or not notice.notice_text or notice.retired_at:raise ValueError('notice_unavailable')
            target.notice_version_id=notice.id;db.commit();return
        if args.action == 'receive-email':
            from datetime import datetime
            row=privacy.record_manual_request(db,kind=args.kind,received_at=datetime.fromisoformat(args.received_at))
            db.commit();print(row.id);return
        if args.action == 'queue':
            rows,next_after=privacy.pending_request_page(db,after=args.after)
            print(json.dumps({'requests':[{'case':str(r.id),'kind':r.kind,'state':r.state,'owner':r.owner,'due':r.due_at.isoformat(),'overdue':r.due_at<privacy.utcnow(),'verified':r.verified_at is not None} for r in rows],
                'next_after':next_after}));return
        if args.action == 'retention':
            count=privacy.purge_retained_data(db);db.commit();print(count);return
        if args.action == 'status':
            from uuid import UUID
            row=db.get(PrivacyRequest,UUID(args.request))
            if not row or row.owner!=args.operator:raise ValueError('request_unavailable')
            print(json.dumps({'case':str(row.id),'kind':row.kind,'state':row.state,'due':row.due_at.isoformat(),
                'verified':row.verified_at is not None,'completed':str(row.completed_at),'evidence':row.evidence}));return
        row=privacy.owned_request(db,args.request)
        if row.owner!=args.operator:raise ValueError('assigned_owner_required')
        if args.action == 'export':
            payload=privacy.export_request(db,args.request)
            # Refuse overwrite and require owner-only permissions from creation.
            fd=os.open(args.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as file:json.dump(payload,file,default=str,ensure_ascii=False,sort_keys=True)
            db.commit()
        elif args.action == 'correct':
            privacy.correct_request(db,args.request,json.loads(Path(args.changes_file).read_text()));db.commit()
        elif args.action == 'complete':
            privacy.complete_request(db,args.request,evidence_code=args.evidence);db.commit()
        elif args.action == 'delete':
            # Production bot owns a live scheduler. Offline operator marks the
            # durable fence; worker resumes external erasure in that process.
            privacy.begin_account_deletion(db,args.request);db.commit()
            print('Deletion fenced; live privacy worker will erase stores. Check queue, never claim completion yet.')


if __name__=='__main__':
    from app.logging.privacy_logging import install_privacy_logging
    install_privacy_logging()
    try:
        main()
    except Exception as error:
        # Operator stdout/stderr must not become a second personal-data store.
        import sys
        print('privacy_operation_failed: '+type(error).__name__,file=sys.stderr)
        raise SystemExit(1) from None
