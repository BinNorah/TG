"""Panel de ajustes por chat para módulos, idioma y canal de registros."""
import html
import logging

import telebot

from flamie.database.db import get_db
from flamie.utils.cache import get_chat_member_cached, get_bot_id_cached

logger = logging.getLogger(__name__)

MODULES = {
    "moderation": "Moderación",
    "rules": "Reglas",
    "utilities": "Utilidades",
    "fun": "Diversión",
    "quote": "Stickers de cita",
    "custom": "Comandos personalizados",
    "welcome": "Bienvenida y despedida",
    "captcha": "Captcha de ingreso",
    "antispam": "Antispam",
    "filters": "Filtros y notas",
    "join_requests": "Solicitudes de ingreso",
    "antiraid": "Modo antiraid",
}
NEW_MODULES_DISABLED_BY_DEFAULT = {
    "welcome", "captcha", "antispam", "filters", "join_requests", "antiraid",
}


def get_chat_setting(chat_id, setting, default=None):
    con = get_db()
    try:
        row = con.execute(
            "SELECT value FROM chat_settings WHERE chat_id=? AND setting=?",
            (chat_id, setting),
        ).fetchone()
        return row[0] if row else default
    finally:
        con.close()


def set_chat_setting(chat_id, setting, value):
    con = get_db()
    try:
        if value is None:
            con.execute("DELETE FROM chat_settings WHERE chat_id=? AND setting=?", (chat_id, setting))
        else:
            con.execute(
                "INSERT INTO chat_settings(chat_id,setting,value) VALUES(?,?,?) "
                "ON CONFLICT(chat_id,setting) DO UPDATE SET value=excluded.value",
                (chat_id, setting, str(value)),
            )
        con.commit()
    finally:
        con.close()


def module_enabled(chat_id, module):
    default = "0" if module in NEW_MODULES_DISABLED_BY_DEFAULT else "1"
    return get_chat_setting(chat_id, f"module:{module}", default) == "1"


def module_enabled_for_message(message, module):
    return message.chat.type == "private" or module_enabled(message.chat.id, module)


def chat_language(chat_id):
    return get_chat_setting(chat_id, "language", "es")


def _is_admin(bot, owner_id, chat_id, chat_type, user_id):
    if user_id == owner_id:
        return True
    if chat_type == "private":
        return False
    try:
        return bot.get_chat_member(chat_id, user_id).status in ("administrator", "creator")
    except Exception:
        logger.exception("No se pudo comprobar la administración del usuario")
        return False


def _message_is_admin(bot, owner_id, message):
    sender_chat = getattr(message, "sender_chat", None)
    if sender_chat and sender_chat.id == message.chat.id:
        return True  # comando enviado como el grupo por un administrador anónimo
    user = getattr(message, "from_user", None)
    return bool(user and _is_admin(bot, owner_id, message.chat.id, message.chat.type, user.id))


def _keyboard(chat_id):
    markup = telebot.types.InlineKeyboardMarkup()
    english_titles = {
        "moderation": "Moderation", "rules": "Rules", "utilities": "Utilities",
        "fun": "Fun", "quote": "Quote stickers", "custom": "Custom commands",
        "welcome": "Welcome and farewell", "captcha": "Join captcha", "antispam": "Antispam",
        "filters": "Filters and notes", "join_requests": "Join requests", "antiraid": "Raid mode",
    }
    en = chat_language(chat_id) == "en"
    for key, title in MODULES.items():
        enabled = module_enabled(chat_id, key)
        if en:
            title = english_titles[key]
        label = f"{'✅' if enabled else '❌'} {title}"
        markup.row(telebot.types.InlineKeyboardButton(label, callback_data=f"settings:toggle:{key}"))
    language = "en" if en else "es"
    markup.row(
        telebot.types.InlineKeyboardButton(
            f"Language: {'Español' if language == 'es' else 'English'} · change" if en else f"Idioma: {'Español' if language == 'es' else 'English'} · cambiar",
            callback_data="settings:language",
        )
    )
    markup.row(telebot.types.InlineKeyboardButton("Moderation log channel" if en else "Canal de logs", callback_data="settings:logs"))
    return markup


