# LY Workday — Pre-MVP Implementation Roadmap

## Document status

**Status:** APPROVED / ACTIVE IMPLEMENTATION BASELINE  
**Version:** 1.0  
**Created:** 2026-08-20  
**Dependency review:** completed 2026-08-20  
**Founder approval:** 2026-08-20  
**Planned implementation start:** Saturday, 2026-08-22  
**Owner and product authority:** Founder, Love Yourself  
**Implementation model:** founder-directed, AI-assisted solo development  
**Target:** a verified LY Workday external-beta release and a controlled path
to the first company production deployment

This roadmap converts the approved audit baseline into an executable,
dependency-based implementation program. It is not a second audit, a Jira
backlog, a calendar promise, or permission to implement every idea mentioned in
historical product material.

Canonical input:

* `docs/audit/pre_mvp_code_audit_findings.md`, version 1.0, closed 2026-08-20;
* audit commit `c2a380a70be2f74cb5cbfe8a59ef61de76561d76`;
* 19 accepted Founder Decisions;
* 216 unique numbered findings across 19 finding areas;
* `docs/audit/product_contract.md` and
  `docs/audit/delivery_contract.md`, subject to the authority order recorded in
  the audit.

The roadmap may group and sequence findings. It may not weaken an accepted
product boundary or mark a finding resolved without proportionate code,
migration, test, and operational evidence.

---

## 1. Program objective

Move the repository from the closed pre-MVP audit baseline to one coherent,
beta-ready system:

```text
approved audit
→ reproducible development and infrastructure baseline
→ authoritative data and migration model
→ target lifecycle and removal of rejected legacy behavior
→ canonical content, presentation, and scheduled delivery
→ deterministic access, onboarding, controls, and Coach
→ independent exercise-on-demand channel
→ truthful telemetry, privacy, and reporting
→ hardening and full-system verification
→ isolated testnet acceptance
→ controlled production release
```

The dominant implementation strategy is **remove, consolidate, migrate, and
verify**. It is not feature accumulation on top of the current hybrid system.

### Success condition

The program is complete only when:

1. every audit finding has an explicit disposition and implementation owner;
2. every beta-blocking finding is either `VERIFIED` or covered by an explicitly
   accepted release exception;
3. the database can be recreated and migrated from one authoritative ledger;
4. the target user journeys pass automated and real-device verification;
5. testnet and production are isolated and reproducible;
6. backup, restore, rollback, monitoring, and incident controls are exercised;
7. a completed release checklist records a founder Go/No-Go decision;
8. product hypotheses remain labeled as hypotheses and are measurable without
   misleading denominators or employer-facing individual data.

---

## 2. Working model for a founder + AI team

### 2.1 Deliberately lightweight governance

The project will not use Jira, story points, sprint ceremonies, or a separate
ADR system during MVP implementation.

The operating records are:

| Record | Purpose |
|---|---|
| This roadmap | Program sequence, dependencies, block status, and audit coverage |
| One current work-package plan | Exact scope, files, migrations, tests, and acceptance criteria for the next bounded implementation round |
| Git commits | Authentic implementation history |
| Automated and manual test evidence | Proof that implemented behavior works |
| Private founder daily log in the separate `logs` repository | Optional continuity notes, completed work, blockers, and next package; not PR evidence or a merge prerequisite |
| Release checklist | Final methodical Go/No-Go record |

Important technical decisions use four fields: `context`, `decision`, `reason`,
and `consequence`. If a decision affects implementation or acceptance, its
sanitized record belongs in the work-package document and PR so reviewers can
inspect it. The private founder log may mirror the decision but is not an
application-repository artifact. A separate ADR is created only if the founder
later explicitly asks for one.

### 2.2 Work-package rule

Only the **next** package is expanded into a detailed execution plan. Later
packages stay at roadmap level until their dependencies and current code are
stable. This prevents speculative file/function plans from becoming stale.

Each execution plan must contain:

* objective and non-goals;
* audit IDs and Founder Decisions covered;
* exact files and data stores in scope;
* preconditions and dependencies;
* reversible implementation steps;
* migration/backfill/compatibility treatment;
* test matrix;
* acceptance and rollback criteria;
* documentation updates;
* review instructions.

### 2.3 Review rule

Implementation and review are separate passes even when both use Codex:

1. create a dedicated `wp/<package>-<slug>` branch from the current
   `implementation/pre-mvp` integration branch;
2. implement against an approved work-package contract and run targeted checks;
3. run the package acceptance suite and create a stable diff;
4. perform a fresh-context local review against the diff, audit IDs, and
   acceptance criteria;
5. push only the work-package branch and open a GitHub PR targeting
   `implementation/pre-mvp`;
6. let the configured GitHub code review run; investigate every P0/P1 and every
   behaviorally credible lower-severity finding against the approved target
   architecture;
7. correct valid findings and rerun evidence; document stale-architecture or
   hallucinated findings instead of changing correct target behavior to satisfy
   them;
8. mark the package `VERIFIED` only after local acceptance and the useful
   GitHub review findings are resolved;
9. the founder alone manually merges the PR in GitHub.

The reviewer must inspect behavior, failure paths, migrations, concurrency,
privacy, security, and regressions. It must not merely summarize the author's
changes.

Codex may create commits, push the dedicated work-package branch, open/update
the PR, analyze review comments, and prepare fixes. Codex must not merge a PR,
push package commits directly to `implementation/pre-mvp` or `main`, or bypass
GitHub review. After the founder merges, the local integration branch is updated
from the remote merge result before the next work package starts.

### 2.4 AI workspace and account separation

The current Codex account remains dedicated to Love Yourself because it owns
the local repository and the accumulated audit context. A second separately
managed environment is planned for ATP so the two products do not compete for
one working context.

Saturday's preparation includes:

* establish the second ATP environment/account;
* keep Love Yourself on the current account and workspace;
* choose the least-friction local separation, potentially separate macOS users
  or another native profile arrangement;
* verify repository, shell, Git identity, credentials, and connectors cannot be
  confused across the two products;
* keep product knowledge in repositories and logs, not solely in chat history.

The exact macOS account/profile arrangement is a convenience decision made
during setup. It does not block roadmap approval and does not belong in the
product architecture.

No manual model-routing matrix, token diary, or credit-optimization workflow is
part of this roadmap.

---

## 3. Status and evidence model

### 3.1 Package statuses

| Status | Meaning |
|---|---|
| `NOT STARTED` | Dependencies or approval are not yet satisfied |
| `READY` | Preconditions are met and an execution plan may be written |
| `IN PROGRESS` | The bounded package is being implemented |
| `IMPLEMENTED` | Code/doc/migration changes exist but independent verification is incomplete |
| `IN REVIEW` | Stable diff is under acceptance and regression review |
| `VERIFIED` | Acceptance evidence and review passed |
| `RELEASED` | Verified package is present in the applicable testnet/production release |
| `BLOCKED` | A named external decision, provider, legal, or data condition prevents progress |

Only the founder changes a block to `RELEASED` or accepts a release exception.

### 3.2 Finding lifecycle

```text
AUDITED → PLANNED → IMPLEMENTED → VERIFIED → RELEASED
```

* `IMPLEMENTED` is not synonymous with fixed.
* `VERIFIED` requires evidence proportional to the finding.
* `OK`, resolved, frozen, accepted, deferred, and cross-reference findings are
  still assigned to a package so their invariant or disposition is preserved.
* A deferred finding is verified when the forbidden scope is absent and the
  deferral is correctly documented; it does not require building the deferred
  feature.

### 3.3 Evidence attached to a verified package

Record, where applicable:

* commit SHA and reviewed diff;
* migration version and rehearsal result;
* exact test commands and results;
* seed/reconciliation output;
* built image ID;
* device/Telegram QA record;
* backup and restore evidence;
* release manifest and environment checks;
* unresolved risks or accepted exceptions.

---

## 4. Gates and sequencing rules

### Gate G0 — development may start

Required before the first application-code work package:

* audit and roadmap are committed snapshots;
* every pre-existing dirty-worktree file is classified as keep, discard by
  founder instruction, split into a prior commit, or supersede;
* an implementation branch/baseline is selected deliberately;
* one reproducible local environment and canonical targeted-test command work;
* tracked credentials are rotated and the repository no longer ships `.env`;
* work-package and review conventions are written down.

Railway production does not need to be running merely to write local code.

### Gate G1 — durable-data and production migration may start

