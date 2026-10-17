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

The existing Railway environment retains the legacy dashboard label
`production`. That provider-side display name is non-authoritative and does not
change the contour's testnet classification. Runtime identity comes from the
validated `ENVIRONMENT` and `DEPLOYMENT_ID` values. The future company
production contour is created separately and must not reuse this environment.

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
| Backups | pre-MVP data is disposable; no scheduled backup during the accepted refactoring exception | daily and weekly scheduled backups plus manual pre-change backup |

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

### Accepted pre-MVP exception

On 2026-08-26 the founder accepted complete loss of the current testnet database
during the refactoring phase. It contains no valuable company or user data and
must remain disposable: it is never promoted or copied into production. A
logical dump was created and its archive/checksum validated, but restore was
not proven; this is not recovery evidence and Gate G1 remains open.

This exception permits repository refactoring and founder-only testnet use
without Railway scheduled backups. It does not permit company enrollment,
market launch, or treating testnet data as durable. Before any such use, the
founder must provision the production contour, enable the paid Railway backup
capability, and complete the scratch restore drill above. A fresh backup is
also required before any operation that could destroy non-disposable data.

## WP-04.1 access provisioning (no live rollout)

The access head is `20261015_access_authority`. Existing user rows acquire no
implicit license. Stop the prior binary before migration. This source change
is not permission to enroll company users: Gate G1 and the release gates remain
required. Only newly created disposable databases were used for rehearsals.

Additional typed settings:

