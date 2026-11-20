"""Bienvenidas, despedidas, captcha, antiraid y solicitudes de ingreso."""
from collections import defaultdict, deque
import html
import logging
import threading
import time
from urllib.parse import urlparse

import telebot

from flamie.database.db import get_db
from flamie.modules.modlog import log_mod_action
from flamie.modules.settings import _is_admin, get_chat_setting, module_enabled, set_chat_setting
from flamie.utils.cache import get_bot_id_cached, get_chat_member_cached

logger = logging.getLogger(__name__)
_join_times = defaultdict(deque)
_sweeper_lock = threading.Lock()
_sweeper_started = False
_stop_sweeper = threading.Event()


def _permissions(allowed):
    return telebot.types.ChatPermissions(
        can_send_messages=allowed,
        can_send_media_messages=allowed,
        can_send_audios=allowed,
        can_send_documents=allowed,
        can_send_photos=allowed,
        can_send_videos=allowed,
        can_send_video_notes=allowed,
        can_send_voice_notes=allowed,
        can_send_polls=allowed,
        can_send_other_messages=allowed,
        can_add_web_page_previews=allowed,
    )


def _format_template(template, user, chat):
    raw = template or "¡Bienvenido, {nombre}, a {grupo}!"
    safe = html.escape(raw)
    safe = safe.replace("{nombre}", html.escape(user.first_name or "Usuario"))
    safe = safe.replace("{grupo}", html.escape(chat.title or "el grupo"))
    safe = safe.replace("{id}", str(user.id))
    return safe


def _join_keyboard(chat_id, user_id, title_key):
    button_text = get_chat_setting(chat_id, f"{title_key}_button_text", "")
    button_url = get_chat_setting(chat_id, f"{title_key}_button_url", "")
    if not button_text or not button_url:
        return None
    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(telebot.types.InlineKeyboardButton(button_text, url=button_url))
    return markup


def _send_template(bot, chat, user, setting, button_key):
    try:
        template = get_chat_setting(chat.id, setting, "")
        text = _format_template(template, user, chat)
        bot.send_message(chat.id, text, parse_mode="HTML", reply_markup=_join_keyboard(chat.id, user.id, button_key))
    except Exception:
        logger.exception("No se pudo enviar %s en el chat %s", setting, chat.id)


def _is_raid_active(chat_id):
    try:
        return int(get_chat_setting(chat_id, "raid_until", "0")) > int(time.time())
    except (TypeError, ValueError):
        return False


def _record_join_burst(chat_id, count):
    now = time.monotonic()
    events = _join_times[chat_id]
    while events and now - events[0] > 30:
        events.popleft()
    for _ in range(count):
        events.append(now)
    if len(events) >= 5:
        until = int(time.time()) + 120
        set_chat_setting(chat_id, "raid_until", until)
        logger.warning("Antiraid activado automáticamente en chat_id=%s por 120 segundos", chat_id)


def _start_captcha(bot, chat, user):
    timeout = 60
    challenge = None
    restricted = False
    try:
        timeout = max(30, min(900, int(get_chat_setting(chat.id, "captcha_timeout", "60"))))
        bot.restrict_chat_member(chat.id, user.id, permissions=_permissions(False))
        restricted = True
        markup = telebot.types.InlineKeyboardMarkup()
        markup.row(telebot.types.InlineKeyboardButton(
            "✅ Verificar que soy una persona",
            callback_data=f"captcha:{chat.id}:{user.id}",
        ))
        challenge = bot.send_message(
            chat.id,
            f"{html.escape(user.first_name or 'Usuario')}, pulsa el botón en {timeout} segundos para poder escribir.",
            reply_markup=markup,
        )
        deadline = int(time.time()) + timeout
        con = get_db()
        try:
            con.execute(
                "INSERT INTO captcha_pending(chat_id,user_id,message_id,deadline) VALUES(?,?,?,?) "
                "ON CONFLICT(chat_id,user_id) DO UPDATE SET message_id=excluded.message_id, "
                "deadline=excluded.deadline, created_at=CURRENT_TIMESTAMP",
                (chat.id, user.id, challenge.message_id, deadline),
            )
            con.commit()
        finally:
            con.close()
    except Exception:
        logger.exception("No se pudo iniciar captcha para user_id=%s en chat_id=%s", user.id, chat.id)
        if challenge:
            try:
                bot.delete_message(chat.id, challenge.message_id)
            except Exception:
                logger.debug("No se pudo limpiar el captcha incompleto", exc_info=True)
        if restricted:
            try:
                bot.restrict_chat_member(chat.id, user.id, permissions=_permissions(True))
            except Exception:
                logger.exception("No se pudo retirar una restricción de captcha incompleta")


