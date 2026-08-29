# WP-01.2 storage and infrastructure unit economics

**Model date:** 2026-08-29

**Scope:** planning model for the WP-01.2 target contract; no Railway or
OpenAI account inspection, billing mutation, schema change, or production-data
query

This note answers a narrower question than commercial pricing: whether the
target persistence choices are likely to be economically proportionate at
100, 1,000, 10,000, and 100,000 active users. It separates provider facts from
workload assumptions so the model can be replaced by measured usage later.

## Executive finding

The target schema is storage-cost efficient through the first pilot and normal
growth. On Railway, correctness-critical immutable snapshots and relational
constraints cost very little compared with application/database memory and
Coach model calls. Removing those records to save volume fees would create
material correctness/privacy risk for negligible savings.

The two scale-sensitive parts are different:

1. **accumulating user-linked events, delivery history, and snapshots** become
   a capacity/maintenance problem before they become a direct storage-price
   problem; and
2. **Coach tokens** are the likely dominant marginal cost at any meaningful
   engagement level.

At 100,000 monthly active users, the one-year PostgreSQL footprint is modeled
at roughly 200–800 GB, or $30–120/month in Railway volume charges. The same
population at the reference Coach workload is roughly $16,000–27,000/month in
OpenAI model usage, before discounts or negotiated pricing. These are planning
ranges, not a provider quote.

## Provider price facts

Railway's current public usage rates are:

* Pro has a $20 monthly minimum and that amount counts toward resource usage;
* RAM: $10/GB-month;
* CPU: $20/vCPU-month;
* service egress: $0.05/GB;
* volume storage: $0.15/GB-month;
* object storage: $0.015/GB-month with free bucket egress;
* Pro advertises up to 1 TB of volume storage and 3,000 volume IOPS.

