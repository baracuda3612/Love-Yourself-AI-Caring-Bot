# WP-SCALE-01 local evidence

Integration base is `247a626f36bc0b60b9a6614a1655ed03d777b605`.

## Saved changes

| Saved source | Disposition against the integration base |
| --- | --- |
| `5866b8f`, async email start/verify and OIDC start | **reuse**. Preserve separate DB and provider phases. Enrollment SQL and Redis still ran in async routes and SMTP blocked the shared loop. |
| `5866b8f`, OIDC callback | **reuse**. The base already used a FastAPI sync worker and closed the session before exchange. The candidate bounds admission and reserves a provider worker before consuming the one-use state. |
| `5866b8f`, operator preview/apply/handoff/revoke/replacement and controls transaction | **reuse**. Move Redis and the transaction off the common loop. Operator work gets one slot of its own. |
| `5866b8f`, controls post-commit wake | **superseded** by PR #275. Keep the exercise and completion wake hooks with independent error handling. |
| `5866b8f`, AnyIO run variables and capacity implementation | **reject**. Use explicit worker admission shared with the runtime limits. A canceled caller does not release its worker slot early. |
| `5866b8f`, concurrent enrollment HTTP test | **reuse**. The controlled sender also runs on the base so the red result proves that the unrelated request waited for it to return. |
| Uncommitted `app/access.py`, roster-version helper and indexed membership lookup | **reuse**. The base still materializes every roster member inside verified grant. Use the current roster version and its `(roster_version_id, entry_key)` key for the specific identity. |
| `a5cd7be`, pilot checklist and gate | **reject** for this package. Both hunks belong to the later WP-09.4 decision. |

All three backup patch hashes match the handoff. Both extra worktrees and the dirty
roster diff were left in place. The main checkout is the sole implementation worktree.

## Supported entrances and observed effects

| Entrance | Shared boundary and evidence |
| --- | --- |
| Enrollment forms, OIDC callback and authenticated operator routes | On the base, a controlled SMTP call from `POST /enroll/{id}/email` stopped an unrelated enrollment GET until the call returned. The request now closes its SQL session before SMTP and the other request returns while SMTP remains in flight. Provider exchanges and Redis rate limits also use worker admission. |
| Telegram enrollment middleware and participant mutations | A real transaction holding the pinned-notice authority for one member made a second member of the same deployment miss the one-second middleware assertion on the base. Shared deployment read fences allow the second member through and retain exclusive fences for controls, roster application, revocation and replacement. The primary owner row still serializes conflicting operations on the same account. |
| Email/OIDC proof issuance and verified membership | The base's issuance deleted all old proofs across deployments during a request. Retention now selects up to 500 expired proofs per privacy pass, skipping locked rows. Verifying a member uses a single keyed roster lookup. Operator preview/apply still process a whole atomic roster change. Their lock duration at a large accepted roster size is `NOT_PROVEN`. |
| First onboarding plan with delivery disabled | Telegram `/start`, setup confirmation and the real operator controls route commit the first plan and switch. On the base, the stored future step had no job after re-enable. The candidate registers the exact future timer while disabled and fences the actual send at claim time. The rehearsal invokes the persisted job twice at the recorded due instant, with a controlled clock and Telegram response. PostgreSQL records one attempt, one message ID and one delivery event. |
| Startup, due recovery, expiry and active calendar | Future restoration is off-loop in pages of 32 by time and step ID. Each write re-reads current state under the owner fence. One failed restore item leaves a durable plan-schedule repair and the batch continues. Due recovery admits two sends independently of the four normal timer workers. Status projection has two recovery workers. Retry cleanup, interrupted attempts and dirty projections are selected in pages of 16. Expiry takes 32 IDs and enqueues continuation after commit. Because page limits bound materialized results, query plans and actual SQL scan or lock times still require measurement. Calendar commands run off-loop. Explicit lifecycle changes persist the owed proof in the same transaction. Repair leases at most two plans per poll, keyed by owner, and dispatches through the existing exact schedule and format-switch proofs. |
| Telegram status projection | Status and feedback are captured in a short transaction. Telegram edit runs after commit, with a per-step in-process gate and a 20-second caller deadline. The second transaction records the exact projection sent so a later state remains discoverable. Unknown delivery attempts remain fenced by the durable attempt journal. |
| Completion and successor scheduling | PR #275's independent continuation, schedule and summary recovery remain authoritative. Each existing lane admits two jobs. Directly prepared reports enter the same report lane, with dedicated receipt workers. Existing scenarios cover a successor re-enabled before its first step, independently of the new onboarding scenario. |
| Privacy erasure, retention and export | The five-minute worker admits ten erasure cases, two concurrently. It scans up to sixteen 128-job keyset pages per case per pass, saves the cursor after each page and schedules an early follow-up for cases that made progress. A durable cursor survives restart and includes custom historical jobs matched by step ownership. The user fence is committed before external cleanup and the receipt is written after erasure. Support export comes from a separate operator CLI, holds the subject's row lock while materializing that subject's data and preserves the existing coverage. Its duration and shared DB pressure are `NOT_PROVEN`. |
| Silent reminders and weekly snapshots | Both were scheduled at startup on the base. Silent sends wait serially per user. Weekly snapshots also await Telegram while visiting the active cohort. Startup now removes their persisted cron IDs. The daily pulse was already disabled. WP-08.1 still owns removal of the legacy implementations and related cleanup. |

## Configured per-process budget

