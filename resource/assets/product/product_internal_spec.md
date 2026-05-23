# Love Yourself — Product Internal Spec (v2.0)

> травень 2026 · внутрішній документ для Codex / builder / internal agents
> conceptual_map.md — user-facing версія для Coach

---

## 1. Суть продукту

Love Yourself — система щоденної підтримки, яка дає дню передбачуваний ритм і захищає нервову систему від перевантаження.

**Мета — не штовхати до результату. Мета — не дати впасти.**

Це інструмент самодопомоги, не терапія. Не ставимо діагнози, не замінюємо лікаря.

---

## 2. Дві механіки — вся бібліотека

В бібліотеці v5 **8 вправ** і **2 механіки**. Більше нічого.

### state_switch
Фізично або сенсорно вирвати з поточного стану. Працює і при "втомився", і при "застряг".
Слоти: **DAY, EVENING**

### unload
Вивантажити шум або закрити день. Особливо ввечері.
Слоти: **EVENING тільки**

> **Правило слотів — derived, не stored:**
> `switch` → може бути в DAY і EVENING
> `unload` → тільки EVENING
> MORNING не використовується в P1 plan recipes. DAY і EVENING — внутрішні технічні теги. Юзер ніколи не бачить назв слотів — тільки конкретний час HH:MM.

---

## 3. Схема вправи (exercise)

```
id                  str        унікальний ідентифікатор
is_active           bool
mechanic            switch | unload
duration_minutes    int        default — для scheduled channel (30–60 сек)
extended_minutes    int|null   extended — для reactive / user-initiated channel (до 2 хв)
cooldown_days       int        мінімум між повтореннями
weight              float      для зваженого random у plan builder

display:
  title             str        один рядок
  steps             list[str]  2–4 кроки — саме те що йде в Telegram
  duration_label    str        "30–60 сек" — для юзера

variations:                  опціонально
  id                str
  label             str       "remote", "office", "active", "passive", ...
  steps             list[str]
  duration_minutes  int
  duration_label    str
```

**Не в схемі (прибрано):** `focus`, `load`, `difficulty`, `energy_cost`, `impact_areas`, `priority_tier`, `category`, `allowed_slots`

---

## 4. Бібліотека v5 — 8 вправ

| ID | Назва | Механіка | Хв | Варіації |
|----|-------|----------|----|----------|
| `somatic_004_v2` | Дихання | switch | 30–60 сек (ext: 2 хв) | — |
| `somatic_005_v2` | Холодна вода | switch | 30–60 сек (ext: 2 хв) | — |
| `somatic_006_v2` | Мікроходьба | switch | 30–60 сек (ext: 2 хв) | remote / office / stuck |
| `somatic_001_combined` | Перезавантаження тіла | switch | 30–60 сек | active / passive |
| `somatic_003_v2` | Сенсорний якір | switch | 30–60 сек | sight / touch / sound / smell / body |
| `rest_104_v2` | Brain Dump | unload | 3 | — |
| `cognitive_008_v2` | Одна річ | unload | 2 | — |
| `cognitive_001_v2` | Думка | unload | 2 | — |

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