Required before company-user enrollment, market launch, or migration of any
non-disposable data:

* Railway daily/weekly backups are enabled;
* a fresh backup is restored to a separate scratch environment;
* the restored physical schema and representative row counts are inspected
  read-only;
* actual data anomalies and legacy rows are recorded;
* one Alembic baseline and forward migration command are established;
* migrations have been rehearsed outside production.

### Gate G2 — feature integration may start

Required before onboarding, Coach, and on-demand are integrated:

* plan-centric lifecycle authority is verified;
* one-current-plan and operation-idempotency boundaries exist;
* canonical content identity and `ExercisePresentation` exist;
* scheduled callback and delivery states are authoritative and race-safe;
* rejected adaptation/engagement paths cannot run.

### Gate G3 — external testnet beta may start

Required before inviting any external beta user:

* production/testnet isolation passes negative tests;
* enrollment/token boundary is implemented;
* privacy notice and rights path are live;
* complete canonical test suite passes;
* real-device user journey passes on iOS and Android;
* monitoring, kill switches, Coach limits, backup/restore, and incident runbook
  are exercised;
* no unresolved audit `BLOCKER` applies to testnet beta.

### Gate G4 — first company production deployment may start

Required in addition to G3:

* legal roles, employee notice, contracts, and any DPIA determination are in
  place;
* production roster/identity gateway and revocation are verified;
* company deployment checklist passes;
* capacity test passes at twice the expected peak simultaneous cohort;
* production migration, rollback choice, one-runtime-owner rollout, and smoke
  test are approved;
* no unresolved production `BLOCKER` remains.

### Non-breaking implementation invariant

Every verified package must leave the selected development baseline buildable,
startable, and testable. A future package may complete a feature, but it may not
be required to repair avoidable breakage introduced by an earlier package.

For cross-package database and lifecycle changes:

1. **expand** compatible schema and introduce the target authority;
2. **backfill/reconcile** only facts supported by real data;
3. **switch** all named readers/writers through a tested application boundary;
4. **contract** old columns, enums, tables, routes, jobs, and tests only after a
   final reachability check proves no live consumer remains.

No package may:

* drop a field/state/table while a later package still needs to remove its live
  reader or writer;
* enable an externally reachable partial feature before its persistence,
  authorization, failure, privacy, and test prerequisites pass;
* merge a compatibility shim into a second long-lived source of truth;
* call a future package's unfinished behavior from the active runtime;
* treat a feature flag as a substitute for deleting rejected behavior at the
  owning cleanup gate.

If a target feature spans packages, its new entry surface remains disabled
until the final owning package is `VERIFIED`.

---

## 5. Program dependency map

```text
B0 Readiness
   ↓
B1 Recovery, migration, and data authority
   ↓
B2 Target lifecycle and runtime actions
   ↓
B3 Content, plan generation, presentation, and scheduled delivery
   ↓
B4 Deployment access, privacy minimum, onboarding, and deterministic controls
   ↓
B5 Coach integration and bounded agency
   ↓
B6 Exercise on demand
   ↓
B7 Telemetry, cycle summary, aggregation, and founder reporting
   ↓
B8 Security hardening, legacy closure, documentation, and tooling cleanup
   ↓
B9 Full-system verification and isolated testnet release
   ↓
B10 Company production gate and controlled launch
```

Some foundations are introduced early and completed later. For example, event
tables are designed under B1, populated by features under B3–B6, and reconciled
and reported under B7. Early schema work does not prematurely close telemetry
findings.

### Critical package dependency register

This is the execution authority when a block-level arrow is too coarse. A
package does not become `READY` until every listed package/gate is satisfied.

| Package | Required predecessors |
|---|---|
| `WP-00.1` | Approved audit; none inside this roadmap |
| `WP-00.2` | `WP-00.1` repository ownership understood |
| `WP-00.3` | `WP-00.1` baseline selected |
| `WP-00.4` | `WP-00.1`; founder access to relevant credentials/Railway |
| `WP-01.1` | `WP-00.4`; may use only explicitly disposable testnet data until Gate G1 |
| `WP-01.2` | `WP-01.1` physical-schema evidence |
| `WP-01.3` | `WP-01.2` target invariant ledger |
| `WP-01.4` | `WP-01.2`; schema design aligned with `WP-01.3` lifecycle identity |
| `WP-02.1` | `WP-01.3`; reachability/import evidence retained |
| `WP-02.2` | `WP-01.3`, `WP-01.4` |
| `WP-02.3` | `WP-02.2` |
| `WP-03.1` | `WP-01.2`; Delivery UX audit already closed; `DG-02` for cool water only; `DG-08` for required GIF assets |
| `WP-03.2` | `WP-03.1`, `WP-01.3` |
| `WP-03.3` | `WP-03.1`, `WP-01.4` |
| `WP-03.4` | `WP-02.2`, `WP-03.3`, `WP-01.4` event operation |
| `WP-03.5` | `WP-02.2`, `WP-02.3`, `WP-03.2`, `WP-03.4` |
| `WP-04.1` | `WP-00.4`, `WP-01.4`, `WP-02.2` |
| `WP-04.2` | `WP-01.4`, `WP-04.1` for deployment-bound notice behavior |
| `WP-04.3` | `WP-03.2`, `WP-03.4`, `WP-04.1`, `WP-04.2`, `DG-01` |
| `WP-05.1` | `WP-02.3`, `WP-03.3`, `WP-04.3` |
| `WP-05.2` | `WP-05.1`, `WP-02.2` |
| `WP-05.3` | `WP-05.2`, `WP-04.2`, `DG-04` disposition |
| `WP-05.4` | `WP-05.1…05.3` |
| `WP-06.1` | `WP-01.4`, `WP-03.1`, `WP-03.3`, `WP-04.1` |
| `WP-06.2` | `WP-06.1`, `WP-03.4`, `WP-04.3`, `WP-05.2` |
| `WP-06.3` | `WP-06.2`, `WP-01.4`, `WP-04.2` |
| `WP-07.1` | authoritative feature paths from `WP-02.2`, `WP-03.4`, `WP-04.3`, `WP-05.3`, `WP-06.3` |
| `WP-07.2` | `WP-07.1` reconciled facts |
| `WP-07.3` | `WP-01.4`, `WP-04.2`, `WP-07.1` |
| `WP-07.4` | `WP-03.5`, `WP-06.3`, `WP-07.1` |
| `WP-08.1` | all replacement paths in B2–B7 that own removed behavior |
| `WP-08.2` | `WP-00.4`, `WP-05.3`, `WP-07.1`; may proceed beside `WP-08.1` |
| `WP-08.3` | `WP-07.4`, `WP-08.1`; core product behavior stable |
| `WP-09.1` | B1–B8 implementation complete enough for a full release candidate |
| `WP-09.2` | `WP-08.2`; stable runtime responsibilities |
| `WP-09.3` | `WP-01.1`, `WP-00.4`, `WP-08.2`, `WP-09.1` |
| `WP-09.4` | `WP-09.1…09.3`, `DG-03`, required media assets |
| `WP-10.1` | `WP-04.1`, `WP-04.2`, `WP-07.3`, `WP-09.4`, `DG-06…07` |
| `WP-10.2` | `WP-10.1`, `WP-09.4` verified release candidate |
| `WP-10.3` | `WP-10.2`, `WP-09.3`, Gate G4 |
| `WP-10.4` | `WP-10.3`, `WP-07.2`, `WP-07.3` |

The dependency graph is acyclic. Packages may overlap only where the table
explicitly permits parallel work; a later package never supplies a prerequisite
to an earlier one.

---

## 6. Block 0 — Development and infrastructure readiness

**Status:** `IN PROGRESS`

**Target start:** Saturday, 2026-08-22  
**Objective:** remove setup ambiguity before product refactoring begins.

### WP-00.1 — Freeze the authentic starting baseline

**Status:** `VERIFIED` — local acceptance and GitHub PR review passed;
founder merged PR #248. Evidence in
`docs/implementation/work_packages/WP-00.1_starting_baseline.md`.

**Scope**

* preserve audit commit `c2a380a`;
* inventory the current dirty worktree, including application drafts,
  migrations, tests, documents, virtual environments, R&D copies, caches, and
  OS files;
* determine ownership and disposition for each change without destructive
  cleanup;
* create the implementation branch only after the baseline is understood;
* add ignore rules for local environments, caches, `.DS_Store`, generated
  output, and non-release R&D material as appropriate;
