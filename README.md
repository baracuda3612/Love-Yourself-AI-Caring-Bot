
# Love Yourself — Wellness Bot pre-MVP

The current implementation combines an aiogram polling bot, FastAPI report
pages, APScheduler delivery, deterministic plan generation and OpenAI-backed Coach behavior,
PostgreSQL, and optional local Redis-backed state.

Implementation progress: B3, WP-04.1 and WP-04.2 are verified. The founder merged
WP-04.2 in [PR #271](https://github.com/baracuda3612/Love-Yourself-AI-Caring-Bot/pull/271)
on 2026-10-24 (`55645ec`); final automatic review of `9af0ce2` had no findings.
The next package is WP-04.3, deterministic onboarding and user menu.
See the [roadmap](docs/implementation/pre_mvp_implementation_roadmap.md) and
[next-session handoff](docs/implementation/work_packages/WP-04.3_start_message.md).
This is implementation status, not a production release claim.

Copy `.env.example` to an untracked `.env` for local development and replace
the placeholders with local-only values. The typed runtime accepts exactly
`dev`, `staging`, or `prod`; staging/production configuration fails closed when
required infrastructure or safe public settings are missing. The non-secret
Railway variable and isolation contract is documented in
`docs/deployment/railway_topology.md`.

## Local development

The supported runtime is Python 3.12.14. Rebuild the complete development
environment from the reviewed lock with:

```bash
make bootstrap
```

Common verification commands:

```bash
make test-collect
make test-targeted
make test
make audit
```

Override the targeted files when working on one package:

```bash
make test-targeted TARGET_TESTS="tests/test_plan_runtime_tools.py"
```

Ephemeral PostgreSQL and Redis are available through Docker:

```bash
make services-up
make services-down
```

`requirements.in` and `requirements-dev.in` contain reviewed direct
dependencies. Their compiled `.txt` files contain the exact transitive versions
used by local development and Docker. Artifact hashes and the final image digest
are added at the release-security gate after the target platform is fixed.

Build the same restricted Docker context used for the eventual release
artifact with:

```bash
docker build -t love-yourself:local .
```
