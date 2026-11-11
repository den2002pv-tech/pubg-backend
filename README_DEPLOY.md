# Развёртывание PUBG Cards

## Текущая схема

- Backend и frontend работают на Enzonic. Контейнер использует образ `ghcr.io/parkervcp/yolks:python_3.14` (Python 3.14), один процесс Uvicorn и primary allocation `0.0.0.0:25722`. Внешний HTTPS-origin `https://pubg-card.enzonic.dev:443` активен и проксирует запросы в контейнер. PostgreSQL размещён как отдельный сервис Enzonic; параметры подключения приведены ниже.
- FastAPI обслуживает frontend по `/`, админ-панель по `/admin`, API и `GET /health` на том же origin. Поэтому `API = ''` в `index.html` и `API_BASE = ''` в `admin.html` используют same-origin API и не требуют CORS.
- SQLite — первичная база приложения. Запросы приложения выполняются через SQLite; PostgreSQL не участвует в пути запросов и используется только как цель полного снапшот-бэкапа через `db_sync.backup_snapshot_to_pg()`. `DATABASE_URL` нужен для этого резервного копирования. Не храните DSN в репозитории и не выводите его в логи.
- Startup-скрипт Enzonic задаёт `SQLITE_DATABASE_PATH=/home/container/data/pubg_local.db`. Файл `/home/container/data/pubg_local.db` существует (около 440 KB); постоянство тома подтверждается длительным uptime контейнера (~356 дней), наличием файлов от 2026-10-25 и более поздних дат и резервной копией от 2026-11-06 с данными. WAL/SHM-файлы на момент проверки отсутствовали. Без переменной `database_manager._sqlite_path()` выбирает `/home/container/data/pubg_local.db`, если существует `/home/container/data` или `/home/container`, иначе `./data/pubg_local.db`.
- `PUBLIC_BASE_URL` в Enzonic не задан. Используется `app_config.DEFAULT_PUBLIC_BASE_URL=https://pubg-card.enzonic.dev`, совпадающий с активным managed-доменом панели.

В репозитории нет конфигурационного файла Enzonic: startup command хранится в панели как кастомный bash-скрипт, а не как egg-команда по умолчанию, Dockerfile или Procfile репозитория. Скрипт выполняет `cd /home/container`, валидирует `SERVER_PORT`, экспортирует `PORT=$SERVER_PORT`, `PYTHONPATH=/home/container/.deps`, `TMPDIR=/home/container/.tmp` и `SQLITE_DATABASE_PATH=/home/container/data/pubg_local.db`, устанавливает зависимости в `.deps` при изменении `requirements.txt` с контролем hash и завершает запуск командой `exec python3 -m uvicorn main:app --app-dir /home/container --host 0.0.0.0 --port $SERVER_PORT`. Сейчас `SERVER_PORT=25722`. `exec` критичен: PID 1 становится процессом Uvicorn, а SIGTERM при остановке контейнера доставляется приложению и позволяет выполнить финальный `_final_database_backup()` из `lifespan`. Зависимости проекта перечислены в `requirements.txt`.

По разделу Limits панели Enzonic установлены memory=512 MB, CPU=100% (1 core), disk=1024 MB. На 2026-11-11 панель показывала потребление около 80.4 MB RAM, 23% CPU и 511.5 MB диска (примерно 50% лимита); uptime — 30 807 616 секунд. Это показатели панели, а не измерения runtime cgroup. Точные значения `ps`, `ss`, `df`, `du` и `/sys/fs/cgroup/*` требуют отдельной проверки из shell контейнера. Диагностика читает доступные приложению `/proc`/cgroup значения, если они присутствуют. Никакая искусственная CPU-нагрузка не создаётся.

Для ограничения ресурсов сканирования доступны `SCAN_CONCURRENCY` (максимум одновременных сканов, по умолчанию `1`), `CV2_NUM_THREADS` (потоки OpenCV, по умолчанию `1`), `COUNTER_MAX_CONTOURS_PER_PASS` (обрабатываемые контуры на один проход детектора, по умолчанию `64`), `SCAN_RATE_LIMIT` (запросов на один ключ за период, по умолчанию `10`) и `SCAN_RATE_WINDOW_SECONDS` (период в секундах, по умолчанию `60`). `POST /scan` ограничивает анонимных по адресу клиента и авторизованных по проверенному Telegram user ID. Загрузка ограничена 15 МБ и 30 мегапикселями.

