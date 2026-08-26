
# Love Yourself — Wellness Bot MVP (Telegram + OpenAI)

Minimal MVP:
- aiogram bot
- APScheduler daily message
- /ask with daily limit
- token usage logging
- PostgreSQL database 

See `.env.example` for config.

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
