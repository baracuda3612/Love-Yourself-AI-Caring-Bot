"""Explicit fresh-contour binding; never copy a testnet database to production."""
import argparse
import json


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--print-resources',action='store_true',help='Print only SHA256 infrastructure/credential fingerprints')
    parser.add_argument('--bind-empty',action='store_true',help='Bind a fresh database without users or access authority and dedicated Redis')
    args=parser.parse_args()
    from app.access_environment import bind_environment,resource_fingerprints
    from app.db import SessionLocal,audit_startup_schema
    if args.print_resources:
        print(json.dumps(resource_fingerprints(),sort_keys=True,indent=2))
    elif args.bind_empty:
        audit_startup_schema()
        with SessionLocal() as db:
            bind_environment(db)
            db.commit()
        print('Fresh environment bound. No account or entitlement created.')
    else:parser.error('Choose --print-resources or --bind-empty')


if __name__=='__main__':main()
