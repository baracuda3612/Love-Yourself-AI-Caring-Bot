# Database migration and schema-authority runbook

This runbook defines the WP-01.1/WP-01.4 Alembic authority boundary. It does not turn
the disposable founder testnet into production and does not satisfy Gate G1.

## Ownership

Alembic owns the 33 application tables represented by revision
`20260905_event_privacy`. The physical baseline deliberately preserves the
inspected starting schema, including legacy columns, duplicate indexes, and
unused enum types. Target cleanup belongs to the packages that define the new
schema and must arrive as reviewed forward revisions.

`apscheduler_jobs` is not an application table. APScheduler's
`SQLAlchemyJobStore` owns its table, index, serialization format, and lifecycle.
No Alembic revision may create, alter, backfill, or drop it.

## Content-free inspection

`scripts/inspect_database_schema.py` opens a database-level read-only
transaction with short statement and lock timeouts. It exports table/column
metadata, enums, constraints, indexes, null counts, row counts, selected
lifecycle-value counts, and scheduler-job aggregates. It never selects user
identifiers, messages, profile fields, notes, facts, event context, job payloads,
or other row content.

Use a chmod-600 env file outside the repository:

```bash
.venv/bin/python scripts/inspect_database_schema.py \
  --database-url-file /private/tmp/ly-wp01-db.env \
  --output /private/tmp/ly-wp01-inventory.json
```

Delete the credential file immediately after the inspection. The sanitized
inventory may be retained as package evidence; the connection value may not.

## Existing-database adoption

The baseline creates the inspected application schema on an empty PostgreSQL
database. An existing database must not run that create migration. After its
physical schema has been compared successfully with the reviewed baseline,
adopt it once with:

```bash
.venv/bin/python -m alembic -c alembic.ini stamp 20260827_schema_baseline
```

Stamping writes only the Alembic ledger; it does not repair or reinterpret the
schema. Do not stamp a database that fails the physical comparison. For any
non-disposable database, Gate G1 backup/restore evidence is additionally
mandatory before adoption or migration.

## Forward migration command

All forward schema changes use one command:

```bash
make migrate
```

This resolves to `alembic upgrade head`. Application startup never invokes it.
Startup performs only a read-only `alembic_version` check and fails closed when
the ledger is absent, branched, behind, or ahead of the revision required by the
running code.

Rehearse the complete ledger on an isolated disposable PostgreSQL database:

```bash
make services-up
make test-migrations
make services-down
```

The harness creates uniquely named temporary databases. It upgrades a seeded
legacy schema, verifies backfill and mirror retirement, repeats `upgrade head`
for idempotent resume, and separately proves that ambiguous current plans abort
transactionally and can resume after evidence-based remediation. It also
verifies application-table/enum fingerprints and that `apscheduler_jobs` was
not created, then drops only those temporary databases.

## WP-01.3 cutover boundary

Do not run old and new lifecycle writers concurrently. Stop every old polling,
worker, and scheduler process before `make migrate`; then start only the build
whose required revision is `20260902_plan_lifecycle`. The migration uses legacy
columns as evidence, writes the normalized plan/step facts, and clears the
mirrors. An old binary is intentionally unable to restart because its exact
revision check will fail. Recovery is a reviewed database restore or forward
fix, never an unstamped downgrade or an old-binary restart.

## WP-01.4 cutover boundary

The event/privacy/deployment revision uses another stop-the-world writer
switch. Stop the WP-01.3 application, poller, workers, and scheduler before
`make migrate`; then start only the build whose required revision is
`20260905_event_privacy`. Legacy event columns and detached telemetry tables
remain evidence-only, while new writes use the canonical event operation. The
full storage/removal map is
`docs/implementation/wp_01_4_event_compatibility_manifest.md`.

The revision may be rehearsed only on local disposable/restored PostgreSQL.
Do not launch the new build against the old Railway schema, and do not apply
this package to Railway as part of repository verification.

## Restore and scheduler reconciliation

For the future Gate G1 restore drill:

1. Keep the restored application and bot stopped.
2. Record the restored Alembic revision and compare application schema and
   representative aggregate counts read-only.
3. Clear `apscheduler_jobs` only in the restored copy, outside Alembic.
4. Reconcile restored plan/day/step state and recreate only valid future jobs
   through the reviewed scheduler path.
5. Start exactly one polling owner and one scheduler writer.
6. Verify that no past, canceled, completed, expired, skipped, or delivered
   step is sent.
7. Record backup timestamp, observed RPO/RTO, source/target service identifiers,
   result, and cleanup decision without credentials or personal content.

RPO/RTO are intentionally not measured for the disposable founder testnet.
They remain required Gate G1 evidence before company enrollment, durable data,
production migration, or market launch.
