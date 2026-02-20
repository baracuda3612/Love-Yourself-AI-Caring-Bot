# Алгоритм створення план-драфту (станом на зараз)

## 1) Призначення документа

Цей документ описує **актуальну** логіку формування `draft` плану:
- як система збирає параметри в діалозі,
- як валідує їх до побудови,
- як компонує вправи по днях/слотах,
- як забезпечує детермінованість і варіативність,
- де стоять safety-net перевірки.

Документ орієнтований на продуктову дискусію (PM/PO/дизайн/інженерія).

---

## 2) Рівні системи (хто за що відповідає)

### 2.1 Orchestrator (UX gatekeeper)
Відповідає за:
- збір параметрів від користувача в `PLAN_FLOW:DATA_COLLECTION`,
- **валідацію правил load→slots до запису в session memory**,
- перехід у `PLAN_FLOW:CONFIRMATION_PENDING` лише з валідним станом,
- запуск побудови preview-драфту.

### 2.2 DraftBuilder (алгоритм композиції)
Відповідає за:
- алгоритмічне складання драфту з бібліотеки вправ,
- дотримання структури слотів за load,
- відбір вправ (cooldown/складність/категорія/fallback),
- призначення тайм-слотів у межах дня,
- фінальну валідацію драфту.

### 2.3 Rules layer (чисті правила)
Містить:
- матрицю load→slot structure,
- правила fallback-відбору вправ,
- призначення time-slot з урахуванням user preferences та already used slots,
- seeded weighted choice.

---

## 3) Вхідні параметри драфту

Обов’язкові параметри плану:
- `duration` = `SHORT | STANDARD | LONG`
- `focus` = `SOMATIC | COGNITIVE | BOUNDARIES | REST | MIXED`
- `load` = `LITE | MID | INTENSIVE`
- `preferred_time_slots` (залежить від load)

### 3.1 Політика `load -> expected slots`
- `LITE` → рівно **1** слот
- `MID` → рівно **2** слоти
- `INTENSIVE` → рівно **3** слоти (`MORNING, DAY, EVENING`)

---

## 4) DATA_COLLECTION: UX-контракт і валідація до збереження

Під час DATA_COLLECTION orchestrator:
1. Читає поточні `known_parameters` з session memory.
2. Берe `plan_updates` поточного ходу, санітизує, збирає `proposed_parameters`.
3. Якщо змінено `load`, скидає старі `preferred_time_slots`.
4. Перевіряє валідність слотів **до** `set_plan_parameters(...)`.

### 4.1 `INTENSIVE`
- User custom slots ігноруються.
- Примусово ставиться `['MORNING','DAY','EVENING']`.
- Якщо у memory був невалідний набір — робиться self-heal + UX-повідомлення.
- Можливий перехід у `CONFIRMATION_PENDING` без додаткового питання про слоти.

### 4.2 `MID`
- Вимагає рівно 2 слоти.
- Якщо користувач надіслав 1 або 3 — оркестратор повертає correction message і **рано завершує** хід без запису невалідного стану.

### 4.3 `LITE`
- Вимагає рівно 1 слот.
- Якщо надіслано 2 або 3 — correction message, ранній вихід, без запису невалідного стану.

### 4.4 Умова переходу у `CONFIRMATION_PENDING`
Тільки якщо одночасно:
- `duration != None`
- `focus != None`
- `load != None`
- кількість слотів = expected для цього load.

---

## 5) Побудова драфту (DraftBuilder)

### 5.1 Передумови (guard-и)
DraftBuilder перед стартом побудови перевіряє:
- три базові параметри (three pillars) присутні,
- `preferred_time_slots` присутні,
- їх кількість відповідає expected для `load`.

На порушення кидається `RuntimeError` (safety-net).