def _expire_captchas(bot):
    con = get_db()
    try:
        rows = con.execute(
            "SELECT chat_id,user_id,message_id FROM captcha_pending WHERE deadline<=? LIMIT 50",
            (int(time.time()),),
        ).fetchall()
    finally:
        con.close()
    for chat_id, user_id, message_id in rows:
        try:
            bot.ban_chat_member(chat_id, user_id)
            bot.unban_chat_member(chat_id, user_id)
            try:
                bot.delete_message(chat_id, message_id)
            except Exception:
                logger.debug("No se pudo borrar el captcha vencido", exc_info=True)
            log_mod_action(bot, chat_id, 0, user_id, "captcha_timeout", "No completó el captcha")
            con = get_db()
            try:
                con.execute("DELETE FROM captcha_pending WHERE chat_id=? AND user_id=?", (chat_id, user_id))
                con.commit()
            finally:
                con.close()
        except Exception:
            logger.exception("No se pudo expulsar a user_id=%s al vencer captcha", user_id)


def _captcha_sweeper(bot):
    while not _stop_sweeper.wait(5):
        try:
            _expire_captchas(bot)
        except Exception:
            logger.exception("Falló la revisión de captchas vencidos")


def start_captcha_sweeper(bot):
    global _sweeper_started
    with _sweeper_lock:
        if _sweeper_started:
            return
        _sweeper_started = True
        threading.Thread(target=_captcha_sweeper, args=(bot,), name="captcha-sweeper", daemon=True).start()


