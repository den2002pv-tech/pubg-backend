# Развёртывание PUBG Cards

## Текущая схема

- Размещение backend, frontend и PostgreSQL на Enzonic, публичный origin приложения и фактическая схема подключения требуют проверки в панели Enzonic и настройках сервиса.
- FastAPI обслуживает frontend по `/`, админ-панель по `/admin`, API и `GET /health` на том же origin. Поэтому `API = ''` в `index.html` и `API_BASE = ''` в `admin.html` используют same-origin API и не требуют CORS.
- SQLite — первичная база приложения. Запросы приложения выполняются через SQLite; PostgreSQL не участвует в пути запросов и используется только как цель полного снапшот-бэкапа через `db_sync.backup_snapshot_to_pg()`. `DATABASE_URL` нужен для этого резервного копирования. Не храните DSN в репозитории и не выводите его в логи.
- Путь первичной SQLite задаётся `SQLITE_DATABASE_PATH`; фактическое постоянство выбранного тома Enzonic требует проверки. Без переменной `database_manager._sqlite_path()` выбирает `/home/container/data/pubg_local.db`, если существует `/home/container/data` или `/home/container`, иначе `./data/pubg_local.db`. Не считайте путь постоянным без проверки панели Enzonic.
- Публичный origin и фактическое значение `PUBLIC_BASE_URL` требуют проверки в настройках Enzonic.

В репозитории нет конфигурационного файла Enzonic. Команда запуска в панели Enzonic, build/start-настройки, привязка порта и параметры PostgreSQL требуют проверки непосредственно в панели; предполагаемую команду запуска нельзя считать подтверждённой. Зависимости проекта перечислены в `requirements.txt`.

Постоянство SQLite-тома, фактическое размещение PostgreSQL, cgroup-лимиты, PID 1 и команда запуска в панели Enzonic требуют отдельной проверки. Диагностика читает доступные приложению `/proc`/cgroup значения, если они присутствуют; они описывают runtime/container и сами по себе не доказывают модель тарифа или гарантию ресурсов. Никакая искусственная CPU-нагрузка не создаётся.

Для ограничения ресурсов сканирования доступны `SCAN_CONCURRENCY` (максимум одновременных сканов, по умолчанию `1`), `CV2_NUM_THREADS` (потоки OpenCV, по умолчанию `1`), `COUNTER_MAX_CONTOURS_PER_PASS` (обрабатываемые контуры на один проход детектора, по умолчанию `64`), `SCAN_RATE_LIMIT` (запросов на один ключ за период, по умолчанию `10`) и `SCAN_RATE_WINDOW_SECONDS` (период в секундах, по умолчанию `60`). `POST /scan` ограничивает анонимных по адресу клиента и авторизованных по проверенному Telegram user ID. Загрузка ограничена 15 МБ и 30 мегапикселями.

## Диагностика сканера и локальные файлы

Диагностические события `POST /scan` записываются в JSONL в `scanner_diagnostics/` рядом с `main.py` (или в путь из `SCANNER_DIAGNOSTICS_DIR`). Это локальная файловая система процесса: в репозитории нет конфигурации Enzonic, сведений о постоянном томе либо подтверждения сохранения файлов после рестарта/деплоя. Не используйте эти логи как постоянное хранилище; сначала подтвердите свойства диска в панели Enzonic.

Файлы изолированы от пользовательских данных, имеют права создания `0600`/`0700`, ротируются по 5 МиБ и очищаются в пределах 100 МиБ/7 дней. Лимиты настраиваются переменными `SCANNER_DIAGNOSTICS_MAX_FILE_BYTES`, `SCANNER_DIAGNOSTICS_MAX_TOTAL_BYTES`, `SCANNER_DIAGNOSTICS_RETENTION_DAYS` и `SCANNER_DIAGNOSTICS_MAX_REPORT_BYTES` (10 МиБ). `SCANNER_DIAGNOSTICS_ENABLED` и `SCANNER_DIAGNOSTICS_LOCAL_STORAGE_ENABLED` позволяют отключать запись. Сбой файлового логирования не прерывает сканирование.

Логи и сводка доступны только через серверную административную авторизацию: `GET /admin/scan-logs/files`, `GET /admin/scan-logs/summary` и скачивание конкретного файла через `GET /admin/scan-logs/files/{filename}`. В админ-панели есть список файлов и прямая загрузка. Несколько worker ведут отдельные журналы; их локальные каталоги не объединяются автоматически.

Лёгкий прогрев выполняется при старте worker и может запускаться после подтверждённого Telegram heartbeat, открытия сканера или выбора файла. Сигналы не отправляют изображения и не ждут завершения прогрева. Сканирование не блокируется прогревом. Флаги: `SCANNER_PREWARM_ENABLED`, `SCANNER_PREWARM_ON_START_ENABLED`, `SCANNER_PREWARM_ONLINE_ENABLED`, `SCANNER_PREWARM_ON_OPEN_ENABLED`, `SCANNER_PREWARM_ON_FILE_SELECT_ENABLED`, `SCANNER_PREWARM_ON_SCAN_ENABLED`. `SCANNER_PREWARM_CPU_EXPERIMENT_ENABLED` по умолчанию выключен; CPU-эксперимент не реализован и не запускается даже при включённом запросе флага, поскольку политика/модель CPU Enzonic недоступна в репозитории. Доступные процессные/cgroup метрики записываются только в моменты событий и запросов; недоступные значения остаются `null`.

