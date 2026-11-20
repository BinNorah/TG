"""Herramientas privadas del propietario del bot."""
import html
import logging
import os
import sqlite3
import tempfile
import threading
import time

from flamie.core.config import DB_PATH, MI_ID
from flamie.database.db import get_db
from flamie.modules.settings import get_chat_setting, set_chat_setting

logger = logging.getLogger(__name__)
_broadcast_lock = threading.Lock()


def _owner_private(message):
    return bool(message.from_user and message.from_user.id == MI_ID and message.chat.id == MI_ID and message.chat.type == "private")


def _target_id(message, argument):
    if message.reply_to_message and message.reply_to_message.from_user:
        return int(message.reply_to_message.from_user.id)
    first = argument.split(maxsplit=1)[0] if argument else ""
    if not first.isdecimal():
        raise ValueError("Indica un ID numérico o responde a un mensaje del usuario.")
    return int(first)


def register_owner_tools(bot):
    @bot.message_handler(commands=["stats"])
    def cmd_stats(message):
        try:
            if not _owner_private(message):
                return
            con = get_db()
            try:
                users = con.execute("SELECT COUNT(DISTINCT user_id) FROM chat_users").fetchone()[0]
                groups = con.execute("SELECT COUNT(*) FROM chat_registry").fetchone()[0]
                actions = con.execute("SELECT COUNT(*) FROM mod_actions").fetchone()[0]
                reminders = con.execute("SELECT COUNT(*) FROM reminders WHERE status='pending'").fetchone()[0]
                bans = con.execute("SELECT COUNT(*) FROM global_bans").fetchone()[0]
            finally:
                con.close()
            bot.reply_to(message, f"<b>Estadísticas de Flamie</b>\nGrupos registrados: {groups}\nUsuarios vistos: {users}\nAcciones de moderación: {actions}\nRecordatorios pendientes: {reminders}\nGban activos: {bans}\nMantenimiento: {'activado' if get_chat_setting(0, 'maintenance', '0') == '1' else 'desactivado'}", parse_mode="HTML")
        except Exception:
            logger.exception("Falló /stats")
            bot.reply_to(message, "No pude consultar las estadísticas.")

    @bot.message_handler(commands=["broadcast"])
    def cmd_broadcast(message):
        acquired = False
        try:
            if not _owner_private(message):
                return
            text = (message.text or "").partition(" ")[2].strip()
            if not text or len(text) > 4000:
                bot.reply_to(message, "Uso: /broadcast mensaje (máximo 4000 caracteres). Se enviará a los grupos registrados.")
                return
            con = get_db()
            try:
                chats = [int(row[0]) for row in con.execute("SELECT chat_id FROM chat_registry ORDER BY chat_id").fetchall()]
            finally:
                con.close()
            if not _broadcast_lock.acquire(blocking=False):
                bot.reply_to(message, "Ya hay un envío en curso.")
                return
            acquired = True

            def deliver():
                sent = failed = 0
                for chat_id in chats:
                    try:
                        bot.send_message(chat_id, html.escape(text), parse_mode="HTML", disable_web_page_preview=True)
                        sent += 1
                    except Exception:
                        failed += 1
                        logger.exception("Falló broadcast al chat %s", chat_id)
                    time.sleep(0.05)
                try:
                    bot.send_message(MI_ID, f"Envío finalizado. Entregados: {sent}. Fallidos: {failed}.")
                except Exception:
                    logger.exception("No se pudo informar el resultado del broadcast")
                finally:
                    _broadcast_lock.release()

            worker = threading.Thread(target=deliver, name="owner-broadcast", daemon=True)
            worker.start()
            acquired = False
            bot.reply_to(message, f"Iniciando envío a {len(chats)} grupos.")
        except Exception:
            logger.exception("Falló /broadcast")
            if acquired:
                _broadcast_lock.release()
            bot.reply_to(message, "No pude iniciar el envío.")

    @bot.message_handler(commands=["gban"])
    def cmd_gban(message):
        try:
            if not _owner_private(message):
                return
            argument = (message.text or "").partition(" ")[2].strip()
            try:
                user_id = _target_id(message, argument)
            except ValueError as exc:
                bot.reply_to(message, html.escape(str(exc)))
                return
            if user_id == MI_ID:
                bot.reply_to(message, "No puedes aplicar un gban al propietario.")
                return
            parts = argument.split(maxsplit=1)
            reason = (parts[1] if parts and parts[0].isdecimal() and len(parts) > 1 else argument)[:500]
            con = get_db()
            try:
                con.execute("INSERT INTO global_bans(user_id,reason) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET reason=excluded.reason,created_at=CURRENT_TIMESTAMP", (user_id, reason))
                chats = [int(row[0]) for row in con.execute("SELECT chat_id FROM chat_registry").fetchall()]
                con.commit()
            finally:
                con.close()
            banned = failed = 0
            for chat_id in chats:
                try:
                    bot.ban_chat_member(chat_id, user_id)
                    banned += 1
                except Exception:
                    failed += 1
                    logger.exception("No se pudo aplicar gban a %s en %s", user_id, chat_id)
            bot.reply_to(message, f"Gban guardado para <code>{user_id}</code>. Aplicado en {banned} grupos; fallidos: {failed}. También se bloqueará si intenta escribir o entrar en un grupo registrado.", parse_mode="HTML")
        except Exception:
            logger.exception("Falló /gban")
            bot.reply_to(message, "No pude completar el gban.")

    @bot.message_handler(commands=["ungban"])
    def cmd_ungban(message):
        try:
            if not _owner_private(message):
                return
            argument = (message.text or "").partition(" ")[2].strip()
            try:
                user_id = _target_id(message, argument)
            except ValueError as exc:
                bot.reply_to(message, html.escape(str(exc)))
                return
            con = get_db()
            try:
                con.execute("DELETE FROM global_bans WHERE user_id=?", (user_id,))
                chats = [int(row[0]) for row in con.execute("SELECT chat_id FROM chat_registry").fetchall()]
                con.commit()
            finally:
                con.close()
            lifted = failed = 0
            for chat_id in chats:
                try:
                    bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
                    lifted += 1
                except Exception:
                    failed += 1
                    logger.exception("No se pudo retirar gban de %s en %s", user_id, chat_id)
            bot.reply_to(message, f"Gban retirado de <code>{user_id}</code>. Grupos actualizados: {lifted}; fallidos: {failed}.", parse_mode="HTML")
        except Exception:
            logger.exception("Falló /ungban")
            bot.reply_to(message, "No pude retirar el gban.")

    @bot.message_handler(commands=["backup"])
    def cmd_backup(message):
        try:
            if not _owner_private(message):
                return
            with tempfile.NamedTemporaryFile(prefix="flamie-backup-", suffix=".db", delete=False) as tmp:
                backup_path = tmp.name
            try:
                source = sqlite3.connect(str(DB_PATH), timeout=15)
                destination = sqlite3.connect(backup_path)
                try:
                    source.backup(destination)
                finally:
                    destination.close()
                    source.close()
                with open(backup_path, "rb") as backup_file:
                    bot.send_document(MI_ID, backup_file, caption="Respaldo de la base de datos de Flamie.")
            finally:
                try:
                    os.unlink(backup_path)
                except OSError:
                    logger.exception("No se pudo eliminar el archivo temporal del respaldo")
        except Exception:
            logger.exception("Falló /backup")
            bot.reply_to(message, "No pude generar o enviar el respaldo.")

    @bot.message_handler(commands=["maintenance"])
    def cmd_maintenance(message):
        try:
            if not _owner_private(message):
                return
            value = (message.text or "").partition(" ")[2].strip().lower()
            if value not in {"on", "off", "estado"}:
                bot.reply_to(message, "Uso: /maintenance on, /maintenance off o /maintenance estado")
                return
            if value != "estado":
                set_chat_setting(0, "maintenance", "1" if value == "on" else "0")
            active = get_chat_setting(0, "maintenance", "0") == "1"
            bot.reply_to(message, f"Modo mantenimiento: {'activado' if active else 'desactivado'}.")
        except Exception:
            logger.exception("Falló /maintenance")
            bot.reply_to(message, "No pude actualizar el modo mantenimiento.")

    @bot.message_handler(commands=["grupos"])
    def cmd_groups(message):
        try:
            if not _owner_private(message):
                return
            con = get_db()
            try:
                rows = con.execute("SELECT chat_id,title,chat_type,last_seen FROM chat_registry ORDER BY last_seen DESC LIMIT 50").fetchall()
                total = con.execute("SELECT COUNT(*) FROM chat_registry").fetchone()[0]
            finally:
                con.close()
            if not rows:
                bot.reply_to(message, "Todavía no hay grupos registrados.")
                return
            lines = [f"<b>Grupos ({total}; últimos {len(rows)})</b>"]
            lines.extend(f"• {html.escape(row[1] or 'Sin título')} · <code>{row[0]}</code> · {html.escape(row[2])} · {html.escape(row[3])}" for row in rows)
            bot.reply_to(message, "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            logger.exception("Falló /grupos")
            bot.reply_to(message, "No pude consultar los grupos registrados.")

    @bot.message_handler(commands=["logs"])
    def cmd_logs(message):
        try:
            if not _owner_private(message):
                return
            argument = (message.text or "").partition(" ")[2].strip()
            try:
                limit = max(1, min(20, int(argument or "10")))
            except ValueError:
                bot.reply_to(message, "Uso: /logs [cantidad, de 1 a 20]")
                return
            con = get_db()
            try:
                rows = con.execute("SELECT chat_id,actor_id,target_user_id,action,reason,created_at FROM mod_actions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            finally:
                con.close()
            if not rows:
                bot.reply_to(message, "No hay acciones registradas.")
                return
            lines = ["<b>Acciones recientes</b>"]
            for row in rows:
                lines.append(f"<code>{row[5]}</code> · <b>{html.escape(row[3])}</b> chat <code>{row[0]}</code> · usuario <code>{row[2] or '—'}</code> · actor <code>{row[1]}</code>\n{html.escape((row[4] or '')[:250])}")
            bot.reply_to(message, "\n".join(lines), parse_mode="HTML")
        except Exception:
            logger.exception("Falló /logs")
            bot.reply_to(message, "No pude consultar los registros.")
