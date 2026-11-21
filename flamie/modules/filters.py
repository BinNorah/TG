"""Filtros, notas y configuración de reglas anti-spam por grupo."""
import html
import logging
import re

from flamie.database.db import get_db
from flamie.modules.antispam import BLOCKABLE_TYPES
from flamie.modules.settings import (
    _message_is_admin, ensure_module_for_admin, module_enabled_for_message, set_chat_setting,
)

logger = logging.getLogger(__name__)


def _split_key_value(argument):
    """Acepta «clave | valor» (formato original) y también «clave valor».

    Devuelve (clave, valor); ambos vacíos si no hay datos suficientes.
    """
    argument = argument.strip()
    if "|" in argument:
        key, _, value = argument.partition("|")
    else:
        key, _, value = argument.partition(" ")
    return key.strip(), value.strip()


def register_filters(bot, owner_id):
    def admin_config(message, denial, module):
        """Comprueba chat de grupo + admin + activa el módulo. True si se puede seguir."""
        if message.chat.type == "private":
            bot.reply_to(message, "Este comando solo funciona dentro de un grupo.")
            return False
        if not _message_is_admin(bot, owner_id, message):
            bot.reply_to(message, denial)
            return False
        return ensure_module_for_admin(bot, message, module)

    @bot.message_handler(commands=["filter"])
    def cmd_filter(message):
        try:
            if not admin_config(message, "Solo los administradores pueden crear filtros.", "filters"):
                return
            argument = (message.text or "").partition(" ")[2]
            trigger, response = _split_key_value(argument)
            if not trigger or not response or len(trigger) > 80 or len(response) > 3000:
                bot.reply_to(message, "Uso: /filter palabra | respuesta (también vale /filter palabra respuesta). Máximo 80 y 3000 caracteres.")
                return
            con = get_db()
            try:
                con.execute(
                    "INSERT INTO chat_filters(chat_id,trigger,response) VALUES(?,?,?) "
                    "ON CONFLICT(chat_id,trigger) DO UPDATE SET response=excluded.response, created_at=CURRENT_TIMESTAMP",
                    (message.chat.id, trigger, response),
                )
                con.commit()
            finally:
                con.close()
            bot.reply_to(message, f"Filtro guardado para <code>{html.escape(trigger)}</code>.", parse_mode="HTML")
        except Exception:
            logger.exception("Falló /filter")
            bot.reply_to(message, "No pude guardar el filtro.")

    @bot.message_handler(commands=["unfilter"])
    def cmd_unfilter(message):
        try:
            if not admin_config(message, "Solo los administradores pueden eliminar filtros.", "filters"):
                return
            trigger = (message.text or "").partition(" ")[2].strip()
            if not trigger:
                bot.reply_to(message, "Uso: /unfilter palabra")
                return
            con = get_db()
            try:
                cur = con.execute("DELETE FROM chat_filters WHERE chat_id=? AND lower(trigger)=lower(?)", (message.chat.id, trigger))
                con.commit()
                removed = cur.rowcount
            finally:
                con.close()
            bot.reply_to(message, "Filtro eliminado." if removed else "No encontré ese filtro.")
        except Exception:
            logger.exception("Falló /unfilter")
            bot.reply_to(message, "No pude eliminar el filtro.")

    @bot.message_handler(commands=["save"])
    def cmd_save(message):
        try:
            if not admin_config(message, "Solo los administradores pueden guardar notas.", "filters"):
                return
            argument = (message.text or "").partition(" ")[2]
            name, content = _split_key_value(argument)
            name = name.lstrip("#").lower()
            if not re.fullmatch(r"[a-z0-9_]{1,40}", name) or not content or len(content) > 3500:
                bot.reply_to(message, "Uso: /save nombre | contenido (también vale /save nombre contenido). El nombre admite hasta 40 letras, números o _.")
                return
            con = get_db()
            try:
                con.execute(
                    "INSERT INTO chat_notes(chat_id,name,content) VALUES(?,?,?) "
                    "ON CONFLICT(chat_id,name) DO UPDATE SET content=excluded.content,updated_at=CURRENT_TIMESTAMP",
                    (message.chat.id, name, content),
                )
                con.commit()
            finally:
                con.close()
            bot.reply_to(message, f"Nota <code>#{html.escape(name)}</code> guardada.", parse_mode="HTML")
        except Exception:
            logger.exception("Falló /save")
            bot.reply_to(message, "No pude guardar la nota.")

    @bot.message_handler(commands=["get"])
    def cmd_get(message):
        try:
            if message.chat.type == "private":
                bot.reply_to(message, "Las notas se guardan por grupo. Usa /get dentro del grupo.")
                return
            if not module_enabled_for_message(message, "filters"):
                return
            name = (message.text or "").partition(" ")[2].strip().lstrip("#").lower()
            if not name:
                bot.reply_to(message, "Uso: /get nombre")
                return
            con = get_db()
            try:
                row = con.execute(
                    "SELECT content FROM chat_notes WHERE chat_id=? AND lower(name)=lower(?) LIMIT 1",
                    (message.chat.id, name),
                ).fetchone()
            finally:
                con.close()
            if row:
                bot.send_message(message.chat.id, html.escape(row[0]), parse_mode="HTML")
            else:
                bot.reply_to(message, "No encontré esa nota.")
        except Exception:
            logger.exception("Falló /get")
            bot.reply_to(message, "No pude buscar la nota.")

    @bot.message_handler(commands=["antilinks"])
    def cmd_antilinks(message):
        try:
            if not admin_config(message, "Solo los administradores pueden cambiar esta regla.", "antispam"):
                return
            value = (message.text or "").partition(" ")[2].strip().lower()
            if value not in {"on", "off", "estado"}:
                bot.reply_to(message, "Uso: /antilinks on|off. Se bloquearán enlaces de usuarios que no sean administradores.")
                return
            if value == "estado":
                enabled = (get_setting(message.chat.id, "anti_links") == "1")
                bot.reply_to(message, "Antienlaces activado." if enabled else "Antienlaces desactivado.")
                return
            set_chat_setting(message.chat.id, "anti_links", "1" if value == "on" else "0")
            bot.reply_to(message, "Antienlaces actualizado.")
        except Exception:
            logger.exception("Falló /antilinks")
            bot.reply_to(message, "No pude cambiar la regla de enlaces.")

    @bot.message_handler(commands=["floodconfig"])
    def cmd_floodconfig(message):
        try:
            if not admin_config(message, "Solo los administradores pueden configurar el antiflood.", "antispam"):
                return
            parts = (message.text or "").split()
            if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
                bot.reply_to(message, "Uso: /floodconfig <mensajes 2-30> <segundos 3-60> (por defecto 5 8).")
                return
            limit, window = int(parts[1]), int(parts[2])
            if not (2 <= limit <= 30 and 3 <= window <= 60):
                bot.reply_to(message, "El límite debe ser 2-30 mensajes y la ventana 3-60 segundos.")
                return
            set_chat_setting(message.chat.id, "flood_limit", limit)
            set_chat_setting(message.chat.id, "flood_window", window)
            bot.reply_to(message, f"Antiflood configurado: {limit} mensajes cada {window} segundos.")
        except Exception:
            logger.exception("Falló /floodconfig")
            bot.reply_to(message, "No pude guardar la configuración antiflood.")

    @bot.message_handler(commands=["blockword"])
    def cmd_blockword(message):
        try:
            if not admin_config(message, "Solo los administradores pueden editar la lista negra.", "antispam"):
                return
            parts = (message.text or "").split(maxsplit=2)
            if len(parts) == 1 or (len(parts) > 1 and parts[1].lower() == "list"):
                con = get_db()
                try:
                    words = [row[0] for row in con.execute(
                        "SELECT word FROM blacklist_words WHERE chat_id=? ORDER BY word", (message.chat.id,)
                    ).fetchall()]
                finally:
                    con.close()
                bot.reply_to(message, "Palabras bloqueadas: " + (", ".join(html.escape(word) for word in words) or "ninguna"), parse_mode="HTML")
                return
            if len(parts) != 3 or parts[1].lower() not in {"add", "del"}:
                bot.reply_to(message, "Uso: /blockword add|del palabra, o /blockword list")
                return
            word = parts[2].strip().casefold()
            if not 1 <= len(word) <= 80:
                bot.reply_to(message, "La palabra debe tener entre 1 y 80 caracteres.")
                return
            con = get_db()
            try:
                if parts[1].lower() == "add":
                    con.execute("INSERT OR IGNORE INTO blacklist_words(chat_id,word) VALUES(?,?)", (message.chat.id, word))
                else:
                    con.execute("DELETE FROM blacklist_words WHERE chat_id=? AND word=?", (message.chat.id, word))
                con.commit()
            finally:
                con.close()
            bot.reply_to(message, "Lista negra actualizada.")
        except Exception:
            logger.exception("Falló /blockword")
            bot.reply_to(message, "No pude actualizar la lista negra.")

    @bot.message_handler(commands=["blockcontent"])
    def cmd_blockcontent(message):
        try:
            if not admin_config(message, "Solo los administradores pueden cambiar los bloqueos de contenido.", "antispam"):
                return
            parts = (message.text or "").split()
            if len(parts) != 3 or parts[1].lower() not in BLOCKABLE_TYPES or parts[2].lower() not in {"on", "off"}:
                bot.reply_to(message, "Uso: /blockcontent tipo on|off. Tipos: " + ", ".join(sorted(BLOCKABLE_TYPES)))
                return
            set_chat_setting(message.chat.id, f"block_content:{parts[1].lower()}", "1" if parts[2].lower() == "on" else "0")
            bot.reply_to(message, f"Bloqueo de {html.escape(parts[1].lower())} actualizado.", parse_mode="HTML")
        except Exception:
            logger.exception("Falló /blockcontent")
            bot.reply_to(message, "No pude cambiar el bloqueo de contenido.")


def get_setting(chat_id, key):
    from flamie.modules.settings import get_chat_setting
    return get_chat_setting(chat_id, key, "0")
