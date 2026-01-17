# Plan Assets

plan_context_template.yaml:
- є шаблоном canonical shape для plan_context;
- не використовується напряму в runtime;
- заповнюється бекендом під час створення snapshot.

Runtime Plan Context:
- є snapshot стану плану;
- передається Coachʼу як цілісний обʼєкт без додаткової збірки на льоту;
- telemetry буде агрегуватись окремо пізніше.

plan_context is a read-only snapshot for AI agents. Any mutation must go through backend workflows.
