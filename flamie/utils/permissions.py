"""Decorador reutilizable de permisos para handlers de TeleBot."""
import functools
import logging

import telebot

from flamie.utils.cache import get_bot_id_cached

logger = logging.getLogger(__name__)


def user_has_role(bot, owner_id, chat_id, chat_type, user_id, role):
    if role == "user":
        return True
    if role == "owner":
        return user_id == owner_id
    if role == "admin":
        if user_id == owner_id:
            return True
        if chat_type == "private":
            return False
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    raise ValueError(f"Rol de permiso desconocido: {role}")


def require_permission(
    bot, owner_id, role="user", bot_permissions=(), denial_text=None,
    bot_denial_text="El bot no tiene los permisos necesarios en este chat.",
):
    """Comprueba el rol del usuario y los permisos exigidos al propio bot.

    Para admin y owner el bot consulta sus derechos en el chat antes de ejecutar
    el handler. ``bot_permissions`` acepta los nombres canónicos de Telegram,
    por ejemplo ``can_delete_messages`` o ``can_restrict_members``.
    """
    def decorate(handler):
        @functools.wraps(handler)
        def wrapped(message, *args, **kwargs):
            chat = getattr(message, "chat", None)
            user = getattr(message, "from_user", None)
            if chat is None:
                return None
            try:
                sender_chat = getattr(message, "sender_chat", None)
                anonymous_chat_admin = bool(
                    role == "admin" and sender_chat and sender_chat.id == chat.id
                )
                authorized = anonymous_chat_admin or bool(
                    user and user_has_role(bot, owner_id, chat.id, chat.type, user.id, role)
                )
                if not authorized:
                    if denial_text:
                        bot.reply_to(message, denial_text)
                    return None
                required_permissions = (
                    bot_permissions(message) if callable(bot_permissions) else bot_permissions
                )
                if required_permissions:
                    bot_member = bot.get_chat_member(chat.id, get_bot_id_cached(bot))
                    if not all(
                        bool(getattr(bot_member, permission, False))
                        for permission in required_permissions
                    ):
                        if bot_denial_text:
                            bot.reply_to(message, bot_denial_text)
                        return None
                return handler(message, *args, **kwargs)
            except Exception:
                logger.exception("Error comprobando permisos para %s", handler.__name__)
                if denial_text:
                    try:
                        bot.reply_to(message, denial_text)
                    except Exception:
                        logger.exception("No se pudo enviar la respuesta de permisos")
                return None
        return wrapped
    return decorate


_PERMISSION_FIELDS = (
    "can_send_audios", "can_send_documents", "can_send_photos", "can_send_videos",
    "can_send_video_notes", "can_send_voice_notes", "can_send_polls",
    "can_send_other_messages", "can_add_web_page_previews", "can_change_info",
    "can_invite_users", "can_pin_messages", "can_manage_topics",
)


def no_permissions():
    """Silencio total: el usuario no puede enviar ningún tipo de contenido."""
    return telebot.types.ChatPermissions(
        can_send_messages=False, can_send_audios=False, can_send_documents=False,
        can_send_photos=False, can_send_videos=False, can_send_video_notes=False,
        can_send_voice_notes=False, can_send_polls=False, can_send_other_messages=False,
        can_add_web_page_previews=False,
    )


def default_unrestrict_permissions(bot, chat_id):
    """Permisos para quitar una restricción sin saltarse las normas del grupo.

    Parte de los permisos por defecto del chat (los que ve cualquier miembro) y
    fuerza ``can_send_messages`` porque el objetivo es que el usuario pueda
    escribir de nuevo. Si Telegram no devuelve esos permisos, concede los de un
    miembro normal (texto, multimedia, enlaces) pero no los de gestión.
    """
    try:
        defaults = bot.get_chat(chat_id).permissions
    except Exception:
        logger.warning("No se pudieron leer los permisos por defecto de %s", chat_id, exc_info=True)
        defaults = None
    values = {"can_send_messages": True}
    if defaults is None:
        for field in _PERMISSION_FIELDS:
            values[field] = field not in {"can_change_info", "can_invite_users", "can_pin_messages", "can_manage_topics"}
    else:
        for field in _PERMISSION_FIELDS:
            values[field] = bool(getattr(defaults, field, False))
    return telebot.types.ChatPermissions(**values)
