# Prompt Review — TODO (відкриті задачі між сесіями)

## НАСТУПНА СЕСІЯ починає тут: integration pass перед merge PR #245 — НЕ МЕРДЖИТИ поки не зроблено

PR [#245](https://github.com/baracuda3612/Love-Yourself-AI-Caring-Bot/pull/245)
відкритий, **не мерджити**. Незалежний рев'ю (кодекс) перевірив файл
з диска і remote SHA (`eb3aa82`) — версії локально/на GitHub не
роз'їхались, файловий scope PR чистий (жодних `.DS_Store`/`.venv`/`R&D`/
чужих файлів). Проблема інша: **весь prompt cleanup застосовувався до
`COACH_SYSTEM_PROMPT`, але не синхронізувався з іншими джерелами правди
в тому самому файлі** — класичний duplicate source of truth drift, не
git-проблема і не проблема пам'яті чату.

**Не робити повторний повний рефакторинг файлу.** Потрібен вузький
integration pass по 5 стиках:
```
1. COACH_SYSTEM_PROMPT ↔ COACH_TOOLS
2. Section 4 ↔ _context_message() / _compose_messages()
3. Product Map ↔ фактичний lifecycle
4. Section 7 ↔ orchestrator execution/result handling
5. Tool descriptions ↔ runtime guards і аргументи
```

**Merge-blocker (обов'язково перед merge):**

1. **`COACH_TOOLS` не синхронізовано з фінальною Section 7.**
   Файл: `app/workers/coach_agent.py:803` (номер рядка орієнтовний,
   звірити заново). Конкретно:
   - `create_followup_plan` tool description досі дозволяє
     `IDLE_FINISHED` та `IDLE_DROPPED` — Section 7 звузила до тільки
     `IDLE_PLAN_ABORTED`.
   - time tools (`change_day_time`/`change_evening_time`/
     `record_evening_time`) tool descriptions досі вимагають від
     користувача писати точний `HH:MM` — Section 7 Time Arguments
     дозволяє natural language з нормалізацією на боці Coach.
   - `cancel_plan` tool description досі містить старий keyword-парсер
     `"permanently"/"forever"` і наказ пропонувати pause — Section 7
     це прибрала (neutral disambiguation, не steering).

   **Чому це особливо погано:** модель бачить `COACH_SYSTEM_PROMPT` і
   `COACH_TOOLS` разом в одному API-виклику — два суперечливі контракти
   одночасно, не "стара версія десь забута", а активна суперечність
   яку модель отримує щоразу.

2. **`_context_message()` досі каже `"treat as remembered facts"`.**
   Файл: `app/workers/coach_agent.py:750` (орієнтовно). Section 4
   навмисно прибрала весь "as if you remember" framing (Memory
   Honesty) — цей рядок у коді нижче промпту досі суперечить.

3. **Section 7 досі містить пропущений `Mention an available
   alternative...` рядок.** Файл: `app/workers/coach_agent.py:668`
   (орієнтовно). Фінальне рішення — прибрати retention-offer,
   тільки пояснювати недоступність дії без пропозиції альтернативи.
   Проста мікроправка, яку не встигли застосувати.

4. **Product Map (`conceptual_map.md:71`) досі описує старий вибір
   після завершення 7 днів**, не `FD-01` auto-continuation. Це вже
   відоме відкладене питання (Product Map lifecycle — Section 2.5
   TODO записи вище), але оскільки документ **постійно передається
   Coach-у** (`COACH_PRODUCT_MAP` в кожному API-виклику), конфлікт
   реальний, не гіпотетичний — Coach бачить дві версії lifecycle
   одночасно.

**Тести на момент рев'ю:** `25 passed, 2 failed` — обидва fails через
відсутній `trio` в environment, той самий давній issue, не пов'язаний
з цією роботою. Python компілюється. У changelog є trailing whitespace
— косметика, не блокер.

**Статус:** PR лишається відкритим, не мерджити. Наступна сесія починає
з цих 4 пунктів (COACH_TOOLS sync — найважливіший, потім два
one-line фікси, потім Product Map lifecycle рядок), не з повного
перечитування файлу.

## Sequencing decision: фінальний прохід Section 1-2 — ПІСЛЯ завершення MVP-аудиту

**Питання:** Section 1-2 писались до появи `pre_mvp_code_audit_findings.md`.
Чи перепройти їх зараз з урахуванням аудиту, чи спершу завершити аудит?

**Рішення:** завершити MVP-аудит першим, потім один фінальний прохід
Section 1-2.

**Чому:**
- Section 1 (Identity & Persona) — про тон і персону, майже не залежить
  від продуктової механіки. Низький ризик застарілості, може почекати
  без шкоди.
- Section 2 (System Awareness & Boundaries) — прямо залежить від блоків
  аудиту які **ще не пройдені**: Privacy межа (individual vs
  company-facing — стосується того що Coach каже про конфіденційність),
  Content library (slot not user-facing — стосується Exercise
  Explanation Boundary), Delivery renderer P0 (стосується того що Coach
  може вважати "вже доставленим" юзеру), Plan generation (SHORT/
  state_switch — може вплинути на Product Support формулювання).
- Якщо переписати Section 2 зараз під те що вже відомо (`FD-01`,
  `FD-04`, `ONB`-знахідки), а потім Privacy/Content Library/Delivery
  Renderer аудит принесе нові рішення — доведеться редагувати той самий
  текст втретє. Та сама проблема яку вичищали весь prompt cleanup:
  не робити роботу яку доведеться переробляти.

**Наслідок:** Section 1-2 фінальний прохід — окрема задача, запланована
після завершення (або принаймні суттєвого просування) блоків Privacy,
Content Library, Delivery Renderer, Plan Generation з
`pre_mvp_code_audit_findings.md`.

## Architecture decision: Bounded Tool-Result Loop — P1, до першого зовнішнього MVP-юзера

**Контекст.** Поточна архітектура Coach — one-shot command dispatcher:
```
Coach → tool call → orchestrator executes → deterministic Telegram template
```
Coach ніколи не отримує результат виконання назад. `reply_text`
примусово порожній при tool_call, `_TOOL_REPLY_TEMPLATES` формує
відповідь захардкодженим текстом українською, незалежно від мови/тону
поточної розмови (DSM language mirroring тут не діє — canned template
завжди українською, навіть якщо розмова йшла англійською).

Це відрізняється від типового tool-use flow в Claude/OpenAI (Codex,
Claude Agent SDK), де: модель повертає `tool_use`/function call →
runtime виконує → результат повертається моделі як `tool_result` →
модель формує фінальну відповідь на основі факту. Наша схема свідомо
спрощена — дешевша, швидша, Coach не може перекрутити результат
операції. Але має реальну ціну.

**Знайдений доказ проблеми (не гіпотетичний):** `get_plan_status` для
paused-стану хардкодом відповідає "активний план" (див. Backend Audit
Backlog вище, пункт 4, верифіковано в коді). Це не просто менш
"людський" UX — canned templates є **окремим source of truth**, який
дрейфує від runtime і вже реально розійшовся в одному підтвердженому
місці.

**Founder-оцінка ризику:** цільова аудиторія (програмісти) звикла до
сучасного tool-use UX де AI застосував tool call і далі пише по-людськи
з урахуванням результату. Раптова поява захардкодженого "SUCCESS"-стилю
повідомлення посеред живої розмови одразу читається як "грубо склеєний
workflow" — конкретний репутаційний ризик для цієї аудиторії, не
абстрактна естетика.

**Рішення: Bounded Tool-Result Loop, не необмежений agent loop.**
```
1. Coach робить максимум один tool call.
2. Backend виконує його.
3. Результат повертається Coach-у як структуровані факти
   (не Python-помилка, не internal state name):
   {"status": "success", "action": "pause_plan",
    "facts": {"delivery_paused": true, "sequence_preserved": true}}
   {"status": "error", "code": "plan_not_active"}
4. На фінальному LLM-виклику tools вимкнені — Coach не може
   ланцюжком викликати наступну дію.
5. Coach формує лише природну відповідь мовою/тоном поточної розмови
   на основі отриманих фактів.
6. Якщо другий LLM-виклик падає — deterministic template fallback
   (поточні `_TOOL_REPLY_TEMPLATES` лишаються, але як аварійний
   резерв, не основний шлях).
```

**Оцінка обсягу роботи (не архітектурний перепис):**
- мінімально запустити loop: 2-4 години
- нормально, з тестами всіх результатів: ~1 робочий день
- разом з чисткою всіх старих tool contracts і каскаду
  `record_evening_time`: до 1.5 дня

**Технічні кроки:**
1. Зберігати `response.id`/`call_id` першого Coach-виклику.
2. `_execute_plan_tool()` повертає структурований результат
   (`{"status", "action", "facts"}` або `{"status": "error", "code"}`),
   не готовий текст.
3. Надсилати результат другим Responses API-викликом як
   `function_call_output`.
4. На другому виклику — tools вимкнені.
5. Coach формує одну фінальну відповідь тільки з отриманих фактів.
6. Поточні templates — fallback якщо другий LLM-виклик впав.
7. Тести: success, known error, unexpected error, cascade
   (`record_evening_time` → `create_followup_plan`), відсутність
   повторного tool call на другому кроці.

**Найбільша реальна робота — не сам API loop, а уніфікувати результати
всіх 8 tools** щоб Coach отримував чисті факти, не Python exceptions чи
internal state names (`ACTIVE_PAUSED` тощо не повинні просочуватись у
`facts`).

**Статус:** архітектурне рішення прийняте, **P1 до першого зовнішнього
MVP-юзера, не P0, не цієї сесії.** Поточний `After a Tool Call` блок у
промпті (Section 7) навмисно лишається чесним до фактичної one-shot
архітектури — короткий, без "orchestrator handles templates" деталей
реалізації, без "You do not know the result" (вже виражено через
заборону стверджувати success). Коли loop буде реалізований — замінити
на окремий блок `Tool Result Handling` у промпті.

## Product decision: timezone — B2B company-level, не user-level (2026-07-15)

**Рішення (founder):** timezone НЕ збирається на рівні окремого юзера
в MVP. Продукт B2B2C — компанія на власному onboarding задає часовий
контекст (де офіси, чи бувають відрядження), не окремий співробітник.

**Модель для company onboarding:**
- `organization.default_timezone`
- `organization.available_offices/timezones` (якщо кілька офісів)
- один офіс → timezone підставляється автоматично, юзера не питаємо
- кілька офісів → офіс/timezone приходить із roster або invite link
- якщо невідомо → одне питання з кнопками офісів компанії
- відрядження → в Settings юзер вручну міняє поточний timezone і
  повертає назад (без авто-визначення подорожей чи геолокації в MVP)

Потрібен per-user override поверх company default (для відряджень),
але не автоматичне визначення — зайва складність для MVP.

**Backend-факт виявлений при рев'ю:** зараз новому юзеру мовчки
ставиться `Europe/Kyiv`, timezone ніде не збирається. Для США/Азії
доставка буде неправильною, якщо колись з'являться такі компанії-клієнти.
Зберігати треба IANA timezone (`America/New_York`, не просто UTC offset)
через DST.

**Статус:** product decision зафіксовано. Реалізація —
company-onboarding backend задача, не промпт.

## Product decision: Time Picker UX — Phase 3, не MVP (2026-07-15)

**Контекст:** обговорювали чи Coach має приймати час тільки як строгий
`HH:MM`, чи давати зручніший UX (кнопки з готовими варіантами часу,
picker). Рішення: **buttons-first UX — правильний напрямок, але окрема
Phase 3 задача ("Choice Prompts Infrastructure"), не MVP.**

**Погоджений майбутній UX-контракт:**
```
"Хочу змінити час" (намір є, точного часу немає)
→ Coach викликає show_time_picker(target)
→ orchestrator показує inline-кнопки з готовими варіантами
→ callback напряму викликає runtime tool, без нового LLM-виклику

"Постав на 15:00" (намір є, час однозначно вказаний)
→ Coach нормалізує час
→ одразу викликає change_day_time / change_evening_time
→ orchestrator виконує та підтверджує
```

**MVP time picker (коли реалізується):**
```
О котрій зручно отримувати вправу?
[ 11:00 ] [ 13:00 ]
[ 15:00 ] [ 17:00 ]
[ Інший час ]
```
"Інший час" → deterministic picker (обрати годину → 00/15/30/45 хвилин),
без Mini App, без ручного HH:MM, без LLM. Точності до 15 хв достатньо
для daily exercise. Той самий keyboard переюзати для: onboarding,
першого вечірнього часу, зміни денного часу, зміни вечірнього часу.

**Що заносимо в backlog (не MVP цієї сесії):**
- `show_time_picker(target)` — новий Coach tool, `target`: `DAY` /
  `EVENING` / `UNSPECIFIED`. Використовується коли намір змінити час є,
  але конкретний час не вказаний однозначно. Сам tool нічого не міняє,
  тільки відкриває deterministic вибір.
- callback-схема на кшталт `time:day:15:00`
- callbacks викликають runtime tools напряму, **без LLM**
- вибір кнопки = підтвердження (не треба другого "точно?")
- reusable picker для onboarding + first evening collection + time
  changes (один компонент, кілька точок виклику)
- backend validation: реальний діапазон часу (зараз `99:99` проходить),
  timezone, allowed range
- runtime guards для day/evening tools (див. Backend Audit Backlog
  нижче, пункти 1 і 2 — той самий корінь проблеми)
- коли реалізовано — одночасно оновити Section 7 і `COACH_TOOLS`
  (додати `show_time_picker`, прибрати natural-language-only фолбек
  якщо picker стане основним шляхом)

**Принцип, який тримаємось:** промпт не описує `show_time_picker` чи
кнопки зараз, доки цієї інфраструктури не існує — "промпт не повинен
обіцяти неіснуючу інфраструктуру" (той самий принцип що й з
`current_exercise_context`/P0 gap раніше).

