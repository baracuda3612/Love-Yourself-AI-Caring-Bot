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
| Startup, due recovery, expiry and active calendar | Future restoration is off-loop in pages of 32 by time and step ID. Each write re-reads current state under the owner fence. Due recovery admits four sends at a time and pages retry cleanup, interrupted attempts and dirty projections by 16. Expiry takes 32 IDs and enqueues continuation after commit. Because page limits bound materialized results, query plans and actual SQL scan or lock times still require measurement. Calendar commands run off-loop and use the existing exact post-commit reconciliation. |
| Telegram status projection | Status and feedback are captured in a short transaction. Telegram edit runs after commit, with a per-step in-process gate and a 20-second caller deadline. The second transaction records the exact projection sent so a later state remains discoverable. Unknown delivery attempts remain fenced by the durable attempt journal. |
| Completion and successor scheduling | PR #275's independent continuation, schedule and summary recovery remain authoritative. Each existing lane admits two jobs. Directly prepared reports enter the same report lane, with dedicated receipt workers. Existing scenarios cover a successor re-enabled before its first step, independently of the new onboarding scenario. |
| Privacy erasure, retention and export | The five-minute worker admits ten erasure cases, two concurrently. It scans one 128-job keyset page per case per pass. A durable cursor survives restart and includes custom historical jobs matched by step ownership. The user fence is committed before external cleanup and the receipt is written after erasure. Support export comes from a separate operator CLI, holds the subject's row lock while materializing that subject's data and preserves the existing coverage. Its duration and shared DB pressure are `NOT_PROVEN`. |
| Silent reminders and weekly snapshots | Both were scheduled at startup on the base. Silent sends wait serially per user. Weekly snapshots also await Telegram while visiting the active cohort. Startup now removes their persisted cron IDs. The daily pulse was already disabled. WP-08.1 still owns removal of the legacy implementations and related cleanup. |

## Configured per-process budget

| Resource | Limit and admission |
| --- | --- |
| DB connections | 20 general, 4 APScheduler, 6 reserved for durable transport receipts. Total 30 with zero overflow, a 2-second checkout wait and a 3-second connection setting. The receipt pool shares the PostgreSQL server and keeps its own checkout capacity. |
| HTTP | 3 enrollment workers, 1 operator worker, 2 private report/calendar workers and 4 SMTP/OIDC workers. Work above lane capacity receives a retryable 503. Authentication and proof validity continue to use their existing authority. |
| Bot | 32 polling updates in flight, 8 ingress DB workers and 2 calendar/scheduler effect workers. The default async blocking executor has 4 workers. |
| Delivery | 4 scheduled timer workers, 4 recovery worker slots and a common 4-send permit held through the transport result and receipt processing. The 4 exercise receipt workers are independent of general work. APScheduler reads at most 64 due persistent jobs at a time. Rejected date jobs leave pending DB facts for recovery. |
| Completion | 2 continuation, 2 successor scheduling and 2 report dispatch slots. A separate 2-worker receipt lane persists observed report outcomes. The durable source and obligations remain the backlog. |
| Privacy | 2 blocking workers and 2 active erasures. Retention selects at most ten 500-message batches and one 500-proof batch per pass. |

## Local and release evidence

The PostgreSQL and Redis rehearsals use the repository Compose file in an
isolated project, fresh migrated test databases and test-only credentials.
SMTP, OIDC and Telegram transports are controlled. The first-plan test advances
application clocks to the persisted due timestamp and invokes the exact
persisted job. The receipt-capacity test fills a two-slot general pool and
records receipt latency through a separate checkout. Observed local receipt
latency in two runs was 192–278 ms. The baseline deployment-lock case missed
a one-second deadline while the other user's transaction was open.

Live provider/device behavior, actual SQL deadlines and query plans under
contention, production pool saturation, recovery lag at a large backlog and
large-cohort capacity remain `NOT_PROVEN`. These settings and local results
establish no production throughput threshold.

## Independent local audit

The independent read-only review used `c5657abaca55e5be4a25b5ed772f21758de013c6`
against the integration merge base above. It reported two P1 and two P2
findings. The full local suite with the WebApp proof fixture corrected passed
881 tests against disposable PostgreSQL and Redis. The audit findings are open.

| Priority | Confirmed behavior or modeled consequence |
| --- | --- |
| P1 | An admission rejection removes a one-shot APScheduler job. An isolated five-job probe with four held workers reproduced the removal. The pending step survives in PostgreSQL, but four recoveries per 30-second pass can leave a backlog beyond the configured late window. |
| P1 | Telegram first-plan and calendar transactions commit before obtaining schedule-effect capacity. An admission failure returns pending proof while leaving automatic future reconciliation dependent on startup or user replay. |
| P2 | One operator lane handles bulk roster work and unrelated deployment revocation. A busy roster slot produces a 503 before that revocation can commit. |
| P2 | The five-minute privacy job resumes one 128-job page per case. Lag grows with the number of global jobstore entries. The local test covers a restart and exact deletion across two pages. |

The candidate is awaiting disposition and remediation before publication. The
local PostgreSQL results cover correctness under the exercised conditions.
