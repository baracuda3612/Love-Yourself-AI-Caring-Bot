# Local UX adapter — author evidence, not Telegram

Run from the repository root with dev dependencies. Uses existing test fixtures,
a fresh UUID-named PostgreSQL database and actual telegram/setup/menu handlers.
Only Telegram transport is captured; jobs are paused; Coach raises if invoked.
All identities and submitted values in session.json are synthetic test data.
Web email/code success responses are presentation fixtures, not authentication.

```sh
docker compose -f compose.test.yml -p ly-wp043-ux up -d --wait postgres
PYTHONPATH=. .venv/bin/python docs/implementation/work_packages/WP-04.3_evidence/ux-adapter/ux_adapter.py
```

Open http://127.0.0.1:8766/ux in a browser. Ctrl-C cleans up the new database;
then `docker compose -f compose.test.yml -p ly-wp043-ux down --volumes`.
Do not point this adapter at production or use real personal information.
The policy reader uses the actual product endpoint; entry/email/code/handoff
pages use actual renderers. No external provider/SMTP/Telegram handoff is tested.
The chat layout is a transport adapter, not a pixel-faithful Telegram preview.
Its footer overlap was corrected in the adapter; cancellation then passed.