* optionally update the founder's private daily log without making it package
  evidence or a merge prerequisite.

**Exit criteria**

* no unexplained change can leak into the first implementation commit;
* audit artifacts remain recoverable and unchanged;
* `git status` is either clean or every remaining change has a written owner and
  package;
* branch and commit conventions are fixed for the program.

### WP-00.2 — Establish the AI-assisted development workspace

**Status:** `VERIFIED` — workspace separation, founder acceptance, and GitHub
review passed; founder merged PR #249 at `5197284`. Evidence in
`docs/implementation/work_packages/WP-00.2_ai_workspace.md`.

**Scope**

* keep this account/workspace for Love Yourself;
* establish the separate ATP working environment;
* decide whether separate macOS users materially improve native switching;
* verify Git identity, local paths, shells, keys, and connected services are not
  accidentally shared between products;
* add a concise repository `AGENTS.md` only if it provides durable commands and
  safety conventions that would otherwise be repeated in every package;
* adopt one-package/one-execution-context and fresh-context review.

**Exit criteria**

* Love Yourself can be developed without switching into ATP context;
* canonical context is available from repository documents;
* the founder does not need to route tasks manually among models or maintain a
  usage ledger.

### WP-00.3 — Make the local environment reproducible

**Status:** `VERIFIED` — GitHub review completed without findings; founder
merged PR #250 at `d885bb3`. Evidence in
`docs/implementation/work_packages/WP-00.3_reproducible_environment.md`.

**Scope**

* use the supported Python 3.12 patch line locally and pin Python 3.12.14 for
  the release container;
* resolve and lock runtime and test dependencies;
* inspect aiogram 3.5.0 compatibility before choosing an upgrade; upgrade only
  when compatibility or security evidence requires it;
* establish one documented environment bootstrap command;
* establish one canonical targeted-test command and one future full-suite
  command;
* make test imports safe without production secrets or network calls;
* provide local/ephemeral PostgreSQL and Redis for integration tests;
* record the current collection/pass/failure baseline and separate obsolete
  tests from genuine product failures.

**Primary audit coverage:** `OPS-05`, `SEC-08` (dependency reproducibility),
`FSM-07`, `DEL-06`, `RT-15`, `LIF-15`.

**Exit criteria**

* another clean environment can install and collect the intended tests;
* failures are deterministic and classified;
* no production credential is required for collection;
* the environment is ready for package-level red/green development.

### WP-00.4 — Secure configuration and Railway topology

**Scope**

* rotate the tracked Telegram/OpenAI credentials and any related compromised
  secrets;
* untrack `.env`, fix `.gitignore`, and add a restrictive `.dockerignore`;
* define the typed `dev` / `staging` / `prod` configuration contract;
* inventory all required Railway variables without printing secret values;
* choose Docker as the eventual single release artifact and record the current
  `Procfile` conflict for B9;
* define isolated testnet and production services, bots, databases, Redis,
  OpenAI projects, token keys, URLs, alerts, backups, and aggregate sinks;
* define the Railway daily/weekly production-volume backup prerequisite;
* define the scratch restore required by Gate G1;
* record one replica / one polling owner / one scheduler writer as the beta
  topology.

**Primary audit coverage:** `SEC-01…03`, `FD-12`, `FD-18`, `COMP-08`,
`OPS-06…07` (readiness portion).

**Exit criteria**

* no live secret is tracked or copied into the build context;
* invalid production configuration fails closed;
* environment topology and ownership are explicit;
* the production backup/restore gate is explicit, including any accepted
  pre-production exception;
* testnet work cannot write production data.

### Block 0 completion gate

B0 closes when G0 is satisfied. G1 may continue as the first part of B1, but no
production-derived migration is designed from ORM assumptions alone.

---

## 7. Block 1 — Recovery, migration authority, and data foundations

**Status:** `NOT STARTED`  
**Depends on:** B0; Gate G1 before any non-disposable or production migration
**Objective:** establish PostgreSQL as reproducible durable truth before the
first large refactor.

### WP-01.1 — Restore, inspect, and establish Alembic authority

**Deliverables**

* physical schema, enum, constraint, index, row-count, nullability, legacy-row,
  and scheduler-job inventory from disposable testnet during refactoring, then
  from a verified production backup restore before production migration;
* authoritative Alembic baseline representing the real starting schema;
* one forward migration command and migration test harness;
* production startup changed from schema mutation to read-only version check;
* `apscheduler_jobs` explicitly excluded from application-table migration
  ownership;
* restore runbook with scheduler clearing/reconstruction and measured RPO/RTO.

**Primary audit coverage:** `DB-01`, `FSM-11`, `OPS-02`, `OPS-07`, `FD-12`.

**Exit criteria**

* repository migrations reproduce the inspected schema;
* migration rehearsals run against disposable or restored copies, never first
  against non-disposable production data;
* application startup cannot silently mutate production schema;
* restore cannot replay stale scheduled deliveries.

### WP-01.2 — Define the target schema and invariant ledger

**Deliverables**

* target tables, foreign keys, enums, partial unique constraints, check
  constraints, and high-value indexes;
* explicit authority for content, lifecycle, deployment, entitlement,
  occurrence, event, feedback, notice acknowledgement, report grants, and
  aggregates;
* classification of derived fields: calculate, snapshot immutably, or remove;
* Redis namespaces limited to transient coordination/session state with TTL;
* explicit deferral for pool unification, broad index tuning, leader election,
  and harmless legacy tables unless evidence makes them necessary.

**Primary audit coverage:** `DB-09…15`, `DB-18…19`, `PRIV-09`, `CONTENT-07`.

**Exit criteria**

* every durable target mechanism has one named database authority;
* no migration introduces a second source of lifecycle or content truth;
* deferred database work is documented rather than accidentally half-built.

### WP-01.3 — Implement the plan-centric lifecycle migration

**Deliverables**

* FD-08 plan-centric ownership and one derived `current_mode` function;
* normalized plan and step statuses;
* one-current-plan protection per user;
* aggregate-existence checks;
* operation-level locking/conditional transitions and idempotency primitives;
* legacy state remapping, including removal/merge of retired idle states;
* expand → backfill → switch sequence with compatibility tests;
* an explicit contract manifest naming every remaining old reader/writer and
  the later package that removes it; destructive contract steps run only after
  those consumers are gone;
* no synchronized duplicate `User.current_state` authority.

**Primary audit coverage:** `FSM-08…09`, `FSM-11…13`, `DB-02…04`, `FD-08`.

**Exit criteria**

* lifecycle is derived from authoritative plan facts;
* concurrent mutations have one winner;
* migration handles real restored rows and can resume safely;
* old and new application versions have an explicit compatibility boundary;
* inert legacy storage may remain temporarily for compatibility, but no live
  code continues writing it as a second authority.

### WP-01.4 — Establish event, privacy, and deployment primitives

**Deliverables**

* canonical event envelope/catalogue validation, identity columns, and
  source-operation uniqueness;
* distinct `plan_step_id`, `exercise_id`, `content_version`, deployment,
  environment, and future on-demand occurrence linkage;
* immutable `User.first_seen_at` and deployment chronology;
* deployment, access identity, entitlement, invitation, notice acknowledgement,
  personal feedback, sealed aggregate, and revocable report-grant structures;
* one idempotent personal-event + independent sealed-aggregate write boundary
  that later feature packages can call inside their authoritative operation;
* clear separation between operational events and user-behavior events;
* additive on-demand occurrence table contract, introduced only when B6 is
  implemented;
* migrations that do not backfill invented facts.

**Primary audit coverage:** `DB-05…07`, `DB-11`, `DB-15`, `CONTENT-07`,
`TEL-03…04`, `TEL-13`, `COMP-01`.

**Exit criteria**

* later features can write truthful, idempotent facts without legacy execution
  windows or mixed identifiers;
* B2–B6 do not need to invent local event or aggregate writers;
* migration tests prove null/legacy handling;
* the foundation is functional but does not pretend that feature
  instrumentation, reconciliation, privacy deletion, or reporting are complete.

---

## 8. Block 2 — Target lifecycle and runtime actions

**Status:** `NOT STARTED`  
**Depends on:** B1 lifecycle authority  
**Objective:** make all plan operations use one atomic lifecycle before adding
new user-facing channels. Completion/continuation orchestration is finalized in
WP-03.5 after the target plan builder and delivery states exist.

