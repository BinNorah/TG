"""Configuración central de Flamie."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]


def load_local_env():
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def require_env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Falta la variable de entorno: {name}")
    return value


load_local_env()
TOKEN = require_env("TELEGRAM_BOT_TOKEN")
MI_ID = int(require_env("TELEGRAM_OWNER_ID"))
_configured_data_dir = os.getenv("DATA_DIR")
if _configured_data_dir:
    DATA_DIR = Path(_configured_data_dir)
elif (BASE_DIR / "mensajes_flamie.db").exists():
    # Conserva la base antigua en instalaciones locales sin DATA_DIR.
    DATA_DIR = BASE_DIR
else:
    DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "mensajes_flamie.db"
BOT_PUBLIC_USERNAME = os.getenv("BOT_PUBLIC_USERNAME", "@Flamie_SpeeddrawBot").strip()
BOT_PUBLIC_LINK = os.getenv("BOT_PUBLIC_LINK", "https://t.me/Flamie_SpeeddrawBot").strip()
OWNER_DISPLAY_NAME = os.getenv("OWNER_DISPLAY_NAME", "MsTarantula").strip()
OWNER_ALIAS = os.getenv("OWNER_ALIAS", "@MsTarantula").strip()
