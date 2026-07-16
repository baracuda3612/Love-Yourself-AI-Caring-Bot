# Prompt Review — TODO (відкриті задачі між сесіями)

## Product Map target-contract update (2026-07-16)

Синхронно оновлено українську та англійську Product Map:
- час доставки доповнено обраними робочими днями;
- після завершення 7 днів автоматично готуються наступні 7, після 14 —
  наступні 14, зі збереженням формату, часу та робочих днів;
- completion message описано окремо від вправ;
- зміну 7 ↔ 14 зафіксовано як одну підтверджену atomic operation;
- при першому переході на 14 днів збирається час другої, вечірньої вправи;
- пояснено користувацьку цінність варіативності вправ без internal selection
  fields і без клінічних фреймів;
- `weekly summary` замінено на summary за завершені 7 або 14 днів;
- прибрано `user's mood` з опису незмінності сформованої послідовності;
- додано lifecycle кнопок `Виконано` / `Пропустити`;
- bot-initiated messages звужено до scheduled exercises + completion message.

Product Map тут описує цільовий контракт. Automatic continuation і atomic
`switch_plan_format` ще мають бути реалізовані в backend до production.

### Exercise response window: runtime audit

Product Map фіксує user-facing контракт: кнопки `Виконано` і `Пропустити`
доступні до кінця локального календарного дня; без відповіді вправа
враховується як невиконана.

Поточний runtime майже відповідає цьому контракту, але потребує окремої
перевірки в MVP-аудиті:
- `expires_at` правильно розраховується як `23:59:59` у timezone юзера;
- `expire_overdue_steps()` запускається періодично, тому фактичне зникнення
  кнопок може відбутися з технічним лагом після опівночі;
- step отримує статус `expired`, а `task_ignored` зараз створюється окремим
  `check_ignored_tasks()` через sliding 24-hour window о 08:00 UTC;
- треба звести expiry, видалення клавіатури та ignored telemetry до одного
  локально-денного lifecycle і перевірити race біля опівночі.

### Beta idea: progressive exercise discovery

Не додавати в поточний Product Map і не обіцяти в MVP. Перевірити на одній із
бет retention-гіпотезу: після завершення циклу повідомляти, що в наступній
послідовності з'явиться одна ще не показана вправа.

Принципи експерименту:
- discovery відкривається за завершення циклу, не за високий completion rate;
- без дитячих `levels`, балів чи покарання за пропуски;
- нова вправа додається до нормальної ротації разом із уже знайомими;
- потрібна телеметрія показаних `exercise_id`;
- спочатку перевірити достатність бібліотеки: з 8 активними вправами нову
  вправу щотижня стабільно обіцяти не можна.

## Integration status — target Coach contract (2026-07-16)

