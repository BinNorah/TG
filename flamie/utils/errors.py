"""Registro central de errores de callbacks de TeleBot."""
import logging
import telebot

logger = logging.getLogger(__name__)


class GlobalErrorHandler(telebot.ExceptionHandler):
    def handle(self, exception):
        logger.error(
            "Error no controlado en un handler de Telegram",
            exc_info=(type(exception), exception, exception.__traceback__),
        )
        return True