def _panel_text(chat_id):
    language = chat_language(chat_id)
    log_channel = get_chat_setting(chat_id, "moderation_log_channel")
    if language == "en":
        log_value = html.escape(str(log_channel)) if log_channel else "not configured"
        return (
            "<b>Flamie settings</b>\nToggle group modules below. Changes are saved for this chat.\n"
            f"Language: <b>English</b> · moderation log channel: <code>{log_value}</code>"
        )
    log_value = html.escape(str(log_channel)) if log_channel else "sin configurar"
    return (
        "<b>Ajustes de Flamie</b>\nActiva o desactiva los módulos del grupo. Los cambios se guardan por chat.\n"
        f"Idioma: <b>Español</b> · canal de logs de moderación: <code>{log_value}</code>"
    )


def register_settings(bot, owner_id):
    @bot.message_handler(commands=["settings"])
    def cmd_settings(message):
        try:
            if message.chat.type == "private":
                bot.reply_to(message, "Usa /settings dentro del grupo para configurar Flamie.")
                return
            if not _message_is_admin(bot, owner_id, message):
                bot.reply_to(message, "Solo los administradores del grupo pueden cambiar estos ajustes.")
                return

            argument = (message.text or "").partition(" ")[2].strip()
            if argument.lower().startswith("logs"):
                channel_value = argument[4:].strip()
                if not channel_value:
                    bot.reply_to(
                        message,
                        "Indica el ID del canal: <code>/settings logs -1001234567890</code>\n"
                        "Para quitarlo, usa <code>/settings logs off</code>.",
                        parse_mode="HTML",
                    )
                    return
                if channel_value.lower() == "off":
                    set_chat_setting(message.chat.id, "moderation_log_channel", None)
                    bot.reply_to(message, "Canal de logs desactivado.")
                    return
                try:
                    channel_id = int(channel_value)
                    target = bot.get_chat(channel_id)
                    bot_member = get_chat_member_cached(bot, channel_id, get_bot_id_cached(bot))
                    if target.type != "channel" or not getattr(bot_member, "can_post_messages", False):
                        raise ValueError("channel unavailable")
                except Exception:
                    logger.exception("No se pudo validar el canal de logs indicado")
                    bot.reply_to(
                        message,
                        "No pude validar ese canal. Añade el bot como administrador con permiso para publicar y vuelve a intentarlo.",
                    )
                    return
                set_chat_setting(message.chat.id, "moderation_log_channel", channel_id)
                bot.reply_to(message, f"Canal de logs guardado: <code>{channel_id}</code>.", parse_mode="HTML")
                return

            bot.send_message(
                message.chat.id,
                _panel_text(message.chat.id),
                parse_mode="HTML",
                reply_markup=_keyboard(message.chat.id),
                reply_to_message_id=message.message_id,
            )
        except Exception:
            logger.exception("Falló el comando /settings")
            bot.reply_to(message, "No pude abrir los ajustes. Inténtalo de nuevo más tarde.")

    @bot.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith("settings:")))
    def callback_settings(call):
        try:
            if not call.message or call.message.chat.type == "private":
                bot.answer_callback_query(call.id, "Estos ajustes solo están disponibles en grupos.")
                return
            chat_id = call.message.chat.id
            if not _is_admin(bot, owner_id, chat_id, call.message.chat.type, call.from_user.id):
                bot.answer_callback_query(call.id, "Solo los administradores pueden cambiar estos ajustes.", show_alert=True)
                return

            parts = call.data.split(":")
            action = parts[1] if len(parts) > 1 else ""
            if action == "toggle" and len(parts) == 3 and parts[2] in MODULES:
                module = parts[2]
                set_chat_setting(chat_id, f"module:{module}", "0" if module_enabled(chat_id, module) else "1")
            elif action == "language":
                current = chat_language(chat_id)
                next_language = "en" if current == "es" else "es"
                set_chat_setting(chat_id, "language", next_language)
            elif action == "logs":
                bot.answer_callback_query(
                    call.id,
                    "Usa /settings logs ID_DEL_CANAL (o /settings logs off para quitarlo).",
                    show_alert=True,
                )
                return
            else:
                bot.answer_callback_query(call.id, "Opción no válida.")
                return

            bot.edit_message_text(
                _panel_text(chat_id),
                chat_id,
                call.message.message_id,
                parse_mode="HTML",
                reply_markup=_keyboard(chat_id),
            )
            bot.answer_callback_query(call.id, "Ajuste guardado.")
        except Exception:
            logger.exception("Falló un callback de /settings")
            try:
                bot.answer_callback_query(call.id, "No pude guardar el ajuste.")
            except Exception:
                logger.exception("No se pudo responder el callback de /settings")