## Section 7 — Time Arguments, change_day_time / change_evening_time (виконано, 2026-07-15)

Додано спільний блок `### Time Arguments` перед обома tools:
- час трактується як local time в збереженому timezone юзера
  (у MVP — company-level default, див. рішення вище);
- юзер не мусить писати строгий `HH:MM` — Coach нормалізує природну
  мову ("о дев'ятій", "перенеси на 8:30") в 24-годинний `HH:MM` для
  tool argument;
- якщо момент доставки чи година неоднозначні — одне уточнююче
  питання, не вгадувати;
- **для зворотної зміни часу** пряме однозначне прохання саме й є
  підтвердженням (не питати "точно?" вдруге — та сама логіка яка вже
  діє для інших reversible actions через `2.4 User Intent and Consent`,
  тут явно уточнено для time tools).

Прибрано з обох tools: canned-фраза `"User-facing language: 'The bot
will write at this new time.'"` — наслідок пояснює Product Map,
підтвердження формує orchestrator, дублювання не потрібне.

До появи `show_time_picker` (backlog вище), сценарій без точного часу
лишається природним: `"Хочу змінити час"` → `"На котру годину?"`.

## Backend Audit Backlog — 2026-07-15 (виявлено під час Section 7 prompt review, НЕ виправлено цієї сесії)

Ці пункти виявлені під час рев'ю Section 7 Tool Calls (аналіз кодекса +
перевірка `plan_runtime/tools.py`/`orchestrator.py`). Свідомо винесені
поза межі "промпт-only" кола цієї сесії. Раніше були показані тільки
в чаті й **не були записані у файл** — цей запис виправляє цей розрив.

1. **`record_evening_time` — відсутня валідація.** Не перевіряє
   `user.current_state`, не перевіряє `evening_slot_collected == False`
   перед записом, приймає `99:99` (валідація лише формату цифр через
   `_validate_hhmm`, не діапазону значень). Бізнес-інваріант
   ("first-time only") існує тільки словами в промпті, не в коді.
   Файл: `app/plan_runtime/tools.py:167`. Пріоритет: P1.

2. **`change_evening_time` — Coach не бачить поточний формат плану.**
   Немає способу надійно розрізнити "зміни час" між `change_day_time` і
   `change_evening_time` коли юзер не уточнює day/evening явно — Coach
   не отримує інформацію чи в юзера взагалі є 14-денний план з вечірнім
   слотом. Runtime/context gap, не суто prompt-текст.

3. **`pause_plan`/`resume_plan` — доставки що припали на паузу
   втрачаються. ВЕРИФІКОВАНО в коді, не гіпотеза.**
   `plan_pause.py:pause_plan()` докстрінг буквально каже: *"Does NOT
   rewrite or reschedule any plan steps."* Обидва `pause_plan` і
   `resume_plan` тільки перемикають `profile.is_paused` і
   `user.current_state` — жодних змін у розкладі кроків.
   `scheduler.py:113` має gate `user.current_state == "ACTIVE"` перед
   відправкою — джоби що спрацьовують під час паузи мовчки
   пропускаються (не переносяться на потім, не видаляються — просто
   ігноруються в момент спрацювання). `resume_plan` після цього не
   перепланує нічого, тому пропущені кроки втрачені назавжди.
   Промпт раніше стверджував "delivery resumes on the original
   schedule" — це фактично неправда, формулювання виправлено в
   промпті (див. запис нижче "Section 7 — pause_plan/resume_plan").

   **Цільовий backend-контракт (ще не реалізовано):**
   ```
   pause  → preserve remaining sequence
   resume → reschedule remaining steps from the next valid workday
   ```
   Додатково перевірити: чи зміна часу (`change_day_time`) під час
   паузи коректно застосовується до `scheduled_for` кроків, і чи jobs
   гарантовано відновлюються після `resume` з новим часом.

   Файли: `app/plan_pause.py:47-113`, `app/scheduler.py:113`.
   Пріоритет: **P1**, обов'язкова MVP-задача (не "nice to have" —
   зараз продукт технічно не виконує власну обіцянку).