## Переменные окружения

Храните значения только в настройках окружения сервиса, не в Git и не в логах. `DATABASE_URL` используется для PostgreSQL-снапшота; для Telegram-функций нужны `TELEGRAM_BOT_TOKEN` и `TELEGRAM_WEBHOOK_SECRET`; админ-функциям требуются `ADMIN_PASSWORD` и рекомендуется `ADMIN_SESSION_SECRET`. Публичный origin и `PUBLIC_BASE_URL` требуют проверки. Для версии приложения можно задать `APP_COMMIT`. `app_config.get_app_commit()` выбирает первое непустое значение в порядке `APP_COMMIT` → `GITHUB_SHA` → `RENDER_GIT_COMMIT` → `SOURCE_VERSION`. Если все переменные отсутствуют, функция запрашивает HEAD настроенной GitHub-ветки через `_github_head_sha`, затем пробует `git rev-parse --short=12 HEAD`, а при неудаче возвращает `unknown`. GitHub API fallback нужен, поскольку Enzonic может копировать файлы приложения без обновления локального `.git`.

Для первичной базы предусмотрена `SQLITE_DATABASE_PATH`. Для фонового PostgreSQL-снапшота используется `DB_BACKUP_DEBOUNCE` (задержка debounce, по умолчанию 7 секунд). Устаревшие параметры failover/retry не следует считать частью текущей модели резервирования.

### Статус SQLite и PostgreSQL-снапшота

SQLite — единственная первичная база приложения. PostgreSQL используется только как приёмник полного снапшота через `db_sync.backup_snapshot_to_pg()`; запросы приложения не читают и не записывают PostgreSQL.

Фоновый поток `database-snapshot-backup` выполняет снапшот с debounce, задаваемым `DB_BACKUP_DEBOUNCE` (по умолчанию 7 секунд). После успешного `commit()` в контекстном менеджере `get_connection()` вызывается `mark_dirty()`, чтобы запланировать резервное копирование. При завершении приложения `_final_database_backup()` выполняет финальный flush, если `has_pending_backup()`; lifespan ограничивает ожидание общим таймаутом 5 секунд.

Outbox-механизм в текущей схеме не используется. `db_sqlite_schema.ensure_sqlite_schema()` удаляет legacy-таблицы `outbox_operations`, `sync_lock`, `outbox_applied_in_pg`, `replicated_outbox_transactions`, `outbox_applied`, `database_metadata`. Перед заменой данных `db_sync.backup_snapshot_to_pg()` удаляет legacy-триггеры с именами `trg_app_outbox_*`. Утверждения о незавершённом failover/outbox и об обязательном маркере `postgres_snapshot_complete` не описывают текущую реализацию: такого маркера в коде нет.

`database_manager.restore_from_pg_if_missing()` проверяет локальный SQLite-файл (наличие, размер и результат `PRAGMA quick_check`) и при необходимости восстанавливает его из PostgreSQL-снапшота. Решение не основано на `postgres_snapshot_complete`. Это механизм восстановления SQLite из резервной копии, а не переключение запросов на PostgreSQL.

Наличие постоянного тома для SQLite, размещение PostgreSQL, политика резервных копий Enzonic и сохранность данных после рестарта/деплоя требуют отдельной проверки в панели платформы.

## Telegram

Код принимает webhook на `POST /telegram/webhook`, сверяет `X-Telegram-Bot-Api-Secret-Token` с `TELEGRAM_WEBHOOK_SECRET` и использует `TELEGRAM_BOT_TOKEN` для вызовов Bot API. Кнопка Telegram Mini App открывает `PUBLIC_BASE_URL`. Наличие маршрута в исходном коде не подтверждает регистрацию webhook в Telegram — проверяйте её отдельно в безопасном канале, не публикуя URL с секретом.

## Проверка после изменения

1. Проверьте ветку, SHA коммита и состав diff.
2. Убедитесь, что Enzonic собрал и запустил нужный SHA; настройки сервиса в панели не подтверждаются этим репозиторием.
3. Проверьте `GET /health` и пользовательские сценарии. Health endpoint подтверждает доступность HTTP-процесса, но не исправность операций с БД, авторизации, сканирования или обменов.
4. Проверьте same-origin frontend/API, ссылки изображений и Telegram Mini App.
5. Убедитесь, что PostgreSQL на Enzonic доступен приложению через заданный `DATABASE_URL`; не запускайте destructive SQL для проверки деплоя.
6. Проверьте GitHub Actions для соответствующего SHA: текущий CI-статус требует проверки в GitHub Actions и не подтверждается наличием workflow-файлов. Успешный деплой не доказывает прохождение тестов.

Не удаляйте записи или файлы данных вслепую: сначала определите источник и подтвердите тестовый характер затрагиваемых данных.