### WP-02.1 — Remove dead lifecycle entrances and the schedule-adjustment tunnel

Remove as complete subsystems, not isolated symbols:

* `SCHEDULE_ADJUSTMENT` dispatcher, handlers, callbacks, keyboards, Redis keys,
  job, state, tests, and `_resume_plan_if_paused`;
* producerless legacy worker-envelope mutation architecture;
* dead first-plan wrapper and retired idle states as part of the lifecycle
  migration;
* unreachable generic FSM guard/commit path once all live callers use the
  target service;
* obsolete `start_plan:` continuation buttons.

**Primary audit coverage:** `SCH-02`, `FSM-01…05`, `FSM-07`, `FSM-10`,
`RT-05`, `LEG-01…02`, part of `LIF-05`.

**Exit criteria**

* no producer, handler, job, state, test, or Redis key can re-enter the retired
  model;
* deletion is supported by import/reachability and regression evidence;
* replacement behavior exists where the target contract requires it.

### WP-02.2 — Build the authoritative lifecycle service

Create one backend boundary for:

* current status and remaining-delivery facts;
* plan activation;
* terminal step transitions;
* expiry;
* pause, resume, cancel;
* schedule/time reconciliation;
* plan format switch;
* completion transition and continuation orchestration interfaces.

The service re-reads authoritative state, enforces ownership and entitlement,
serializes conflicting operations, returns structured results, and does not
send ambient Telegram messages from persistence helpers.

**Primary audit coverage:** `LIF-01`, `LIF-09`, `LIF-16…18`, `FSM-06`,
`FSM-09`, `RT-08`, `RT-11…14`.

**Exit criteria**

* all deterministic surfaces call the same service;
* failure cannot be returned as success;
* duplicate/replayed operations return the existing authoritative result;
* external side effects remain observable and retryable.

### WP-02.3 — Complete runtime controls and plan-format behavior

**Scope**

* semantic time validation;
* plan-aware evening-time collection/change;
* correct pause/resume rescheduling and paused-plan completion protection;
* deterministic cancel semantics;
* status derived from real remaining/delivered steps;
* user-requested follow-up only from the accepted aborted state;
* atomic `switch_plan_format` and resolution of the immediate post-continuation
  switch seam;
* outcome-level tests rather than wiring-only mocks.

**Primary audit coverage:** `COACH-01`, `COACH-03…05`, `COACH-11…12`,
`RT-01…08` except `RT-05`, `RT-10…15`, `FSM-06`, `MISC-02` remains deferred.

**Exit criteria**

* invalid state, time, plan type, or context fails before persistence;
* every accepted control has one deterministic backend result;
* no LLM is required to execute or confirm the operation;
* deferred time-picker UI is not accidentally resurrected from legacy code.

---

## 9. Block 3 — Content, plan generation, presentation, and scheduled delivery

**Status:** `NOT STARTED`  
**Depends on:** B2 and canonical identity from B1  
**Objective:** make the primary product touchpoint deterministic, versioned,
and reliable without live OpenAI dependency.

### WP-03.1 — Migrate the versioned Content Library

**Scope**

* implement the FD-10 target records and schema;
* encode stable exercise ID, content version, exact steps/duration, structured
  requirements, review status, and optional media/alt text;
* remove legacy parent/variation/weight/adaptation metadata;
* enforce release eligibility fail-closed;
* create and version required GIFs for breathing and fist PMR;
* keep cool water excluded until its medical review and required GIF pass;
* synchronize content-specific contracts and tests.

**Primary audit coverage:** `CONTENT-01…06`, `CONTENT-08…09`, `FD-10`,
`FD-16`, `EOD-03` prerequisite.

**Exit criteria**

* one source of content truth feeds builder and renderer;
* only reviewed, eligible, versioned records can enter beta;
* five-record on-demand launch pool is valid without cool water;
* content migration and rollback/version semantics are tested.

### WP-03.2 — Correct deterministic plan generation

**Scope**

* stable per-cycle discriminator;
* cooldown continuity across cycle boundaries;
* same-cycle deterministic rebuild and next-cycle variation;
* no SHORT→MEDIUM replay of days 1–7;
* `source_exercises` records scheduled exercises only;
* remove decorative weight/adaptation code;
* preserve accepted within-week repetition and `cooldown_days: 1` hypothesis;
* keep runtime variations and behavioral personalization deferred.

**Primary audit coverage:** `PLAN-01…09`, `FD-05`, relevant `DB-09`.

**Exit criteria**

* builder invariants and accepted repetition behavior are explicit in tests;
* no feedback or hidden score changes plan generation;
* created steps snapshot correct content identity/version.

### WP-03.3 — Introduce canonical `ExercisePresentation` and media delivery

**Scope**

* one structured presentation object shared by scheduled, on-demand, and Coach
  context;
* neutral notification preview and in-chat title, duration, exact steps,
  actions, deadline, status, and media metadata;
* escaped and size-bounded Telegram HTML;
* no internal scheduling/category/rationale metadata;
* versioned GIF delivery with complete text fallback;
* renderer contains no plan selection or lifecycle ownership.

**Primary audit coverage:** `DEL-01…02`, `DEL-07…08`, `UX-03`, `UX-15…16`,
`FD-06`, `FD-13…14`, `FD-16`.

**Exit criteria**

* scheduled and on-demand render the same content truth without sharing state
  aggregates;
* media failure cannot block the exercise;
* presentation snapshot records the actual delivered variant;
* no LLM call is introduced into scheduled delivery (`OPS-12`).

### WP-03.4 — Make scheduled delivery and callbacks reconcilable

**Scope**

* bounded send retry and stable source-operation identity;
* durable delivered/retryable/terminal-failure states;
* atomic Done/Skip/expiry transitions and event write;
* same-message visible terminal states;
* completed-only `better / same / worse` feedback linked to content and step;
* pause preserves already-delivered action until deadline; cancel closes it;
* late/duplicate taps return factual state;
* expiry, button removal, and event timing are one lifecycle event;
* restart and Telegram failure reconciliation.

**Primary audit coverage:** `DEL-03…06`, `UX-04…07`, `SEC-07`, `DB-08`,
`FD-06…07`, `OPS-12`.

**Exit criteria**

* transient failure cannot silently lose an exercise;
* callback races accept exactly one outcome;
* telemetry never records an opportunity before confirmed delivery;
* delivery remains functional during OpenAI outage.

### WP-03.5 — Implement durable completion and automatic continuation

**Depends on:** WP-02.2, WP-02.3, WP-03.2, and WP-03.4.

**Scope**

* remove the two-hour completion trigger;
* detect terminal completed/skipped action quickly;
* use expiry + fixed cron safety net for no-action completion;
* finalize the explicit plan before creating the summary obligation;
* create exactly one same-format successor through the target plan builder for
  the next active day at DAY time;
* guarantee no same-day successor collision;
* make summary delivery obligation durable and retryable;
* remove dead legacy CTA/copy;
* preserve scheduled-only plan denominator and correct 14-day facts;
* test active completion, no-action expiry, timezone/work-days, duplicates,
  ordering, format-switch interaction, failure, restart, and continuation
  idempotency.

**Primary audit coverage:** `FD-01…02`, `SCH-03`, `LIF-02…08`,
`LIF-10…15`, `LIF-17…19`. Final summary image/payload belongs to WP-07.4.

**Exit criteria**

* old plan closes once, summary obligation exists once, successor exists once;
* successor uses the verified target builder/content identity rather than the
  soon-to-be-replaced legacy generator;
* first successor task is scheduled for the exact promised future moment;
* delivery failure never reopens or duplicates lifecycle finalization;
* completion tests express target rather than deprecated behavior.

---

## 10. Block 4 — Access, privacy minimum, onboarding, and deterministic controls

**Status:** `NOT STARTED`  
**Depends on:** B2–B3  
**Objective:** create a trustworthy entry path that remains usable without the
Coach model.

### WP-04.1 — Implement deployment, entitlement, and enrollment authority

**Scope**

* organization/deployment/access-identity/entitlement/invitation models;
* isolated founder-issued testnet entitlement;
* production roster import and reconciliation with explicit snapshot/delta
  modes;
* OIDC or verified-email authorization and deployment entitlement check;
* high-entropy, hashed, expiring, single-use Telegram handoff token;
* atomic redemption, replay/cross-environment rejection, and revocation;
* deployment timezone mode and environment isolation;
* removal of the legacy `newplan_*` entry model.