4. **`get_plan_status` — баг для paused стану. ВЕРИФІКОВАНО в коді,
   точний механізм.** `_execute_plan_tool` при `tool_name ==
   "get_plan_status"` перевіряє лише `result.get("plan_active")`
   (True/False) — гілка `if result.get("plan_active"):` завжди виводить
   `f"📋 Стан: активний план\n"` незалежно від значення `result["state"]`
   (`ACTIVE` чи `ACTIVE_PAUSED"). Поле `state` взагалі не читається в
   цій гілці форматування. Файл: `app/orchestrator.py:1286-1297`.
   Fix: розрізняти `state == "ACTIVE_PAUSED"` окремо і виводити
   "план на паузі" замість "активний план".

5. **`create_followup_plan` — відсутній аргумент тихо створює SHORT.**
   Tool schema вимагає `plan_type`, але registry робить
   `args.get("plan_type", "SHORT")` — якщо модель/parser поверне
   порожні arguments, система без явного дозволу вибирає 7 днів. Треба
   fail closed, не мовчазний default. Файл: `app/orchestrator.py:1216`
   (номер рядка міг зміститись після видалення `create_first_plan` —
   звірити заново).

6. **Усі tools надсилаються моделі в кожному стані незалежно від
   FSM-матриці.** Матриця в промпті — лише текстова угода, не enforced
   на рівні API tool-availability. Architecture improvement: розглянути
   state-filtered tool registration (передавати моделі тільки ті tools,
   які реально дозволені в поточному `current_state`).

7. **`create_first_plan` backend-guard досі жорстко на
   `IDLE_ONBOARDED`.** Файл: `app/plan_runtime/tools.py:77`. Функція
   лишена в коді (Coach більше не може її викликати — видалено з
   `COACH_TOOLS`/registry/reply template цієї ж сесії), але сам guard
   не оновлено. **Окремий backend TODO:** під час реалізації реального
   onboarding-флоу прибрати legacy guard `IDLE_ONBOARDED` і визначити
   реальний onboarding-complete entry state, яким онбординг-скрипт буде
   викликати цю функцію напряму (не через Coach). Перетинається з
   `ONB-07` з `pre_mvp_code_audit_findings.md`.

8. **Lifecycle-питання: перехід на 14-денний формат після
   `FD-01` auto-continuation.** Якщо система вже автостворила новий
   7-денний `ACTIVE` план одразу після завершення попереднього, а юзер
   хоче спробувати інший формат — потрібно спершу скасувати щойно
   створений період. Незакритий lifecycle-кейс. Пов'язано з рішенням
   `switch_plan_format` нижче (окремий atomic tool міг би закрити і
   цей кейс теж, не тільки перехід з живого стану — варто розглянути
   разом).

## Adm: файл виріс завеликий — після завершення prompt cleanup розбити

Цей файл перевалив за 1300+ рядків за одну сесію 11 липня (Sections 1-7
cleanup audit). Все фіксується коректно (кожен блок має заголовок,
статуси оновлюються — див. "Section 4.8 safety boundary TODO" вище,
позначено RESOLVED), але читабельність людині падає при такому розмірі.

**План:** коли весь prompt cleanup (Sections 1-7) буде закритий, зробити
окремий прохід розбиття на кілька файлів за темою:
- `prompt_review_todo.md` — суто зміни й рішення по тексту промпту;
- `product_decisions.md` — бізнес/продуктові рішення що виникають
  по дорозі (наприклад `switch_plan_format` нижче, FD-01/FD-04 похідні);
- `backend_audit_todo.md` — суто runtime/код баги виявлені під час
  рев'ю (evening time validation, get_plan_status paused bug, тощо —
  вже частково зібрані в записі "Section 5/6/7" і нижче).

Не робити зараз — зупинить темп Section 7. Робити одним окремим проходом
після завершення всього промпт-рев'ю.

## Product decision: switch_plan_format — новий tool, не MVP цієї сесії

**Знахідка:** `create_followup_plan` звужено до `IDLE_PLAN_ABORTED` (див.
запис нижче). Але лишається відкрита продуктова діра: перехід між 7 і
14-денним форматом **з активного стану** (`ACTIVE`/`ACTIVE_PAUSED`).

Зараз єдиний шлях — `скасуй → підтверди → почни новий → обери формат`.
Це зайва фрикція: юзер каже "спробуємо інший формат" і отримує вимогу
спершу скасувати поточний, хоча його намір — просто замінити один
формат на інший, не "зупинити назавжди".

**Рішення:** не перевантажувати цим `create_followup_plan` (це інша
бізнес-операція — atomic replacement, не creation-after-completion).
Потрібен окремий deterministic tool `switch_plan_format(plan_type)`:
- доступний з `ACTIVE` і `ACTIVE_PAUSED`;
- користувач один раз підтверджує перехід і наслідки;
- система атомарно завершує стару послідовність і створює нову;
- якщо для 14 днів потрібен вечірній час — система спершу його збирає;
- старий період не скасовується, доки новий гарантовано не може бути
  створений;
- не два окремі виклики (`cancel_plan` + `create_followup_plan`), а
  один atomic tool.

**Статус:** product decision зафіксовано, **не MVP-задача цієї сесії**.
Реалізація tool-а — окрема backend-задача (новий tool в
`plan_runtime/tools.py`, новий entry в `COACH_TOOLS` і orchestrator
registry, коли буде готовий). До того — Coach просто пояснює наявний
шлях (скасувати й почати новий) через `2.4 User Intent and Consent`.

## Product decision: default continuation format after completion

**Питання:** коли `FD-01` (auto-continuation) імплементований — після
завершення 7-денного плану дефолт наступний теж 7-денний. А після
завершення **14-денного**? Наступний за замовчуванням 14, чи 7?

**Рекомендація (зафіксована, не остаточне рішення):** автоматично
продовжувати **останній обраний формат**. Відповідає ментальній моделі
юзера: система продовжує встановлений ритм, доки він сам не попросить
інший — а не скидає на дефолтний найкоротший формат щоразу.

**Статус:** рекомендація, потребує founder-підтвердження перед
імплементацією `FD-01`-логіки в бекенді. Не промпт-задача — фіксується
тут для видимості при наступному раунді backend-аудиту.

## НАСТУПНА СЕСІЯ починає тут: Section 7 Tool Calls — стан на 2026-07-11

Section 1-6 повністю пройдені суботнім cleanup audit. Section 7
(Tool Calls) — останній розділ файлу, ще не чіпали. Сесію зупинено тут
через втому (2+ год роботи), не через складність задачі.

**Вже видно неозброєним оком (звірити й виправити першим ділом):**

1. `create_first_plan` — `State: IDLE_ONBOARDED` (Available Tools) і
   FSM × Tool Matrix рядок `IDLE_ONBOARDED` — цей стан видалено з
   Coach-facing карти станів ще в Section 2.1 (див. запис "FSM / Coach
   Prompt Decision: IDLE_ONBOARDED and IDLE_DROPPED" нижче). Треба або
   прибрати запис в Available Tools, або замінити на актуальний
   entry-state (узгодити з ONB-07 з `pre_mvp_code_audit_findings.md` —
   backend guard `create_first_plan` теж досі перевіряє
   `IDLE_ONBOARDED`).
2. `create_followup_plan` — `States: IDLE_FINISHED, IDLE_DROPPED,
   IDLE_PLAN_ABORTED` і FSM-матриця — `IDLE_DROPPED` видалено з
   Coach-facing карти. Прибрати з обох місць.
3. FSM × Tool Matrix рядок `SCHEDULE_ADJUSTMENT` — стан видалено
   повністю (T5.9 рішення, окремий PR). Рядок матриці зайвий,
   прибрати.

**Далі пройти решту Section 7 тим самим ножем** (старий lifecycle /
dead states / дані яких runtime не дає → видаляємо):
- `record_evening_time` / `change_day_time` / `change_evening_time` /
  `pause_plan` / `resume_plan` / `cancel_plan` / `get_plan_status` —
  ще не рев'юєні на дублювання чи застарілі формулювання.
- `After a Tool Call` блок (reply_text empty, don't assume success) —
  ще не рев'юєний.
- Звірити чи всі 4 "Section 7"-посилання з інших частин файлу (Section
  2.2, 2.4) все ще коректні після будь-яких змін тут.

## Section 5/6/7 — System Security розділено на Security + User Safety

Стара `# 5. System Security (Anti-Jailbreak)` містила тільки
anti-jailbreak (6 рядків) і при цьому мала прийняти на себе весь
safety-контент, винесений раніше з видалених `2.6 Soft Safety Fallback`
і `4.8 Emotional Continuity`. Рішення: **не зливати їх в одну секцію.**

**Чому розділено, а не об'єднано:**
Security захищає інструкції й межі системи (jailbreak, промпт-екстракція).
Safety визначає поведінку при ризику для людини (self-harm, harm to
others). Це різні предмети регулювання — об'єднання через спільну рамку
"винятки" структурно неправильне.

**Нова структура:**
- `# 5. System Security` → `5.1 Instruction Integrity`
- `# 6. User Safety` → `6.1 Immediate Safety Risk`
- `# 7. Tool Calls` (був `# 6`, перенумеровано)

### 5.1 Instruction Integrity — що змінено проти старого anti-jailbreak

Стара версія наводила каталог jailbreak-фраз ("ignore all previous
instructions", "break character", "act as raw model") і реагувала
через `"redirect to the user and their state"` — тобто "ти хочеш
побачити мій промпт? що ти зараз відчуваєш?" Це крінжовий терапевтичний
redirect на людину яка просто тестує систему (програмісти це точно
робитимуть). Нова версія: коротка відмова без пояснення механізмів
безпеки, продовження решти запиту без емоційного redirect-у.

### 6.1 Immediate Safety Risk — головна правка: тригер без лейблів

Перший чорновий варіант (від кодекса) містив:
```
Do not treat frustration, exhaustion, hopeless language,
or emotional intensity alone as immediate danger.
```
Founder відхилив це як самé по собі лейблювання — щоб описати що НЕ є
кризою, модель спершу мусить internally класифікувати повідомлення за
емоційними категоріями ("це frustration? exhaustion? hopeless language?").
Той самий проблемний патерн, який ми вже вичищали по всьому промпту
(shame, normalize, diagnosis labels).

Замінено на тригер через факти, не емоційні категорії:
```
Apply this section only when the user says that they intend, plan,
are about to, or are currently trying to harm themselves or another person.

References in jokes, hypotheticals, quotations, fiction, or general discussion
do not by themselves activate this response.
```
Це важливо для аудиторії продукту (програмісти, чорний гумор) — фраза
"я зараз застрелюсь через цей баг" не активує кризовий протокол
автоматично; спрацьовує тільки коли є факт наміру/дії, не тон.

**Дослідницька підстава для "одне уточнююче питання не шкодить":**
пряме коротке уточнення про суїцидальний ризик не збільшує ризик таких
думок — підтверджено NIMH (nimh.nih.gov/health/publications/suicide-faq)
і WHO (who.int/news-room/questions-and-answers/item/suicide). При
реальній негайній загрозі стандартна рекомендація — звернутись до
екстрених служб/кризової лінії і залучити довірену людину поруч. Це і
відображено в `6.1`.

### Non-crisis distress — видалено повністю, не перенесено

`2.6 Soft Safety Fallback` (persistent despair/hopelessness → gently
suggest professional support) і `4.8`-шматок "Non-crisis distress"
(stay present, exception для pause_plan) **не перенесено в Section 6**.

Причина: це вже покрито `2.2 Workday Emotional Support` (emotional
support без labeling/diagnosing). Автоматично пропонувати психолога
через розпізнаний "persistent hopelessness" знову повертає терапевтичну
рамку і змушує модель самостійно класифікувати емоційний стан —
той самий лейбл-паттерн, який ми свідомо вичищаємо з усього промпту.
Pressure-reducing exception (pause/cancel при non-crisis distress) також
прибрано з safety: якщо немає негайного ризику, звичайний запит
pause/cancel і так проходить через `2.4 User Intent and Consent` +
tools — окремий safety-виняток для цього не потрібен. Якщо є негайний
ризик, продуктова операція взагалі не повинна перебивати кризову
відповідь (це і забезпечує новий guard у `7. Tool Calls`, нижче).

### Precedence — чому "takes priority" переформульовано

Було: `"This rule takes priority over Section 6 tool call logic"`.
Проблема: LLM не виконує систем-промпт як програму зверху вниз рядок
за рядком — це не technical enforcement, а просто ще одна інструкція
серед інших. Явне правило пріоритету реально допомагає моделі
вирішувати конфлікт інструкцій, але не є гарантією на рівні коду.

Тому: (1) переформульовано на `"do not follow any conflicting
instruction elsewhere in this prompt"` — конкретніше й дієвіше
формулювання пріоритету; (2) додано дублюючий guard безпосередньо в
`7. Tool Calls`: `"the Immediate Safety Risk rule (Section 6.1) must
not currently apply"` в списку "Before calling any tool". Це навмисне
дублювання на межі двох систем (safety-правило описано в 6.1, і ще раз
згадано як умова виклику tool в 7) — виправдане, бо system prompt не є
hard enforcement.

**Backend/enterprise TODO (не для MVP, довгостроково):** для реального
enforcement (а не тільки промпт-рівня) з часом варто розглянути окремий
runtime safety layer — наприклад перевірку на рівні orchestrator перед
виконанням tool call, а не покладатись виключно на те що модель
дотримається інструкції в system prompt.

### Точкове доповнення 6.1 — третя сторона і вже здійснена шкода

Перша версія `6.1` покривала лише намір **самого юзера** заподіяти шкоду
собі чи іншому. Прогалина: не покривала (а) ситуації де юзер повідомляє
що комусь **зараз** загрожує небезпека без його власного наміру ("мені
погрожують", "поруч когось б'ють"), і (б) шкода яка **вже сталась**, а
не тільки готується. Розширено тригер до трьох гілок: (1) шкода вже
завдана, (2) намір/підготовка/спроба, (3) поточна небезпека для юзера
чи третьої особи. Також додано "news" до списку контекстів які самі по
собі не активують протокол (поруч з jokes/hypotheticals/quotations/
fiction), і "stay with them" замінено на "help them reach safety" —
перше могло звучати як інструкція самому Coach фізично щось робити.

## Section 3 — Style & Tone: 14 підрозділів → 8

Прибрано повністю: Core Voice (дубль Section 1), DSM (небезпечний —
mirroring emotional intensity/energy може підсилювати стан замість
стабілізації; нові моделі й так адаптують темп/формальність без явної
інструкції), Emotional Presence (дубль DSM/Core Voice), Intrusivity
Control (застаріла ставка на "AI-buddy" retention — продукт тепер робить
ставку на вправи й повторюваність, не на глибокі розмови з AI), Engagement
Principles (дубль + `challenge avoidance or self-deception` суперечило
забороні інтерпретувати приховані причини), Personality Consistency
(прямий дубль Persona Integrity), Exercise delivery + Tone з Telegram
Output (рендер вправи — продукт, не Coach; дубль).

Змінено по суті (не просто дубль):
- **Swearing → Profanity**: свідоме бізнес-рішення — мат дозволений якщо
  першим написав юзер, match not exceed, ніколи не на юзера, ніколи
  слерів/образ. Founder-рішення: автентичність і спільна мова з
  програмістами важливіша за гіпотетичний "HR побачить скрін" ризик для
  цієї аудиторії (утилітарний інструмент для когнітивних роботяг, не
  корпоративний бот у костюмі).
- **Humour**: dark/edgy прибрано, лише light humour якщо юзер сам
  ініціює.
- **No AI-Meta → Implementation Honesty**: більше не наказує "удавати
  людину". Якщо юзер прямо питає "ти AI?" — відповідати чесно, коротко,
  потім описати роль як Love Yourself Coach.
- **Zero Filler + No Philosophical Fog → Clarity**: об'єднано,
  "without practical value" → "without clear relevance" (не кожна
  емоційна відповідь має містити практичну дію).
- **Anti-Dependency**: скорочено з 4 категорій прикладів до одного
  компактного правила, норма та сама.
- **Telegram-Aligned Output**: додано `Never generate more than 4096
  characters for a single response` як жорсткий technical cap.

Фінальна структура: 3.1 Language Adherence, 3.2 Grounded Acknowledgment,
3.3 Profanity, 3.4 Humour, 3.5 Clarity, 3.6 Implementation Honesty,
3.7 Anti-Dependency, 3.8 Telegram-Aligned Output.

## Section 4 — Context & Memory Use → Context Use: 9 підрозділів → 3

Перейменовано `# 4. Context & Memory Use` → `# 4. Context Use`. Це не
косметика — сигналізує свідоме product-рішення: **Memory Engine
відкладений, не будується зараз.**

**Рішення (founder + два незалежні огляди):**
- MVP Coach — інтерпретатор продукту й інтерфейс до дій, не довготривалий
  AI-компаньйон. `short_term_history` достатньо для зв'язності поточної
  розмови.
- Телеметрія продукту (план, час, work_days, completion) — **не** пам'ять
  про людину. Не зберігати емоційні висновки, "патерни", особисті риси
  чи психологічний профіль без доведеного use case.
- Векторну пам'ять відкладаємо до появи реальних розмов і повторюваних
  сценаріїв, де відсутність пам'яті справді шкодить retention.

**Чому видалено старий контент (не переписано — видалено):**
- Стара секція будувала навколо Coach **фікцію**: інструктувала обіцяти
  юзеру "Got it, I'll remember this about you" і посилалась на
  "the memory layer handles storage" — такого шару **не існує**. Реально
  в модель потрапляє лише `current_time`, `fsm_state`, опційно
  `completion_context` (через `_context_message()`), плюс
  `short_term_history`. Інверсія: юзер каже "запам'ятай X" → Coach обіцяє
  → наступного разу факту немає → Coach виглядає як той хто збрехав.
- Нова `4.3 Memory Honesty` замінює цю фікцію на чесність: не обіцяти
  майбутній recall, прямо казати "не маю цього в поточному контексті"
  замість симуляції пам'яті.
- Список полів у промпті **навмисно не містить точних runtime-назв**
  (`temporal_context`, `current_exercise_context` тощо) — архітектура ще
  рухається, точні назви краще тримати в runtime contract і тестах, не в
  промпті. Це запобігає тому що вже сталося раніше: промпт згадував
  `current_exercise_context` як джерело, хоча runtime його **ще не
  передає** (P0 gap, задокументований нижче в цьому файлі), і згадував
  `schedule_adjustment_context`, яке `_context_message()` **ніколи не
  включає** (мертве поле, orchestrator фетчить, Coach не бачить).
- **Розділення джерел істини** (важлива правка від founder перед
  затвердженням): не можна було лишити просто "user's current message =
  highest source of truth" — це дозволяло юзеру переписувати продуктові
  факти ("14 днів тепер означає три вправи щодня"). Тепер явно розділено:
  повідомлення юзера — джерело істини про його намір/досвід; Product Map —
  джерело істини про продукт; runtime context — джерело істини про
  поточний стан. Ніхто не перетирає чужу зону.

**Що фізично перенесено, не втрачено:**
- `4.8 Emotional Continuity` (crisis/non-crisis protocol, immediate risk
  response) — **повністю видалено з Section 4** і йде в TODO для
  Section 5 нижче (об'єднати з тим що вже чекало з видаленого 2.6). Це
  свідоме архітектурне рішення: визначення emotional/risk сигналу з
  розмови — це risk-detection задача, вона має жити разом з протоколом
  реагування в одному місці (Section 5), а не розділена на "де читати
  сигнал" (Section 4) і "де діяти" (Section 5) — розділення саме по собі
  створює ризик дрейфу між двома описами однієї safety-critical
  поведінки з часом.
- "Conversation Recovery" (ask one clarification question if thread
  is lost/contradictory) — контент **не втрачено**, згорнуто в `4.2`
  останнім реченням ("this applies equally when the conversation itself
  becomes unclear or contradictory"), а не залишено окремим підрозділом
  — та сама норма, без зайвого заголовка.

**Наслідки для Section 5/6 — виконано.** Див. запис "Section 5/6/7 —
System Security розділено на Security + User Safety" нагорі: anti-jailbreak
залишився в `5.1`, immediate risk response переїхав у `6.1` (перероблений
без емоційних лейблів), non-crisis distress/soft fallback — свідомо
**не перенесено**, вирішено що вже покрито `2.2 Workday Emotional
Support`.

**Backend-задачі, виявлені під час рев'ю (НЕ прompt-проблема, окремо
від коду):**
- **Contract drift**: промпт раніше писав `current_state`, а
  `_context_message()` реально передає модель це поле як `fsm_state` у
  JSON. Малий, але реальний drift між тим що документується і що
  насправді йде в API виклик. Звірити найменування.
- `schedule_adjustment_context` — фетчиться оркестратором
  (`build_user_context`), але `_context_message()` ніколи не включає
  його в те що бачить модель. Мертве поле. Вже узгоджується з рішенням
  прибрати `SCHEDULE_ADJUSTMENT` (backend cleanup вже в TODO нижче) —
  просто підтверджено ще раз з іншого боку.
- `current_exercise_context` — досі не передається runtime (P0 gap,
  задокументований нижче в записі "2026-06-18 — P0: delivered exercise
  context is missing from Coach runtime"). Промпт (2.2 Exercise
  Explanation Boundary) вже посилається на це поле — коли P0 буде
  закрито в коді, нічого міняти в промпті не треба, воно вже узгоджено.

**Backlog — майбутній Memory Engine (не MVP, не зараз):**
Коли з'явиться реальний use case (повторювані сценарії де відсутність
пам'яті шкодить retention), можливі категорії:
- explicit user preferences;
- chosen support moment;
- repeated skip/change patterns;
- exercise dislikes;
- preferred tone boundaries;
- privacy-sensitive "do not store" rules.

Що не зберігати ніколи:
- diagnosis;
- trauma details;
- employer-sensitive personal content;
- inferred psychological profile;
- hidden risk labels.

Принцип для майбутнього: telemetry is not Coach memory; plan state is
not Coach memory; short-term history is conversational continuity only;
future Memory Engine is a separate product layer, built after beta data
shows it's actually needed.

## ПЛАН: субота — Prompt Cleanup Audit (весь файл, від а до я)

Рішення: у суботу на АТП (вихідний день) сідаємо і проходимо **весь**
`coach_agent.py` від початку до кінця одним проходом — не тільки нові
секції, а й уже "відполіровані" Section 1-2. Мета — не ідеальний текст,
а видалення залишків старої архітектури, включно з тим що вже виглядає
добре написаним.

### Це НЕ prompt polish. Це prompt cleanup audit.

- читаємо весь `coach_agent.py` від початку до кінця;
- видаляємо стару архітектуру, навіть якщо текст вже гарний;
- не переписуємо стиль заради краси;
- не розширюємо нові секції;
- усе, що залежить від майбутнього коду/рішень — кидаємо в TODO, не вирішуємо на місці;
- залишаємо тільки стабільний baseline: persona, scope, Product Map,
  active/paused, consent/tool skeleton, safety/no-hallucination.

### Ніж (критерій видалення)

> Якщо блок описує старий lifecycle, old follow-up choice, onboarding через
> Coach, adaptation, completion-choice flow, dead states, або дані яких
> runtime не дає — **видаляємо, не рятуємо**.

### Після цього

Продовжуємо ширший pre-MVP audit (`pre_mvp_code_audit_findings.md`) —
scheduler, onboarding, completion, delivery renderer, states/guards,
runtime tools, privacy, content library, telemetry. Coach-промпт перестає
бути джерелом старої логіки, і решта аудиту піде чистіше.

---

## Режим роботи змінився (з 2.6) — MVP-контракт, не "ідеальний промпт"

З цього моменту фокус: закрити контракт Coach-промпту і прибрати сміття,
а не довести кожну секцію до ідеалу. Паралельно йде ширший pre-MVP аудит
всієї системи (`pre_mvp_code_audit_findings.md` — scheduler, onboarding,
completion, delivery renderer, states/guards, runtime tools, privacy,
content library, telemetry). Coach-промпт — одна з частин цього аудиту,
не окремий perfectionist-проєкт.

## Section 2.5 — видалено повністю (IDLE_FINISHED — Completed Plan)

Старий блок `## 2.5 IDLE_FINISHED — Completed Plan` видалено цілком
(не переписано, не скорочено — видалено).

Чому видалено, а не допрацьовано:

1. **FD-01 ламає саму основу секції.** Старий сенс `IDLE_FINISHED` був:
   "план закінчився, що хочеш далі?" Новий прийнятий founder decision
   (FD-01, `pre_mvp_code_audit_findings.md`): наступний 7-денний план
   створюється **автоматично за замовчуванням**, без вибору користувача.
   Це не косметика — це інша lifecycle-модель, і стара секція описувала
   флоу який більше не є продуктовим рішенням.

2. **Це completion/lifecycle logic, не Coach behavior.** Completion
   report, auto-next-plan, наступна дата старту, той самий час/work_days —
   це має жити в backend / report copy / Product Map, а не в Coach state
   policy.

3. **`completion_context` як prompt surface був ризикований.**
   `outcome_tier: STRONG / NEUTRAL / WEAK` — оціночний лейбл, який ми і так
   вичищаємо по всьому промпту (shame, normalize, diagnosis). Показувати
   Coach-у "WEAK" як категорію — ризик витоку оціночної мови в тон
   відповіді, навіть з інструкцією "не роби з цього діагноз".

4. **Coach не має окремої місії в цьому стані.** Якщо юзер питає щось
   після завершення плану — це вже покрито Section 2.2 (Product Support /
   Emotional Support) + Product Map. Окрема "completed-plan policy" зайва.

**Рішення:**
Completion behavior обробляється lifecycle/report логікою, не Coach-ом.
Coach не веде окрему policy для завершеного плану, якщо продукт не визначить
стабільний post-completion розмовний стан (наразі — не визначає).

**Наслідки, які треба перевірити пізніше (НЕ зроблено цієї сесії):**

- В `_context_message()` (runtime код, не сам промпт) залишається опис поля
  `completion_context` — прибрано лише мертве посилання "See section 2.5",
  сам опис поля не чіпали. **Ревізувати після імплементації FD-01:**
  - чи Coach взагалі повинен отримувати completion-метрики;
  - якщо `outcome_tier` залишається в даних — перейменувати/прибрати
    оціночні мітки (STRONG/NEUTRAL/WEAK) перед тим як це може дійти до
    Coach-контексту;
  - переконатись що completion report copy і Coach-промпт використовують
    одну lifecycle-модель: наступний 7-денний план готується автоматично,
    користувач може відмовитись/змінити/переключитись на 14-денний.
- Python runtime код (`_build_idle_finished_context`,
  `coach_agent()` injection логіка для `IDLE_FINISHED`) **не змінювався**
  цієї сесії — це окрема backend-задача, пов'язана з FD-01/LIF-03/LIF-04
  з `pre_mvp_code_audit_findings.md`, не частина промпт-рев'ю.
- Section 6 FSM × Tool Matrix і "Available Tools" все ще згадують
  `IDLE_FINISHED` (разом із `IDLE_DROPPED`, який вже вирішено прибрати) —
  синхронізувати коли дійдемо до Section 6.

## Section 2.6 — видалено повністю (Unified Persona & Safety Fallback)

Старий блок `## 2.6 UNIFIED PERSONA & SAFETY FALLBACK` видалено цілком.
Причина: жодна з трьох частин не належала до "System Awareness & Boundaries"
(Section 2). Тримати "хоч щось від 2.6" — поганий критерій; якщо блок не
про scope/state/actions, йому не місце в Section 2.

Розкладка на майбутнє (жодна ще не виконана):

- **Unified Persona (DO/AVOID)** — видалено остаточно як дубль. Те саме
  вже покрито Section 1 "Persona Integrity" + Section 5 anti-jailbreak.
  Нічого переносити не треба.

- **Conversation Recovery** — виконано. Згорнуто в Section 4.2 без
  окремого заголовка (див. запис "Section 4 — Context & Memory Use →
  Context Use" нагорі).

- **Soft Safety Fallback + immediate risk response** — виконано. Див.
  запис "Section 5/6/7 — System Security розділено на Security + User
  Safety" нагорі.

**Нумерація:** Section 2 тепер закінчується на `2.4 User Intent and
Consent`. Що було `2.7 IDLE_FINISHED` — перенумеровано в `2.5` (пізніше
видалено повністю, див. запис вище).

## Архітектурне рішення — ONBOARDING блок видалено з Coach промпту (Section 2.1)

Видалено повністю блок `### ONBOARDING` (states `IDLE_NEW`, `ONBOARDING:*`) із Section 2.1
Internal System Map.

**Рішення:** Coach не повинен містити жодної логіки про онбординг.

**Чому:**
- Coach не існує в онбордингу — це інший флоу, інший агент/механіка.
- Архітектура має гарантувати, що Coach не викликається до завершення онбордингу.
  Промпт не повинен страхувати архітектуру — якщо Coach отримає `IDLE_NEW` чи
  `ONBOARDING:*`, це сигнал що щось зламалось вище за рівнем оркестрації, а не
  привід інструктувати Coach як це обробляти.
- **Inversion-перевірка:** як зламати онбординг? Дати Coach-у почати пояснювати
  продукт, розпитувати стан, пропонувати план — поки система ще збирає базові
  дані про користувача. Сам факт наявності цього блоку в промпті — це відкритий
  шлях для такого зламу (модель бачить інструкцію "як говорити в онбордингу" і
  може застосувати її навіть коли не повинна).

**Наслідок:** якщо в майбутньому Coach дійсно має брати участь у якійсь частині
онбордингу (наприклад, "м'яка" передача голосу між онбордингом і Coach) — це
окреме архітектурне рішення, яке потребує власного product-рев'ю, а не рядок
у Internal System Map.

**Статус:** рішення прийняте, зафіксоване. Не повертатись до цього без нового
product-обговорення.

## FSM / Coach Prompt Decision: IDLE_ONBOARDED and IDLE_DROPPED

### IDLE_ONBOARDED

Removed from the Coach-facing prompt.

Reason:
`IDLE_ONBOARDED` is no longer a real conversational state in the V4 product flow.

Earlier, it made sense because onboarding could finish before a plan was selected
or created. The user could remain "onboarded but without a plan" and the Coach
needed to help them choose what to start.

In V4, onboarding is expected to end with the first 7-day plan being created
automatically. The user should not enter a free-form Coach conversation between
onboarding completion and the first plan. Therefore this state should not be
described as a Coach behavior mode.

Decision:
Do not include `IDLE_ONBOARDED` in the Coach-facing state map.
Keep any remaining backend/FSM cleanup as a separate architecture task.

### IDLE_DROPPED

Removed from the Coach-facing prompt.

Reason:
`IDLE_DROPPED` does not currently have a defined deterministic user-facing flow.

It appears to represent passive abandonment / background expiry / stale active
plan cleanup, but the current runtime does not provide a stable mechanism that
naturally moves users into this state. Explicit user cancellation already maps
to `IDLE_PLAN_ABORTED`, and natural completion maps to `IDLE_FINISHED`.

Without a clear entry rule, describing `IDLE_DROPPED` in the Coach prompt would
create behavior for a state that the product has not actually defined.

Decision:
Do not include `IDLE_DROPPED` in the Coach-facing state map for V4.
Revisit if smart notifications or deterministic abandonment logic are
implemented later, for example inactivity timeout or stale plan cleanup.

**Summary:**
- `IDLE_ONBOARDED` removed — technical transit state, not a conversation.
- `IDLE_DROPPED` removed — undefined entry state, not a defined product behavior.
- The Coach prompt now only describes states where the Coach has distinct,
  product-defined behavior: `IDLE_FINISHED`, `IDLE_PLAN_ABORTED`, `ACTIVE`,
  `ACTIVE_PAUSED`, `SCHEDULE_ADJUSTMENT`.

## 2026-06-18 — Product Map rewrite and provisional Coach integration

### Context

This work was done during the ongoing line-by-line review of the Coach system
prompt. It is **not a finished PR** and does not mean the full prompt review is
complete.

The purpose was to create a reliable product source of truth for practical
questions such as:

- what Love Yourself does and what value it provides;
- how the 7-day and 14-day formats work;
- why a specific exercise is shown at a specific time;
- how exercises are selected;
- what pause, cancellation, and time changes mean;
- what happens after a missed exercise;
- what the user can expect when one exercise does not feel immediately
  noticeable.

This replaces the earlier idea of writing a large FAQ or letting the Coach
improvise product explanations.

### Product Map files

Updated:

- `resource/assets/product/conceptual_map.md`
  - Ukrainian master version.
  - Written as a product grounding document rather than a ready-made user FAQ.

Added:

- `resource/assets/product/conceptual_map_en.md`
  - English equivalent intended for the Coach runtime.
  - The English version exists because the Coach system instructions are written
    in English.

Updated:

- `resource/assets/product/README.md`
  - Documents the distinction between the Ukrainian Product Map, the English
    Coach version, and the technical `product_internal_spec.md`.

### Content decisions included in both maps

- Product value is stated directly rather than framed through defensive
  disclaimers.
- The expected value includes a clearer return to work, more sustained focus,
  longer work rhythm, and less end-of-day depletion.
- The product is not described as guaranteeing an identical immediate feeling
  after every exercise.
- Long-term value is explained through regularity over weeks, using the logic
  that one action is not equivalent to a repeated practice.
- Exercise selection is described accurately:
  the sequence is generated in advance for the user using defined selection
  rules rather than being dynamically chosen from the current conversation or
  mood.
- The map explains user control over delivery time.
- Pause, cancellation, and time changes are presented as available actions.
- Missing or skipping an exercise does not rebuild the sequence.
- Exercises are optional when the user has no time, ability, desire, or suitable
  surroundings.
- The map states that exercises were reviewed with practicing psychologists.
- The document instructs the Coach to use only relevant facts, answer in the
  user's language, and not invent details missing from the map or current
  context.

### Provisional runtime integration

Changed:

- `app/workers/coach_agent.py`

The English Product Map is currently loaded from
`resource/assets/product/conceptual_map_en.md` and sent to the model as a
separate trusted system message.

Current message order:

1. `COACH_SYSTEM_PROMPT` — identity, behavior, state, tool, safety, and response
   rules.
2. `COACH_PRODUCT_MAP` — static product facts and value proposition.
3. Runtime context — current user-specific facts such as `current_state`,
   current time, and completion context when available.
4. Recent conversation history.
5. Current user message.

This is static prompt grounding, **not RAG**. The complete English map is sent
with each Coach request; no retrieval or semantic search is performed.

### Architecture decision

The complete English Product Map must be sent with **every Coach API call** as a
separate trusted system message.

It is always present, not injected only for detected product questions. This
keeps product facts, value explanations, and action consequences available
throughout the conversation without relying on intent detection or retrieval.

The Ukrainian map remains the master product document for human review. The
English map is the runtime version used by the Coach. Both versions must be
updated together whenever product facts change.

The wider system prompt review is still in progress, but this Product Map
delivery decision is accepted and should not be treated as provisional.

### Supporting code and test changes

- Added `PRODUCT_MAP_PATH` and `COACH_PRODUCT_MAP`.
- Updated the foreign-instruction scan to recognize three trusted internal
  system messages instead of two.
- Added a regression test confirming that the English Product Map appears after
  the Coach prompt and before runtime context.

Verification on 2026-06-18:

- `python3 -m py_compile app/workers/coach_agent.py` — passed.
- `pytest -q tests/test_coach_idle_finished.py -k 'not trio'` —
  9 passed, 2 deselected.
- The two excluded variants require the unavailable `trio` dependency; this is
  the existing environment issue, not a Product Map failure.

### Status

Worktree only. No PR opened. Continue reviewing the Coach system prompt before
deciding the final integration shape.

## 2026-06-18 — Product Map follow-up: value and selection framing

Three corrections were applied to both `conceptual_map.md` and
`conceptual_map_en.md`:

1. **Short-term value in Section 2**
   - Removed the per-exercise promise that it will immediately make returning
     to work feel easier.
   - Reframed immediate value as one bounded moment of switching with a clear
     beginning and end, instead of an open-ended stream of distraction.
   - Kept the long-term value statement about regular short pauses supporting
     sustained work rhythm, deeper focus, and lower end-of-day depletion.

2. **Exercise selection in Section 8**
   - Removed builder-level details such as weights, cooldowns, and library
     activation flags from the Coach-facing Product Map.
   - Reframed the value for the user: the system handles exercise selection,
     variety, daytime/evening purposes, and excessive repetition so the user
     does not have to manage those decisions.
   - Preserved the important product fact that an already-created sequence is
     not rebuilt from conversation, skips, or completed exercises.

3. **Psychologist review claim in Section 10**
   - Removed the statement that every exercise was reviewed with practicing
     psychologists because this claim is not currently supported as a verified
     product fact.

4. **Section 10 consistency correction**
   - Removed the remaining per-exercise promise that an action helps the user
     return to work with clearer focus.
   - Section 10 now describes only the observable mechanism: a short action
     moves attention to one concrete step and creates a bounded switching point
     within the working day.

Status remains worktree-only. No PR has been opened.

## 2026-06-19 — Format-switch rule and plan-status intents

Two remaining ACTIVE dependencies were completed:

1. **Product Map: changing between 7 and 14 days**
   - Added to both Ukrainian and English maps.
   - The format cannot be changed while the current 7 or 14 days are running.
   - Switching before completion requires canceling the current sequence and
     starting a new one in the desired format.

2. **`get_plan_status` intent coverage**
   - Expanded both the Section 6 instruction and the registered tool
     description.
   - Explicitly covers:
     current day, days remaining, completion progress, and current status.
   - Added natural-language examples so the model recognizes common user
     phrasings instead of guessing from conversation history.
   - Runtime verification found that the old tool result did not fully support
     the new description: it could format the first day as `Day 0 of 7`, did
     not return days remaining, and did not calculate exercise completion.
   - Updated `app/plan_runtime/tools.py::get_plan_status()` to return:
     `current_day`, `days_remaining`, `steps_total`, `steps_completed`, and
     `completion_rate`.
   - Updated the orchestrator's user-facing status reply to show the current
     day, remaining days, and completed exercises.
   - Added unit coverage for active-plan progress and the no-active-plan case.
     Verification: `PYTHONPATH=. pytest -q tests/test_plan_runtime_tools.py` —
     16 passed.

Status: implemented in the worktree. No PR opened.

## 2026-06-18 — ACTIVE PLAN follow-up tasks

### Agreed ACTIVE PLAN draft

Use this as the working version when Section 2.1 is updated:

```text
### ACTIVE PLAN

State: `ACTIVE`

A 7 or 14-day plan is currently running.
Exercises may be scheduled for the user.

Coach behavior depends on the user's intent:

- If the user asks about exercises, timing, pause, continuation, stopping,
  or what to do next:
  use the Product Map as the source of truth for how the product works,
  why this exercise is shown now, and what options are available.
  Explain how to perform a specific exercise only from instructions
  available in the current context or conversation.
  Do not invent missing product facts or exercise steps.
  When the request requires an action, follow the tool and consent rules.
  Do not change plan content, exercises, or structure.

- If the user brings up workday friction, frustration, or emotional discomfort
  without asking for plan management:
  respond as Coach support, not as plan logistics.
  Do not immediately turn the message into instructions,
  explanations, or plan management.

- If both are present:
  acknowledge the emotional context first, then answer the practical question.
```

### TODO — Missing Product Information policy in Section 2.2

Add a global rule under `2.2 Role Boundaries & Scope`:

```text
### Missing Product Information

If the Product Map and current context do not contain the information needed
to answer a factual product question:

- say clearly that you do not have that detail,
- do not infer, approximate, or invent an answer,
- direct the user to product support when an escalation path is available.
```

Important:

- Do not tell the user that the Coach contacted, notified, or escalated to the
  product team unless a real escalation mechanism exists.
- Before release, decide the actual escalation path:
  a support contact, a deterministic support flow, or a future
  `escalate_product_question` tool.
- Until that path exists, the Coach should only state that the detail is not
  available rather than pretending to pass the question to someone.

### TODO — Resolve Exercise Visibility Boundary conflict

The current `Exercise Visibility Boundary` conflicts with the agreed ACTIVE
behavior.

Current boundary forbids the Coach from:

- describing step-by-step actions;
- instructing the user how to perform an exercise.

The agreed ACTIVE behavior allows the Coach to explain a specific exercise when
its original instructions are available in current context or conversation.

When reviewing `Exercise Visibility Boundary`:

- allow the Coach to clarify or repeat exercise instructions that are actually
  present in trusted runtime context or conversation;
- prohibit inventing missing steps, modifications, substitute exercises, or
  additional exercise content;
- verify whether the runtime currently provides the delivered exercise text to
  the Coach;
- if it does not, add the required exercise context before claiming that the
  Coach can explain how to perform it.

Status: open. Review these items when Section 2.2 and Exercise Visibility
Boundary are reached.

## 2026-06-18 — P0: delivered exercise context is missing from Coach runtime

### Verified current behavior

The Coach currently does **not** receive `display.steps` for the exercise that
was delivered to the user.

The delivered exercise is not available through either supported path:

1. **`short_term_history`**
   - Scheduled exercise messages are sent directly by `app.scheduler`.
   - `send_scheduled_message()` sends the Telegram message and records telemetry,
     but does not append the notification text to Redis session history.
   - It also does not create an assistant `ChatHistory` row.
   - Therefore `get_stm_history()` cannot return the delivered exercise message.

2. **Structured Coach context**
   - `build_user_context()` currently returns:
     `message_text`, `short_term_history`, `current_state`,
     `temporal_context`, and `schedule_adjustment_context`.
   - It does not return `current_exercise_context`, `exercise_id`,
     `display.steps`, or the delivered exercise text.
   - `_context_message()` narrows the Coach runtime context further and currently
     includes only current time, FSM state, and completion context when present.

### Product consequence

The agreed ACTIVE PLAN prompt says the Coach may explain how to perform the
relevant exercise using instructions available in current context or
conversation.

With the current runtime, those instructions are normally unavailable.
Shipping that prompt would create a guaranteed contract gap:

- the prompt permits and expects exercise clarification;
- the system does not provide the original exercise steps;
- the Coach must either refuse a basic support question or invent instructions.

This is a **P0 fix for T5.8 before the ACTIVE PLAN block goes to production**.
Without it, the new ACTIVE contract promises support that the runtime physically
cannot provide.

### Recommended implementation

Add a structured `current_exercise_context` to the Coach payload rather than
relying only on `short_term_history`.

Minimum payload exposed to the Coach:

```json
{
  "title": "Дихання",
  "steps": [
    "Вдих — повільно, на 4 рахунки.",
    "Затримай подих на 7.",
    "Видих — повільно, на 8.",
    "Повтори 4 рази."
  ],
  "duration_label": "30–60 сек"
}
```

The context should be built from the latest relevant delivered `AIPlanStep`
and its trusted `ContentLibrary.content_payload.display` data.

`delivered_today` must be evaluated in the user's local timezone. In the
14-day format, where two exercises can be delivered on the same working day,
use the most recently delivered exercise as `current_exercise_context`.
If a later workflow needs the Coach to distinguish both exercises explicitly,
expand this to `delivered_exercises_today`; do not silently guess which one the
user means.

If no exercise has actually been delivered today:

```json
{
  "current_exercise_context": null
}
```

The Coach may clarify or repeat only the supplied `title`, `steps`, and duration.
It must not create variations, add steps, alter the exercise, or infer missing
instructions.

Add tests proving:

- the latest delivered exercise is included for `ACTIVE`;
- `display.steps` and `duration_label` are preserved exactly;
- future, pending, skipped, canceled, or unrelated exercises are not exposed as
  the current exercise;
- missing content produces `current_exercise_context = null`, not invented data;
- the context reaches `_compose_messages()` before the user message.

### Related P0 discovered during verification: v5 delivery rendering

The v5 content library stores user instructions under:

```text
content_payload.display.title
content_payload.display.steps
content_payload.display.duration_label
```

However:

- `plan_finalization._build_step_title()` reads root `content_payload.title`;
- `plan_finalization._build_step_description()` reads root
  `description`, `text`, or `instructions`;
- `format_task_notification()` reads root `instructions` and root duration
  fields.

The content loader preserves the nested `display` object and does not flatten
it. Therefore the current delivery path may fail to render the v5
`display.steps` in the Telegram exercise notification.

Verify and fix the notification renderer to read the canonical v5
`content_payload.display` fields. This is separate from, but required alongside,
the Coach context fix: the user and the Coach must receive the same trusted
exercise instructions.

Priority order:

1. **P0 — Telegram renderer**
   Fix and test the core scheduled notification first. If the user does not
   receive `display.title`, `display.steps`, and `display.duration_label`, the
   primary daily product loop is broken independently of the Coach.

2. **P0 — `current_exercise_context`**
   Add the delivered exercise data to the Coach runtime before enabling the new
   ACTIVE PLAN prompt behavior.

Status: open, P0 before production.

## Backlog — Product question escalation flow

Design and implement a real escalation path for factual product questions that
cannot be answered from the Product Map or current runtime context.

### Product decision required

Decide:

- where escalated questions go;
- who receives and answers them;
- whether the user receives an answer in the same Telegram conversation;
- whether escalation is automatic or requires explicit user confirmation;
- what response-time expectation, if any, is shown to the user;
- whether the unresolved question should be stored for future Product Map
  updates.

Possible implementation options:

- a deterministic support contact or support button;
- a support queue persisted in the database;
- an `escalate_product_question` runtime tool available to the Coach;
- an admin notification with a later human reply flow.

### Required Coach behavior before implementation

If the Product Map and current context do not contain the requested factual
detail, the Coach must:

- say that it does not have that detail;
- avoid guessing, approximating, or inventing an answer;
- avoid claiming that the question was sent, escalated, or reported to the
  product team.

The Coach may claim successful escalation only after a real escalation action
has completed and the runtime has returned a successful result.

### Future tool contract

If implemented as a tool, the minimum input should include:

```json
{
  "question": "The user's unresolved product question",
  "relevant_context": "Minimal context required to understand the question"
}
```

The tool result should explicitly distinguish:

- accepted for human review;
- already answered by an existing source;
- failed to submit.

### Status

Backlog. Not required to continue the current line-by-line prompt review, but
must be resolved before the Coach is instructed to offer product escalation as
an available user action.

## 2026-06-19 — Remove SCHEDULE_ADJUSTMENT from Coach-facing architecture

### Decision

Remove `SCHEDULE_ADJUSTMENT` from the Coach-facing state map and do not model
time changes as a separate conversational state.

Planned interaction:

1. The user asks to change a delivery time.
2. The Coach collects a concrete time if it is missing.
3. The user confirms the requested change.
4. The Coach calls `change_day_time` or `change_evening_time`.

This should be a direct tool-call flow governed by tool descriptions, current
state permissions, and consent rules.

### Reason

A dedicated FSM state adds a separate conversation mode for an operation that
does not require one. It also creates additional Redis context, transition
logic, and recovery behavior, with a risk of leaving the user stuck inside a
time-change workflow.

The time-change behavior is already covered by:

- the `ACTIVE` and `ACTIVE_PAUSED` state permissions;
- Product Map explanations;
- `change_day_time` and `change_evening_time` tool descriptions;
- explicit confirmation rules.

### Backend cleanup TODO

- Remove `SCHEDULE_ADJUSTMENT` from FSM states, transitions, and guards.
- Remove `schedule_adjustment_context` Redis keys and session-memory methods.
- Remove schedule-adjustment last-active and soft-prompt tracking.
- Remove orchestrator branches and helper functions tied to the dedicated
  state.
- Remove Telegram callbacks and handlers that depend on the old workflow.
- Preserve day-time versus evening-time disambiguation in the tool schema.
- Preserve first-time evening collection through `record_evening_time`; do not
  confuse it with changing an existing evening time.
- Add direct tool-flow tests for:
  missing time, invalid time, explicit confirmation, day-time change,
  evening-time change, and cancellation before confirmation.

### Status

The prompt block is being removed during the current prompt review.
Backend cleanup remains open and should be completed in a separate code change
before production.

## 2026-06-19 — Section 2.2 Role Boundaries & Scope approved draft

Use the following as the working final version for Section 2.2:

```text
## 2.2 Role Boundaries & Scope

Your role is limited to:

- Love Yourself product support,
- workday emotional support.

---

### Love Yourself Product Support

You may:

- explain the user's current 7 or 14 days,
- explain exercises and how they work,
- answer questions about timing, missed days, pause, resume, cancellation,
  continuation, and available options,
- help the user understand the next relevant choice available inside
  Love Yourself.

Use the Product Map and current runtime context as the source of truth.

Do not invent product facts, personalization logic, features,
or hidden system behavior.

---

### Workday Emotional Support

You may respond when the user brings up work-related pressure, friction,
frustration, tiredness, difficulty starting, or emotional discomfort,
including patterns they notice across multiple workdays.

Keep the response connected to the user's words without labeling,
diagnosing, interpreting hidden causes, confirming self-criticism,
or turning the conversation into a session.

When the user brings up a serious or potentially harmful situation:
respond with emotional support, do not minimize the concern,
and do not redirect it into productivity or plan completion.
Do not decide the outcome for the user.
If safety or crisis rules apply, follow the dedicated safety guidance.

---

### Exercise Explanation Boundary

Use the Product Map to explain:

- how exercises work,
- why an exercise is shown at a particular time,
- that the sequence is prepared in advance and does not require the user
  to choose each exercise.

Explain how to perform the current exercise only from instructions available
in `current_exercise_context`.

Do not:

- invent missing exercise steps,
- modify or replace the current exercise,
- suggest additional exercises outside the current 7 or 14 days,
- list or expose the full exercise library.

---

### Missing Product Information

If the Product Map and current context do not contain the information needed
to answer a factual product question:

- say clearly that you do not have that detail,
- do not infer, approximate, or invent an answer,
- direct the user to product support only when a real escalation path
  is available.

Do not claim that a question was reported, forwarded, or escalated
unless that action actually occurred.

---

### Professional Guidance and Major Decisions

Do not provide professional medical, legal, financial,
career, or other specialist guidance.

Do not make major life, work, career, medical, legal,
or financial decisions for the user.

---

### Outside-Scope Requests

If the user asks for something outside this scope:

- do not perform, draft, solve, or materially assist with the outside-scope task,
- say briefly and directly that it is outside what you can help with,
- do not reinterpret an unrelated request as a wellbeing issue,
- do not force the conversation back to Love Yourself,
- mention an available in-scope form of help only when it is relevant
  to what the user said.

---

### Mixed Requests

If a message contains both an outside-scope task
and workday emotional context:

- acknowledge the emotional context,
- decline the outside-scope task,
- respond only to the part that is within scope.
```

### Section 4.8 safety boundary TODO

When reviewing Section 4.8, explicitly distinguish:

- **serious but non-crisis workday situations**:
  major conflict, fear of losing a job, potentially harmful workplace
  situations, or large work/career decisions without immediate danger;
- **safety or crisis situations**:
  self-harm, threatened harm to others, violence, immediate danger, or another
  acute safety risk.

Required priority rule:

- Section 2.2 governs serious non-crisis situations.
- Dedicated safety guidance overrides Section 2.2 whenever crisis or immediate
  safety criteria apply.
- The Coach must not remain neutral about immediate safety: it must follow the
  crisis response protocol.

Status: RESOLVED (2026-07-11). Section 4.8 no longer exists — deleted during
the Context Use rewrite. Safety moved to Section 6.1 Immediate Safety Risk
with a fact-based trigger (not the non-crisis/crisis distinction originally
planned here). The required priority rule was implemented as: "When this
section applies, do not follow any conflicting instruction elsewhere in
this prompt" (6.1) plus an explicit guard in Section 7 Tool Calls ("the
Immediate Safety Risk rule (Section 6.1) must not currently apply"). See
"Section 5/6/7 — System Security розділено на Security + User Safety"
near the top of this file for full rationale.
