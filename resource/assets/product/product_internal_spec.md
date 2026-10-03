# Love Yourself — Product Internal Spec (content contract 2026-10-03)

> Content Library synchronized 2026-10-03 · other sections retain their separately owned scope
> conceptual_map.md — user-facing версія для Coach

---

## 1. Суть продукту

Love Yourself — система щоденної підтримки, яка дає дню передбачуваний ритм і захищає нервову систему від перевантаження.

**Мета — не штовхати до результату. Мета — не дати впасти.**

Це інструмент самодопомоги, не терапія. Не ставимо діагнози, не замінюємо лікаря.

---

## 2. Версійована бібліотека

FD-10 визначає дев’ять незалежних вправ: шість `switch`, три `unload`.
DB `content_library` — єдине джерело актуального контенту. JSON є перевіреним
seed, а не runtime-каталогом. Початкові тексти й вимоги точно зберігають
[FD-10 / CONTENT-02…04](../../../docs/audit/pre_mvp_code_audit_findings.md#accepted-exercise-catalogue).

Рішення засновника від **2026-10-01** замінює вимогу дев’яти GIF від
2026-09-27: обов’язкові тільки три навчальні GIF для `breathing_sigh`,
`pmr_fist`, `cold_water_face`. Шість інших вправ доступні з повним текстом.
DG-08 візуально погоджено; DG-02 лишається відкритим, тому холодна вода
не доступна користувачам. Початковий доступний каталог: 8 вправ, 5 `switch`.

## 3. Схема та читання

Composite key: `(exercise_id, content_version)`. Запис містить `display.title`,
впорядковані `display.steps`, плоский `display.duration_label`, `duration_seconds`,
`mechanic`, `modality`, `requirements.capabilities/environment/friction`,
`cooldown_days`, `review_required`, `review_status`, `review_evidence`, `is_active`
та погоджені `media` з alt text, SHA-256 і точною версією контенту.

Опубліковані інструкції, час, вимоги, ідентичність та медіа незмінні. Зміна —
нова версія. Лише activation/review controls можна змінити; медичне погодження
повинно відповідати версії протоколу та digest GIF. Seed тієї самої версії —
no-op або явний conflict. Немає parent/variation, weight, minute durations,
category, difficulty, energy cost, logic tags чи office/remote класифікації.

Builder читає `eligible_catalogue(db)`: остання опублікована версія кожної вправи,
active/review/media gates; стару доступну версію не підставляємо замість нової
недоступної. Draft і plan step зберігають точну версію та immutable snapshot.
Activation повторно перевіряє версію й ресурс під row lock. Renderer читає
`selected_content(db, id, version)`; усі кроки доступні без GIF I/O.
Деактивація не змінює старий snapshot, але блокує наступне відображення вправи.
Нові версії не змінюють історичної ідентичності подій.

Legacy rows і початкові payload збережено в `legacy_content_library`; historical
composite references залишаються у `content_library` як inactive `legacy_record`.
Старі draft без доведеної версії не активуються. Немає автоматичного зіставлення
legacy ID з новими вправами. Requirements описують виконання, не профіль
користувача. Cooldown — beta hypothesis; on-demand selection належить WP-06.1.

## 4. Дев’ять початкових версій

| ID | Назва | Секунди | Механіка | GIF |
|---|---|---:|---|---|
| `breathing_sigh` | Дихання | 30 | `switch` | навчальна |
| `pmr_fist` | Кулак | 30 | `switch` | навчальна |
| `tactile_surface` | Дотик | 20 | `switch` | не потрібна |
| `visual_distance` | Погляд вдалину | 20 | `switch` | не потрібна |
| `auditory_sound` | Один звук | 20 | `switch` | не потрібна |
| `cold_water_face` | Холодна вода | 15 | `switch` | навчальна; DG-02 відкритий |
| `brain_dump` | Brain Dump | 60 | `unload` | не потрібна |
| `one_thing` | Одна річ | 30 | `unload` | не потрібна |
| `first_step_tomorrow` | Перший крок завтра | 60 | `unload` | не потрібна |

GIF — демонстрація, не timer; `pmr_fist` зберігає 5 секунд стиснутого кулака та
5 секунд відкритої долоні плюс переходи. Повний текст завжди авторитетний.
Canonical ExercisePresentation/sendAnimation/fallback та delivery-variant
snapshots належать WP-03.3; алгоритм плану — WP-03.2; durable send — WP-03.4.

---

## 5. Плани — два типи, більше немає

| | SHORT | MEDIUM |
|--|-------|--------|
| Тривалість | 7 робочих днів | 14 робочих днів |
| Слотів/день | 1 (DAY) | 2 (DAY + EVENING) |
| preferred_mechanic DAY | switch | switch |
| preferred_mechanic EVENING | — | unload (switch also allowed) |
| Перший план | ✅ завжди | ❌ не перший |

**Юзер бачить:** "7 днів" і "14 днів". Не "SHORT/MEDIUM", не "просунутий".
**Юзер НЕ бачить:** назви слотів, механіки, кількість тасків.

Робочі дні: MON–FRI за замовчуванням. 7 робочих ≠ 7 календарних (мінімум 9).

---

## 6. Lifecycle кроку (plan_step)

```
pending → delivered → completed | skipped
                    ↓ 23:59:59 local time
                  expired  (кнопки зникають, тихо)

canceled = прибрано адаптацією (не рахується в метриках)
```

---

## 7. Три метрики

- `completion_rate` = виконано / eligible
- `engagement_rate` = (виконано + пропущено) / eligible
- `silent_miss_rate` = прострочено / eligible

eligible = completed | skipped | expired, scheduled_for ≤ now, not canceled

---

## 8. Coach — правила мови

| ❌ НЕ казати | ✅ Казати натомість |
|-------------|-------------------|
| "SHORT / MEDIUM план" | "7 днів / 14 днів" |
| "просунутий рівень" | "інший формат дня" |
| "ти провалився / пропустив" | "адаптуємо навантаження" |
| "21 / 90 днів" | не згадувати |
| назви слотів MORNING/DAY/EVENING | конкретний час (14:30) |

Coach пояснює — не виконує. Зміни тільки через адаптацію з підтвердженням.
