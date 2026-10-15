import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent
DATA_DIR = Path(os.environ.get("LOGSCOPE_DATA_DIR", PROJECT_DIR / "data")).resolve()
DB_PATH = DATA_DIR / "logscope.sqlite3"
ALLOWED_ROOT = Path(os.environ.get("LOGSCOPE_ALLOWED_ROOT", DATA_DIR / "inbox")).resolve()
MAX_FILE_BYTES = int(os.environ.get("LOGSCOPE_MAX_FILE_BYTES", str(3 * 1024**3)))
SUPPORTED_EXTENSIONS = {".txt", ".log", ".csv", ".jtl"}