### 5.2 Кроки алгоритму
1. Визначає `total_days` від `duration`.
2. Берe `slot_structure` від `load` (LITE/MID/INTENSIVE).
3. Рахує загальну кількість слотів та category distribution (фокус + 80/20 логіка).
4. Для кожного дня:
   - визначає `max_difficulty` за тижнем,
   - алокує `used_slots_today = []`.
5. Для кожного слота в дні:
   - обирає category для слота,
   - формує кандидатів без порушення cooldown,
   - будує `seed_key = f"{user_id}:{day}:{slot_index}"`,
   - обирає вправу через fallback + weighted choice,
   - призначає time slot через `get_time_slot_for_slot_type(..., already_used_slots=used_slots_today)`,
   - додає slot у `used_slots_today`,
   - фіксує usage вправи для cooldown.
6. Після генерації валідовує драфт валідатором.

---

## 6) Відбір вправ: fallback + детермінованість

### 6.1 Fallback (3 рівні)
`select_exercise_with_fallback` застосовує послідовно:
1. preferred category + priority tier + max difficulty
2. any category + priority tier + max difficulty
3. any category + max difficulty

Якщо всі порожні → `None`.

### 6.2 Weighted deterministic selection
На кожному рівні вибір робиться через `_weighted_choice`:
- стабільно сортує пул (`internal_name`, `id`),
- використовує `random.Random(seed_key)`,
- ваги = `base_weight`.

Ефект:
- для однакового `(user_id, day, slot_index)` результат відтворюваний,
- між днями/слотами є варіативність,
- вищі ваги частіше виграють, але не «always-first».

---

## 7) Розподіл time slots у межах дня

`get_time_slot_for_slot_type`:
- враховує відповідність типу слота (CORE/SUPPORT/REST/...) до бажаних time slots,
- враховує user preferences у пріоритеті,
- приймає `already_used_slots`,
- намагається не повторювати слот у тому самому дні,
- fallback до повтору робить лише коли без цього неможливо.

Це забезпечує:
- для 2 задач/день — зазвичай 2 різні часові слоти,
- для 3 задач/день — мінімізація повторів, якщо є доступні альтернативи.

---

## 8) Валідації та safety layers

### 8.1 Orchestrator-level (primary UX contract)
- не дає записати невалідні слоти в session memory,
- керує UX-повідомленнями при помилці вибору слотів,
- не пропускає FSM далі без валідного стану.

### 8.2 DraftBuilder-level (defense-in-depth)
- повторно перевіряє слот-контракт,
- валідовує структуру драфту по завершенні,
- падає явно, якщо стан все ж невалідний.

---

## 9) Що бачить користувач по UX

1. У DATA_COLLECTION користувач збирає `duration/focus/load`.
2. Для `MID/LITE` система просить рівно потрібну кількість слотів.
3. Для `INTENSIVE` слоти призначаються автоматично (`MORNING/DAY/EVENING`).
4. Після валідних параметрів показується draft preview (confirmation stage).
5. На фіналізації користувач одразу отримує «⏳ План генерується…», а важка частина виконується у background.

---

## 10) Відомі продуктові trade-offs для обговорення

1. **Жорсткий slot contract** покращує передбачуваність, але зменшує свободу для edge-case користувачів.
2. **INTENSIVE auto-slots** простіше для UX, але не враховує персональні графіки в деталях.
3. **Seeded weighted choice** балансує детермінізм і різноманіття, але потребує зрозумілої комунікації для команди (чому інколи «та сама» вправа повторюється).
4. **Подвійні guard-и** (orchestrator + draft builder) додають надійність, ціною складнішої підтримки.

---

## 11) Коротко: інваріанти системи

- Session memory має містити **лише валідний** стан параметрів плану.
- `load` визначає slot-policy однозначно.
- Draft генерується тільки з валідних параметрів.
- Вибір вправ детермінований по seed, але не повністю «жорсткий» через weighted randomness.
- Finalization дає негайний UX-відгук, важка робота не блокує відповідь користувачу.
