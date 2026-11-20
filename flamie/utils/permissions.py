"""Decorador reutilizable de permisos para handlers de TeleBot."""
import functools
import logging

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