def register_welcome(bot, owner_id):
    @bot.message_handler(commands=["setwelcome"])
    def cmd_setwelcome(message):
        try:
            if message.chat.type == "private" or not _is_admin(bot, owner_id, message.chat.id, message.chat.type, message.from_user.id):
                bot.reply_to(message, "Solo administradores pueden configurar la bienvenida en un grupo.")
                return
            if not module_enabled(message.chat.id, "welcome"):
                bot.reply_to(message, "Activa primero Bienvenida y despedida en /settings.")
                return
            parts = (message.text or "").partition(" ")[2].split("|")
            template = parts[0].strip()
            button_text = parts[1].strip() if len(parts) > 1 else ""
            button_url = parts[2].strip() if len(parts) > 2 else ""
            if not template or len(template) > 1500 or len(parts) not in (1, 3):
                bot.reply_to(message, "Uso: /setwelcome texto | botón | https://enlace. Variables: {nombre}, {grupo}, {id}.")
                return
            if button_text:
                parsed = urlparse(button_url)
                if not 1 <= len(button_text) <= 64 or parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    bot.reply_to(message, "El botón requiere un texto y una URL http(s) válida.")
                    return
            set_chat_setting(message.chat.id, "welcome_text", template)
            set_chat_setting(message.chat.id, "welcome_button_text", button_text)
            set_chat_setting(message.chat.id, "welcome_button_url", button_url)
            bot.reply_to(message, "Bienvenida guardada.")
        except Exception:
            logger.exception("Falló /setwelcome")
            bot.reply_to(message, "No pude guardar la bienvenida.")

    @bot.message_handler(commands=["setgoodbye"])
    def cmd_setgoodbye(message):
        try:
            if message.chat.type == "private" or not _is_admin(bot, owner_id, message.chat.id, message.chat.type, message.from_user.id):
                bot.reply_to(message, "Solo administradores pueden configurar la despedida en un grupo.")
                return
            if not module_enabled(message.chat.id, "welcome"):
                bot.reply_to(message, "Activa primero Bienvenida y despedida en /settings.")
                return
            parts = (message.text or "").partition(" ")[2].split("|")
            template = parts[0].strip()
            button_text = parts[1].strip() if len(parts) > 1 else ""
            button_url = parts[2].strip() if len(parts) > 2 else ""
            if not template or len(template) > 1500 or len(parts) not in (1, 3):
                bot.reply_to(message, "Uso: /setgoodbye texto | botón | https://enlace. Variables: {nombre}, {grupo}, {id}.")
                return
            if button_text:
                parsed = urlparse(button_url)
                if not 1 <= len(button_text) <= 64 or parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    bot.reply_to(message, "El botón requiere un texto y una URL http(s) válida.")
                    return
            set_chat_setting(message.chat.id, "goodbye_text", template)
            set_chat_setting(message.chat.id, "goodbye_button_text", button_text)
            set_chat_setting(message.chat.id, "goodbye_button_url", button_url)
            bot.reply_to(message, "Despedida guardada.")
        except Exception:
            logger.exception("Falló /setgoodbye")
            bot.reply_to(message, "No pude guardar la despedida.")

    @bot.message_handler(commands=["captchaconfig"])
    def cmd_captchaconfig(message):
        try:
            if message.chat.type == "private" or not _is_admin(bot, owner_id, message.chat.id, message.chat.type, message.from_user.id):
                bot.reply_to(message, "Solo administradores pueden configurar el captcha.")
                return
            if not module_enabled(message.chat.id, "captcha"):
                bot.reply_to(message, "Activa primero Captcha de ingreso en /settings.")
                return
            parts = (message.text or "").split()
            if len(parts) != 2 or not parts[1].isdigit() or not 30 <= int(parts[1]) <= 900:
                bot.reply_to(message, "Uso: /captchaconfig <segundos 30-900>. El valor inicial es 60.")
                return
            set_chat_setting(message.chat.id, "captcha_timeout", int(parts[1]))
            bot.reply_to(message, f"Tiempo de captcha configurado a {parts[1]} segundos.")
        except Exception:
            logger.exception("Falló /captchaconfig")
            bot.reply_to(message, "No pude guardar el tiempo del captcha.")

    @bot.message_handler(commands=["antiraid"])
    def cmd_antiraid(message):
        try:
            if message.chat.type == "private" or not _is_admin(bot, owner_id, message.chat.id, message.chat.type, message.from_user.id):
                bot.reply_to(message, "Solo administradores pueden cambiar el modo antiraid.")
                return
            value = (message.text or "").partition(" ")[2].strip().lower()
            if value not in {"on", "off", "estado"}:
                bot.reply_to(message, "Uso: /antiraid on|off|estado. Con 5 ingresos en 30 s, exige captcha por 2 min.")
                return
            if value == "estado":
                active = module_enabled(message.chat.id, "antiraid")
                bot.reply_to(message, "Antiraid activado." if active else "Antiraid desactivado.")
                return
            enabled = value == "on"
            set_chat_setting(message.chat.id, "module:antiraid", "1" if enabled else "0")
            if not enabled:
                set_chat_setting(message.chat.id, "raid_until", 0)
            bot.reply_to(message, "Antiraid activado." if enabled else "Antiraid desactivado.")
        except Exception:
            logger.exception("Falló /antiraid")
            bot.reply_to(message, "No pude cambiar el modo antiraid.")

    @bot.message_handler(content_types=["new_chat_members"])
    def on_new_members(message):
        try:
            if message.chat.type not in ("group", "supergroup"):
                return
            users = [user for user in (message.new_chat_members or []) if not user.is_bot]
            if not users:
                return
            raid_enabled = module_enabled(message.chat.id, "antiraid")
            if raid_enabled:
                _record_join_burst(message.chat.id, len(users))
            for user in users:
                if module_enabled(message.chat.id, "welcome"):
                    _send_template(bot, message.chat, user, "welcome_text", "welcome")
                if module_enabled(message.chat.id, "captcha") or (raid_enabled and _is_raid_active(message.chat.id)):
                    _start_captcha(bot, message.chat, user)
        except Exception:
            logger.exception("Falló la bienvenida o validación de nuevos miembros")

    @bot.message_handler(content_types=["left_chat_member"])
    def on_left_member(message):
        try:
            user = message.left_chat_member
            if user and not user.is_bot and module_enabled(message.chat.id, "welcome"):
                _send_template(bot, message.chat, user, "goodbye_text", "goodbye")
            if user:
                con = get_db()
                try:
                    con.execute("DELETE FROM captcha_pending WHERE chat_id=? AND user_id=?", (message.chat.id, user.id))
                    con.commit()
                finally:
                    con.close()
        except Exception:
            logger.exception("Falló la despedida de un miembro")

    @bot.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith("captcha:")))
    def callback_captcha(call):
        try:
            _, raw_chat_id, raw_user_id = call.data.split(":", 2)
            chat_id, user_id = int(raw_chat_id), int(raw_user_id)
            if call.from_user.id != user_id:
                bot.answer_callback_query(call.id, "Este botón pertenece a otra persona.", show_alert=True)
                return
            con = get_db()
            try:
                row = con.execute(
                    "SELECT deadline FROM captcha_pending WHERE chat_id=? AND user_id=?",
                    (chat_id, user_id),
                ).fetchone()
            finally:
                con.close()
            if not row or int(row[0]) <= int(time.time()):
                bot.answer_callback_query(call.id, "El tiempo de verificación terminó.", show_alert=True)
                return
            bot.restrict_chat_member(chat_id, user_id, permissions=_permissions(True))
            con = get_db()
            try:
                con.execute("DELETE FROM captcha_pending WHERE chat_id=? AND user_id=?", (chat_id, user_id))
                con.commit()
            finally:
                con.close()
            log_mod_action(bot, chat_id, 0, user_id, "captcha_passed", "Verificación completada")
            try:
                bot.delete_message(chat_id, call.message.message_id)
            except Exception:
                logger.debug("No se pudo borrar el captcha aprobado", exc_info=True)
            bot.answer_callback_query(call.id, "Verificación completada. Ya puedes escribir.")
        except Exception:
            logger.exception("Falló la verificación de captcha")
            try:
                bot.answer_callback_query(call.id, "No pude completar la verificación.", show_alert=True)
            except Exception:
                logger.exception("No se pudo contestar el callback de captcha")

    @bot.chat_join_request_handler()
    def on_join_request(request):
        try:
            if not module_enabled(request.chat.id, "join_requests"):
                return
            user = request.from_user
            name = html.escape(user.first_name or "Usuario")
            username = f" (@{html.escape(user.username)})" if user.username else ""
            markup = telebot.types.InlineKeyboardMarkup()
            markup.row(
                telebot.types.InlineKeyboardButton("✅ Aprobar", callback_data=f"join:approve:{request.chat.id}:{user.id}"),
                telebot.types.InlineKeyboardButton("❌ Rechazar", callback_data=f"join:decline:{request.chat.id}:{user.id}"),
            )
            bot.send_message(
                request.chat.id,
                f"Solicitud de ingreso de <a href=\"tg://user?id={user.id}\">{name}</a>{username} · <code>{user.id}</code>",
                parse_mode="HTML",
                reply_markup=markup,
            )
        except Exception:
            logger.exception("Falló la notificación de solicitud de ingreso")

    @bot.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith("join:")))
    def callback_join_request(call):
        try:
            action, raw_chat_id, raw_user_id = call.data.split(":", 2)[1:]
            chat_id, user_id = int(raw_chat_id), int(raw_user_id)
            if not _is_admin(bot, owner_id, chat_id, "supergroup", call.from_user.id):
                bot.answer_callback_query(call.id, "Solo administradores pueden resolver solicitudes.", show_alert=True)
                return
            if action == "approve":
                bot.approve_chat_join_request(chat_id, user_id)
                status = "aprobada"
            elif action == "decline":
                bot.decline_chat_join_request(chat_id, user_id)
                status = "rechazada"
            else:
                bot.answer_callback_query(call.id, "Acción no válida.")
                return
            log_mod_action(bot, chat_id, call.from_user.id, user_id, f"join_request_{action}", "Solicitud de ingreso")
            bot.edit_message_text(
                f"Solicitud de ingreso de <code>{user_id}</code> {status} por <code>{call.from_user.id}</code>.",
                chat_id,
                call.message.message_id,
                parse_mode="HTML",
            )
            bot.answer_callback_query(call.id, f"Solicitud {status}.")
        except Exception:
            logger.exception("Falló la resolución de solicitud de ingreso")
            try:
                bot.answer_callback_query(call.id, "No pude resolver la solicitud.", show_alert=True)
            except Exception:
                logger.exception("No se pudo responder el callback de solicitud")