| Variable | Purpose |
|---|---|
| `ACCESS_TOKEN_SECRET` | Dedicated random secret, at least 32 characters; never the report/operator/bot/OpenAI secret |
| `ACCESS_KEY_VERSION` | Positive generation, default 1; rotating the secret and incrementing this invalidates all outstanding proof/preview/handoff values |
| `ACCESS_OPERATOR_SECRET` | Separate random founder/operator bearer credential, at least 32 characters; HR never receives it |
| `ACCESS_ISOLATION_INVENTORY` | Restricted JSON inventory below; required outside dev and checked at every runtime start |
| `OPENAI_PROJECT_ID` | Explicit `proj_*` identity, passed to the OpenAI client; separate per environment |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Optional configured pair; authorization-code application; exact registered callback |
| `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET` | Optional configured pair; tenant-specific authorization-code application; exact registered callback |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM` | Verified-email transport; port 587 by default, STARTTLS with certificate verification. Required in production; no delivery retry loop |

Use the same artifact with different configuration. A provider dashboard name
is not runtime identity. Record two profiles, keyed `testnet` and `production`.
Each profile contains `runtime_id`, `resources`, `openai_project`, `backup_scope`,
`alert_route`, `aggregate_sink`. `resources` is the exact SHA256 mapping emitted
by `python -m scripts.access_environment --print-resources` from that contour's
configuration. Only fingerprints are output; do not post them publicly. While
production is not provisioned its profile may be null in testnet. Production
startup requires both profiles and rejects every shared fingerprint or scope ID.
Provider project, backup and alert scope IDs require actual provisioning/release
evidence; a self-declared inventory does not establish that external resources
were provisioned. The application checks configured IDs/fingerprints, explicit
OpenAI project selection, immutable DB binding and dedicated Redis binding.

After reviewed migration on an **empty fresh contour**, set the complete
inventory and run `python -m scripts.access_environment --bind-empty`. This
explicit operation binds PostgreSQL and a dedicated Redis instance. Runtime
startup is read-only and refuses missing/mismatched bindings, cross-environment
deployments and shared resources. A failed bind may leave Redis bound safely;
repair the empty contour explicitly rather than overwriting another identity.
Never rebind/promote testnet data to production. Resource relocation requires a
reviewed provisioning/restore decision; normal secret rotation does not alter
stable infrastructure binding.

Before inserting a deployment, configure its existing orthogonal controls,
commercial dates, notice pin and timezone policy. `access_policy` holds an
explicit `allowed_domains` list (including any approved contractor/personal
mail domains), optional Google Workspace `google_domain`, and/or a Microsoft
`microsoft_tenant` UUID. Email strings/domain membership alone grant nothing.
The restricted access schema and founder operator credential are separate from
behavior/reporting tools. HR submits roster data and receives validation/total
count only; HR must not receive operator APIs, preview secrets, SSO/enrollment
status, token lists or identity-to-activity mappings.

The production employee entry is `/enroll/{deployment_id}`. Register exact
`/enroll/{deployment_id}/oidc/google/callback` and/or
`/enroll/{deployment_id}/oidc/microsoft/callback` HTTPS redirect URIs. Request
only `openid email`. Google must verify Workspace `hd`, verified email and
stable subject; both documented issuer strings are accepted and canonicalized
for subject binding. Microsoft uses validated `sub` and tenant, without an
unused `oid`/`profile` requirement. Its email claim requires mailbox verification
on first binding; an absent claim returns to the existing email verification
form without inferring an address or granting access. Browser-bound state/nonce, PKCE and
single-use proofs prevent login CSRF/replay. Email proofs expire after ten
minutes. Unknown and roster-listed addresses receive identical challenge
messages/responses; only verified current roster membership can grant access.
Expired proof rows older than one day are removed opportunistically on new
proof creation. Raw roster uploads are never persisted.

Operator JSON endpoints require the bearer credential and bounded input:

* `/access/roster/preview`: `deployment_id`, explicit `mode`, `rows` of
  `{email, action}` (`add`/`revoke`), company-declared `expected_total`, and a
  non-sensitive `source_ref`. Full snapshots must be nonempty and match the
  independently declared expected total; this catches unexpectedly small files.
* `/access/roster/apply`: `deployment_id`, `version_id`, returned `confirmation`
  and exact `counts`. The preview expires after 30 minutes; a concurrent accepted
  version invalidates it. No apply occurs on missing/failed/rejected imports.
  Accepted versions retain the effective set and explicit input actions. To
  roll back a roster mistake, prepare/confirm a new import from the prior
  accepted set; never edit audit history or restore raw token values. A manually revoked identity requires an explicitly accepted add in a later
  roster and then re-verification before a new entitlement is granted; unrelated
  deltas cannot restore it.
* `/access/testnet/handoff`: founder-only `deployment_id` and restricted test
  `label`; production is rejected. Raw handoff is returned once, valid ten
  minutes. Reissuance invalidates abandoned unspent links.
* `/access/revoke`: `entitlement_id`; closes sponsored access and outstanding
  invitations, retaining the account/history.
* `/access/deployment/controls`: `deployment_id`, `enrollment_open`,
  `delivery_enabled`. Delivery pause and enrollment closure are independent;
  plan-pause callback actionability remains unchanged.
* `/access/enrollment/replace`: `user_id`, `expected_enrollment_id`, freshly
  issued `handoff` after re-verification, and explicit `replacement_mode`.
  The expected enrollment must still be current for that user. Ordinary
  `same_identity` replacement requires the same access identity and deployment.
  A verified employer/address change requires the explicit `identity_change`
  mode after founder support confirms the intended person and target. The
  operator audit identifies the old enrollment, whose ending reason records
  the operation and target entitlement. The new token remains single-use and
  can bind the intended Telegram account.
  Never copy the old account/history to another account. Employer changes also
  require this explicit identity-change action. A same-identity/same-deployment regrant
  may replace its own revoked enrollment when the same Telegram user redeems.

Roster cadence is not expiry. Overdue collection/reminders belong to the company
operating cadence in WP-10.1; no missed/rejected upload revokes entitlement.
Commercial end is evaluated on every access/send; central renewal extends the
period without SSO/token/user action. Revocation does not delete personal data.

`POST /user/time-slots` requires `X-Telegram-Init-Data` containing the complete
signed WebApp initData from this contour's bot, at most five minutes old. The
server derives the actor, checks ownership/current enrollment/entitlement and
limits attempts to 20/minute per signed actor. `user_id` remains only an asserted
target, and `Idempotency-Key` remains lifecycle replay identity. Invalid targets
and denied access share a neutral response; authorized owners keep the existing
saved/deferred/replayed lifecycle behavior.

All enrollment/access responses carry no-store/no-referrer, CSRF protections
and restrictive framing/content policies. Uvicorn access logging is disabled so
OIDC query codes are not emitted. Before any release, configure the upstream
proxy/APM to exclude enrollment/access query strings, request bodies, cookies,
authorization headers and WebApp payloads. No enrollment page analytics or raw
identity/proof logging is supported. Emergency rotation replaces the dedicated
access secret and advances its generation, restarts the same artifact and
invalidates outstanding links/challenges/previews without revoking entitlements
or enrolled accounts. Rotate operator/SMTP/provider credentials separately if
exposed. Generic monitoring/recovery and company launch operations remain with
their roadmap owners.

Founder testnet must not be configured with OIDC client secrets or SMTP
credentials: initial grants are issued by the founder, and startup rejects
these unused production identity credentials in that contour. Employee-proof
and founder-grant paths converge on the same invitation/enrollment service.


OIDC callback processing consumes state durably, releases authority locks,
performs provider I/O, then revalidates current roster/deployment before grant.
Provider failure requires a new sign-in attempt. Preview identity/entry writes
and apply revocations are bulk operations; no network call runs while holding a
deployment authority lock. Personalized outbound replies/pulses have a final
current-access check after generation. Historical event attribution is captured
at the action/send claim; receipt timestamps remain actual confirmation times.
Old timezone values are retained as unknown legacy data until an explicit user
choice marks the override; they do not silently defeat deployment policy.
