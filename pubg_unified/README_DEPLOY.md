# PUBG backend unified build

Заменить в репозитории:
- `main.py`
- `scanner.py`
- `admin.html`
- `index.html`

`requirements.txt` оставь текущий.

После push:
1. Render: дождаться redeploy.
2. Vercel: redeploy/новый deployment из этого commit.
3. Открыть `/health` — должен вернуть `{"status":"ok"}`.
4. Открыть `/admin`, загрузить `11433.png`, нажать «Сканировать».
5. Для этого скриншота ожидается 4 карточки основного ряда.

Старые записи с `hash: "undefined"` можно удалить и пересохранить.
