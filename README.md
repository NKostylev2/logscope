# LogScope MVP

Локальный просмотрщик логов: импорт TXT/LOG/CSV, SQLite-индекс, временной поиск, источник, текстовый поиск и светлая/тёмная тема.

## Требования
- Python 3.11+
- pip

## Запуск (Windows PowerShell)
```powershell
cd backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# Для тестов дополнительно: pip install -r requirements-dev.txt
uvicorn app.main:app --host 127.0.0.1 --port 8765
```
Откройте http://127.0.0.1:8765

## Первый срез
- `POST /api/import/path` импортирует выбранную серверу папку.
- `GET /api/sources` список источников.
- `GET /api/events?start_ms=...&end_ms=...&source_id=...&q=...` поиск.
- `GET /api/events/{event_id}` детали события.
- `GET /api/health` проверка API.

## Важно
Это каркас MVP, а не production-ready система. В текущей версии:
- парсер распознаёт несколько распространённых timestamp-форматов;
- CSV использует эвристики выбора колонки времени;
- XML JTL, сложные многострочные stack trace, инкрементальная индексация и Request ID correlation пока не реализованы;
- импорт выполняется синхронно и предназначен для проверки вертикального среза, а не для многогигабайтного импорта;
- импорт путей ограничен корнем, указанным в `LOGSCOPE_ALLOWED_ROOT` (по умолчанию `./data/inbox`), чтобы API не мог читать произвольные файлы.
