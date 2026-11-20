"""Arranque del bot y registro de sus módulos actuales."""
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from flamie.modules.legacy_handlers import run_bot

logger = logging.getLogger(__name__)


def main():
    try:
        run_bot()
    except KeyboardInterrupt:
        logger.info("Bot detenido por señal de teclado")
    except Exception:
        logger.exception("Falló el proceso principal del bot")
        raise


if __name__ == "__main__":
    main()