Sources: [Railway pricing documentation](https://docs.railway.com/pricing),
[Railway plan and limit table](https://railway.com/pricing), and
[Railway right-sizing formula](https://docs.railway.com/guides/right-size-cpu-memory).
Prices exclude tax and may change.

The checked-in runtime defaults are `COACH_MODEL=gpt-5.1`,
`MODEL=gpt-5-mini`, `PLAN_MODEL=gpt-4.1-mini`, and a 300-token output cap.
Current official OpenAI text-token prices per one million tokens are:

| Model | Input | Cached input | Output |
|---|---:|---:|---:|
| `gpt-5.1` | $1.25 | $0.125 | $10.00 |
| `gpt-5-mini` | $0.25 | $0.025 | $2.00 |
| `gpt-4.1-mini` | $0.40 | $0.10 | $1.60 |

Sources: official OpenAI documentation for
[`gpt-5.1`](https://developers.openai.com/api/docs/models/gpt-5.1),
[`gpt-5-mini`](https://developers.openai.com/api/docs/models/gpt-5-mini), and
[`gpt-4.1-mini`](https://developers.openai.com/api/docs/models/gpt-4.1-mini).

## Population and workload assumptions

The scale axis below means **monthly active users (MAU)**, not rostered,
eligible, invited, or merely registered users. That distinction is essential:
identity/profile rows are cheap, while events, chat, deliveries, and AI calls
follow actual activity. If a 100,000-seat deployment has 30% MAU, its variable
workload is closer to the 30,000-MAU case.

The reference active user generates in one month:

* 31 scheduled exercise occurrences (a mix of 7-day and 14-day plans);
* 4 on-demand occurrences;
* about 105 allow-listed personal events;
* 20 Coach turns / 40 retained chat messages;
* bounded feedback and aggregate-contribution rows.

This is intentionally a planning scenario, not a product forecast. The target
contract does not impose a visible Coach or on-demand quota. Light and heavy
behavior must therefore be measured separately.

## PostgreSQL volume model

The model includes row headers, foreign keys, high-value B-tree indexes,
immutable snapshots, TOAST/MVCC/vacuum headroom, and bounded 90-day chat. It
is limited to live application data and runtime resources.

After one year, use **2–8 MB per MAU** as the planning range:

* identity, entitlement, profile, and current lifecycle are a small fixed
  component;
* plan steps, delivery records, presentation/content snapshots, and
  user-linked events accumulate with active months;
* chat is bounded at 90 days;
* restricted aggregate contributions must remain bounded and sealed cells are
  compact.

| MAU | One-year PostgreSQL range | Railway volume cost/month |
|---:|---:|---:|
| 100 | 0.2–0.8 GB | $0.03–$0.12 |
| 1,000 | 2–8 GB | $0.30–$1.20 |
| 10,000 | 20–80 GB | $3–$12 |
| 100,000 | 200–800 GB | $30–$120 |

The dominant uncertainty is retention duration for user-linked behavioral
history. The accepted audit model retains it while the account exists, subject
to the final Privacy Policy and deletion workflow. After bounded stores reach
steady state, model another **0.15–0.60 MB per active-user-month**. At 100,000
continuously active users, a three-year footprint can therefore reach roughly
560 GB–2.24 TB. The high case exceeds Railway Pro's advertised 1 TB volume
limit. Before that scale, WP-09.4 capacity tests and the final retention policy
must choose partitioning/retention and, if necessary, a different database
topology. WP-01.2 must not invent that policy.

## Redis RAM model

Redis is billed as RAM, about 67 times the Railway price per GB of PostgreSQL
volume. Keeping a 90-day duplicate cache would therefore be economically
backwards. The target now uses:

* PostgreSQL as 90-day durable conversation truth;
* at most 20 timestamped cached messages;
* per-message 90-day pruning;
* a one-day Redis key TTL and PostgreSQL rebuild on miss.

At 25–50 KB per cached user, the all-users-cached ceiling at 100,000 MAU is
2.5–5 GB, or $25–50/month. With 20% of MAU using Coach on a given day, the
one-day TTL holds about 0.5–1 GB, or $5–10/month. This is why the shorter key
TTL is a real economic improvement without changing retention or product
truth.

| MAU | Redis at 20% daily Coach activity | RAM cost/month | All-cached ceiling |
|---:|---:|---:|---:|
| 100 | 0.0005–0.001 GB | <$0.01 | $0.03–$0.05 |
| 1,000 | 0.005–0.01 GB | $0.05–$0.10 | $0.25–$0.50 |
| 10,000 | 0.05–0.10 GB | $0.50–$1.00 | $2.50–$5.00 |
| 100,000 | 0.5–1.0 GB | $5–$10 | $25–$50 |

## Railway compute planning envelope

User count alone does not determine CPU/RAM cost. Delivery bursts, database
query plans, scheduler behavior, connection counts, Coach concurrency, and
replica count do. Railway's own formula is:

```text
monthly cost ~= avg RAM GB * $10
             + avg vCPU * $20
             + egress GB * $0.05
             + volume GB * $0.15
```

The table below is a **single-region reference capacity scenario**, not a
forecast or accepted production sizing. It provides a consistent placeholder
until WP-09.4 measures p50/p95 load and real resource averages.

| MAU | Assumed avg RAM | Assumed avg CPU | Egress | High-case volume | Reference Railway bill |
|---:|---:|---:|---:|---:|---:|
| 100 | 1.5 GB | 0.10 vCPU | 5 GB | 0.8 GB | $20 minimum |
| 1,000 | 2 GB | 0.25 vCPU | 20 GB | 8 GB | about $27 |
| 10,000 | 7 GB | 1.5 vCPU | 100 GB | 80 GB | about $117 |
| 100,000 | 35 GB | 9 vCPU | 1,000 GB | 800 GB | about $700 |

The 100,000-MAU row is not a statement that the current one-replica scheduler
architecture is production-ready at that scale. Redundancy, burst capacity,
writer/scheduler leadership, and database maintenance could make the real
Railway bill 2–3 times higher. Those are WP-09.2/WP-09.4 decisions triggered by
measured requirements, not reasons to add speculative infrastructure in
WP-01.2.

## OpenAI cost sensitivity

The current Coach system prompt is about 20,661 characters (roughly 5,200
tokens before tools, state, and conversation context), so token cost cannot be
modeled as a tiny chat prompt. A reasonable reference Coach turn is 8,000
input tokens and 250 output tokens. At 20 turns per MAU-month:

```text
uncached Coach cost/user-month
  = 20 * (8,000 / 1M * $1.25 + 250 / 1M * $10)
  = $0.25
```

Allow another $0.01–$0.03 per MAU-month for `gpt-5-mini` and
`gpt-4.1-mini` work. If about 5,000 stable-prefix tokens per Coach call achieve
the published cached-input price, the reference total falls to roughly $0.16;
without a cache hit it is roughly $0.27.

| MAU | Reference AI cost with cache sensitivity |
|---:|---:|
| 100 | $16–$27/month |
| 1,000 | $160–$270/month |
| 10,000 | $1,600–$2,700/month |
| 100,000 | $16,000–$27,000/month |

A light user with about five shorter Coach turns is roughly $0.05–$0.07/month;
a heavy user with 40 long turns is roughly $0.70–$0.80/month. Because the MVP
has no visible quota, FD-17's global cost circuit breaker and privacy-safe
token/model/outcome telemetry are necessary economic controls, not optional
dashboard polish.

## Combined reference and margin implication

Using a cache-efficient AI reference ($0.17/MAU-month) and the single-region
Railway placeholder above:

| MAU | Railway | OpenAI | Combined | Combined per MAU |
|---:|---:|---:|---:|---:|
| 100 | $20 | $17 | $37 | $0.37 |
| 1,000 | $27 | $170 | $197 | $0.20 |
| 10,000 | $117 | $1,700 | $1,817 | $0.18 |
| 100,000 | $700 | $17,000 | $17,700 | $0.18 |

This excludes taxes, support, engineering/on-call, legal/compliance, customer
success, sales/CAC, payment fees, Telegram-adjacent services, production
redundancy, and negotiated provider terms. It is therefore not gross margin or
a recommended selling price.

As a simple infrastructure-only guardrail, a base variable cost near
$0.18–$0.20 per active user requires roughly $0.90–$1.00 revenue per active
user for an 80% infrastructure gross margin. A heavy-Coach case near $0.75
requires about $3.75. Commercial pricing must also decide whether the unit of
sale is an eligible seat, enrolled user, MAU, deployment minimum, or a hybrid;
WP-10.1 already owns that founder decision and explicitly forbids speculative
billing implementation.

## Decision assessment

Keep:

* immutable plan/content/presentation snapshots — their storage cost is small
  and they prevent historical truth from silently changing;
* normalized foreign keys, partial unique constraints, and idempotency keys —
  they reduce duplicate deliveries/events and therefore avoid both correctness
  failures and wasted provider spend;
* canonical `user_events` plus independent restricted contributions/sealed
  aggregates — this is the privacy-preserving measurement contract, provided
  contribution retention remains bounded;
* PostgreSQL as conversation truth with a disposable Redis cache.

Do not add now:

* speculative GIN/duplicate indexes, event payload copies, or indefinite Redis
  history;
* a second event/lifecycle/content store for analytics;
* an object-store copy of relational behavioral truth merely because its GB
  price is lower;
* a billing subsystem or user-visible quota.

Required measurement handoffs:

* **WP-05.3:** persist/emit privacy-safe model, input/cached/output token,
  latency, outcome, and estimated-cost telemetry; enforce the global cost
  circuit breaker;
* **WP-09.2:** Railway usage/cost alerts and dependency/runtime supervision;
* **WP-09.4:** production-shaped capacity tests and p50/p95 resource baselines;
* **WP-10.1:** choose the pilot pricing basis and unit of sale without building
  speculative billing.

Until those measurements exist, the correct conclusion is directional:
**the target data model is economically proportionate; Coach utilization and
long-lived event volume, not integrity constraints or snapshots, are the
variables that need active cost control.**