**Primary audit coverage:** `SEC-10`, `COMP-01`, `COMP-04`, `COMP-06`,
`COMP-08`, `UX-01`, `FD-18`, `MISC-01` pointer.

**Exit criteria**

* no token, Telegram sender, URL parameter, or email string alone grants
  production access;
* testnet and production credentials/data are non-interchangeable;
* roster revocation removes sponsored access without deleting history or
  exposing behavior;
* already-enrolled users resume idempotently.

### WP-04.2 — Implement the minimum privacy and data-rights lifecycle

**Scope**

* accurate notice and recorded notice version, bound to deployment;
* support/manual access, export, correction, and deletion request path;
* 90-day Coach/conversation retention across PostgreSQL, Redis, and logs;
* account deletion across database, Redis, scheduler, open occurrences,
  personal events, and report grants;
* revocable/expiring bearer grants or retirement of obsolete report routes;
* removal of unused sensitive-looking schema;
* remove or authenticate `/user/time-slots`;
* Redis cannot become stale business authority.

**Primary audit coverage:** `PRIV-01…05`, `PRIV-08…10`, `DB-16…17`,
`COMP-07`, `UX-13`.

**Exit criteria**

* user-facing privacy wording matches actual behavior;
* one tested deletion request reaches every personal store;
* user-linked telemetry is never described as anonymous;
* company access cannot reach individual or free-text data.

### WP-04.3 — Implement deterministic onboarding and user menu

**Scope**

* canonical `/start` handoff after authorization;
* FD-04 deterministic mechanism-sale onboarding;
* collect exact DAY time and selected work days;
* create the first plan through the canonical service;
* confirm the finalized first delivery date/time;
* keep internal slot tags out of user-facing copy;
* deterministic menu for status, next delivery, time, pause/resume/cancel,
  format switch, privacy/data, support, and about;
* already-onboarded, invalid/revoked token, repeat start, and recovery paths.

**Primary audit coverage:** `ONB-01…08`, `UX-12`, `UX-18`, `FD-03…04`.

**Decision gate:** the exact allowed delivery window/company configurability
must be founder-approved before hardcoding constraints. `MISC-02` time picker
remains post-MVP.

**Exit criteria**

* onboarding requires no LLM and creates one valid first plan;
* first delivery confirmation uses the actual finalized step;
* product controls work during OpenAI degradation;
* no abandoned plan-parameter UI remains reachable.

---

## 11. Block 5 — Coach integration and bounded agency

**Status:** `NOT STARTED`  
**Depends on:** canonical services from B2 and access/context from B4  
**Objective:** make Coach reactive, grounded, secure, and non-essential to core
product operation.

### WP-05.1 — Build canonical Coach context and plan-aware tool exposure

**Scope**

* provide current mode, plan type, days, evening configuration, current
  exercise presentation, entitlement, and pending action;
* expose only tools meaningful for the current authoritative context;
* fix delivered-exercise explanation context;
* re-authorize tool and arguments at execution time;
* keep unsupported states and retired tools out of the registry;
* reconcile the existing Coach prompt/PR work against current FD authority
  before merging anything.

**Primary audit coverage:** `COACH-07…08`, `RT-03…04`, `SEC-05`.

### WP-05.2 — Implement the bounded tool-result loop

**Scope**

* exactly one allowlisted tool call;
* strict structured backend result;
* one second model pass with tools disabled;
* reply grounded only in safe factual result;
* deterministic fallback on malformed tool call, execution failure, or second
  model failure;
* user-language outcome tests and no recursive tool chain.

**Primary audit coverage:** `COACH-09`, `RT-09`, `UX-11`.

### WP-05.3 — Add failure, privacy, concurrency, and cost boundaries

**Scope**

* deterministic unsupported-input and OpenAI-outage responses;
* `store=False` and minimized request payload;
* privacy-safe token/model/latency/outcome/cost telemetry;
* one in-flight Coach worker per user and bounded FIFO;
* FD-17 flood limits, timeout, retry, global cost circuit breaker, and neutral
  refusal;
* no visible user quota;
* security-safe logs and prompt/tool adversarial tests;
* product-question escalation is either implemented with a real destination or
  removed from Coach promises before beta.

**Primary audit coverage:** `COACH-10`, `PRIV-03`, `SEC-04…06`, `UX-08`,
`FD-17`.

### WP-05.4 — Coach acceptance and regression suite

Verify resolved and preserved behavior as well as new work:

* paused-status display and state-filtered registration remain correct;
* runtime validations and structured soft failures;
* wrong-state, cross-user, malformed-time, fabricated-tool, cancellation-
  consent, tool-result, outage, overflow, timeout, and cost-breaker cases;
* Coach does not browse, access files, execute arbitrary code, or become the
  authority for lifecycle success.

**Primary audit coverage:** `COACH-01…12` including resolved `COACH-02` and
`COACH-06`; `RT-15` outcome proof.

**Block exit criteria**

* deterministic menu/product delivery work without OpenAI;
* Coach can only request authorized bounded operations;
* no failed backend action is voiced as success;
* usage/cost is observable without storing conversation content.

---

## 12. Block 6 — Independent exercise-on-demand channel

**Status:** `NOT STARTED`  
**Depends on:** B1, B3, B4, and B5 tool loop  
**Objective:** implement FD-19 without coupling voluntary requests to plan
lifecycle or corrupting scheduled-channel evidence.

### WP-06.1 — Add the occurrence aggregate, service, and selector

**Scope**

* additive `on_demand_request` table, constraints, indexes, repository, and
  lifecycle service;
* one-open request invariant and stable source-operation identity;
* selected exercise/content/presentation snapshot before delivery;
* equal-probability random selection with immediate-repeat exclusion only;
* five eligible launch records; approved cool water becomes sixth;
* fail closed with fewer than two eligible records;
* no plan, FSM, scheduler-plan, or Redis business mutation.

### WP-06.2 — Add command and Coach entry, delivery, callback, and expiry

**Scope**

* deterministic command-menu entry before catch-all text;
* explicit-intent Coach tool over the same service;
* availability after onboarding in active, paused, and no-plan modes and
  outside work schedule;
* canonical media/text delivery;
* 30-minute deadline, atomic complete/skip/expiry, visible terminal state;
* bounded retry, restart recovery, GIF fallback, and delivery-failure handling;
* simultaneous scheduled and on-demand delivery without lifecycle collision.

### WP-06.3 — Add on-demand events, privacy hooks, and reconciliation

**Scope**

* canonical requested/delivered/delivery-failed/completed/skipped/expired and
  feedback events;
* occurrence/source/content/version/variant linkage through the WP-01.4 event
  and sealed-aggregate boundary;
* personal retention/deletion and complete exclusion from company channel
  reporting;
* seeded reconciliation across DB state, events, aggregates, restart, and
  failure paths;
* one independent completion-count query contract consumed later by WP-07.4;
  B6 does not render the final cycle summary or founder analytics.

**Primary audit coverage for B6:** `EOD-01…12`, `FD-19`.

**Exit criteria**

* command and Coach use one service and differ only by entry surface;
* duplicate requests/taps create one occurrence and one behavioral outcome;
* plan and on-demand persistence remain independent;
* delivery failure is not counted as user inactivity;
* company output cannot expose exercise channel or individual behavior;
* the complete EOD required test matrix in the audit passes.

---

## 13. Block 7 — Telemetry, aggregation, cycle summary, and founder reporting

**Status:** `NOT STARTED`  
**Depends on:** authoritative feature paths from B2–B6  
**Objective:** make beta evidence truthful, privacy-bounded, and reconcilable.

### WP-07.1 — Complete feature instrumentation and event reconciliation

**Scope**

* use and harden the WP-01.4 event envelope/catalogue and source-operation
  idempotency boundary rather than creating a second logger;
* verify plan, step, exercise, content, deployment, environment, and on-demand
  linkage;
* instrument deployment and onboarding funnel;
* authoritative delivery, expiry, callback, runtime-action, continuation,
  Coach-turn, and on-demand events;
* separate operational failures from user behavior;
* remove legacy streak, compensation, engagement, inferred-failure, and
  adaptation event consumers;
* conservative first-seen backfill.

**Primary audit coverage:** `TEL-01…11`, `TEL-13`, `COMP-02`.

### WP-07.2 — Implement canonical metrics and restricted founder view

**Scope**

* eligible → authorized/invited → enrolled → onboarded → first delivery → first
  response funnel;
