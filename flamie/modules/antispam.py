"""Middleware de antispam, bloqueo de contenido y respuestas guardadas."""
from collections import defaultdict, deque
import html
import logging
import re
import threading
import time

from telebot.handler_backends import BaseMiddleware, CancelUpdate

from flamie.core.config import MI_ID
from flamie.database.db import get_db
from flamie.modules.modlog import log_mod_action
from flamie.modules.settings import get_chat_setting, module_enabled
from flamie.utils.cache import get_chat_member_cached

logger = logging.getLogger(__name__)

LINK_RE = re.compile(r"(?:https?://|www\.|t\.me/|(?:[a-z0-9-]+\.)+(?:com|net|org|io|gg|co|me|app|xyz)\b)", re.IGNORECASE)
BLOCKABLE_TYPES = {
    "photo", "video", "animation", "document", "audio", "voice", "sticker",
    "video_note", "contact", "location", "poll",
}


class GroupAutomationMiddleware(BaseMiddleware):
    def __init__(self, bot):
        self.bot = bot
        self.update_types = ["message"]
        self._lock = threading.Lock()
        self._recent = defaultdict(deque)
        self._last_notice = {}

    def _is_admin(self, message):
        try:
            return get_chat_member_cached(
                self.bot, message.chat.id, message.from_user.id
            ).status in ("administrator", "creator")
        except Exception:
            logger.exception("No se pudo comprobar el rol al aplicar filtros del grupo")
            return False

    def _delete_and_record(self, message, action, reason):
        try:
            self.bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            logger.exception("No se pudo borrar mensaje por %s", action)
        try:
            log_mod_action(
                self.bot,
                message.chat.id,
                0,
                message.from_user.id,
                action,
                reason,
            )
        except Exception:
            logger.exception("No se pudo guardar acción automática %s", action)

    def _notice(self, message, text, key):
        now = time.monotonic()
        notice_key = (message.chat.id, message.from_user.id, key)
        if now - self._last_notice.get(notice_key, 0) < 30:
            return
        self._last_notice[notice_key] = now
        if len(self._last_notice) > 4096:
            self._last_notice = {key: stamp for key, stamp in self._last_notice.items() if now - stamp < 300}
        try:
            self.bot.send_message(message.chat.id, text)
        except Exception:
            logger.exception("No se pudo avisar de una regla de antispam")

    def _captcha_pending(self, chat_id, user_id):
        con = get_db()
        try:
            return con.execute(
                "SELECT 1 FROM captcha_pending WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            ).fetchone() is not None
        finally:
            con.close()

    def _has_link(self, message):
        text = message.text or message.caption or ""
        entities = (getattr(message, "entities", None) or []) + (getattr(message, "caption_entities", None) or [])
        return bool(LINK_RE.search(text)) or any(
            getattr(entity, "type", "") in {"url", "text_link"} for entity in entities
        )

    def _moderate_flood(self, message):
        chat_id, user_id = message.chat.id, message.from_user.id
        try:
            limit = max(2, min(30, int(get_chat_setting(chat_id, "flood_limit", "5"))))
            window = max(3, min(60, int(get_chat_setting(chat_id, "flood_window", "8"))))
        except (TypeError, ValueError):
            limit, window = 5, 8
        now = time.monotonic()
        key = (chat_id, user_id)
        with self._lock:
            if len(self._recent) > 4096:
                self._recent = defaultdict(deque, {key: value for key, value in self._recent.items() if value})
            events = self._recent[key]
            while events and now - events[0] > window:
                events.popleft()
            events.append(now)
            flooding = len(events) > limit
        if flooding:
            self._delete_and_record(message, "antiflood", f"Más de {limit} mensajes en {window}s")
            self._notice(message, "Evita enviar mensajes tan rápido; se eliminó el mensaje excedente.", "flood")
            return True
        return False

    def _match_filter(self, message):
        text = (message.text or "").strip()
        if not text or text.startswith("/"):
            return False
        con = get_db()
        try:
            rows = con.execute(
                "SELECT trigger,response FROM chat_filters WHERE chat_id=? ORDER BY length(trigger) DESC",
                (message.chat.id,),
            ).fetchall()
        finally:
            con.close()
        folded = text.casefold()
        for trigger, response in rows:
            if trigger.casefold() in folded:
                try:
                    self.bot.send_message(message.chat.id, html.escape(response), parse_mode="HTML")
                except Exception:
                    logger.exception("No se pudo responder al filtro del grupo")
                return True
        return False

    def _match_note(self, message):
        match = re.fullmatch(r"\s*#([A-Za-z0-9_]{1,40})\s*", message.text or "")
        if not match:
            return False
        con = get_db()
        try:
            row = con.execute(
                "SELECT content FROM chat_notes WHERE chat_id=? AND lower(name)=lower(?) LIMIT 1",
                (message.chat.id, match.group(1)),
            ).fetchone()
        finally:
            con.close()
        if row:
            try:
                self.bot.send_message(message.chat.id, html.escape(row[0]), parse_mode="HTML")
            except Exception:
                logger.exception("No se pudo enviar la nota del grupo")
            return True
        return False

    def _check_afk(self, message):
        if not module_enabled(message.chat.id, "utilities"):
            return
        con = get_db()
        try:
            own = con.execute(
                "SELECT reason FROM chat_afk WHERE chat_id=? AND user_id=?",
                (message.chat.id, message.from_user.id),
            ).fetchone()
        finally:
            con.close()
        text = message.text or message.caption or ""
        if own and not text.lstrip().lower().startswith("/afk"):
            con = get_db()
            try:
                con.execute("DELETE FROM chat_afk WHERE chat_id=? AND user_id=?", (message.chat.id, message.from_user.id))
                con.commit()
            finally:
                con.close()
            self._notice(message, "Ya no estás AFK; tu estado se quitó al volver a escribir.", "afk_back")

        targets = set()
        replied = getattr(message, "reply_to_message", None)
        if replied and replied.from_user:
            targets.add(replied.from_user.id)
        for entity in getattr(message, "entities", None) or []:
            if getattr(entity, "type", "") == "text_mention" and getattr(entity, "user", None):
                targets.add(entity.user.id)
            elif getattr(entity, "type", "") == "mention":
                mention = text[entity.offset:entity.offset + entity.length].lstrip("@").casefold()
                con = get_db()
                try:
                    rows = con.execute(
                        "SELECT user_id FROM chat_users WHERE chat_id=? AND lower(username)=? LIMIT 1",
                        (message.chat.id, mention),
                    ).fetchone()
                    if rows:
                        targets.add(rows[0])
                finally:
                    con.close()
        for target_id in targets:
            con = get_db()
            try:
                row = con.execute(
                    "SELECT reason FROM chat_afk WHERE chat_id=? AND user_id=?",
                    (message.chat.id, target_id),
                ).fetchone()
            finally:
                con.close()
            if row:
                reason = f"\nMotivo: {html.escape(row[0])}" if row[0] else ""
                self._notice(message, f"Ese usuario está AFK.{reason}", f"afk_{target_id}")
                break

    @staticmethod
    def _users_to_remember(message):
        """Usuarios visibles en el mensaje: autor, autor del mensaje respondido y nuevos miembros."""
        candidates = [getattr(message, "from_user", None)]
        replied = getattr(message, "reply_to_message", None)
        if replied is not None:
            candidates.append(getattr(replied, "from_user", None))
        candidates.extend(getattr(message, "new_chat_members", None) or [])
        seen = {}
        for user in candidates:
            if user is not None and not user.is_bot:
                seen[user.id] = user
        return list(seen.values())

    def _record_activity(self, message):
        if not message.from_user or message.from_user.is_bot:
            return
        text = message.text or message.caption or ""
        now = int(time.time())
        con = get_db()
        try:
            con.execute(
                "INSERT INTO group_activity(chat_id,user_id,username,first_name,count) "
                "VALUES(?,?,?,?,1) "
                "ON CONFLICT(chat_id,user_id) DO UPDATE SET "
                "username=excluded.username,first_name=excluded.first_name,count=group_activity.count+1",
                (message.chat.id, message.from_user.id, message.from_user.username,
                 message.from_user.first_name or ""),
            )
            if text.strip() and not text.lstrip().startswith("/"):
                con.execute(
                    "UPDATE group_activity SET xp=xp+1,last_xp=? "
                    "WHERE chat_id=? AND user_id=? AND last_xp<=?",
                    (now, message.chat.id, message.from_user.id, now - 60),
                )
            con.commit()
        finally:
            con.close()

    def pre_process(self, message, data):
        chat = getattr(message, "chat", None)
        user = getattr(message, "from_user", None)
        if not chat or not user or user.is_bot:
            return
        try:
            maintenance = get_chat_setting(0, "maintenance", "0") == "1"
            if maintenance and user.id != MI_ID:
                return CancelUpdate()
            if chat.type not in ("group", "supergroup"):
                return
            if getattr(message, "is_automatic_forward", False):
                return  # publicaciones del canal vinculado: no se les aplican reglas
            con = get_db()
            try:
                con.execute(
                    "INSERT INTO chat_registry(chat_id,title,chat_type) VALUES(?,?,?) "
                    "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,chat_type=excluded.chat_type,last_seen=CURRENT_TIMESTAMP",
                    (chat.id, chat.title or "", chat.type),
                )
                known = [
                    (chat.id, member.id, member.username, member.first_name or "")
                    for member in self._users_to_remember(message)
                ]
                if known:
                    con.executemany(
                        "INSERT INTO chat_users(chat_id,user_id,username,first_name,updated_at) "
                        "VALUES(?,?,?,?,CURRENT_TIMESTAMP) "
                        "ON CONFLICT(chat_id,user_id) DO UPDATE SET username=excluded.username,"
                        "first_name=excluded.first_name,updated_at=CURRENT_TIMESTAMP",
                        known,
                    )
                new_members = getattr(message, "new_chat_members", None) or []
                globally_banned = []
                if new_members:
                    for joined in new_members:
                        ban_row = con.execute("SELECT reason FROM global_bans WHERE user_id=?", (joined.id,)).fetchone()
                        if ban_row:
                            globally_banned.append(joined.id)
                banned = con.execute("SELECT reason FROM global_bans WHERE user_id=?", (user.id,)).fetchone()
                con.commit()
            finally:
                con.close()
            if globally_banned:
                for joined_id in globally_banned:
                    try:
                        self.bot.ban_chat_member(chat.id, joined_id)
                    except Exception:
                        logger.exception("No se pudo aplicar gban al ingreso en el chat %s", chat.id)
                try:
                    self.bot.delete_message(chat.id, message.message_id)
                except Exception:
                    logger.exception("No se pudo retirar el aviso de ingreso de un usuario con gban")
                return CancelUpdate()
            if banned and user.id != MI_ID:
                try:
                    self.bot.ban_chat_member(chat.id, user.id)
                except Exception:
                    logger.exception("No se pudo aplicar el gban en el chat %s", chat.id)
                return CancelUpdate()
            if self._captcha_pending(chat.id, user.id):
                if message.content_type != "new_chat_members":
                    try:
                        self.bot.delete_message(chat.id, message.message_id)
                    except Exception:
                        logger.exception("No se pudo borrar mensaje de usuario pendiente de captcha")
                    return CancelUpdate()

            is_admin = self._is_admin(message)
            if module_enabled(chat.id, "antispam") and not is_admin:
                if self._moderate_flood(message):
                    return CancelUpdate()
                blocked_type = get_chat_setting(chat.id, f"block_content:{message.content_type}", "0") == "1"
                if message.content_type in BLOCKABLE_TYPES and blocked_type:
                    self._delete_and_record(message, "blocked_content", message.content_type)
                    return CancelUpdate()
                if get_chat_setting(chat.id, "anti_links", "0") == "1" and self._has_link(message):
                    self._delete_and_record(message, "anti_link", "Se eliminó un enlace no permitido")
                    self._notice(message, "No se permiten enlaces en este grupo.", "link")
                    return CancelUpdate()
                text = (message.text or message.caption or "").casefold()
                con = get_db()
                try:
                    words = [row[0] for row in con.execute(
                        "SELECT word FROM blacklist_words WHERE chat_id=?", (chat.id,)
                    ).fetchall()]
                finally:
                    con.close()
                matched = next((word for word in words if word.casefold() in text), None)
                if matched:
                    self._delete_and_record(message, "blacklist_word", matched)
                    self._notice(message, "Tu mensaje contiene una palabra bloqueada.", "word")
                    return CancelUpdate()

            if module_enabled(chat.id, "fun"):
                self._record_activity(message)
            if module_enabled(chat.id, "filters") and message.content_type == "text":
                if self._match_note(message):
                    return CancelUpdate()
                if self._match_filter(message):
                    return CancelUpdate()

            self._check_afk(message)
        except Exception:
            logger.exception("Falló el middleware de antispam del grupo")

    def post_process(self, message, data, exception):
        if exception:
            logger.error("Error posterior al middleware de grupo: %s", exception)