PR [#245](https://github.com/baracuda3612/Love-Yourself-AI-Caring-Bot/pull/245)
навмисно закладає цільовий Coach contract, а не тимчасово відновлює
старі manual fallback-шляхи.

Зафіксовано:
- `create_first_plan` не повертається в Coach tools;
- `create_followup_plan` доступний Coach лише в `IDLE_PLAN_ABORTED`;
- `IDLE_FINISHED` не отримує Coach tools;
- відсутні deterministic first-plan creation та automatic continuation
  залишаються backend-задачами MVP-аудиту, а не причиною повертати
  стару архітектуру в цей PR;
- `COACH_TOOLS`, state-filtering і runtime-context framing приведені до
  цільового контракту;
- релевантну доступну альтернативу після недоступної дії дозволено
  згадати; це UX recovery, не автоматичний retention push.

PR не вважати production-ready, доки backend не реалізує відсутні
lifecycle-механізми та Bounded Tool-Result Loop, зафіксований нижче.

## Sequencing decision: Coach prompt (весь файл) — точковий прохід зроблено 2026-07-16, повний фінальний прохід досі ПІСЛЯ завершення MVP-аудиту

**Оновлення (2026-07-16):** Section 1-2 отримали точковий integration-pass
у межах тієї ж сесії що й `COACH_TOOLS`/Product Map sync — виправлено
consent wording (reversible actions), `current_state`/`fsm_state`
розсинхрон, видалено `IDLE_FINISHED` з 2.1 (0 продакшн-юзерів, тому
без ризику), закрито prompt-injection шлях в ACTIVE PLAN ("or
conversation"), перенумеровано `2.4 → 2.3`, точний pointer на `Section
6.1` замість розмитого "dedicated safety guidance". Це виправлення
конкретних багів/розсинхронів, не audit-informed рерайт.

**Досі відкрито:** повний фінальний прохід усього промпту (не тільки
Section 2) — з урахуванням Privacy межі (individual vs company-facing),
Content library (slot not user-facing), Delivery renderer P0, Plan
generation (SHORT/state_switch) — ці блоки `pre_mvp_code_audit_findings.md`
**ще не пройдені**. Причина чекати та сама: переписати промпт під
часткові знахідки зараз means редагувати той самий текст ще раз коли
решта аудиту принесе нові рішення.

**Наслідок:** точкові баги в промпті виправлені (Section 1-7), але
**повний узгоджений прохід усього промпту все ще чекає** Privacy/
Content Library/Delivery Renderer/Plan Generation з аудиту.

## Architecture decision: Bounded Tool-Result Loop — ПЕРЕНЕСЕНО в pre_mvp_code_audit_findings.md

Повний опис, оцінка обсягу роботи, технічні кроки — тепер `COACH-09` у
секції "Coach / Orchestrator Integration Findings" в
`pre_mvp_code_audit_findings.md` (не Coach-специфічний backend-документ,
а частина ширшого MVP-аудиту). Короткий підсумок: Coach зараз one-shot
dispatcher, не бачить результат tool call, canned templates дрейфують
від runtime (доказ — `get_plan_status` paused-баг, `COACH-02`). P1 до
першого зовнішнього MVP-юзера, не P0.

## Product decision: timezone — ПЕРЕНЕСЕНО в pre_mvp_code_audit_findings.md

Повний опис тепер `MISC-01` в секції "Miscellaneous Findings"
(`pre_mvp_code_audit_findings.md`) — чекає власного audit round
"Company / B2B Onboarding". Короткий підсумок: timezone НЕ збирається
на рівні окремого юзера в MVP, компанія задає це на власному onboarding
(`organization.default_timezone`/`available_offices`), per-user override
для відряджень. Код зараз мовчки ставить `Europe/Kyiv` всім.

## Product decision: Time Picker UX — ПЕРЕНЕСЕНО в pre_mvp_code_audit_findings.md

Повний опис тепер `MISC-02` в секції "Miscellaneous Findings"
(`pre_mvp_code_audit_findings.md`) — чекає власного audit round
"Delivery UX". Короткий підсумок: buttons-first UX для вибору часу,
Phase 3, не MVP. Coach поки використовує natural-language fallback
(Section 7 Time Arguments).

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

## Backend Audit Backlog — ПЕРЕНЕСЕНО в pre_mvp_code_audit_findings.md

Усі 8 пунктів (виявлені під час рев'ю Section 7 Tool Calls) тепер
`COACH-01` через `COACH-08` в секції "Coach / Orchestrator Integration
Findings" (`pre_mvp_code_audit_findings.md`), у форматі Severity/Status/
Fix узгодженому з рештою audit-документа. `COACH-06` (state-filtered
tool registration) там же позначений **RESOLVED** — реалізовано цієї
сесії через `_coach_tools_for_state()`.

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

## Product decision: switch_plan_format — ПЕРЕНЕСЕНО в pre_mvp_code_audit_findings.md

Повний опис тепер `COACH-11` (і пов'язаний `COACH-12` — lifecycle
friction після FD-01 auto-continuation) в секції "Coach / Orchestrator
Integration Findings" (`pre_mvp_code_audit_findings.md`). Короткий
підсумок: CONFIRMED founder decision, Product Map вже описує atomic
format-switch як ціль, tool ще не реалізований — тимчасовий, прийнятний
розрив (0 продакшн-юзерів).

## Product decision: default continuation format after completion — RESOLVED 2026-07-16

**Питання:** коли `FD-01` (auto-continuation) імплементований — після
завершення 7-денного плану дефолт наступний теж 7-денний. А після
завершення **14-денного**? Наступний за замовчуванням 14, чи 7?

**Рішення:** автоматично продовжувати **останній обраний формат**. Після
7-денного формату система готує наступні 7 робочих днів; після 14-денного —
наступні 14. Зберігаються поточний формат, час доставки та обрані робочі дні.

Це відповідає ментальній моделі юзера: система продовжує встановлений ритм,
доки він сам не попросить інший, а не скидає його на коротший формат.

**Статус:** founder-рішення прийняте й синхронізоване в обох Product Map.
Backend-реалізація лишається частиною `FD-01`/MVP-аудиту.

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

## 2026-06-18 — P0: delivered exercise context missing — ПЕРЕНЕСЕНО в pre_mvp_code_audit_findings.md

Повний опис (JSON payload shape, тести, пов'язаний renderer-баг з
точними назвами функцій) тепер `COACH-08` в секції "Coach / Orchestrator
Integration Findings" (`pre_mvp_code_audit_findings.md`). Короткий
підсумок: Coach не отримує `current_exercise_context` жодним шляхом —
не через `short_term_history`, не через structured payload. P0 перед
продакшном, разом з окремим P0 в renderer (`display.*` не читається).

## Backlog — Product question escalation flow — ПЕРЕНЕСЕНО в pre_mvp_code_audit_findings.md

Повний опис тепер `COACH-10` в секції "Coach / Orchestrator Integration
Findings" (`pre_mvp_code_audit_findings.md`). Короткий підсумок:
escalation-каналу немає, Coach поки безпечно каже "не маю цієї деталі"
і не вигадує. Founder decision потрібне: куди йдуть питання, хто
відповідає, automatic vs confirmed escalation.

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