* working-day D1/D3/D7 and explicit denominators;
* cycle coverage, task outcome, feedback, next-cycle response;
* response latency derived from linked delivery/callback facts;
* operational reliability view separate from behavior;
* raw `n`, cohort/window dates, delivery failures, and seeded reconciliation;
* founder-only report/CSV without conversation/free text.

**Primary audit coverage:** `TEL-14…15`, remaining measurement outcomes of
`TEL-01…13`, `EOD-12`.

### WP-07.3 — Complete independent aggregation and personal deletion

**Scope**

* verify and complete the WP-01.4 personal-event + sealed independent-aggregate
  operation across every feature source;
* contributor uniqueness without a reversible user join;
* account deletion removes personal data but not an already sealed aggregate
  contribution;
* 100-eligible / 50-contributor centralized company gate;
* no team, office, channel, exercise, feedback, Coach, or small-group slices;
* aggregation and deletion race tests.

**Primary audit coverage:** `PRIV-06…07`, `TEL-12`, `FD-09`, relevant
`EOD-10`.

### WP-07.4 — Replace the legacy report with deterministic cycle summary

**Scope**

* application-rendered Telegram image and factual text fallback;
* scheduled numerator/denominator and correct 7/14-day day-level facts;
* conditional on-demand completed count only when greater than zero;
* count-only total, no shared adherence percentage;
* next delivery/continuation facts from authoritative lifecycle;
* delivery durability/idempotency and retirement of obsolete evaluative HTML
  path, CTA, streak, persona, adaptation, and long-lived bearer link.

**Primary audit coverage:** `FD-15`, `LIF-14`, `UX-14`, `EOD-09`,
`PRIV-08` as applicable.

**Block exit criteria**

* seeded plans/events/aggregates reconcile exactly;
* no metric claims observed execution, wellbeing, burnout, productivity, or
  causality;
* scheduled and on-demand denominators remain separate;
* company report boundary is enforced in schema/query tests;
* founder view is sufficient to interpret beta without direct DB archaeology.

---

## 14. Block 8 — Legacy closure, security hardening, docs, and tooling

**Status:** `NOT STARTED`  
**Depends on:** replacement paths from B2–B7  
**Objective:** remove contradictory active context and harden the release
surface after target behavior exists.

### WP-08.1 — Complete legacy removal

Follow the audit's removal-ordering index and verify both producers and
consumers. Remove:

* re-engagement, pulse, snapshot, persona, streak, comeback, silence, quotation,
  adaptation, and `adapt_suggest` paths;
* `AIPlanVersion` after its last adaptation writer is removed;
* dead modules/symbols and their test-only legacy fixtures;
* retired plan draft preview/actions and old deep-link tail;
* `FailureSignal` and inference model;
* obsolete user-facing report routes and documentation;
* remaining Redis legacy namespaces.

`MORNING` remains frozen internal metadata until a founder decision says
otherwise. Do not scatter new behavior around it.

**Primary audit coverage:** `SCH-01`, `SCH-04…06`, `UX-09…10`,
`LEG-03…04`, `TEL-09`, `DB-10`, remaining `RT-05` inventory.

### WP-08.2 — Harden dependencies, container, configuration, and incident controls

**Scope**

* exact dependency lock and reviewed SCA results;
* deliberate base-image pin, non-root runtime, final image scan;
* one typed configuration contract and fail-closed startup;
* security event taxonomy and privacy-safe logging;
* kill switches for Coach, scheduled delivery, and deployment;
* alert destinations and credential-rotation paths;
* founder-owned incident runbook and one rehearsal;
* accepted exceptions recorded with review date.

**Primary audit coverage:** `SEC-08…09`, completion of `SEC-01…03`,
`COMP-09` shared controls.

### WP-08.3 — Synchronize documentation, website, and development tools

**Scope**

* remove/archive stale product and architecture documents from active AI
  context;
* finalize Product Contract after core implementation stabilizes;
* synchronize Product Maps, public website, privacy language, name, claims,
  enrollment, and company-report boundary;
* retire or rebuild devtools/smoke/stress utilities for the target lifecycle;
* exclude unapproved development tooling and R&D from production artifact;
* preserve history in Git rather than live contradictory files.

**Primary audit coverage:** `MISC-03…06`, `CONTENT-08`, `UX-13`, `FD-13`.

**Exit criteria for B8**

* no rejected product subsystem remains reachable;
* active docs no longer teach obsolete architecture to humans or AI;
* image and dependency scans are reviewed;
* operational tools exercise the target model only.

---

## 15. Block 9 — Full-system verification and isolated testnet release

**Status:** `NOT STARTED`  
**Depends on:** B1–B8  
**Objective:** prove the exact release artifact works under normal, failure,
restart, and recovery conditions.

### WP-09.1 — Establish the canonical release test and CI baseline

**Scope**

* clean dependency install;
* complete test collection with no production credentials;
* unit, PostgreSQL, Redis, migration, scheduler, renderer, Telegram callback,
  lifecycle, privacy, event, and integration tests;
* Docker startup tests and canonical smoke entry;
* repeatable CI workflow using the same commands;
* test inventory showing which audit findings each high-value scenario proves.

**Primary audit coverage:** `OPS-05`, all stale-test cross-references.

### WP-09.2 — Implement runtime supervision, health, and monitoring

**Scope**

* one lifecycle supervisor for polling, HTTP, scheduler, and dependencies;
* retained task handles, graceful SIGTERM order, bounded drain, and non-zero
  incomplete shutdown;
* `/live`, truthful `/ready`, `PORT`, schema/dependency checks;
* polling/scheduler heartbeat, overdue backlog, delivery failure/retry,
  dependency health, restart, backup age, and cost-control alerts;
* privacy-safe alert payloads and named response owner.

**Primary audit coverage:** `OPS-03…04`, `OPS-09`, `DB-20`, `COMP-09`.

### WP-09.3 — Produce one reproducible release artifact and recovery contract

**Scope**

* Docker-only build/start declaration and checked Railway manifest;
* Git SHA, image ID, schema, config, and content version release provenance;
* one replica, controlled stop-old/start-new non-overlapping deployment;
* current backup and manual pre-change backup;
* compatible code rollback vs forward fix vs destructive restore decision tree;
* scheduler reconstruction after restore;
* testnet and production built from the same release behavior with disjoint
  stores and credentials.

**Primary audit coverage:** `OPS-01`, `OPS-06…08`, completion of `FD-12`.

### WP-09.4 — Run testnet acceptance, device QA, drills, and capacity tests

**Scope**

* real iOS and Android notification preview and Telegram journey;
* enrollment → onboarding → delivery → complete/skip/feedback → expiry → time
  change → pause/resume/cancel → Coach/tool → summary → continuation → on-demand;
* duplicate tap/update and stale callback;
* Telegram timeout, GIF failure, OpenAI outage, Redis outage, database outage,
  SIGTERM during delivery, restart between side effects, and restored database;
* delivery burst at twice the largest expected launch cohort;
* exact release checklist with recorded evidence.

**Primary audit coverage:** `UX-02`, `UX-04`, `UX-17`, `OPS-10…11`, all
cross-domain beta acceptance requirements.

### Block 9 Go/No-Go

The founder reviews the completed release checklist. A release does not proceed
because most tests passed; every failed gate has an explicit No-Go, correction,
or documented founder exception that does not violate a privacy/security/legal
boundary.

### Main integration gate — explicit and not memory-dependent

`main` remains the stable release branch while implementation accumulates on
`implementation/pre-mvp`. The transition is a required Block 9 closeout action,
not an informal task to remember later:

1. WP-00.4 records which Git branch, if any, each Railway environment watches
   and ensures that merging `main` cannot accidentally launch company
   production.
2. WP-09.3 pins the exact release-candidate Git SHA and reproducible artifact.
3. WP-09.4 and Gate G3 must pass against that exact SHA.
4. A reviewed PR is created from `implementation/pre-mvp` to `main`; CI and the
   release diff must pass with no unrelated or unverified package.
5. Only after an explicit founder Go decision does the founder manually merge
   the verified SHA to `main`; the resulting release is then tagged as the
   release of record.
6. The merge, tag, source SHA, resulting `main` SHA, and post-merge verification
   are written into the release checklist. They may also be copied into the
   private founder daily log, but that copy is optional.

