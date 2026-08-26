# Railway configuration and topology contract

This document is the non-secret operational contract for Love Yourself. It
describes the target topology; it does not prove that an inactive Railway
project currently matches it.

## Release contract

Docker is the eventual single release artifact. Testnet and production must run
the same reviewed image digest; environment variables are the only intended
deployment differences. The current `Dockerfile` command is authoritative for
container startup. The same command is duplicated in `Procfile`; removing that
competing declaration, adding the final Railway release manifest, health check,
drain window, restart policy, and pre-deploy migration command belong to the B9
release package after those runtime contracts exist.

Until that release work is complete, neither environment is release-ready and
production must not auto-deploy a Git branch. The testnet branch trigger, if
used during implementation, may watch only founder-merged
`implementation/pre-mvp`; its actual dashboard state must be recorded before
activation. Production promotion remains manual and digest-based.

The beta application topology is exactly one replica, one Telegram polling
owner, and one scheduler writer. Scaling above one replica is prohibited until
polling, Coach serialization, and scheduler ownership are distributed safely.

## Environment isolation

`staging` is the isolated founder testnet. `prod` is company production.

Provisioning is phased. During pre-MVP development only one runtime contour is
active: the existing Railway contour is classified as testnet with
`ENVIRONMENT=staging` and `DEPLOYMENT_ID=love-yourself-testnet`. The production
topology remains defined here but does not need to be provisioned or billed
until preparation for the first company deployment. It must exist and pass the
production gate before any company user is enrolled.

The testnet contour is never promoted in place: its bot, PostgreSQL data,
Redis, OpenAI project, token secrets, URLs, and aggregates are not renamed,
copied, or reused as production. Production is created as a clean isolated
contour from the reviewed release artifact.

| Surface | Testnet (`staging`) | Production (`prod`) |
|---|---|---|
| Railway boundary | dedicated project or fully isolated environment | dedicated project or fully isolated environment |
| Application service | one replica; testnet URL | one replica; production URL |
| Telegram | dedicated test bot and token | dedicated production bot and token |
| PostgreSQL | dedicated database/volume; pre-MVP/test data only, never promoted | dedicated database/volume; company data only |
| Redis | dedicated instance and namespace | dedicated instance and namespace |
| OpenAI | dedicated project, key, budget, and alerts | dedicated project, key, budget, and alerts |
| Token signing | dedicated report/enrollment secrets and namespace | dedicated report/enrollment secrets and namespace |
| Telemetry and aggregates | testnet-only sinks marked by immutable deployment ID | production-only sinks marked by immutable deployment ID |
| Alerts | founder testnet channel/routing | founder production channel/routing |
| Backups | restore/scratch drills; no production data copied in | daily and weekly scheduled backups plus manual pre-change backup |

No credential, database, Redis instance, bot identity, token namespace, URL,
alert route, or aggregate sink may be shared across these environments. Testnet
data is never promoted or copied into production. The application validates an
immutable `DEPLOYMENT_ID`; persistence and telemetry must carry it when those
schemas are implemented by their owning packages.

## Railway variable inventory

Values live only in the relevant Railway environment. Secret values must never
appear in Git, image layers, build arguments, logs, screenshots, PRs, or this
document.

| Variable | Secret | Dev | Staging | Prod | Source / rule |
|---|---:|---:|---:|---:|---|
| `ENVIRONMENT` | no | required | required | required | exactly `dev`, `staging`, or `prod` |
| `DEPLOYMENT_ID` | no | optional | required | required | immutable and unique per environment |
| `BOT_TOKEN` | yes | required | required | required | dedicated BotFather bot per deployed environment |
| `OPENAI_API_KEY` | yes | required | required | required | dedicated OpenAI project per deployed environment |
| `DATABASE_URL` | yes | required | required | required | injected/referenced from the environment's PostgreSQL service |
| `REDIS_URL` | yes | optional | required | required | injected/referenced from the environment's Redis service |
| `REPORT_TOKEN_SECRET` | yes | optional | required | required | random, environment-specific, at least 32 characters |
| `APP_BASE_URL` | no | optional | required | required | public HTTPS base URL outside development |
| `BOT_USERNAME` | no | optional | required | required | username of the matching environment bot |
| `PORT` | no | optional | injected | injected | Railway-provided listener port; local default is 8000 |
| `ADMIN_IDS` | sensitive | optional | optional | optional | comma-separated positive Telegram IDs |
| `TZ` | no | optional | optional | optional | default `Europe/Kyiv` |
| `MODEL` | no | optional | optional | optional | general model selection |
| `PLAN_MODEL` | no | optional | optional | optional | plan generation model |
| `COACH_MODEL` | no | optional | optional | optional | Coach model |
| `MAX_TOKENS` | no | optional | optional | optional | integer 1–32768; default 300 |
| `TEMPERATURE` | no | optional | optional | optional | number 0–2; default 0.7 |

Future enrollment, telemetry, and aggregate variables must be added here and
to the typed settings contract by the package that introduces their runtime
consumer. Unsupported aliases are not accepted silently.

## Credential disposition

Repository history contains an old non-placeholder Telegram bot token and
OpenAI API key. Both are compromised regardless of whether the repository was
private and must be revoked/rotated before any bot or environment is restarted.
The current tree does not contain `.env`, and `.gitignore` excludes environment
overrides. History rewriting is optional defense-in-depth after rotation; it
does not make an exposed credential safe again.

Rotation evidence records provider, environment, actor, and completion time,
never the old or new value. A new key must be stored directly in the intended
Railway environment and local secret store. It must not pass through a tracked
file.

## Backup and scratch-restore prerequisite

While production data exists, its PostgreSQL volume has daily and weekly
scheduled backups enabled. Before every migration, backfill, cleanup, or schema
drop, the founder creates a fresh manual backup. Backup availability is not
treated as recovery evidence until a fresh backup is restored to a separate,
non-public scratch service.

Gate G1 requires this drill:

1. Keep the application and bot stopped in the scratch environment.
2. Restore the selected production backup to a separate PostgreSQL service;
   never overwrite the source volume.
3. Use read-only access to record schema version, required tables, and
   representative row counts without exporting personal content;
4. clear restored `apscheduler_jobs`, reconcile plan/step state, and recreate
   only valid future jobs before any runtime start;
5. start exactly one runtime owner only after reconciliation, then verify no
   past, canceled, completed, expired, or already-delivered task is sent;
6. record observed RPO, RTO, backup timestamp, source/target service IDs, result,
   and cleanup decision without credentials or user data.

Railway billing does not need to be active for repository hardening. It must be
active before scheduled backups can be enabled and the scratch restore can be
created and verified. Those external actions require founder confirmation and
must be performed only after compromised credentials are rotated.
