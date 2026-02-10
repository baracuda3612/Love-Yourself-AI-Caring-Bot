# Audit: `plan_instances` ORM ↔ DB schema mismatch

## Короткий технічний висновок

`app.db.PlanInstance` очікує поля `contract_version` та `schema_version`, але початкова telemetry-міграція `20250305_add_telemetry_tables.sql` створює `plan_instances` без цих колонок. Через це будь-який ORM `SELECT` по `PlanInstance` падає з `UndefinedColumn`, бо SQLAlchemy включає всі mapped-колонки у запит. Це ламає `log_user_event(...)` у callback/scheduler flows і зупиняє виконання handler до `callback_query.answer()`, що в Telegram виглядає як «вічне завантаження».【F:app/db.py†L341-L349】【F:migrations/20250305_add_telemetry_tables.sql†L13-L19】【F:app/telemetry.py†L167-L177】

## Що очікує ORM (`PlanInstance`)

`PlanInstance` у ORM має такі поля:

- `id`
- `user_id`
- `blueprint_id`
- `initial_parameters` (`nullable=False`, default `{}`)
- `contract_version` (`nullable=False`, default `"v1"`)
- `schema_version` (`nullable=False`, default `"v1"`)
- `created_at`

Джерело: модель `PlanInstance`.【F:app/db.py†L341-L349】

## Що є у базовій SQL-схемі telemetry

`migrations/20250305_add_telemetry_tables.sql` створює `plan_instances` тільки з:

- `id`
- `user_id`
- `blueprint_id`
- `initial_parameters` (без `NOT NULL`/default)
- `created_at`

Колонок `contract_version` і `schema_version` тут немає. 【F:migrations/20250305_add_telemetry_tables.sql†L13-L19】

## Де саме використовується і чому падає

- `log_user_event(...)` викликає `_ensure_plan_instance(...)`.
- `_ensure_plan_instance(...)` робить ORM query по `PlanInstance` (`.first()`), що формує `SELECT` із усіма mapped-полями.
- Якщо в БД відсутні `contract_version`/`schema_version`, Postgres повертає `column ... does not exist`.

Код-шляхи:

- `_ensure_plan_instance(...)` у telemetry.【F:app/telemetry.py†L157-L177】
- `log_user_event(...)` у telemetry.【F:app/telemetry.py†L374-L401】
- Telegram callback handlers викликають `log_user_event(...)`.【F:app/telegram.py†L167-L173】【F:app/telegram.py†L216-L222】
- Scheduler telemetry викликає `log_user_event(...)` в `send_scheduled_message(...)`.【F:app/scheduler.py†L164-L177】

## Які частини системи зачіпає

- Telegram callbacks (`task_complete`/`task_skip`) — ризик зависання UX при exception у DB логуванні.【F:app/telegram.py†L148-L176】【F:app/telegram.py†L197-L226】
- Scheduler telemetry (`task_delivered` / `task_delivery_failed`) — логування падає у runtime path. 【F:app/scheduler.py†L155-L185】
- Інші місця, що пишуть telemetry через `log_user_event` (наприклад finalization/adaptations), також у зоні ризику.【F:app/plan_finalization.py†L335-L343】【F:app/plan_adaptations.py†L234-L247】
- Обчислення метрик (`plan_metrics.calculate_skip_streak`) непрямо залежить від стабільного наповнення `user_events`; коли логування падає, дані неповні/неконсистентні.【F:app/plan_metrics.py†L6-L29】

## Додатково виявлений mismatch

Окрім відсутніх колонок версій, є ще одна невідповідність:

- ORM: `initial_parameters` — `nullable=False`, default `{}`.
- Базова telemetry SQL-схема: `initial_parameters JSONB` без `NOT NULL` і без default.

Це не той самий crash, але це schema drift і потенційні `NULL` у даних. 【F:app/db.py†L346-L349】【F:migrations/20250305_add_telemetry_tables.sql†L17-L18】

## Validation SQL (після міграції)

```sql
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_name = 'plan_instances'
  AND column_name IN ('contract_version', 'schema_version', 'initial_parameters')
ORDER BY column_name;

SELECT id, user_id, contract_version, schema_version, initial_parameters
FROM plan_instances
ORDER BY created_at DESC
LIMIT 10;
```