Block 9 cannot be marked complete while the verified release exists only on the
implementation branch. Block 10 deploys the tagged release-of-record; it does
not introduce unreviewed application changes during production launch.

---

## 16. Block 10 — Company production gate and controlled launch

**Status:** `NOT STARTED`  
**Depends on:** verified B9 testnet release and Gate G4  
**Objective:** launch the first company deployment without improvising access,
privacy, support, measurement, or recovery.

### WP-10.1 — Close legal, commercial, and deployment facts

**Scope**

* legal roles, notice, contracts, subprocessor facts, and DPIA determination;
* organization/champion/support owner;
* pilot start/end, observation window, renewal boundary, free/paid status and
  pricing basis without building speculative billing;
* launch roster version and `eligible_count_at_launch`;
* timezone mode, notice version, channel, language/device baseline;
* written pilot question limited to activation and product interaction;
* exact post-pilot company output and end-of-pilot behavior.

**Primary audit coverage:** `COMP-03`, `COMP-05`, `COMP-07`; pilot pricing
remains the recorded open founder decision.

### WP-10.2 — Complete the per-deployment launch checklist

Run and retain the full 27-point Company Deployment Checklist from the audit,
including:

* roster preview/acceptance and bidirectional reconciliation tests;
* OIDC/email fallback and neutral failure tests;
* one-time token and log-leak tests;
* testnet real-phone smoke path;
* corporate device/network reachability;
* deployment-scoped pause and incident owner;
* production/testnet separation;
* champion announcement and support path.

**Primary audit coverage:** `COMP-09…10`, `SEC-10`, `FD-18`.

### WP-10.3 — Execute the controlled production release

**Scope**

* record release manifest and Go decision;
* verify the deployable artifact resolves to the tagged, reviewed `main`
  release-of-record created at the Block 9 Main integration gate;
* create fresh pre-migration backup;
* run rehearsed forward migrations once;
* stop old owner, start exact new artifact, verify `/ready` and no overlap;
* run production-safe smoke without behavioral test pollution;
* open enrollment only after operational verification;
* keep rollback/restore and kill switches immediately reachable.

### WP-10.4 — Monitor launch and close the deployment round

**Scope**

* D1/D3/D7 founder review of the canonical funnel;
* distinguish denial, non-redemption, delivery failure, blocked bot, timezone
  error, and product non-use;
* record abnormal weeks and incidents;
* roster-reconciliation reminders and overdue alerts without auto-revocation on
  missing input;
* deployment retrospective before the next company;
* produce the one permitted post-pilot company summary only if the 100/50 gate
  is met.

**Primary audit coverage:** remaining `COMP-01…10`, `FD-09`, relevant
`TEL-01…15` and `OPS-09…11` release evidence.

---

## 17. Open decision gates

These are not new Founder Decisions. They are named points where implementation
must pause for founder, medical, legal, or device evidence rather than invent a
policy.

| Gate | Needed by | Required decision/evidence | Default while open |
|---|---|---|---|
| `DG-01` | WP-04.3 | Exact employee delivery window and company configurability | Do not hardcode an audit example |
| `DG-02` | WP-03.1 | Cool-water medical review and asset approval | Exclude cool water from eligible content |
| `DG-03` | WP-03.3 / WP-09.4 | Real-device preview choice, e.g. `Пауза` vs `Перерва` | Keep copy provisional and neutral |
| `DG-04` | WP-05.3 | Real product-question escalation destination or removal of promise | Do not promise escalation |
| `DG-05` | WP-08.1 | Remove `MORNING` or retain one frozen guarded definition | Keep frozen; add no new behavior |
| `DG-06` | WP-10.1 | First pilot pricing and unit of sale | Build no billing system |
| `DG-07` | Gate G4 | Legal roles, notice, contract, cross-border, DPIA determination | No company production launch |
| `DG-08` | WP-03.1 | Final GIF art direction/assets | Technique-sensitive item is ineligible without required asset |

---

## 18. Explicitly deferred or out of scope

The implementation program must not silently add:

* behavioral adaptation, inferred traits, personalization, churn prediction, or
  hidden future-cycle changes;
* exercise levels, unlocks, runtime variations, or catalogue expansion to
  rescue retention before beta evidence;
* time picker, Mini App, persistent keyboard, user-browsable catalogue, or
  workflow/calendar/IDE triggers;
* Slack, WhatsApp, native app, or speculative channel adapters;
* company dashboard, live analytics, team/office slices, individual employer
  view, or wellbeing/productivity/ROI claims;
* D14/D30 automatic verdicts, causal channel claims, or statistical confidence
  unsupported by the cohort;
* multi-replica scheduler, leader election, message broker, universal workflow
  engine, data warehouse, vector store, or broad Redis redesign;
* PITR, zero-downtime deployment, or universal outbox framework unless real
  beta requirements justify them;
* automated billing before the first pilot pricing decision;
* clinical/medical product claims or crisis services that do not exist.

`MISC-02` time-picker UX is explicitly Phase 3/post-MVP. Deferral is not a
license to keep its rejected legacy predecessor.

---

## 19. Audit coverage ownership matrix

This matrix assigns every numbered finding to a primary implementation owner.
Cross-package dependencies remain in the package descriptions above.

| Finding area | Primary package ownership |
|---|---|
| Scheduler `SCH-01…06` | WP-02.1: `SCH-02`; WP-03.5: `SCH-03`; WP-08.1: `SCH-01`, `SCH-04…06` |
| Lifecycle `LIF-01…19` | WP-02.2: `LIF-01`, `LIF-09`, `LIF-16…18`; WP-03.5: `LIF-02…08`, `LIF-10…13`, `LIF-15`, `LIF-19`; WP-07.4: `LIF-14` |
| Onboarding `ONB-01…08` | WP-04.3: `ONB-01…08` |
| Coach `COACH-01…12` | WP-02.3: `COACH-01`, `COACH-03…05`, `COACH-11…12`; WP-05.1: `COACH-07…08`; WP-05.2: `COACH-09`; WP-05.3: `COACH-10`; WP-05.4: resolved/preserved `COACH-02`, `COACH-06` |
| Plan generation `PLAN-01…09` | WP-03.2: `PLAN-01…09` |
| Delivery `DEL-01…08` | WP-03.3: `DEL-01…02`, `DEL-07…08`; WP-03.4: `DEL-03…06` |
| Runtime tools `RT-01…15` | WP-02.1: `RT-05`; WP-02.3: `RT-01…04`, `RT-06…08`, `RT-10…15`; WP-05.2: `RT-09` |
| Lifecycle/FSM `FSM-01…13` | WP-01.3: `FSM-08`, `FSM-11…13`; WP-02.1: `FSM-01…05`, `FSM-07`, `FSM-10`; WP-02.2/02.3: `FSM-06`, `FSM-09` |
| Privacy `PRIV-01…10` | WP-04.2: `PRIV-01…05`, `PRIV-08…10`; WP-07.3: `PRIV-06…07` |
| Content `CONTENT-01…09` | WP-01.4: `CONTENT-07` identity; WP-03.1: `CONTENT-01…06`, `CONTENT-08…09` |
| Telemetry `TEL-01…15` | WP-07.1: `TEL-01…11`, `TEL-13`; WP-07.3: `TEL-12`; WP-07.2: `TEL-14…15` |
| Database/Redis `DB-01…20` | WP-01.1: `DB-01`; WP-01.2: `DB-09…10`, `DB-12…14`, `DB-18…19`; WP-01.3: `DB-02…04`; WP-01.4: `DB-05…07`, `DB-11`, `DB-15`; WP-03.4: `DB-08`; WP-04.2: `DB-16…17`; WP-09.2: `DB-20` |
| Delivery UX `UX-01…18` | WP-04.1/04.3: `UX-01`, `UX-12…13`, `UX-18`; WP-09.4: `UX-02`, `UX-04`, `UX-17`; WP-03.3: `UX-03`, `UX-15…16`; WP-03.4: `UX-05…07`; WP-05.3: `UX-08`; WP-08.1: `UX-09…10`; WP-05.2: `UX-11`; WP-07.4: `UX-14` |
| Security `SEC-01…10` | WP-00.4: `SEC-01…03`; WP-05.2: `SEC-05`; WP-05.3: `SEC-04`, `SEC-06`; WP-03.4: `SEC-07`; WP-08.2: `SEC-08…09`; WP-04.1: `SEC-10` |
| Legacy `LEG-01…04` | WP-02.1: `LEG-01…02`; WP-08.1: `LEG-03…04` |
| Company deployment `COMP-01…10` | WP-04.1: `COMP-01`, `COMP-04`, `COMP-06`, `COMP-08`; WP-07.1: `COMP-02`; WP-10.1: `COMP-03`, `COMP-05`, `COMP-07`; WP-10.2: `COMP-09…10` |
| Operations `OPS-01…12` | WP-01.1: `OPS-02`, `OPS-07`; WP-09.1: `OPS-05`; WP-09.2: `OPS-03…04`, `OPS-09`; WP-09.3: `OPS-01`, `OPS-06`, `OPS-08`; WP-09.4: `OPS-10…11`; WP-03.3/03.4: preserve `OPS-12` |
| Exercise on demand `EOD-01…12` | WP-06.1…06.3: `EOD-01…12` according to the audit's required implementation order |
| Miscellaneous `MISC-01…06` | WP-04.1: `MISC-01`; deferred: `MISC-02`; WP-08.3: `MISC-03…06` |

