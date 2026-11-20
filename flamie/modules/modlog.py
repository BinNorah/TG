"""Registro central de acciones de moderación en SQLite y en el canal."""
import html
import logging

from flamie.database.db import record_mod_action
from flamie.modules.settings import get_chat_setting

logger = logging.getLogger(__name__)


def log_mod_action(bot, chat_id, actor_id, target_id, action, reason="", duration_seconds=None):
    record_mod_action(chat_id, actor_id, target_id, action, reason, duration_seconds)
    channel_id = get_chat_setting(chat_id, "moderation_log_channel")
    if not channel_id:
        return
    body = (
        "<b>Acción de moderación</b>\n"
        f"Grupo: <code>{chat_id}</code>\nAcción: <b>{html.escape(action)}</b>\n"
        f"Usuario: <code>{target_id}</code>\nModerador: <code>{actor_id}</code>"
    )
    if reason:
        body += f"\nMotivo: {html.escape(str(reason)[:1200])}"
    if duration_seconds is not None:
        body += f"\nDuración: <code>{int(duration_seconds)}s</code>"
    try:
        bot.send_message(int(channel_id), body, parse_mode="HTML")
    except Exception:
        logger.exception("No se pudo enviar el registro de moderación al canal %s", channel_id)
