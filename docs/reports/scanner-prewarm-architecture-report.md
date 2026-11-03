# Архитектурное предложение: ScannerPrewarmController v2

Дата: 2026-11-01
Статус: исследование завершено, production-код НЕ изменялся
Исходные файлы: `scanner_prewarm.py` (295 строк), `main.py` (`_scanner_warmup()` строка 72, `SCANNER_PREWARM` строка 86)
Тесты: `tests/test_scanner_prewarm_architecture.py` (9 тестов, фактический запуск: 7 PASSED / 2 FAILED-приёмочных)

---

## 1. Фактически подтверждённые проблемы (измерено, не гипотеза)

| № | Проблема | Доказательство |
|---|----------|----------------|
| P1 | **После ошибки/timeout повторный прогрев невозможен навсегда** — `_attempted=True` не сбрасывается | Тест `test_recovery_after_error_NEW_ARCH`: retry вернул `warmup_already_attempted` |
| P2 | **Отмена задачи оставляет `_status="running"` навсегда** — `CancelledError` (BaseException) не ловится `except Exception` в `_run()` | Тест `test_task_cancellation_state_NEW_ARCH`: статус после cancel = `"running"` |
| P3 | **Поток warmup продолжает работать после timeout** — `asyncio.wait_for` отменяет ожидание, но не поток | Тест `test_timeout_thread_continues_and_retry_blocked`: поток `asyncio_0` жив 1.7 с после timeout |
| P4 | **Shutdown без таймаута** — `await asyncio.gather(self._task)` ждёт поток целиком | Тест `test_shutdown_during_warmup_waits_full_thread`: shutdown занял ≥1.4 с при 1.5-с потоке |
| P5 | **Прогрев и скан параллельны, семафор их не разделяет** | Тест `test_warmup_runs_parallel_to_scan_thread`: интервалы перекрываются |
| P6 | 20 одновременных триггеров → ровно 1 прогрев + 19 `warmup_in_progress` | Тест `test_20_concurrent_triggers_start_single_warmup` PASSED — защита работает |

---

## 2. Новая архитектура: конечный автомат

### Диаграмма переходов состояний

```
                    ┌─────────────────────────────────────────────┐
                    │            (start, attempts < MAX)          │
                    ▼                                             │
             ┌───────────┐   success   ┌───────────┐              │
             │not_started│────────────▶│ completed │ (терминал)   │
             └───────────┘             └───────────┘              │
                    ▲                                             │
                    │ (retry, thread_done)                        │
                    │                                             │
             ┌───────────┐  exception  ┌─────────┐  (retry)       │
             │           │────────────▶│ failed  │────────────────┤
             │  running  │             └─────────┘                │
             │           │  timeout   ┌───────────┐ (retry,       │
             │           │────────────▶│ timed_out│  thread_done) │
             │           │             └───────────┘               │
             │           │  cancel    ┌───────────┐ (retry,       │
             │           │────────────▶│ cancelled│  thread_done) │
             │           │             └───────────┘               │
             └───────────┘  attempts >= MAX   ┌───────────┐       │
             │           │────────────────────▶│ exhausted │       │
             │           │                     └───────────┘       │
             │           │  shutdown timeout  ┌─────────────┐      │
             └───────────┘───────────────────▶│ interrupted │      │
                                              └─────────────┘      │
```

### Ключевые инварианты

1. **Одно поле состояния** `_state` заменяет пару `_status` + `_attempted` (убраны независимые boolean-флаги).
2. **`_thread_done: threading.Event`** — устанавливается в `finally` обёртки warmup. Переход на retry разрешён **только если** `_thread_done.is_set()` — это исключает два одновременных потока прогрева.
3. **Бюджет попыток** `MAX_ATTEMPTS = 3` (env `SCANNER_PREWARM_MAX_ATTEMPTS`), счётчик сбрасывается при успехе. Превышен → `exhausted` (терминал до ручного `reset()`). Бесконечный цикл восстановления исключён.
4. **Публичное API не меняется**: `snapshot()`, `status`, `warmup_age_ms()`, `feature_flags()`, `on_trigger()`, `on_application_start()`, `shutdown()`. Значения `warmup_status` расширяются: `not_started, running, completed, failed, timed_out, cancelled, exhausted, interrupted` (текущие `not_started/running/completed/failed` сохраняются).

### Обработка исключений в `_run()`

```
except asyncio.CancelledError:  → state = "cancelled",  re-raise
except asyncio.TimeoutError:   → state = "timed_out"   (поток может жить дальше — учтено через _thread_done)
except Exception:              → state = "failed"
finally:                        → _attempts += 1, если >= MAX → "exhausted"
```

Отмена задачи больше не оставляет `running` (P2 исправляется).

---

## 3. SCAN_SEMAPHORE

**Единое место захвата** — только `lifespan()` в `main.py` для startup-прогрева (строка 113). Триггерные прогревы (`user_online`, `scanner_open`, `image_selected`) **не захватывают семафор** — как сейчас.

- **Повторный захват исключён**: семафор не реентерабелен, прогрев не вложен в скан.
- **Приоритет реального скана**: `before_scan` никогда не запускает прогрев (подтверждено тестом), скан не ждёт прогрев.
- **Starvation**: при `SCAN_CONCURRENCY=1` startup-прогрев блокирует сканы ≤2 с (timeout) — допустимо. Триггерный прогрев не блокирует сканы вообще.
- **Deadlock**: не обнаружен — прогрев никогда не ждёт скан, скан никогда не ждёт прогрев (односторонняя зависимость).

---

