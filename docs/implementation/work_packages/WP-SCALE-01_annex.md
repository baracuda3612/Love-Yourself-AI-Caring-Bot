# WP-SCALE-01 — annex: runtime isolation in completed packages

**Status:** `READY` after this documentation PR merges. **Route:** one separate
off-roadmap remediation PR before WP-05.2. This annex does not reopen or
rename completed WPs, and it is not a new roadmap block. Its job is to verify
and repair reachable shared-resource bottlenecks already present in completed
package behavior. Make small isolated commits within that one PR, each with
focused evidence, followed by a full-diff independent local review.

## Why this annex exists

Functional tests and a zero-finding code review do not measure how a backlog,
slow database call or slow Telegram send affects other participants. The
2026-11-12 static audit and 2026-11-14 follow-up map are **local working
inputs, not repository dependencies or frozen findings**.
Their line numbers and severity do not transfer to the post-PR #275 baseline.
The implementation task must recheck each candidate from a supported entry
point and record whether it remains active, is already fixed, or is unproven.
The exact participant count at which the system slows down is `NOT_PROVEN`.

## Inventory and original behavior owners

| Surface to verify against the merged head | Original behavior owner | Annex disposition required |
|---|---|---|
| Enrollment/access: deployment-wide lock duration, roster/proof scans, synchronous SQL/SMTP in async HTTP routes | WP-04.1 | Bound one request's work and isolate slow participants without weakening grant/revocation, roster atomics or notice authority. Record each path's actual reachability and current guard. |
| Startup jobstore reconstruction, due delivery selection/recovery, expiry, Telegram keyboard edits, and active calendar-change reconciliation | WP-03.4 / WP-02.3 for delivery/runtime; WP-04.3 for active day/timezone editor and G01 | Bound rows, effects and wait time per pass; prevent one owner or a large backlog from delaying other owners. Preserve immutable attempts, unknown-send quarantine, due dates, pause and exact job/receipt reconciliation. |
| Completion preparation, next-cycle schedule and summary recovery | WP-03.5 follow-up / WP-05.1 | Verify PR #275 isolation and fairness under slow-owner/backlog conditions. Repair only a concrete remaining regression; do not restore summary-first blocking or manual release. |
| Privacy deletion/export and per-account jobstore scan | WP-04.2 | Bound per-pass work and lock/connection holding; retain exact erasure/export coverage and auditable completion. |
| Scheduled legacy silent reminders and weekly pulse snapshots | Legacy behavior; final removal owner WP-08.1 | Recheck whether cron paths are live. If live shared-resource work can stall current delivery, isolate or retire that active producer in this annex with regression proof; record the remaining WP-08.1 cleanup. Do not optimize a disabled path as if it runs. |

For each row, document the supported trigger, the query/lock/I/O boundary,
whether any work is unbounded, and how one slow owner affects another. Mark
unproved claims `NOT_PROVEN` without severity or a required fix. A removed,
disabled or already bounded path gets an explicit evidence-backed disposition.
For confirmed defects, prefer the smallest coherent bound or retirement;
avoid simply adding threads to an unbounded scan.

**Cross-package resource owner:** the WP-SCALE-01 implementer owns one
end-to-end budget for database connections, worker threads, in-flight jobs
and admitted backlog across HTTP, Telegram polling/callbacks, scheduled
delivery, receipts, completion recovery and privacy work. Account for both
existing database pools together without assuming they must be unified.
Define measurable limits and fair admission so bulk recovery or one slow
participant leaves capacity for buttons and durable delivery receipts.
Confirm the limits under concurrent representative workloads; a per-worker
limit alone does not establish a safe combined budget.

## Acceptance for the remediation PR

* Every active candidate has a bounded row/call budget, concurrency limit,
  timeout or deadline where meaningful, and a fair way to resume unfinished
  work. No DB transaction/session stays open across slow Telegram/SMTP I/O.
  Shared event-loop work does not perform blocking database/network calls.
* Slow-owner, backlog and restart tests use supported application entry points
  and disposable PostgreSQL for durable claims. They show an unrelated owner
  progresses, with exact DB/job/receipt effects and no duplicate sends or
  skipped due work. Report what was mocked or controlled.
* Cover first-plan creation while operator delivery is disabled, followed by
  re-enabling delivery **before** its first future exercise is due. Verify the
  committed plan survives, the exact first scheduled job is registered before
  its due time, and the exercise receives one confirmed delivery on schedule.
  Distinguish this from the existing successor-cycle re-enable test. Treat
  any failure as a finding only after proving the supported sequence on the
  post-PR #275 baseline.
* Measure local per-pass work, queue/recovery lag, DB pool and lock waits and
  event-loop lag where the implementation provides instrumentation. Preserve
  the measured setup and limits. Do not label a mock or static trace as
  production throughput proof.
* The independent reviewer checks the full annex diff and direct callers,
  including access, privacy, scheduling and continuation invariants. Close
  credible P0–P2 findings before the PR. The configured GitHub review follows
  on the same candidate. The founder alone merges.

## Boundaries and later owners

WP-05.2 owns bounded context reads and the one-tool result loop; WP-05.3 owns
Coach admission, timeouts, cost and concurrency; WP-05.4 checks assistant
regressions. WP-06/07 must design their new background work with bounded
selection and independent owners. WP-08.1 still owns complete legacy removal.
WP-09.2 owns runtime lag/queue visibility, WP-09.3 restart/restore, and
WP-09.4 the isolated capacity and provider/device rehearsal on the actual
release artifact. The capacity target comes from the expected cohort and
required headroom, not a fixed 30,000-user promise. Until those checks run,
scale and release readiness remain `NOT_PROVEN`.

After this annex PR merges, update the roadmap to mark it `VERIFIED` for
integration and make [WP-05.2](WP-05.2_start_message.md) the current package.

**Local implementation handoff, observed 2026-11-18:** an existing
`fix/runtime-isolation` worktree contains commit `5866b8f` for HTTP isolation
and an uncommitted `app/access.py` roster change. A separate clean
`docs/pilot-mobile-journey-gate` worktree holds an unrelated, unmerged
WP-09.4 roadmap edit. At kickoff use **one active worktree** based on the
post-PR #276 integration head. The merged documentation is already in that
base; do not copy it from an old worktree. Treat both older branches and the
roster diff as read-only candidates: compare each hunk with the current code,
record `reuse`, `already superseded`, or `reject`, and port only accepted
changes once. Do not merge, rebase or cherry-pick either old branch wholesale.
Preserve the dirty roster diff until its disposition is recorded. Retire extra
worktrees only after their unique work is preserved and no other task is using
them. The pilot edit stays outside WP-SCALE-01 and has its own later decision.