### Founder Decision implementation index

| Decision | Primary implementation owner |
|---|---|
| `FD-01…02` | WP-03.5 lifecycle completion and continuation |
| `FD-03…04` | WP-04.3 deterministic onboarding and internal slot tags |
| `FD-05` | WP-03.2 selection boundary; WP-03.4 optional feedback; WP-08.1 adaptation removal |
| `FD-06…07` | WP-03.3/03.4 presentation, callbacks, and feedback storage |
| `FD-08` | WP-01.3 and B2 lifecycle services |
| `FD-09` | WP-07.3 aggregation and B10 company gate |
| `FD-10` | WP-03.1 versioned Content Library |
| `FD-11` | B7 telemetry and interpretation ceiling |
| `FD-12` | WP-00.4, WP-01.1, and WP-09.3 recovery/release |
| `FD-13…14` | WP-03.3 and WP-04.3 user-facing product identity/preview |
| `FD-15` | WP-07.4 deterministic cycle summary |
| `FD-16` | WP-03.1/03.3 instructional GIF boundary |
| `FD-17` | WP-05.3 Coach admission and cost controls |
| `FD-18` | WP-04.1 access and WP-10.2 production deployment |
| `FD-19` | B6 independent on-demand channel |

---

## 20. Progress tracker

### Program blocks

A package checkbox is marked only when the package is `VERIFIED`. A block
checkbox is marked only when every package nested under it is verified and the
block exit gate passes.

- [x] B0 — Development and infrastructure readiness
  - [x] WP-00.1 — Freeze the authentic starting baseline
  - [x] WP-00.2 — Establish the AI-assisted development workspace
  - [x] WP-00.3 — Make the local environment reproducible
  - [x] WP-00.4 — Secure configuration and Railway topology
- [ ] B1 — Recovery, migration authority, and data foundations
  - [ ] WP-01.1 — Restore, inspect, and establish Alembic authority
  - [ ] WP-01.2 — Define the target schema and invariant ledger
  - [ ] WP-01.3 — Implement the plan-centric lifecycle migration
  - [ ] WP-01.4 — Establish event, privacy, and deployment primitives
- [ ] B2 — Target lifecycle and runtime actions
  - [ ] WP-02.1 — Remove dead lifecycle entrances and the schedule-adjustment tunnel
  - [ ] WP-02.2 — Build the authoritative lifecycle service
  - [ ] WP-02.3 — Complete runtime controls and plan-format behavior
- [ ] B3 — Content, plan generation, presentation, and scheduled delivery
  - [ ] WP-03.1 — Migrate the versioned Content Library
  - [ ] WP-03.2 — Correct deterministic plan generation
  - [ ] WP-03.3 — Introduce canonical ExercisePresentation and media delivery
  - [ ] WP-03.4 — Make scheduled delivery and callbacks reconcilable
  - [ ] WP-03.5 — Implement durable completion and automatic continuation
- [ ] B4 — Access, privacy minimum, onboarding, and deterministic controls
  - [ ] WP-04.1 — Implement deployment, entitlement, and enrollment authority
  - [ ] WP-04.2 — Implement the minimum privacy and data-rights lifecycle
  - [ ] WP-04.3 — Implement deterministic onboarding and user menu
- [ ] B5 — Coach integration and bounded agency
  - [ ] WP-05.1 — Build canonical Coach context and plan-aware tool exposure
  - [ ] WP-05.2 — Implement the bounded tool-result loop
  - [ ] WP-05.3 — Add failure, privacy, concurrency, and cost boundaries
  - [ ] WP-05.4 — Coach acceptance and regression suite
- [ ] B6 — Independent exercise-on-demand channel
  - [ ] WP-06.1 — Add the occurrence aggregate, service, and selector
  - [ ] WP-06.2 — Add command and Coach entry, delivery, callback, and expiry
  - [ ] WP-06.3 — Add on-demand events, privacy hooks, and reconciliation
- [ ] B7 — Telemetry, aggregation, cycle summary, and founder reporting
  - [ ] WP-07.1 — Complete feature instrumentation and event reconciliation
  - [ ] WP-07.2 — Implement canonical metrics and restricted founder view
  - [ ] WP-07.3 — Complete independent aggregation and personal deletion
  - [ ] WP-07.4 — Replace the legacy report with deterministic cycle summary
- [ ] B8 — Legacy closure, security hardening, docs, and tooling
  - [ ] WP-08.1 — Complete legacy removal
  - [ ] WP-08.2 — Harden dependencies, container, configuration, and incident controls
  - [ ] WP-08.3 — Synchronize documentation, website, and development tools
- [ ] B9 — Full-system verification and isolated testnet release
  - [ ] WP-09.1 — Establish the canonical release test and CI baseline
  - [ ] WP-09.2 — Implement runtime supervision, health, and monitoring
  - [ ] WP-09.3 — Produce one reproducible release artifact and recovery contract
  - [ ] WP-09.4 — Run testnet acceptance, device QA, drills, and capacity tests
- [ ] B10 — Company production gate and controlled launch
  - [ ] WP-10.1 — Close legal, commercial, and deployment facts
  - [ ] WP-10.2 — Complete the per-deployment launch checklist
  - [ ] WP-10.3 — Execute the controlled production release
  - [ ] WP-10.4 — Monitor launch and close the deployment round

### Current package

| Field | Value |
|---|---|
| Current package | `WP-01.1 — Restore, inspect, and establish Alembic authority` |
| Status | `IN PROGRESS` |
| Next action | Inspect the disposable testnet read-only and establish the authoritative Alembic baseline and migration harness |
| Current blockers | None for disposable founder-only refactoring; paid backup/restore remains deferred to Gate G1 before durable data or market launch |

### Private founder log

When useful, create or update
`love-yourself/session_log_YYYY-MM-DD.md` in the separate private `logs`
repository:

```text
Date and active package
Completed
Verification evidence
Decisions: context → decision → reason → consequence
Audit IDs moved and their new status
Open risks/blockers
Repository state and commits
Next exact action
```

This log is a founder convenience and private continuity record. It is not
required for GitHub code review, package verification, or merge. Everything a
reviewer needs must exist in the PR, work-package record, tests, or other
non-sensitive application-repository evidence.

The roadmap is updated only when package/block state, sequence, scope, or a
decision gate changes. Routine command output belongs in private notes or test
evidence, not in the roadmap.

---

## 21. Change-control rule

Roadmap changes fall into three classes:

1. **Clarification:** improves wording without changing product or sequence;
   edit in the applicable reviewable repository record. A copy in the private
   founder daily log is optional.
2. **Implementation discovery:** real code/data evidence changes a package's
   method or dependency; update the package and explain the evidence.
3. **Product/scope change:** changes an FD, privacy boundary, company output,
   beta hypothesis, or deferred feature; stop and obtain explicit founder
   approval before implementation.

No AI agent may silently convert an implementation convenience into product
behavior.

---

## 22. Immediate next step after approval

1. Complete WP-00.4 repository hardening and targeted verification.
2. Rotate compromised provider credentials with founder approval.
3. Run the founder-only testnet with explicitly disposable data during
   refactoring; defer paid backups and the verified scratch restore to Gate G1.
4. Confirm Gate G0.
5. Prepare `WP-01.1` against explicitly disposable testnet data; satisfy Gate
   G1 before company enrollment, market launch, or non-disposable migration.
6. Start application refactoring with a clean, reviewed, reversible package;
   do not begin with a broad rewrite.
