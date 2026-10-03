"""Load the reviewed release seed. Durable rollout requires Gate G1 first."""
import argparse
from pathlib import Path

from app.content_library import DEFAULT_SEED_PATH, load_content_library
from app.db import SessionLocal, audit_startup_schema


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=DEFAULT_SEED_PATH)
    args = parser.parse_args()
    audit_startup_schema()
    with SessionLocal.begin() as db:
        count = load_content_library(db, args.source)
    print(f'Published {count} new content versions.')


if __name__ == '__main__':
    main()