## Диагностика сканера и локальные файлы

Диагностические события `POST /scan` записываются в JSONL в `scanner_diagnostics/` рядом с `main.py` (или в путь из `SCANNER_DIAGNOSTICS_DIR`). Это локальная файловая система процесса: в репозитории нет конфигурации Enzonic, сведений о постоянном томе либо подтверждения сохранения файлов после рестарта/деплоя. Не используйте эти логи как постоянное хранилище; сначала подтвердите свойства диска в панели Enzonic.

Файлы изолированы от пользовательских данных, имеют права создания `0600`/`0700`, ротируются по 5 МиБ и очищаются в пределах 100 МиБ/7 дней. Лимиты настраиваются переменными `SCANNER_DIAGNOSTICS_MAX_FILE_BYTES`, `SCANNER_DIAGNOSTICS_MAX_TOTAL_BYTES`, `SCANNER_DIAGNOSTICS_RETENTION_DAYS` и `SCANNER_DIAGNOSTICS_MAX_REPORT_BYTES` (10 МиБ). `SCANNER_DIAGNOSTICS_ENABLED` и `SCANNER_DIAGNOSTICS_LOCAL_STORAGE_ENABLED` позволяют отключать запись. Сбой файлового логирования не прерывает сканирование.

Логи и сводка доступны только через серверную административную авторизацию: `GET /admin/scan-logs/files`, `GET /admin/scan-logs/summary` и скачивание конкретного файла через `GET /admin/scan-logs/files/{filename}`. В админ-панели есть список файлов и прямая загрузка. Несколько worker ведут отдельные журналы; их локальные каталоги не объединяются автоматически.

Лёгкий прогрев выполняется при старте worker и может запускаться после подтверждённого Telegram heartbeat, открытия сканера или выбора файла. Сигналы не отправляют изображения и не ждут завершения прогрева. Сканирование не блокируется прогревом. Флаги: `SCANNER_PREWARM_ENABLED`, `SCANNER_PREWARM_ON_START_ENABLED`, `SCANNER_PREWARM_ONLINE_ENABLED`, `SCANNER_PREWARM_ON_OPEN_ENABLED`, `SCANNER_PREWARM_ON_FILE_SELECT_ENABLED`, `SCANNER_PREWARM_ON_SCAN_ENABLED`. `SCANNER_PREWARM_CPU_EXPERIMENT_ENABLED` по умолчанию выключен; CPU-эксперимент не реализован и не запускается даже при включённом запросе флага, поскольку политика/модель CPU Enzonic недоступна в репозитории. Доступные процессные/cgroup метрики записываются только в моменты событий и запросов; недоступные значения остаются `null`.

## Переменные окружения

Храните значения только в настройках окружения сервиса, не в Git и не в логах. Ниже перечислены только имена переменных — их значения и секреты не документируются.

