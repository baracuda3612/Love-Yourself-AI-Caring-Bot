# Reviewed exercise seed

`burnout_combined_content_library.json` contains exactly nine independent
version-1 records from FD-10. Load it via `scripts.load_content_library` after
the matching Alembic migration. Runtime builder/renderer read the database.

Same version + same payload is idempotent; any same-version difference is a
conflict. No parent/variation mapping, overwrite, or media placeholder is allowed.
See the parent README for the three-GIF override, approval links, rollout and
safe rollback boundary.