## 4. Shutdown

```python
async def shutdown(self) -> None:
    if self._task and not self._task.done():
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=5.0)
        except (asyncio.TimeoutError, Exception):
            self._state = "interrupted"
            LOGGER.warning("Warmup thread still alive at shutdown — not stopped")
```

Поток Python **не останавливается** — это честно зафиксировано в состоянии `interrupted` и лог-событии, а не утверждается как остановленный. Бесконечное ожидание исключено таймаутом.

---

## 5. Безопасный «полный» прогрев (только предложение)

Расширить `_scanner_warmup()` в `main.py` (не менять бизнес-логику):

1. Текущий лёгкий warmup (PIL PNG/JPEG, ~30 мс) — оставляем.
2. Добавить: `detect_inventory_cards()` + расчёт хешей на **синтетическом** изображении 640×360 с нарисованными прямоугольниками «карт» — инициализирует пути `cv2.detectContours`, морфологии, `imagehash` без тяжёлой обработки (ориентир <500 мс, в пределах timeout 2 с).
3. Не использовать эталонные скриншоты, не запускать counter-детектор на реальных контурах.

---

## 6. Конкретные изменения по файлам

| Файл | Изменение |
|------|-----------|
| `scanner_prewarm.py` | Перезапись `ScannerPrewarmController`: `_state` + `_attempts` + `_thread_done`; обработка `CancelledError`; `shutdown` с таймаутом; `reset()`; константа `MAX_ATTEMPTS` |
| `main.py` | Опционально: расширенный `_scanner_warmup()` (п. 5); admin-endpoint `POST /admin/scanner-prewarm/reset`; ничего в `lifespan` менять не нужно |
| `tests/test_scanner_prewarm_architecture.py` | **Создан**: 9 тестов, 7 PASSED / 2 FAILED-приёмочных |
| `index.html`, `admin.html` | Не требуются: проверка `warmup_status != "completed"` совместима с новыми значениями |

---

## 7. Результаты тестов (фактические, не предполагаемые)

| Тест | Результат | Что подтверждает |
|------|-----------|------------------|
| 20 одновременных запросов | **PASSED** | 1 прогрев + 19 `warmup_in_progress` |
| Timeout, поток жив, retry | **PASSED** | P3, P1 подтверждены фактом |
| Retry во время работающего потока | **PASSED** | `warmup_in_progress`, второй поток не стартует |
| Восстановление после ошибки (NEW-ARCH) | **FAILED (ожидаемо)** | `warmup_already_attempted` — P1 |
| Отмена задачи (NEW-ARCH) | **FAILED (ожидаемо)** | статус застрял в `running` — P2 |
| Shutdown во время прогрева | **PASSED** | ожидание ≥1.4 с без таймаута — P4 |
| Прогрев ∥ скан-поток | **PASSED** | параллельность подтверждена — P5 |
| Повтор после успеха | **PASSED** | `already_warm`, идемпотентно |
| `before_scan` | **PASSED** | никогда не стартует прогрев |

Запуск: `python -m pytest tests/test_scanner_prewarm_architecture.py -v`

---

## 8. Race conditions и deadlock (итоговый список)

**Подтверждённые:**
- **RC-1**: после `failed`/`timed_out` состояние несовместимо — `_status="failed"` + `_attempted=True` блокирует retry навсегда (P1).
- **RC-2**: cancel задачи → `_status="running"` + `_task.done()` → misleading-состояние для фронта (P2).
- **RC-3**: timeout не останавливает поток; контроллер теряет информацию о живом потоке (P3) — в v2 закрывается `_thread_done`.
- **RC-4**: `shutdown()` без таймаута → зависание при «зависшем» потоке (P4).

**Потенциальные (не воспроизведены, гипотеза):**
- **D-1**: deadlock — не обнаружен; односторонняя зависимость исключает цикл ожидания.
- **S-1**: starvation сканов при `SCAN_CONCURRENCY=1` во время startup-прогрева ≤2 с — допустимый риск.
- **S-2** (гипотеза): при v1-поведении после timeout повторный прогрев (если бы был разрешён) запустил бы второй поток параллельно первому — в v2 исключён `_thread_done`.

---

## 9. Подтверждённое vs гипотетическое

**Подтверждено экспериментами:** P1–P6, корректность защиты от параллельных прогревов, идемпотентность после успеха, приоритет `before_scan`, параллельность прогрева и скана, поведение shutdown.

**Гипотезы (не проверялись):** фактическое время полного прогрева на синтетическом изображении; влияние расширенного warmup на cold-start latency реальных сканов (предыдущие замеры: warm 195 мс vs cold 267 мс); поведение на Enzonic (не запускалось по ограничению).

---

## Приложения: предыдущие замеры производительности (этапы 1–3)

| Режим | Среднее (ms) | Мин (ms) | Макс (ms) | StdDev (ms) |
|-------|-------------|----------|-----------|-------------|
| Cold start (без warmup) | 267.4 | 139.6 | 1251.1 | 214.0 |
| Warm start (после `_scanner_warmup()`) | 195.1 | 89.4 | 1374.5 | 245.0 |
| Hot start (после 3 раундов сканов) | 157.9 | 90.7 | 248.8 | 57.1 |

Потребление ресурсов при прогреве: +1.46 MB RSS, ~0.05 с CPU, длительность ~70 мс.

Тестовые скрипты сессии (не production): `benchmark.py`, `coldstart_benchmark.py`, `experiment_benchmark.py`, `experiment_rigorous.py`, `concurrent_warmup_test.py`, `warmup_recovery_test.py`.