| Resource | Limit and admission |
| --- | --- |
| DB connections | 18 general, 4 APScheduler, 8 reserved for durable transport receipts. Total 30 with zero overflow, a 2-second checkout wait and a 3-second connection setting. The receipt pool shares the PostgreSQL server and keeps its own checkout capacity. |
| HTTP | 3 enrollment workers, 1 operator worker, 1 bulk roster worker, 1 revocation worker, 2 private report/calendar workers and 4 SMTP/OIDC workers. The provider worker also runs the short OIDC finalization transaction after verification. At most 4 such transactions contend with ordinary work for the existing general pool. No database session spans provider I/O. Admission overflow returns 503 before OIDC state is consumed. Authentication and proof validity continue to use their existing authority. |
| Bot | 32 polling updates in flight, 8 ingress DB workers and 2 calendar/scheduler effect workers. The default async blocking executor has 4 workers. Durable schedule repair has 2 workers of its own. |
| Delivery | 4 normal timer sends and 2 recovery sends, with separate permits held through the transport result and receipt. The 6 exercise receipt workers use reserved connections. The persistent jobstore offers only as many due jobs as the executor can admit. At capacity it keeps unadmitted timers in PostgreSQL, and a free worker wakes the scheduler. |
| Completion | 2 continuation, 2 successor scheduling and 2 report dispatch slots. A separate 2-worker receipt lane persists observed report outcomes. The durable source and obligations remain the backlog. |
| Privacy | 2 blocking workers and 2 active erasures, with up to 2,048 jobstore rows per case per pass. Retention selects at most ten 500-message batches and one 500-proof batch per pass. |

## Local and release evidence

The PostgreSQL and Redis rehearsals use the repository Compose file in an
isolated project, fresh migrated test databases and test-only credentials.
SMTP, OIDC and Telegram transports are controlled. The first-plan test advances
application clocks to the persisted due timestamp and invokes the exact
persisted job. The receipt-capacity test fills a two-slot general pool and
records receipt latency through a separate checkout. Observed local receipt
latency across three runs was 172–278 ms. The baseline deployment-lock case missed
a one-second deadline while the other user's transaction was open.

Live provider/device behavior, actual SQL deadlines and query plans under
contention, production pool saturation, recovery lag at a large backlog and
large-cohort capacity remain `NOT_PROVEN`. These settings and local results
establish no production throughput threshold.

## Initial independent local audit

The independent read-only review used `c5657abaca55e5be4a25b5ed772f21758de013c6`
against the integration merge base above. It reported two P1 and two P2
findings. The full local suite with the WebApp proof fixture corrected passed
881 tests against disposable PostgreSQL and Redis before remediation.

| Priority | Confirmed behavior or modeled consequence |
| --- | --- |
| P1 | An admission rejection removes a one-shot APScheduler job. An isolated five-job probe with four held workers reproduced the removal. The pending step survives in PostgreSQL, but four recoveries per 30-second pass can leave a backlog beyond the configured late window. |
| P1 | Telegram first-plan and calendar transactions commit before obtaining schedule-effect capacity. An admission failure returns pending proof while leaving automatic future reconciliation dependent on startup or user replay. |
| P2 | One operator lane handles bulk roster work and unrelated deployment revocation. A busy roster slot produces a 503 before that revocation can commit. |
| P2 | The five-minute privacy job resumes one 128-job page per case. Lag grows with the number of global jobstore entries. The local test covers a restart and exact deletion across two pages. |

The founder approved all four repairs. The corrective rehearsal used the
new Alembic revision `20261119_schedule_reconcile`. It verified an eight-timer
burst with four workers held. The four excess timers remained in the SQL
jobstore and drained in 21 ms after release in one local run. The first-plan
case also saturated both foreground scheduling workers and restarted the
APScheduler instance. Repair took 223 ms from dispatch to durable proof in
one local run, before the first exercise became due. A companion case
verified format-switch and calendar jobs against the latest committed dates.
Revocation proceeded while a roster preview was held. The erasure case
verified an early follow-up, a persisted cursor and exact historical job
ownership after restart. These measurements describe these fixtures and
controlled transports.

The next independent review closed the original four findings and identified
one P2 in the format-switch handoff. A time change committed between job proof
and the switch-ready receipt could lose its newer repair. The ready receipt
now consumes only the repair timestamp read for that switch, keeping a newer
time change queued. A controlled concurrent PostgreSQL test checks the exact
recovered job dates without replaying the participant's action.

In a separate synthetic burst, 128 SQL-backed timers became due together.
While four workers were held, the other 124 timers stayed in PostgreSQL. After
release, all 128 callbacks ran exactly once and the queue drained in 709 ms.
Callbacks used 5 ms of controlled work. An in-process enrollment GET measured
8.4 ms p95 across 44 requests during the drain.

## PR #277 follow-up

- The routed Google callback retains the admitted provider worker through finalization, with a separate short database transaction before and after provider I/O. A disposable PostgreSQL test holds all three ordinary enrollment workers during provider return. It checks that the callback still creates one entitlement and that replay is denied. Provider responses are controlled in this test.
- The production entry point awaits core scheduler initialization before starting HTTP or polling. A failed initialization exits for the runtime to restart. After successful initialization, restoration runs under the service task group and scans future timers in the existing bounded batches. The startup test checks that an initialization exception prevents either ingress from opening.
- The Telegram router captures the action time before waiting for the actor or database. The expiry sweep skips locked owners and defers expiry for a matching timely action still in flight in the single polling process. PostgreSQL remains authoritative for ownership, current access and the committed terminal fact. A dispatcher test holds a real owner lock past the deadline, runs expiry both under and after the lock, and checks unrelated progress, one completion, a duplicate click and a late click. Updates awaiting dispatch or lost with the polling process before commit are outside this in-process ordering proof.

The separate mixed-load audit and measurements against live dependencies are
release evidence still to be collected.