**Установлены в Enzonic:** `MAIN_FILE`, `USER_UPLOAD`, `GIT_ADDRESS`, `BRANCH`, `PY_PACKAGES`, `ADMIN_PASSWORD`, `CARD_MATCH_ACCEPTABLE_DISTANCE`, `CARD_MATCH_COLOR_MAX_RATIO`, `CARD_MATCH_STRONG_DISTANCE`, `DATABASE_URL`, `GITHUB_BRANCH`, `GITHUB_REPO`, `GITHUB_TOKEN`, `SCAN_RATE_LIMIT`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`.

**Экспортируются startup-скриптом:** `SERVER_PORT`, `PORT`, `PYTHONPATH`, `TMPDIR`, `SQLITE_DATABASE_PATH`.

**Не заданы в Enzonic; используются defaults кода:** `PUBLIC_BASE_URL`, `SCAN_CONCURRENCY`, `SENTRY_DSN`, `SENTRY_TRACES_SAMPLE_RATE`, `TEMP_FILE_TTL_SECONDS`, `TEMP_FILE_MAX_BYTES`, `ADMIN_SESSION_SECRET`, `DB_FAILOVER_CONNECT_TIMEOUT`, `DB_BACKUP_DEBOUNCE`, `CARD_CATALOG_ALLOW_DELETE`, `CARD_CATALOG_MAX_SHRINK_PERCENT`, `CARD_MATCH_THRESHOLD`, `CARD_MATCH_MIN_STRONG_HASHES`, `CARD_MATCH_MIN_ACCEPTABLE_HASHES`, `CARD_MATCH_CONFIDENT_MARGIN`, `SCAN_RATE_WINDOW_SECONDS`, `COUNTER_TEST_BRANCH`, `CV2_NUM_THREADS`, `COUNTER_MAX_CONTOURS_PER_PASS`, `SCANNER_DIAGNOSTICS_DIR`, `APP_COMMIT`, `GITHUB_SHA`, `RENDER_GIT_COMMIT`, `SOURCE_VERSION`.

`DATABASE_URL` указывает на отдельный PostgreSQL pooler Enzonic: хост `pubg-db-m6azzf-pooler.db.enzonic.com`, порт `5432`, БД `main`, `sslmode=require`. PostgreSQL используется только как цель полного снапшот-бэкапа через `db_sync.backup_snapshot_to_pg()`; запросы приложения в PostgreSQL не идут. MySQL-сервис `s131204_pubg_cards` в панели присутствует, но приложением не используется.

`app_config.get_app_commit()` выбирает первое непустое значение в порядке `APP_COMMIT` → `GITHUB_SHA` → `RENDER_GIT_COMMIT` → `SOURCE_VERSION`. Все четыре переменные в Enzonic не заданы, поэтому используется fallback через GitHub API (`_github_head_sha`), затем пробуется `git rev-parse --short=12 HEAD`, а при неудаче возвращается `unknown`. Фактическое значение для запущенного процесса требует проверки в логах/диагностике.

Для фонового PostgreSQL-снапшота используется `DB_BACKUP_DEBOUNCE` (задержка debounce, default кода — 7 секунд). Устаревшие параметры failover/retry не следует считать частью текущей модели резервирования.

### Статус SQLite и PostgreSQL-снапшота

SQLite — единственная первичная база приложения. PostgreSQL используется только как приёмник полного снапшота через `db_sync.backup_snapshot_to_pg()`; запросы приложения не читают и не записывают PostgreSQL.

Фоновый поток `database-snapshot-backup` выполняет снапшот с debounce, задаваемым `DB_BACKUP_DEBOUNCE` (по умолчанию 7 секунд). После успешного `commit()` в контекстном менеджере `get_connection()` вызывается `mark_dirty()`, чтобы запланировать резервное копирование. При завершении приложения `_final_database_backup()` выполняет финальный flush, если `has_pending_backup()`; lifespan ограничивает ожидание общим таймаутом 5 секунд.

Outbox-механизм в текущей схеме не используется. `db_sqlite_schema.ensure_sqlite_schema()` удаляет legacy-таблицы `outbox_operations`, `sync_lock`, `outbox_applied_in_pg`, `replicated_outbox_transactions`, `outbox_applied`, `database_metadata`. Перед заменой данных `db_sync.backup_snapshot_to_pg()` удаляет legacy-триггеры с именами `trg_app_outbox_*`. Утверждения о незавершённом failover/outbox и об обязательном маркере `postgres_snapshot_complete` не описывают текущую реализацию: такого маркера в коде нет.

`database_manager.restore_from_pg_if_missing()` проверяет локальный SQLite-файл (наличие, размер и результат `PRAGMA quick_check`) и при необходимости восстанавливает его из PostgreSQL-снапшота. Решение не основано на `postgres_snapshot_complete`. Это механизм восстановления SQLite из резервной копии, а не переключение запросов на PostgreSQL.

PostgreSQL подключён через отдельный pooler Enzonic (`pubg-db-m6azzf-pooler.db.enzonic.com:5432`, БД `main`, `sslmode=require`) и используется только для снапшотов. Автоматические бэкапы контейнера отключены (`auto_backup_enabled: false`); лимит панели — 1 бэкап. Имеется один успешный ручной бэкап от 2026-11-06 09:33 UTC размером около 282 MB, хранящийся в S3. Бэкап панели покрывает файлы контейнера, включая SQLite, но не отдельный PostgreSQL-сервис. Периодичность/retention S3 и наличие/периодичность резервных копий PostgreSQL требуют проверки в соответствующих настройках панели. Восстановление после потери тома выполняется через панель → Backups → Restore.

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
