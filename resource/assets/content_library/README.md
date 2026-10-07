# Versioned Content Library — WP-03.1

The database `content_library` is the runtime release authority. The JSON in
`tasks/` is a reviewed seed, not a second current catalogue. Nine independent
version-1 exercises preserve the exact FD-10 Ukrainian copy and requirements.

`media/manifest.json` records the three exercise-specific founder approvals
(2026-10-01), exact version, revision, SHA-256, descriptive alt text, dimensions,
and demonstration loop duration. The GIF files are packaged unchanged:

| Exercise | Bytes | Dimensions | Loop |
|---|---:|---|---:|
| pmr_fist | 570124 | 640×640 | 12900 ms |
| breathing_sigh | 568010 | 320×320 | 10720 ms |
| cold_water_face | 228777 | 320×320 | 6580 ms |

The 2026-10-01 founder decision supersedes the 2026-09-27 nine-GIF requirement.
The other six records intentionally contain `media: null`. DG-08 visual approval
is closed. DG-02 remains open: cold water cannot be selected or shown until a
qualified review covers the exact content version and matching GIF. Clinical approval dates are optional audit metadata; qualified
approval of the exact version remains required. Initial eligible pool: eight total, five `switch`. No skip-based inferred profile.

Loader validates the whole release before adding rows. Repeating an identical
version is a no-op; changes to the same version conflict. Instructions, duration,
requirements, identity and media are immutable; publish a new content version
and obtain its own matching media/review evidence. Review/activation controls
may change, without changing historical plan snapshots.

A media path already referenced by a released version can be reused only for
identical bytes of the same exercise. Replacement bytes need a distinct asset
filename/path and matching manifest approval; retain the prior files and add
new approved paths explicitly to the Docker allowlist. Other exercises cannot
share that path or digest. This applies only to attached media: the six text-only
exercises still require no GIF.

Builder calls `eligible_catalogue(db)` (latest version per ID); activation locks
and rechecks `selected_content(db, id, version, lock=True)`. Renderer resolves
that same exact version with full text. Builder, activation and presentation
construction never read GIF bytes or verify file hashes; those checks occur
only in the release loader. Runtime rejects inactive and medically gated
content. WP-03.1's narrow adapter retained the existing notification layout;
ExercisePresentation, sendAnimation, definite media-failure fallback and the
observed send variant belong to WP-03.3. The delivery adapter reads the
packaged GIF to upload it, without repeating the
release loader's hash verification. WP-03.4 persists the successful variant and
payload in `exercise_deliveries` and reconciles uncertain send outcomes.

## Rollout and rollback

1. This package rehearses disposable PostgreSQL only. Before any durable founder
   or employee database: Gate G1 fresh backup and verified scratch restore.
2. Confirm the founder-approved zero-user cutover (2026-10-03). Migration refuses
   a populated users table; old unreferenced content is discarded without an
   archive or old-plan backfill. New published versions/snapshots remain immutable.
3. Stop old writers/schedulers. Last safe application rollback point is the
   pre-WP-03.1 schema (`20260905_event_privacy`) before this forward migration.
4. Run `.venv/bin/python -m alembic upgrade head`, then
   `.venv/bin/python -m scripts.load_content_library` using the intended DB URL.
   Deploy the matching application and assets; startup checks the exact revision.
5. The founder manages catalogue changes manually. Automatic queued-message
   revocation, send-time eligibility rechecks and plan rebuilding are deferred.
6. Downgrade is explicitly refused: it could erase new releases or history.
   Use a verified pre-rollout restore (coordinate all writes) or forward repair.
   A failed migration is transactional; repair its invalid input, then retry.

Acceptance: `tests/test_versioned_content_library.py`, builder tests,
`WP03_1_POSTGRES_REHEARSAL=1 .venv/bin/python -m pytest -q
 tests/test_versioned_content_postgres.py`, and `scripts.test_migrations`.
Historical decisions: [FD-10 / FD-16](../../../docs/audit/pre_mvp_code_audit_findings.md)
and [WP-03.1](../../../docs/implementation/work_packages/WP-03.1_versioned_content_library.md).
