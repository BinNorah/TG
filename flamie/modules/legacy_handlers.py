import telebot
from datetime import date, datetime
import html
import threading
import io
import re
import time
import random
import logging

from flamie.core.config import (
    BASE_DIR, MI_ID, TOKEN, OWNER_DISPLAY_NAME, OWNER_ALIAS,
)
from flamie.database.db import (
    clear_activation_state, get_db, init_db, load_activation_states,
    load_usuarios_notificados, query, save_activation_state,
)
from flamie.utils.errors import GlobalErrorHandler
from flamie.utils.cache import get_chat_member_cached
from flamie.utils.permissions import require_permission, user_has_role
from flamie.modules.settings import (
    chat_language, get_chat_setting, module_enabled_for_message,
    register_settings, set_chat_setting,
)
from flamie.modules.antispam import GroupAutomationMiddleware
from flamie.modules.filters import register_filters
from flamie.modules.welcome import register_welcome, start_captcha_sweeper
from flamie.modules.modlog import log_mod_action
from flamie.modules.utilities import register_utilities, start_reminder_worker
from flamie.modules.fun import register_fun
from flamie.modules.owner import register_owner_tools

logger = logging.getLogger(__name__)

# Inicialización compatible con el esquema previo.
init_db()
usuarios_notificados_cache = load_usuarios_notificados()

# ==========================================
# 4. BOT
# ==========================================
bot = telebot.TeleBot(
    TOKEN,
    exception_handler=GlobalErrorHandler(),
    use_class_middlewares=True,
)
bot.setup_middleware(GroupAutomationMiddleware(bot))
_gif_ids = {}
register_settings(bot, MI_ID)
register_filters(bot, MI_ID)
register_welcome(bot, MI_ID)
register_utilities(bot, MI_ID)
register_fun(bot)
register_owner_tools(bot)


def enviar_animacion_cacheada(chat_id, nombre_archivo, caption=None, parse_mode=None,
                              reply_markup=None, reply_to_message_id=None):
    ruta = BASE_DIR / nombre_archivo
    file_id = _gif_ids.get(nombre_archivo)
    opciones = {
        "caption": caption,
        "parse_mode": parse_mode,
        "reply_markup": reply_markup,
        "reply_to_message_id": reply_to_message_id,
    }
    opciones = {clave: valor for clave, valor in opciones.items() if valor is not None}
    if file_id:
        try:
            return bot.send_animation(chat_id, file_id, **opciones)
        except Exception:
            _gif_ids.pop(nombre_archivo, None)
    try:
        with ruta.open("rb") as archivo:
            enviado = bot.send_animation(chat_id, archivo, **opciones)
        if enviado and enviado.animation:
            _gif_ids[nombre_archivo] = enviado.animation.file_id
        return enviado
    except FileNotFoundError:
        return None
    except Exception:
        return None


def comandos_personalizados_menu():
    # Reserva margen dentro del límite de 100 comandos que permite Telegram
    # para mostrar también los comandos administrativos y del propietario.
    filas = query("SELECT command FROM custom_commands ORDER BY command LIMIT 20")
    return [telebot.types.BotCommand(str(fila[0]), "Comando personalizado") for fila in filas]


def usuario_puede_administrar(chat_id, chat_type, user_id):
    if user_id == MI_ID:
        return True
    if chat_type == "private":
        return False
    try:
        return get_chat_member_cached(bot, chat_id, user_id).status in ("administrator", "creator")
    except Exception:
        return False


def registrar_comandos_menu():
    publicos = [
        ("start", "Inicia el bot"), ("help", "Abre los paneles de funciones"),
        ("info", "Muestra informacion del chat y usuario"), ("id", "Muestra identificadores"),
        ("chatid", "Muestra el ID del chat"), ("chatlink", "Obtiene el enlace del grupo"),
        ("staff", "Lista los administradores"), ("rules", "Consulta las reglas del grupo"),
        ("q", "Crea un sticker de conversacion"), ("8ball", "Pregunta a la bola magica"),
        ("dado", "Lanza un dado"),
        ("tr", "Traduce un texto"), ("tts", "Convierte texto a audio"),
        ("clima", "Consulta el clima de una ciudad"), ("calc", "Calcula una expresión"),
        ("uinfo", "Muestra información de un usuario"), ("wiki", "Busca un tema en Wikipedia"),
        ("recordar", "Programa un recordatorio"), ("afk", "Marca tu estado como AFK"),
        ("encuesta", "Crea una encuesta"), ("idsticker", "Muestra el ID de un sticker"),
        ("getsticker", "Reenvía un sticker"),
        ("top", "Muestra el ranking y niveles del grupo"), ("quiz", "Inicia una pregunta de trivia"),
        ("ship", "Calcula compatibilidad entre dos usuarios"), ("verdadoreto", "Juega verdad o reto"),
    ]
    privados = publicos + [("activar", "Solicita una activacion")]
    administradores = [
        ("ban", "Banea a un usuario"), ("tban", "Banea por tiempo"), ("kick", "Expulsa a un usuario"),
        ("mute", "Silencia a un usuario"), ("tmute", "Silencia por tiempo"), ("unmute", "Quita el silencio"),
        ("restrict", "Restringe a un usuario"), ("promote", "Promueve a un administrador"),
        ("demote", "Retira el rol de administrador"), ("warn", "Advierte a un usuario"),
        ("unwarn", "Retira una advertencia"),
        ("warns", "Consulta las advertencias e historial"), ("resetwarns", "Borra las advertencias"),
        ("warnconfig", "Configura el umbral y la acción de advertencias"),
        ("unban", "Retira el baneo de un usuario"), ("purge", "Borra varios mensajes"),
        ("report", "Reporta un mensaje a los administradores"), ("admin", "Avisa a los administradores"),
        ("settitle", "Cambia el título del grupo"), ("setgtitle", "Cambia un título de administrador"),
        ("setdescr", "Cambia la descripción del grupo"),
        ("setwelcome", "Configura la bienvenida del grupo"), ("setgoodbye", "Configura la despedida del grupo"),
        ("captchaconfig", "Configura el límite de tiempo del captcha"), ("antiraid", "Activa o desactiva el modo antiraid"),
        ("filter", "Crea o actualiza un filtro"), ("unfilter", "Elimina un filtro"),
        ("save", "Guarda una nota"), ("antilinks", "Configura el bloqueo de enlaces"),
        ("floodconfig", "Configura el antiflood"), ("blockword", "Edita la lista negra"),
        ("blockcontent", "Bloquea tipos de contenido"),
        ("settings", "Configura los módulos de este grupo"),
        ("del", "Borra el mensaje respondido"), ("pin", "Fija el mensaje respondido"),
        ("unpin", "Desfija un mensaje"), ("unallpin", "Desfija todos los mensajes"),
        ("setrules", "Define las reglas del grupo"), ("clearrules", "Elimina las reglas"),
    ]
    propietario = [
        ("limpiar", "Limpia este historial con confirmacion"),
        ("addcmd", "Crea o actualiza un comando"), ("delcmd", "Elimina un comando personalizado"),
        ("listcmd", "Lista comandos personalizados"),
        ("stats", "Muestra estadísticas del bot"), ("broadcast", "Envía un mensaje a los grupos"),
        ("gban", "Bloquea un usuario en todos los grupos"), ("ungban", "Retira un bloqueo global"),
        ("backup", "Envía una copia de la base de datos"), ("maintenance", "Activa o consulta mantenimiento"),
        ("grupos", "Lista los grupos registrados"), ("logs", "Consulta acciones recientes"),
    ]
    personalizados = comandos_personalizados_menu()
    convertir = lambda pares: [telebot.types.BotCommand(nombre, descripcion) for nombre, descripcion in pares]
    comandos_publicos = convertir(publicos + [("get", "Consulta una nota guardada")]) + personalizados
    comandos_privados = convertir(privados) + personalizados
    comandos_admin = comandos_publicos + convertir(administradores)
    comandos_propietario = comandos_privados + convertir(administradores) + convertir(propietario)
    try:
        bot.set_my_commands(comandos_privados, scope=telebot.types.BotCommandScopeAllPrivateChats())
        bot.set_my_commands(comandos_publicos, scope=telebot.types.BotCommandScopeAllGroupChats())
        bot.set_my_commands(comandos_admin, scope=telebot.types.BotCommandScopeAllChatAdministrators())
        bot.set_my_commands(comandos_propietario, scope=telebot.types.BotCommandScopeChat(chat_id=MI_ID))
    except Exception:
        logger.exception("No se pudo actualizar el menú de comandos de Telegram")


def teclado_ayuda(message, user_id=None, panel="inicio"):
    user_id = user_id or (message.from_user.id if message.from_user else 0)
    chat_id = message.chat.id
    chat_type = message.chat.type
    es_admin = usuario_puede_administrar(chat_id, chat_type, user_id)
    en = chat_language(chat_id) == "en"
    labels = {
        "Informacion": "Information", "Ocio": "Fun", "Stickers": "Stickers",
        "Reglas": "Rules", "Utilidades": "Utilities", "Activacion": "Activation",
        "Moderacion": "Moderation", "Administracion": "Group admin",
        "Propietario": "Owner", "Cerrar": "Close", "Volver a paneles": "All panels",
        "Anterior": "Previous", "Siguiente": "Next",
    }
    label = lambda value: labels.get(value, value) if en else value
    markup = telebot.types.InlineKeyboardMarkup()
    if panel == "inicio":
        markup.row(
            telebot.types.InlineKeyboardButton(label("Informacion"), callback_data="help:info"),
            telebot.types.InlineKeyboardButton(label("Ocio"), callback_data="help:ocio"),
        )
        markup.row(
            telebot.types.InlineKeyboardButton(label("Stickers"), callback_data="help:stickers"),
            telebot.types.InlineKeyboardButton(label("Reglas"), callback_data="help:reglas"),
        )
        markup.row(telebot.types.InlineKeyboardButton(label("Utilidades"), callback_data="help:utilidades"))
        if chat_type == "private":
            markup.row(telebot.types.InlineKeyboardButton(label("Activacion"), callback_data="help:activacion"))
        if es_admin:
            markup.row(
                telebot.types.InlineKeyboardButton(label("Moderacion"), callback_data="help:moderacion"),
                telebot.types.InlineKeyboardButton(label("Administracion"), callback_data="help:adminchat"),
            )
            if user_id == MI_ID:
                markup.row(telebot.types.InlineKeyboardButton(label("Propietario"), callback_data="help:propietario"))
        markup.row(telebot.types.InlineKeyboardButton(label("Cerrar"), callback_data="help:cerrar"))
    else:
        paneles = ["info", "ocio", "stickers", "reglas", "utilidades"]
        if chat_type == "private":
            paneles.append("activacion")
        if es_admin:
            paneles.extend(["moderacion", "adminchat"])
            if user_id == MI_ID:
                paneles.append("propietario")
        if panel in paneles:
            index = paneles.index(panel)
            anterior = paneles[(index - 1) % len(paneles)]
            siguiente = paneles[(index + 1) % len(paneles)]
            markup.row(
                telebot.types.InlineKeyboardButton(label("Anterior"), callback_data=f"help:{anterior}"),
                telebot.types.InlineKeyboardButton(label("Siguiente"), callback_data=f"help:{siguiente}"),
            )
        markup.row(
            telebot.types.InlineKeyboardButton(label("Volver a paneles"), callback_data="help:inicio"),
            telebot.types.InlineKeyboardButton(label("Cerrar"), callback_data="help:cerrar"),
        )
    return markup


def texto_panel_ayuda(panel, user_id, chat_id, chat_type):
    es_admin = usuario_puede_administrar(chat_id, chat_type, user_id)
    paneles = {
        "inicio": "Ayuda de Flamie\nElige un panel. Los paneles administrativos solo se muestran a administradores del grupo y al propietario.",
        "info": "Informacion\n/info, /id — Datos e identificadores del chat y usuario.\n/chatid — ID del chat.",
        "ocio": "Ocio\n/8ball — Respuesta aleatoria. /dado — Lanza un dado.\n/quiz — Trivia. /ship — Compatibilidad.\n/verdadoreto — Elige verdad o reto. /top — Ranking y niveles.\nReacciones: /abrazar, /acariciar, /saludar, /besar, /aplaudir, /bailar, /llorar.",
        "stickers": "Stickers\n/q — Responde a un texto para crear sticker de cita.\n/idsticker — Muestra los IDs de un sticker.\n/getsticker — Reenvía un sticker.",
        "reglas": "Reglas\n/rules — Consulta las reglas de este grupo.",
        "utilidades": "Utilidades\n/tr idioma texto — Traduce; /tts texto — Genera audio.\n/clima ciudad, /calc expresión, /wiki tema, /uinfo — Consultas y cálculo.\n/recordar 10m texto — Recordatorio persistente. /afk [motivo] — Estado AFK.\n/encuesta pregunta | opción 1 | opción 2 — Crea una encuesta.\n/chatlink — Obtiene el enlace del grupo. /staff — Lista administradores.\n/get nombre o #nombre — Consulta una nota guardada. /activar — Solicita una activacion en privado.",
        "activacion": "Activacion\nPulsa Activacion para indicar la plataforma y enviar una solicitud. Tambien puedes usar /activar.",
        "moderacion": "Moderacion\n/ban, /tban, /unban, /kick — Banear, desbanear o expulsar.\n/mute, /tmute, /unmute, /restrict — Silencios y restricciones.\n/warn, /warns, /resetwarns, /warnconfig — Advertencias e historial.\n/purge — Borra varios mensajes. /report o @admin — Avisa a administradores.",
        "adminchat": "Administracion del grupo\n/settings — Configura módulos, idioma y canal de logs.\n/setwelcome, /setgoodbye — Mensajes de entrada y salida. /captchaconfig — Tiempo del captcha.\n/filter, /unfilter, /save — Filtros y notas. /antilinks, /blockword, /blockcontent, /floodconfig — Antispam.\n/antiraid — Activa protección de ingresos masivos.\n/settitle, /setdescr — Edita la información del grupo; /setgtitle — título de administrador.\n/promote y /demote — Gestion de administradores.\n/setrules y /clearrules — Gestion de reglas.\n/del, /pin, /unpin, /unallpin — Herramientas de mensajes.",
        "propietario": "Panel del propietario\n/limpiar — Limpia el historial privado en tu cuenta tras confirmacion.\n/addcmd nombre | respuesta, /delcmd nombre, /listcmd — Comandos personalizados.\n/stats, /grupos, /logs — Estado y registros. /broadcast — Anuncio a grupos.\n/gban y /ungban — Bloqueo global. /backup — Copia de la BD. /maintenance on|off — Mantenimiento.",
    }
    paneles_en = {
        "inicio": "Flamie help\nChoose a page. Group administration pages are available to group admins and the owner.",
        "info": "Information\n/info, /id — Chat and user details.\n/chatid — Chat identifier.",
        "ocio": "Fun\n/8ball — Random answer. /dado — Roll a die.\n/quiz — Trivia. /ship — Compatibility.\n/verdadoreto — Pick truth or dare. /top — Ranking and levels.\nReactions: /abrazar, /acariciar, /saludar, /besar, /aplaudir, /bailar, /llorar.",
        "stickers": "Stickers\n/q — Reply to a text to create a quote sticker.\n/idsticker — Show sticker IDs.\n/getsticker — Resend a sticker.",
        "reglas": "Rules\n/rules — View this group's rules.",
        "utilidades": "Utilities\n/tr language text — Translate; /tts text — Generate audio.\n/clima city, /calc expression, /wiki topic, /uinfo — Queries and calculation.\n/recordar 10m text — Persistent reminder. /afk [reason] — AFK status.\n/encuesta question | option 1 | option 2 — Create a poll.\n/chatlink — Get the group link. /staff — List administrators.\n/get name or #name — Retrieve a saved note. /activar — Request activation in private chat.",
        "activacion": "Activation\nChoose Activation to select a platform and send a request. You can also use /activar.",
        "moderacion": "Moderation\n/ban, /tban, /unban, /kick — Ban, unban, or remove a user.\n/mute, /tmute, /unmute, /restrict — Restrict users.\n/warn, /warns, /resetwarns, /warnconfig — Warnings and history.\n/purge — Delete several messages. /report or @admin — Notify admins.",
        "adminchat": "Group administration\n/settings — Configure modules, language, and moderation log channel.\n/setwelcome, /setgoodbye — Entry and exit messages. /captchaconfig — Captcha timer.\n/filter, /unfilter, /save — Filters and notes. /antilinks, /blockword, /blockcontent, /floodconfig — Antispam.\n/antiraid — Protect against join bursts.\n/settitle, /setdescr — Edit group info; /setgtitle — set an admin title.\n/promote and /demote — Manage administrators.\n/setrules and /clearrules — Manage rules.\n/del, /pin, /unpin, /unallpin — Manage messages.",
        "propietario": "Owner panel\n/limpiar — Clear your private history after confirmation.\n/addcmd name | response, /delcmd name, /listcmd — Custom commands.\n/stats, /grupos, /logs — Status and records. /broadcast — Group announcement.\n/gban and /ungban — Global ban. /backup — Database copy. /maintenance on|off — Maintenance.",
    }
    paneles_admin = {"moderacion", "adminchat"}
    if panel in paneles_admin and not es_admin:
        return "Access denied. This page is only available to group admins." if chat_language(chat_id) == "en" else "Acceso denegado. Este panel solo esta disponible para administradores del grupo."
    if panel == "propietario" and user_id != MI_ID:
        return "Access denied. This page is only available to the owner." if chat_language(chat_id) == "en" else "Acceso denegado. Este panel solo esta disponible para el propietario."
    if chat_language(chat_id) == "en":
        return paneles_en.get(panel, paneles_en["inicio"])
    return paneles.get(panel, paneles["inicio"])


@bot.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith("help:")))
def callback_ayuda(call):
    panel = call.data.split(":", 1)[1]
    user_id = call.from_user.id
    if panel == "cerrar":
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return
    if panel == "activacion":
        if call.message.chat.type != "private":
            bot.answer_callback_query(call.id, "Solicita la activacion en el chat privado del bot.")
            return
        bot.answer_callback_query(call.id)
        esperando_plataforma.add(user_id)
        esperando_codigo.pop(user_id, None)
        save_activation_state(user_id, "plataforma")
        pedir_plataforma_activacion(call.message.chat.id)
        return
    texto = texto_panel_ayuda(panel, user_id, call.message.chat.id, call.message.chat.type)
    markup = teclado_ayuda(call.message, user_id=user_id, panel=panel)
    try:
        if getattr(call.message, "animation", None):
            bot.edit_message_caption(
                caption=texto, chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=markup,
            )
        else:
            bot.edit_message_text(texto, call.message.chat.id, call.message.message_id, reply_markup=markup)
        bot.answer_callback_query(call.id)
    except Exception:
        bot.answer_callback_query(call.id, "No se pudo abrir este panel.")

def registrar_usuarios_vistos(updates):
    """Guarda usuarios observados para poder resolver nombres de usuario."""
    rows = []
    for update in updates:
        message = getattr(update, "message", None)
        if not message or message.chat.type == "private" or not message.from_user:
            continue
        user = message.from_user
        rows.append((message.chat.id, user.id, user.username, user.first_name or ""))
    if not rows:
        return
    try:
        con = get_db()
        try:
            con.executemany(
                """INSERT INTO chat_users (chat_id, user_id, username, first_name, updated_at)
                   VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(chat_id, user_id) DO UPDATE SET
                     username=excluded.username, first_name=excluded.first_name,
                     updated_at=CURRENT_TIMESTAMP""",
                rows,
            )
            con.commit()
        finally:
            con.close()
    except Exception as exc:
        logger.exception("No se pudieron registrar los usuarios del chat")


bot.set_update_listener(registrar_usuarios_vistos)

# ==========================================
# ==========================================
# 5. HELPERS
# ==========================================
def guardar_mensaje(user_id, nombre, mensaje):
    """Guarda el mensaje en SQLite en segundo plano para no demorar la
    respuesta al usuario mientras se escribe a disco."""
    threading.Thread(
        target=_guardar_mensaje_db, args=(user_id, nombre, mensaje), daemon=True
    ).start()

def _guardar_mensaje_db(user_id, nombre, mensaje):
    fecha = date.today().strftime("%Y-%m-%d")
    hora  = datetime.now().strftime("%H:%M:%S")
    con = get_db()
    try:
        con.execute(
            "INSERT INTO mensajes (user_id, nombre, mensaje, fecha, hora) VALUES (?,?,?,?,?)",
            (user_id, nombre, mensaje, fecha, hora)
        )
        con.commit()
    finally:
        con.close()

def nombre_usuario(u):
    return f"{u.first_name} (@{u.username})" if u.username else u.first_name

def ya_fue_notificado(user_id):
    """Revisa el cache en memoria (cargado al iniciar el bot) — ya no consulta
    la DB en cada mensaje, eso era una espera innecesaria por usuario."""
    return user_id in usuarios_notificados_cache

def marcar_notificado(user_id):
    """Marca en memoria al instante; el guardado en disco (para que persista
    entre reinicios) ocurre en segundo plano."""
    if user_id in usuarios_notificados_cache:
        return
    usuarios_notificados_cache.add(user_id)
    threading.Thread(target=_marcar_notificado_db, args=(user_id,), daemon=True).start()

def _marcar_notificado_db(user_id):
    con = get_db()
    try:
        con.execute(
            "INSERT OR IGNORE INTO usuarios_notificados (user_id) VALUES (?)", (user_id,)
        )
        con.commit()
    finally:
        con.close()

def query(sql, params=()):
    con = get_db()
    try:
        cur = con.execute(sql, params)
        return cur.fetchall()
    finally:
        con.close()

# ==========================================
# 6. COMANDOS — ADMIN
# ==========================================
@bot.message_handler(commands=['start'])
def cmd_start(message):
    texto = (
        "Bienvenido. Usa /help para abrir los paneles de funciones o /info "
        "para consultar los datos de este chat."
    )
    enviado = enviar_animacion_cacheada(
        message.chat.id, "FlamieBot.gif", caption=texto,
        reply_to_message_id=message.message_id,
    )
    if enviado is None:
        bot.reply_to(message, texto)


@bot.message_handler(commands=['help'])
def cmd_help(message):
    user_id = message.from_user.id if message.from_user else 0
    texto = texto_panel_ayuda("inicio", user_id, message.chat.id, message.chat.type)
    teclado = teclado_ayuda(message, user_id=user_id)
    enviado = enviar_animacion_cacheada(
        message.chat.id, "FlamieCmds.gif", caption=texto,
        reply_markup=teclado, reply_to_message_id=message.message_id,
    )
    if enviado is None:
        bot.reply_to(message, texto, reply_markup=teclado)


@bot.message_handler(commands=['activar'])
def cmd_activar(message):
    if message.chat.type != "private":
        bot.reply_to(message, "Solicita la activación en el chat privado del bot.")
        return
    esperando_plataforma.add(message.from_user.id)
    esperando_codigo.pop(message.from_user.id, None)
    save_activation_state(message.from_user.id, "plataforma")
    pedir_plataforma_activacion(message.chat.id)


@bot.message_handler(commands=['info', 'id'])
def cmd_info(message):
    if not module_enabled_for_message(message, "utilities"):
        return
    usuario = message.from_user
    nombre = html.escape(usuario.first_name or "Sin nombre")
    username = "@" + html.escape(usuario.username) if usuario.username else "Sin username"
    chat = html.escape(message.chat.title or "Chat privado")
    tipo = html.escape(message.chat.type or "desconocido")
    bot.reply_to(
        message,
        "Información del chat\n"
        f"Nombre: {chat}\nTipo: {tipo}\nID del chat: <code>{message.chat.id}</code>\n\n"
        f"Usuario: {nombre}\nUsername: {username}\nID del usuario: <code>{usuario.id}</code>",
        parse_mode="HTML",
    )


@bot.message_handler(commands=["chatid"])
def cmd_chatid(message):
    if not module_enabled_for_message(message, "utilities"):
        return
    bot.reply_to(message, f"ID del chat: <code>{message.chat.id}</code>", parse_mode="HTML")


@bot.message_handler(commands=["chatlink"])
def cmd_chatlink(message):
    if not module_enabled_for_message(message, "utilities"):
        return
    try:
        chat = bot.get_chat(message.chat.id)
        enlace = f"https://t.me/{chat.username}" if chat.username else bot.export_chat_invite_link(message.chat.id)
        bot.reply_to(message, html.escape(enlace), parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        bot.reply_to(message, "No pude obtener el enlace. El bot necesita permiso para invitar usuarios.")


@bot.message_handler(commands=["staff"])
def cmd_staff(message):
    if not module_enabled_for_message(message, "utilities"):
        return
    if message.chat.type == "private":
        bot.reply_to(message, "Este comando se usa dentro de un grupo.")
        return
    try:
        admins = bot.get_chat_administrators(message.chat.id)
        nombres = [html.escape((a.user.first_name or "Usuario") + (f" (@{a.user.username})" if a.user.username else "")) for a in admins]
        bot.reply_to(message, "Administradores del chat:\n" + "\n".join(nombres), parse_mode="HTML")
    except Exception:
        bot.reply_to(message, "No pude consultar los administradores de este chat.")


@bot.message_handler(commands=["rules"])
def cmd_rules(message):
    if not module_enabled_for_message(message, "rules"):
        return
    filas = query("SELECT rules FROM chat_rules WHERE chat_id=?", (message.chat.id,))
    bot.reply_to(message, str(filas[0][0]) if filas else "Este grupo no tiene reglas configuradas.")


@bot.message_handler(commands=["8ball"])
def cmd_8ball(message):
    if not module_enabled_for_message(message, "fun"):
        return
    if not (message.text or "").split(maxsplit=1)[-1].strip() or len((message.text or "").split()) < 2:
        bot.reply_to(message, "Uso: /8ball pregunta")
        return
    bot.reply_to(message, random.choice(["Sí.", "No.", "Es probable.", "No puedo asegurarlo.", "Inténtalo más tarde."]))


@bot.message_handler(commands=["dado"])
def cmd_dado(message):
    if not module_enabled_for_message(message, "fun"):
        return
    bot.reply_to(message, f"Resultado: {random.randint(1, 6)}")


def exigir_admin(message):
    try:
        return message.chat.type != "private" and user_has_role(
            bot, MI_ID, message.chat.id, message.chat.type, message.from_user.id, "admin"
        )
    except Exception:
        return False


@bot.message_handler(commands=["setrules"])
def cmd_setrules(message):
    if not module_enabled_for_message(message, "rules"):
        return
    reglas = (message.text or "").partition(" ")[2].strip()
    if not exigir_admin(message):
        bot.reply_to(message, "Solo un administrador del grupo puede cambiar las reglas.")
        return
    if not reglas:
        bot.reply_to(message, "Uso: /setrules texto de las reglas")
        return
    con = get_db()
    try:
        con.execute("INSERT INTO chat_rules(chat_id, rules) VALUES (?, ?) ON CONFLICT(chat_id) DO UPDATE SET rules=excluded.rules, updated_at=CURRENT_TIMESTAMP", (message.chat.id, reglas))
        con.commit()
    finally:
        con.close()
    bot.reply_to(message, "Reglas del grupo actualizadas.")


@bot.message_handler(commands=["clearrules"])
def cmd_clearrules(message):
    if not module_enabled_for_message(message, "rules"):
        return
    if not exigir_admin(message):
        bot.reply_to(message, "Solo un administrador del grupo puede cambiar las reglas.")
        return
    con = get_db()
    try:
        con.execute("DELETE FROM chat_rules WHERE chat_id=?", (message.chat.id,))
        con.commit()
    finally:
        con.close()
    bot.reply_to(message, "Reglas eliminadas.")


@bot.message_handler(commands=["del"])
def cmd_del(message):
    if not module_enabled_for_message(message, "moderation"):
        return
    if not exigir_admin(message):
        bot.reply_to(message, "Solo un administrador del grupo puede borrar mensajes.")
        return
    if not message.reply_to_message:
        bot.reply_to(message, "Responde con /del al mensaje que quieras borrar.")
        return
    try:
        bot.delete_message(message.chat.id, message.reply_to_message.message_id)
        bot.delete_message(message.chat.id, message.message_id)
    except Exception as exc:
        logger.exception("No se pudo borrar el mensaje")
        bot.reply_to(message, "No pude borrar el mensaje. Revisa que tenga permisos suficientes.")


@bot.message_handler(commands=["pin"])
def cmd_pin(message):
    if not module_enabled_for_message(message, "moderation"):
        return
    if not exigir_admin(message) or not message.reply_to_message:
        bot.reply_to(message, "Un administrador debe responder con /pin al mensaje.")
        return
    try:
        bot.pin_chat_message(message.chat.id, message.reply_to_message.message_id)
    except Exception as exc:
        logger.exception("No se pudo fijar el mensaje")
        bot.reply_to(message, "No pude fijar el mensaje. Revisa que tenga permisos suficientes.")


@bot.message_handler(commands=["unpin"])
def cmd_unpin(message):
    if not module_enabled_for_message(message, "moderation"):
        return
    if not exigir_admin(message):
        bot.reply_to(message, "Solo un administrador del grupo puede desfijar mensajes.")
        return
    try:
        if message.reply_to_message:
            bot.unpin_chat_message(message.chat.id, message.reply_to_message.message_id)
        else:
            bot.unpin_chat_message(message.chat.id)
    except Exception as exc:
        logger.exception("No se pudo desfijar el mensaje")
        bot.reply_to(message, "No pude desfijar el mensaje. Revisa que tenga permisos suficientes.")


@bot.message_handler(commands=["unallpin"])
def cmd_unallpin(message):
    if not module_enabled_for_message(message, "moderation"):
        return
    if not exigir_admin(message):
        bot.reply_to(message, "Solo un administrador del grupo puede desfijar mensajes.")
        return
    try:
        bot.unpin_all_chat_messages(message.chat.id)
        bot.reply_to(message, "Se desfijaron los mensajes del grupo.")
    except Exception as exc:
        logger.exception("No se pudieron desfijar los mensajes")
        bot.reply_to(message, "No pude desfijar los mensajes. Revisa que tenga permisos suficientes.")


@bot.message_handler(commands=['limpiar'])
def cmd_limpiar(message):
    if message.chat.type != "private" or not message.from_user or message.from_user.id != MI_ID:
        bot.reply_to(message, "Este comando solo está disponible en el chat privado del propietario.")
        return
    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton("Confirmar limpieza", callback_data="limpiar:confirmar"),
        telebot.types.InlineKeyboardButton("Cancelar", callback_data="limpiar:cancelar"),
    )
    bot.reply_to(
        message,
        "Esto borrará el historial de este chat solo en tu cuenta. La acción no se puede deshacer.",
        reply_markup=markup,
    )


@bot.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith("limpiar:")))
def callback_limpiar(call):
    if call.from_user.id != MI_ID or call.message.chat.id != MI_ID:
        bot.answer_callback_query(call.id, "Acción no autorizada.")
        return
    if call.data == "limpiar:cancelar":
        bot.edit_message_text("Limpieza cancelada.", call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id)
        return
    if call.data != "limpiar:confirmar":
        bot.answer_callback_query(call.id, "Acción no válida.")
        return
    bot.edit_message_text("Solicitud confirmada. El historial se limpiará en tu cuenta.", call.message.chat.id, call.message.message_id)
    bot.send_message(MI_ID, "/__limpiar_confirmado")
    bot.answer_callback_query(call.id)

# ==========================================
# 7. PLATAFORMAS Y ACTIVACION
# ==========================================
# Plataformas conocidas para detectar codigos
PLATAFORMAS = {
    "netflix": "Netflix", "spotify": "Spotify", "steam": "Steam",
    "amazon": "Amazon", "prime": "Amazon Prime", "disney": "Disney+",
    "youtube": "YouTube Premium", "xbox": "Xbox", "playstation": "PlayStation",
    "ps": "PlayStation", "nintendo": "Nintendo", "crunchyroll": "Crunchyroll",
    "hbo": "HBO Max", "max": "HBO Max", "apple": "Apple",
    "tidal": "Tidal", "deezer": "Deezer",
}

def detectar_plataforma(texto):
    import re
    texto_lower = texto.lower()
    for clave, nombre in PLATAFORMAS.items():
        if clave in texto_lower:
            return nombre
    if re.search(r'\b[A-Z0-9]{4,6}[-][A-Z0-9]{4,6}', texto):
        return "plataforma desconocida"
    return None

def teclado_responder(user_id):
    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton("🔑 Activación", callback_data=f"resp:act:{user_id}")
    )
    return markup

def pedir_plataforma_activacion(chat_id):
    """Pide al usuario que indique la plataforma cuando elige 'Activación'
    desde el menu de botones (en vez de escribirla directamente en el texto)."""
    bot.send_message(
        chat_id,
        "✦ ¿Para qué plataforma necesitas la activación?\n"
        "Escríbela (ej: Netflix, Disney+, HBO Max...)."
    )

# ==========================================
# 8. CALLBACK — BOTON RESPONDER
# ==========================================
@bot.callback_query_handler(func=lambda call: call.data.startswith("resp:"))
def callback_responder(call):
    if call.from_user.id != MI_ID:
        bot.answer_callback_query(call.id)
        return
    partes = call.data.split(":")
    tipo = partes[1]
    uid  = partes[2]
    etiqueta = "Activación" if tipo == "act" else "Respuesta directa"
    bot.answer_callback_query(call.id, f"Escribe tu respuesta ({etiqueta})")
    msg = bot.send_message(
        MI_ID,
        f"✍️ <b>{etiqueta}</b> — Escribe para <code>{uid}</code>:\n(o /cancelar para abortar)",
        parse_mode='HTML'
    )
    bot.register_next_step_handler(msg, lambda m: enviar_respuesta(m, uid, tipo))

def enviar_respuesta(message, uid, tipo):
    if message.text and message.text.strip() == "/cancelar":
        bot.send_message(MI_ID, "Respuesta cancelada.")
        return
    try:
        if tipo == "act":
            texto = (
                f"⬩➤{OWNER_DISPLAY_NAME}ᓚᘏᗢ\n"
                f"⟡{message.text}\n"
                f"⟡Eɴᴠɪᴀ Cᴀᴘᴛᴜʀᴀ O Fᴏᴛᴏ Aɢʀᴀᴅᴇᴄɪᴇɴᴅᴏ Cᴏɴ Mɪ Aʟɪᴀs {OWNER_ALIAS}"
            )
        else:
            texto = f"⬩➤{OWNER_DISPLAY_NAME}ᓚᘏᗢ\n{message.text}"
        bot.send_message(int(uid), texto, disable_web_page_preview=True)
        bot.send_message(MI_ID, f"✅ Enviado a <code>{uid}</code>.", parse_mode='HTML')
    except Exception:
        logger.exception("No se pudo enviar la respuesta privada")
        bot.send_message(MI_ID, f"No se pudo enviar el mensaje a <code>{html.escape(str(uid))}</code>.", parse_mode="HTML")

# ==========================================
# ==========================================
# 9. ARCHIVOS MULTIMEDIA
# ==========================================
@bot.message_handler(content_types=['photo'])
def recibir_foto(message):
    if message.chat.id == MI_ID:
        return
    uid    = message.from_user.id
    nombre = nombre_usuario(message.from_user)
    hora   = datetime.now().strftime("%H:%M")
    bot.forward_message(MI_ID, message.chat.id, message.message_id)
    bot.send_message(
        MI_ID,
        f"📷 <b>Foto recibida</b> [{hora}]\n"
        f"<a href='tg://user?id={uid}'>{html.escape(nombre)}</a>\n"
        f"ID: <code>{uid}</code>",
        parse_mode='HTML',
        reply_markup=teclado_responder(uid)
    )
    bot.reply_to(message, "✦ Imagen recibida, Sera revisada en breve.")

@bot.message_handler(content_types=['document'])
def recibir_documento(message):
    if message.chat.id == MI_ID:
        return
    uid    = message.from_user.id
    nombre = nombre_usuario(message.from_user)
    hora   = datetime.now().strftime("%H:%M")
    bot.forward_message(MI_ID, message.chat.id, message.message_id)
    bot.send_message(
        MI_ID,
        f"📎 <b>Archivo recibido</b> [{hora}]\n"
        f"<a href='tg://user?id={uid}'>{html.escape(nombre)}</a>\n"
        f"ID: <code>{uid}</code>",
        parse_mode='HTML',
        reply_markup=teclado_responder(uid)
    )
    bot.reply_to(message, "✦ Archivo recibido. Sera revisado.")


# ==========================================
# 11.1 MODERACION Y ADMINISTRACION DE CHATS
# ==========================================
MODERATION_RE = re.compile(
    r"^\s*[/.,]\s*(ban|tban|mute|tmute|unmute|restrict|kick|promote|demote|warn|unwarn|unban|warns|resetwarns)(?:@[A-Za-z0-9_]+)?(?:\s+|$)(.*)$",
    re.IGNORECASE | re.DOTALL,
)

PERMISOS_ADMIN = {
    "change_info": "can_change_info",
    "post_messages": "can_post_messages",
    "edit_messages": "can_edit_messages",
    "delete_messages": "can_delete_messages",
    "invite_users": "can_invite_users",
    "restrict_members": "can_restrict_members",
    "pin_messages": "can_pin_messages",
    "promote_members": "can_promote_members",
    "manage_chat": "can_manage_chat",
    "manage_video_chats": "can_manage_video_chats",
    "manage_voice_chats": "can_manage_voice_chats",
    "manage_topics": "can_manage_topics",
    "post_stories": "can_post_stories",
    "edit_stories": "can_edit_stories",
    "delete_stories": "can_delete_stories",
}


def usuario_es_admin(chat_id, user_id):
    if user_id == MI_ID:
        return True
    miembro = get_chat_member_cached(bot, chat_id, user_id)
    return miembro.status in ("administrator", "creator")


def resolver_objetivo(message, args):
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id, args
    if not args:
        return None, args
    target, _, remaining = args.partition(" ")
    if target.isdecimal():
        return int(target), remaining.strip()
    if target.startswith("@"):
        username = target[1:].lstrip("@").lower()
        rows = query(
            "SELECT user_id FROM chat_users WHERE chat_id=? AND lower(username)=? "
            "ORDER BY updated_at DESC LIMIT 2",
            (message.chat.id, username),
        )
        if len(rows) == 1:
            return int(rows[0][0]), remaining.strip()
        if len(rows) > 1:
            raise ValueError("Ese nombre de usuario aparece asociado a varios usuarios.")
        raise ValueError(
            "No conozco a ese usuario en este chat. Debe haber enviado un mensaje "
            "que el bot haya recibido o debes responder a uno de sus mensajes."
        )
    raise ValueError("Indica un ID numerico, un @usuario conocido o responde a su mensaje.")


def duracion_a_segundos(texto):
    coincidencia = re.fullmatch(r"(\d+)(s|m|h|d)", texto.strip().lower())
    if not coincidencia:
        return None
    cantidad = int(coincidencia.group(1))
    multiplicador = {"s": 1, "m": 60, "h": 3600, "d": 86400}[coincidencia.group(2)]
    total = cantidad * multiplicador
    if total < 30 or total > 366 * 86400:
        raise ValueError("La duracion debe estar entre 30 segundos y 366 dias.")
    return total


def _registrar_accion_mod(chat_id, actor_id, target_id, action, reason="", duration_seconds=None):
    log_mod_action(bot, chat_id, actor_id, target_id, action, reason, duration_seconds)


def _motivo_y_duracion(comando, texto):
    texto = texto.strip()
    duracion = None
    motivo = texto
    if comando in {"tban", "tmute"}:
        partes = texto.split(maxsplit=1)
        duracion = duracion_a_segundos(partes[0]) if partes else None
        if duracion is None:
            uso = "/tban 1d" if comando == "tban" else "/tmute 30m"
            raise ValueError(f"Uso: responde al mensaje con {uso} motivo, o indica ID y duración.")
        motivo = partes[1].strip() if len(partes) > 1 else ""
    elif comando in {"mute", "restrict", "ban"}:
        partes = texto.split(maxsplit=1)
        if partes:
            posible = duracion_a_segundos(partes[0])
            if posible:
                duracion = posible
                motivo = partes[1].strip() if len(partes) > 1 else ""
            elif partes[0][0].isdigit():
                raise ValueError("Duración inválida. Usa s, m, h o d; por ejemplo, 30m o 2h.")
    return motivo or "Sin motivo indicado", duracion


def _aplicar_accion_umbral(chat_id, target_id, action):
    if action == "ban":
        bot.ban_chat_member(chat_id, target_id)
    elif action == "kick":
        bot.ban_chat_member(chat_id, target_id)
        bot.unban_chat_member(chat_id, target_id)
    elif action == "mute":
        permisos = telebot.types.ChatPermissions(
            can_send_messages=False, can_send_media_messages=False, can_send_audios=False,
            can_send_documents=False, can_send_photos=False, can_send_videos=False,
            can_send_video_notes=False, can_send_voice_notes=False, can_send_polls=False,
            can_send_other_messages=False, can_add_web_page_previews=False,
        )
        bot.restrict_chat_member(chat_id, target_id, permissions=permisos)


def procesar_comando_moderacion(message):
    coincidencia = MODERATION_RE.match(message.text or "")
    if not coincidencia:
        return
    comando = coincidencia.group(1).lower()
    args = coincidencia.group(2).strip()
    if message.chat.type == "private":
        bot.reply_to(message, "Este comando debe usarse dentro del grupo.")
        return
    try:
        if not usuario_es_admin(message.chat.id, message.from_user.id):
            bot.reply_to(message, "Solo los administradores del chat pueden usar este comando.")
            return

        objetivo, resto = resolver_objetivo(message, args)
        if objetivo is None:
            bot.reply_to(message, f"Uso: responde al usuario o indica /{comando} ID o @usuario.")
            return
        if objetivo == message.from_user.id:
            bot.reply_to(message, "No puedes aplicar esta acción sobre ti mismo.")
            return

        sanciones = {"ban", "tban", "mute", "tmute", "restrict", "kick", "warn", "unwarn", "resetwarns"}
        miembro_objetivo = get_chat_member_cached(bot, message.chat.id, objetivo)
        if comando in sanciones and miembro_objetivo.status in ("administrator", "creator"):
            bot.reply_to(message, "No se puede sancionar ni modificar las advertencias de un administrador.")
            return

        if comando in {"warn", "unwarn", "warns", "resetwarns"}:
            if comando == "warn":
                motivo = resto or "Sin motivo indicado"
                con = get_db()
                try:
                    con.execute(
                        "INSERT INTO warnings(chat_id,user_id,count) VALUES(?,?,1) "
                        "ON CONFLICT(chat_id,user_id) DO UPDATE SET count=count+1",
                        (message.chat.id, objetivo),
                    )
                    cuenta = int(con.execute(
                        "SELECT count FROM warnings WHERE chat_id=? AND user_id=?",
                        (message.chat.id, objetivo),
                    ).fetchone()[0])
                    con.commit()
                finally:
                    con.close()
                limite = int(get_chat_setting(message.chat.id, "warn_limit", "3"))
                accion_umbral = get_chat_setting(message.chat.id, "warn_action", "ban")
                _registrar_accion_mod(message.chat.id, message.from_user.id, objetivo, "warn", motivo)
                aviso = f"Advertencia {cuenta}/{limite} para <code>{objetivo}</code>. Motivo: {html.escape(motivo[:500])}."
                if cuenta >= limite:
                    _aplicar_accion_umbral(message.chat.id, objetivo, accion_umbral)
                    _registrar_accion_mod(
                        message.chat.id, message.from_user.id, objetivo,
                        f"warn_limit_{accion_umbral}", f"Límite de {limite} advertencias alcanzado",
                    )
                    aviso += f" Se aplicó la acción configurada: <b>{html.escape(accion_umbral)}</b>."
                bot.reply_to(message, aviso, parse_mode="HTML")
                return

            if comando == "warns":
                con = get_db()
                try:
                    fila = con.execute(
                        "SELECT count FROM warnings WHERE chat_id=? AND user_id=?",
                        (message.chat.id, objetivo),
                    ).fetchone()
                    acciones = con.execute(
                        "SELECT actor_id,reason,created_at FROM mod_actions "
                        "WHERE chat_id=? AND target_user_id=? AND action='warn' "
                        "ORDER BY id DESC LIMIT 10",
                        (message.chat.id, objetivo),
                    ).fetchall()
                finally:
                    con.close()
                cuenta = int(fila[0]) if fila else 0
                limite = int(get_chat_setting(message.chat.id, "warn_limit", "3"))
                lineas = [f"<b>Advertencias:</b> {cuenta}/{limite} para <code>{objetivo}</code>"]
                for actor, motivo, fecha in acciones:
                    lineas.append(f"• {html.escape(fecha)} · por <code>{actor}</code>: {html.escape((motivo or 'Sin motivo')[:200])}")
                bot.reply_to(message, "\n".join(lineas), parse_mode="HTML")
                return

            con = get_db()
            try:
                con.execute("DELETE FROM warnings WHERE chat_id=? AND user_id=?", (message.chat.id, objetivo))
                con.commit()
            finally:
                con.close()
            accion = "unwarn" if comando == "unwarn" else "resetwarns"
            _registrar_accion_mod(message.chat.id, message.from_user.id, objetivo, accion, resto or "")
            bot.reply_to(message, f"Advertencias de <code>{objetivo}</code> eliminadas.", parse_mode="HTML")
            return

        if comando in {"ban", "tban", "mute", "tmute", "restrict", "kick"}:
            motivo, duracion = _motivo_y_duracion(comando, resto)
            hasta = int(time.time()) + duracion if duracion else None
            if comando in {"ban", "tban"}:
                bot.ban_chat_member(message.chat.id, objetivo, until_date=hasta)
                respuesta = f"Usuario <code>{objetivo}</code> baneado. Motivo: {html.escape(motivo[:500])}."
            elif comando == "kick":
                bot.ban_chat_member(message.chat.id, objetivo)
                bot.unban_chat_member(message.chat.id, objetivo)
                respuesta = f"Usuario <code>{objetivo}</code> expulsado. Motivo: {html.escape(motivo[:500])}."
            else:
                permisos = telebot.types.ChatPermissions(
                    can_send_messages=False, can_send_media_messages=False, can_send_audios=False,
                    can_send_documents=False, can_send_photos=False, can_send_videos=False,
                    can_send_video_notes=False, can_send_voice_notes=False, can_send_polls=False,
                    can_send_other_messages=False, can_add_web_page_previews=False,
                )
                bot.restrict_chat_member(message.chat.id, objetivo, until_date=hasta, permissions=permisos)
                respuesta = f"Usuario <code>{objetivo}</code> silenciado/restringido. Motivo: {html.escape(motivo[:500])}."
            _registrar_accion_mod(message.chat.id, message.from_user.id, objetivo, comando, motivo, duracion)
            bot.reply_to(message, respuesta, parse_mode="HTML")
            return

        if comando == "unban":
            motivo = resto or "Sin motivo indicado"
            bot.unban_chat_member(message.chat.id, objetivo, only_if_banned=True)
            _registrar_accion_mod(message.chat.id, message.from_user.id, objetivo, "unban", motivo)
            bot.reply_to(message, f"Se retiró el baneo de <code>{objetivo}</code>.", parse_mode="HTML")
            return

        if comando == "unmute":
            permisos = telebot.types.ChatPermissions(
                can_send_messages=True, can_send_media_messages=True, can_send_audios=True,
                can_send_documents=True, can_send_photos=True, can_send_videos=True,
                can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True,
                can_send_other_messages=True, can_add_web_page_previews=True,
            )
            bot.restrict_chat_member(message.chat.id, objetivo, permissions=permisos)
            _registrar_accion_mod(message.chat.id, message.from_user.id, objetivo, "unmute", resto or "")
            bot.reply_to(message, f"Se retiró el silencio de <code>{objetivo}</code>.", parse_mode="HTML")
            return

        if comando in {"demote", "promote"}:
            derechos = {nombre_api: False for nombre_api in PERMISOS_ADMIN.values()}
            if comando == "demote":
                bot.promote_chat_member(message.chat.id, objetivo, **derechos)
                respuesta = f"Se retiraron los permisos de administrador a {objetivo}."
            else:
                palabras = [p.strip().lower() for p in resto.replace(",", " ").split()]
                for palabra in palabras:
                    normalizado = palabra.removeprefix("can_")
                    if normalizado not in PERMISOS_ADMIN:
                        raise ValueError("Permisos inválidos. Usa: " + ", ".join(sorted(PERMISOS_ADMIN)) + ".")
                    derechos[PERMISOS_ADMIN[normalizado]] = True
                if not palabras:
                    derechos["can_manage_chat"] = True
                bot.promote_chat_member(message.chat.id, objetivo, **derechos)
                respuesta = f"Usuario {objetivo} promovido."
            _registrar_accion_mod(message.chat.id, message.from_user.id, objetivo, comando, resto)
            bot.reply_to(message, respuesta)
            return

    except ValueError as exc:
        bot.reply_to(message, html.escape(str(exc)))
    except Exception:
        logger.exception("No se pudo completar la acción de moderación")
        bot.reply_to(message, "No se pudo completar la acción. Revisa los permisos del bot y del usuario objetivo.")


def permisos_bot_comando_moderacion(message):
    coincidencia = MODERATION_RE.match(message.text or "")
    if not coincidencia:
        return ()
    comando = coincidencia.group(1).lower()
    if comando in {"ban", "tban", "kick", "mute", "tmute", "unmute", "restrict", "unban", "warn"}:
        return ("can_restrict_members",)
    if comando in {"promote", "demote"}:
        return ("can_promote_members",)
    return ()


@bot.message_handler(func=lambda m: bool(m.text and MODERATION_RE.match(m.text)))
@require_permission(
    bot, MI_ID, role="admin",
    denial_text="Solo los administradores del chat pueden usar este comando.",
    bot_permissions=permisos_bot_comando_moderacion,
)
def comandos_moderacion(message):
    if not module_enabled_for_message(message, "moderation"):
        return
    procesar_comando_moderacion(message)


@bot.message_handler(commands=["warnconfig"])
@require_permission(
    bot, MI_ID, role="admin", bot_permissions=("can_restrict_members",),
    denial_text="Solo un administrador puede configurar las advertencias.",
)
def cmd_warnconfig(message):
    try:
        if message.chat.type == "private" or not module_enabled_for_message(message, "moderation"):
            return
        parts = (message.text or "").split()
        if len(parts) == 1:
            limit = get_chat_setting(message.chat.id, "warn_limit", "3")
            action = get_chat_setting(message.chat.id, "warn_action", "ban")
            bot.reply_to(message, f"Configuración: {limit} advertencias → {action}.\nUso: /warnconfig <límite> <mute|kick|ban>")
            return
        if len(parts) != 3 or not parts[1].isdigit() or not 1 <= int(parts[1]) <= 20:
            bot.reply_to(message, "Uso: /warnconfig <límite 1-20> <mute|kick|ban>")
            return
        action = parts[2].lower()
        if action not in {"mute", "kick", "ban"}:
            bot.reply_to(message, "La acción debe ser mute, kick o ban.")
            return
        set_chat_setting(message.chat.id, "warn_limit", parts[1])
        set_chat_setting(message.chat.id, "warn_action", action)
        bot.reply_to(message, f"Umbral guardado: {parts[1]} advertencias; acción: {action}.")
    except Exception:
        logger.exception("Falló /warnconfig")
        bot.reply_to(message, "No pude guardar la configuración de advertencias.")


@bot.message_handler(commands=["purge"])
@require_permission(
    bot, MI_ID, role="admin", bot_permissions=("can_delete_messages",),
    denial_text="Solo administradores pueden purgar mensajes; el bot también necesita permiso para borrarlos.",
)
def cmd_purge(message):
    try:
        if message.chat.type == "private" or not module_enabled_for_message(message, "moderation"):
            return
        parts = (message.text or "").split()
        if message.reply_to_message:
            start_id = message.reply_to_message.message_id
            end_id = message.message_id
        else:
            count = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 0
            if not 1 <= count <= 100:
                bot.reply_to(message, "Uso: responde al mensaje inicial con /purge o usa /purge <1-100>.")
                return
            start_id = max(1, message.message_id - count + 1)
            end_id = message.message_id
        if end_id - start_id > 99:
            start_id = end_id - 99
        removed = 0
        for message_id in range(start_id, end_id + 1):
            try:
                bot.delete_message(message.chat.id, message_id)
                removed += 1
            except Exception:
                logger.debug("No se pudo borrar message_id=%s durante /purge", message_id, exc_info=True)
        bot.send_message(message.chat.id, f"Se eliminaron {removed} mensajes.")
        _registrar_accion_mod(message.chat.id, message.from_user.id, None, "purge", f"{removed} mensajes")
    except Exception:
        logger.exception("Falló /purge")
        bot.reply_to(message, "No pude completar la purga.")


def _notify_group_admins(message, reason, action="admin_request", target_id=None):
    admins = bot.get_chat_administrators(message.chat.id)
    mentions = []
    for item in admins:
        user = item.user
        if user.is_bot:
            continue
        name = html.escape(user.first_name or "Administrador")
        mentions.append(f'<a href="tg://user?id={user.id}">{name}</a>')
    if not mentions:
        bot.reply_to(message, "No pude encontrar administradores disponibles.")
        return
    body = html.escape(reason.strip()[:2000]) if reason.strip() else "Sin motivo indicado"
    notification = f"🔔 <b>Solicitud de ayuda</b>\nMotivo: {body}\n" + " ".join(mentions)
    if target_id:
        notification += f"\nMensaje reportado de <code>{target_id}</code>."
    bot.send_message(message.chat.id, notification, parse_mode="HTML", reply_to_message_id=message.message_id)
    actor_id = message.from_user.id if message.from_user else 0
    _registrar_accion_mod(message.chat.id, actor_id, target_id, action, reason)


@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    try:
        if message.chat.type == "private" or not module_enabled_for_message(message, "moderation"):
            return
        reason = (message.text or "").partition(" ")[2].strip()
        _notify_group_admins(message, reason)
    except Exception:
        logger.exception("Falló /admin")
        bot.reply_to(message, "No pude avisar a los administradores.")


@bot.message_handler(commands=["report"])
def cmd_report(message):
    try:
        if message.chat.type == "private" or not module_enabled_for_message(message, "moderation"):
            return
        if not message.reply_to_message:
            bot.reply_to(message, "Responde al mensaje que quieres reportar con /report motivo.")
            return
        reason = (message.text or "").partition(" ")[2].strip()
        author = message.reply_to_message.from_user
        target_id = author.id if author else None
        _notify_group_admins(message, reason, action="report", target_id=target_id)
    except Exception:
        logger.exception("Falló /report")
        bot.reply_to(message, "No pude enviar el reporte.")


@bot.message_handler(func=lambda m: bool(
    m.text and not m.text.lstrip().startswith("/")
    and re.search(r"(?<!\w)@admin\b", m.text, re.IGNORECASE)
))
def mention_admins(message):
    try:
        if message.chat.type == "private" or not module_enabled_for_message(message, "moderation"):
            return
        reason = re.sub(r"(?<!\w)@admin\b", "", message.text, flags=re.IGNORECASE).strip()
        _notify_group_admins(message, reason, action="admin_mention")
    except Exception:
        logger.exception("Falló la notificación @admin")


@bot.message_handler(commands=["settitle"])
@require_permission(
    bot, MI_ID, role="admin", bot_permissions=("can_change_info",),
    denial_text="El administrador y el bot necesitan permiso para cambiar la información del grupo.",
)
def cmd_settitle(message):
    try:
        if message.chat.type == "private" or not module_enabled_for_message(message, "moderation"):
            return
        title = (message.text or "").partition(" ")[2].strip()
        if not title:
            bot.reply_to(message, "Uso: /settitle <nuevo título del grupo>")
            return
        bot.set_chat_title(message.chat.id, title)
        _registrar_accion_mod(message.chat.id, message.from_user.id, None, "settitle", title)
        bot.reply_to(message, "Título del grupo actualizado.")
    except Exception:
        logger.exception("Falló /settitle")
        bot.reply_to(message, "No pude cambiar el título del grupo.")


@bot.message_handler(commands=["setdescr"])
@require_permission(
    bot, MI_ID, role="admin", bot_permissions=("can_change_info",),
    denial_text="El administrador y el bot necesitan permiso para cambiar la información del grupo.",
)
def cmd_setdescr(message):
    try:
        if message.chat.type == "private" or not module_enabled_for_message(message, "moderation"):
            return
        description = (message.text or "").partition(" ")[2].strip()
        if not description:
            bot.reply_to(message, "Uso: /setdescr <descripción del grupo>")
            return
        bot.set_chat_description(message.chat.id, description)
        _registrar_accion_mod(message.chat.id, message.from_user.id, None, "setdescr", description)
        bot.reply_to(message, "Descripción del grupo actualizada.")
    except Exception:
        logger.exception("Falló /setdescr")
        bot.reply_to(message, "No pude cambiar la descripción del grupo.")


@bot.message_handler(commands=["setgtitle"])
@require_permission(
    bot, MI_ID, role="admin", bot_permissions=("can_promote_members",),
    denial_text="El administrador y el bot necesitan permiso para cambiar el título de administrador.",
)
def cmd_setgtitle(message):
    try:
        if message.chat.type == "private" or not module_enabled_for_message(message, "moderation"):
            return
        title = (message.text or "").partition(" ")[2].strip()
        target = message.reply_to_message.from_user if message.reply_to_message else None
        if not target or not title:
            bot.reply_to(message, "Responde al administrador con /setgtitle <título de hasta 16 caracteres>.")
            return
        if len(title) > 16:
            bot.reply_to(message, "El título de administrador no puede superar 16 caracteres.")
            return
        member = get_chat_member_cached(bot, message.chat.id, target.id)
        if member.status != "administrator":
            bot.reply_to(message, "El usuario debe ser administrador del grupo.")
            return
        bot.set_chat_administrator_custom_title(message.chat.id, target.id, title)
        _registrar_accion_mod(message.chat.id, message.from_user.id, target.id, "setgtitle", title)
        bot.reply_to(message, "Título de administrador actualizado.")
    except Exception:
        logger.exception("Falló /setgtitle")
        bot.reply_to(message, "No pude cambiar el título de administrador.")


# ==========================================
# 11.2 COMANDOS PERSONALIZADOS PERSISTENTES
# ==========================================
COMANDOS_RESERVADOS = {
    "start", "help", "activar", "info", "id", "limpiar", "q",
    "uinfo", "chatid", "chatlink", "staff", "rules", "sticker", "idsticker", "getsticker",
    "tr", "tts", "clima", "calc", "wiki", "recordar", "afk", "encuesta",
    "quiz", "ship", "verdadoreto", "abrazar", "acariciar", "saludar", "besar", "aplaudir", "bailar", "llorar", "reir", "guinar", "sonrojar", "golpear", "morder", "8ball", "dado", "top", "ban", "tban", "mute", "tmute", "unmute",
    "restrict", "kick", "promote", "demote", "warn", "unwarn", "unban", "warns",
    "resetwarns", "warnconfig", "purge", "report", "stats", "broadcast", "gban", "ungban", "backup", "maintenance", "grupos", "logs", "admin", "settitle", "setgtitle",
    "setdescr", "setwelcome", "setgoodbye", "captchaconfig", "antiraid", "filter", "unfilter",
    "save", "get", "antilinks", "floodconfig", "blockword", "blockcontent",
    "del", "pin", "unpin", "unallpin", "admin", "msg", "copy", "forward",
    "welcome", "captcha", "setrules", "clearrules", "spam", "addcmd", "delcmd", "listcmd",
}
COMANDO_SLASH_RE = re.compile(r"^\s*/([A-Za-z][A-Za-z0-9_]{0,31})(?:@[A-Za-z0-9_]+)?(?:\s+|$)(.*)$", re.DOTALL)


def comando_personalizado(message):
    coincidencia = COMANDO_SLASH_RE.match(message.text or "")
    if not coincidencia:
        return False
    nombre = coincidencia.group(1).lower()
    if nombre in {"addcmd", "delcmd", "listcmd"}:
        return True
    if nombre in COMANDOS_RESERVADOS:
        return False
    return bool(query("SELECT 1 FROM custom_commands WHERE command=?", (nombre,)))


def procesar_comando_personalizado(message):
    coincidencia = COMANDO_SLASH_RE.match(message.text or "")
    if not coincidencia:
        return
    nombre = coincidencia.group(1).lower()
    argumentos = coincidencia.group(2).strip()
    if nombre in {"addcmd", "delcmd", "listcmd"}:
        if not message.from_user or message.from_user.id != MI_ID:
            bot.reply_to(message, "Solo el propietario puede administrar comandos personalizados.")
            return
        if nombre == "addcmd":
            if "|" not in argumentos:
                bot.reply_to(message, "Uso: /addcmd nombre | respuesta")
                return
            comando, respuesta = (parte.strip() for parte in argumentos.split("|", 1))
            comando = comando.lstrip("/").lower()
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", comando):
                bot.reply_to(message, "El nombre debe iniciar con una letra y contener solo letras, numeros o guion bajo.")
                return
            if comando in COMANDOS_RESERVADOS:
                bot.reply_to(message, "Ese nombre esta reservado por un comando del sistema.")
                return
            if not respuesta:
                bot.reply_to(message, "La respuesta no puede estar vacia.")
                return
            con = get_db()
            try:
                con.execute(
                    """INSERT INTO custom_commands (command, response) VALUES (?, ?)
                       ON CONFLICT(command) DO UPDATE SET
                         response=excluded.response, updated_at=CURRENT_TIMESTAMP""",
                    (comando, respuesta),
                )
                con.commit()
            finally:
                con.close()
            registrar_comandos_menu()
            bot.reply_to(message, f"Comando /{comando} guardado y disponible.")
            return
        if nombre == "delcmd":
            comando = argumentos.lstrip("/").lower().split(maxsplit=1)[0] if argumentos else ""
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", comando):
                bot.reply_to(message, "Uso: /delcmd nombre")
                return
            con = get_db()
            try:
                cursor = con.execute("DELETE FROM custom_commands WHERE command=?", (comando,))
                con.commit()
                eliminado = cursor.rowcount
            finally:
                con.close()
            if eliminado:
                registrar_comandos_menu()
            bot.reply_to(message, f"Comando /{comando} eliminado." if eliminado else f"No existe /{comando}.")
            return
        filas = query("SELECT command FROM custom_commands ORDER BY command")
        lista = "\n".join("/" + str(fila[0]) for fila in filas) if filas else "No hay comandos personalizados."
        bot.reply_to(message, lista)
        return

    filas = query("SELECT response FROM custom_commands WHERE command=?", (nombre,))
    if not filas:
        return
    respuesta = str(filas[0][0])
    respuesta = respuesta.replace("{args}", argumentos)
    respuesta = respuesta.replace("{user}", str(message.from_user.id if message.from_user else ""))
    respuesta = respuesta.replace("{first_name}", message.from_user.first_name or "" if message.from_user else "")
    bot.reply_to(message, respuesta)


@bot.message_handler(func=comando_personalizado)
def comandos_personalizados(message):
    if not module_enabled_for_message(message, "custom"):
        return
    procesar_comando_personalizado(message)


# ==========================================
# 11.3 STICKER DE CITA
# ==========================================
def _fuente_cita(ImageFont, tamano):
    candidatos = [
        "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for ruta in candidatos:
        try:
            return ImageFont.truetype(ruta, tamano)
        except OSError:
            continue
    return ImageFont.load_default()


def _ajustar_lineas(draw, texto, fuente, ancho):
    palabras = texto.split()
    lineas = []
    actual = ""
    for palabra in palabras:
        prueba = f"{actual} {palabra}".strip()
        if actual and draw.textbbox((0, 0), prueba, font=fuente)[2] > ancho:
            lineas.append(actual)
            actual = palabra
        else:
            actual = prueba
    if actual:
        lineas.append(actual)
    return lineas


def crear_sticker_cita(texto, nombre, foto_bytes=None):
    from PIL import Image, ImageDraw, ImageFont

    lienzo = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    draw = ImageDraw.Draw(lienzo)
    draw.rounded_rectangle((10, 10, 502, 502), radius=34, fill=(35, 39, 47, 255))

    avatar = None
    if foto_bytes:
        try:
            foto_bytes.seek(0)
            avatar = Image.open(foto_bytes).convert("RGB")
            lado = min(avatar.size)
            izquierda = (avatar.width - lado) // 2
            arriba = (avatar.height - lado) // 2
            avatar = avatar.crop((izquierda, arriba, izquierda + lado, arriba + lado)).resize((58, 58))
            mascara = Image.new("L", (58, 58), 0)
            ImageDraw.Draw(mascara).ellipse((0, 0, 57, 57), fill=255)
            lienzo.paste(avatar, (30, 29), mascara)
        except Exception:
            avatar = None
    if avatar is None:
        inicial = (nombre.strip()[:1] or "?").upper()
        draw.ellipse((30, 29, 88, 87), fill=(88, 101, 242, 255))
        draw.text((49, 39), inicial, font=_fuente_cita(ImageFont, 28), fill=(255, 255, 255, 255))

    fuente_nombre = _fuente_cita(ImageFont, 23)
    nombre_visible = nombre.strip()[:34] or "Usuario"
    draw.text((104, 31), nombre_visible, font=fuente_nombre, fill=(255, 255, 255, 255))
    draw.line((30, 105, 482, 105), fill=(76, 82, 94, 255), width=2)

    limpio = " ".join(texto.split())
    fuente_texto = None
    lineas = []
    for tamano in range(27, 15, -1):
        candidata = _fuente_cita(ImageFont, tamano)
        lineas_candidatas = _ajustar_lineas(draw, limpio, candidata, 432)
        if len(lineas_candidatas) <= 18 and len(lineas_candidatas) * (tamano + 7) <= 350:
            fuente_texto, lineas = candidata, lineas_candidatas
            break
    if fuente_texto is None:
        fuente_texto = _fuente_cita(ImageFont, 16)
        lineas = _ajustar_lineas(draw, limpio, fuente_texto, 432)
        lineas = lineas[:18]
        if lineas:
            lineas[-1] = lineas[-1].rstrip(".,;:!? ") + "..."

    y = 128
    for linea in lineas:
        draw.text((40, y), linea, font=fuente_texto, fill=(239, 241, 245, 255))
        y += 20 if fuente_texto.size <= 16 else fuente_texto.size + 7

    salida = io.BytesIO()
    lienzo.save(salida, format="WEBP", quality=88, method=6)
    contenido = salida.getvalue()
    if len(contenido) > 512 * 1024:
        salida = io.BytesIO()
        lienzo.save(salida, format="WEBP", quality=68, method=6)
        contenido = salida.getvalue()
    if len(contenido) > 512 * 1024:
        raise ValueError("El sticker supera el limite de tamano de Telegram.")
    salida = io.BytesIO(contenido)
    salida.name = "quote.webp"
    return salida


@bot.message_handler(commands=["q"])
def cmd_quote(message):
    if not module_enabled_for_message(message, "quote"):
        return
    citado = message.reply_to_message
    if not citado or not citado.text:
        bot.reply_to(message, "Responde con /q a un mensaje de texto para crear el sticker.")
        return
    try:
        usuario = citado.from_user
        nombre = " ".join(
            parte for parte in [usuario.first_name, usuario.last_name] if parte
        ) if usuario else "Usuario"
        foto = None
        if usuario:
            try:
                fotos = bot.get_user_profile_photos(usuario.id, limit=1)
                if fotos.total_count and fotos.photos:
                    archivo = bot.get_file(fotos.photos[0][-1].file_id)
                    foto = io.BytesIO(bot.download_file(archivo.file_path))
            except Exception:
                foto = None
        sticker = crear_sticker_cita(citado.text, nombre, foto)
        bot.send_sticker(message.chat.id, sticker, reply_to_message_id=message.message_id)
    except ImportError:
        bot.reply_to(message, "No esta instalada Pillow. Actualiza las dependencias del bot.")
    except Exception as exc:
        logger.exception("No se pudo crear el sticker de cita")
        bot.reply_to(message, "No se pudo crear el sticker. Inténtalo de nuevo más tarde.")

# Estado de espera de codigo por usuario: {user_id: plataforma_detectada_o_None}
esperando_plataforma, esperando_codigo = load_activation_states()

# Usuarios que eligieron el boton "Activacion" en /help y deben indicar
# todavia para que plataforma es (paso previo a esperando_codigo)
# Ambos estados se restauran desde SQLite para sobrevivir reinicios de Railway.

# ==========================================
# 12. MENSAJES DE TEXTO — LOGICA PRINCIPAL
# ==========================================
@bot.message_handler(func=lambda m: True)
def guardaespaldas(message):
    # Los comandos de grupo se manejan por separado; no deben pasar por el
    # flujo privado de recepción y reenvío de mensajes.
    if message.chat.type != "private":
        return
    if message.chat.id == MI_ID:
        bot.send_message(MI_ID, "Comando no reconocido. Usa /help para ver las opciones.")
        return

    uid      = message.from_user.id
    u        = message.from_user
    nombre   = u.first_name or "Sin nombre"
    username = f"@{u.username}" if u.username else "Sin usuario"
    nombre_completo = nombre_usuario(u)
    texto  = message.text or "[sin texto]"
    hora   = datetime.now().strftime("%H:%M")

    def bloque_activacion(plataforma, codigo):
        linea = "══════════════════════"
        return (
            f"<pre>╔{linea}╗\n"
            f"╰┈➤Plataforma: {plataforma}\n"
            f"╰┈➤Codigo: {html.escape(codigo)}\n"
            f"╚{linea}╝\n"
            f"╔{linea}╗\n"
            f"╰┈➤ID➟ {uid}\n"
            f"╰┈➤Nombre➟ {html.escape(nombre)}\n"
            f"╰┈➤Usuario➟ {html.escape(username)}\n"
            f"╚{linea}╝</pre>"
        )

    guardar_mensaje(uid, nombre, texto)

    # --- Usuario eligio "Activacion" en el menu y ahora indica la plataforma ---
    if uid in esperando_plataforma:
        esperando_plataforma.discard(uid)
        plataforma = detectar_plataforma(texto) or texto.strip()
        esperando_codigo[uid] = plataforma
        save_activation_state(uid, "codigo", plataforma)
        bot.reply_to(message,
            f"✦ Entendido, Por favor envia tu codigo de activacion de {plataforma}.\n"
            "✦ Es el numero que aparece en tu pantalla al momento de activar."
        )
        bot.send_message(
            MI_ID,
            f"⏳ <b>Esperando codigo</b> — {html.escape(str(plataforma))}\n"
            f"╰┈➤<b>𝐈𝐃</b>➟ <code>{uid}</code>\n"
            f"╰┈➤<b>𝐍𝐨𝐦𝐛𝐫𝐞</b>➟ <a href='tg://user?id={uid}'>{html.escape(nombre)}</a>\n"
            f"╰┈➤<b>𝐔𝐬𝐮𝐚𝐫𝐢𝐨</b>➟ {html.escape(username)}",
            parse_mode='HTML',
            reply_markup=teclado_responder(uid)
        )
        return

    # --- Usuario en modo espera de codigo ---
    if uid in esperando_codigo:
        plataforma_pendiente = esperando_codigo[uid]
        # Verificar si el mensaje es un codigo numerico (6-10 digitos)
        import re
        if re.fullmatch(r'\d{6,10}', texto.strip()):
            del esperando_codigo[uid]
            clear_activation_state(uid)
            bot.reply_to(message,
                f"✦ Codigo de {plataforma_pendiente} recibido.\n"
                f"✦ Espera un momento mientras {OWNER_DISPLAY_NAME} lo activa para ti."
            )
            bot.send_message(
                MI_ID,
                bloque_activacion(plataforma_pendiente, texto.strip()),
                parse_mode='HTML',
                reply_markup=teclado_responder(uid)
            )
        else:
            # Sigue esperando codigo
            bot.reply_to(message,
                "✦ Por favor envia tu codigo de activacion.\n"
                "Debe ser el numero que te aparece en tu pantalla (generalmente 8 digitos)."
            )
            bot.forward_message(MI_ID, message.chat.id, message.message_id)
            bot.send_message(
                MI_ID,
                f"⏳ <b>Aun esperando codigo</b> — {plataforma_pendiente}\n"
                f"╰┈➤<b>𝐈𝐃</b>➟ <code>{uid}</code>\n"
                f"╰┈➤<b>𝐍𝐨𝐦𝐛𝐫𝐞</b>➟ <a href='tg://user?id={uid}'>{html.escape(nombre)}</a>\n"
                f"╰┈➤Escribio: {html.escape(texto)}",
                parse_mode='HTML',
                reply_markup=teclado_responder(uid)
            )
        return

    # --- Detectar mencion de plataforma sin codigo ---
    plataforma = detectar_plataforma(texto)
    import re
    tiene_codigo = bool(re.search(r'\d{6,10}', texto))

    if plataforma and tiene_codigo:
        # Mensaje ya trae el codigo directamente
        bot.reply_to(message,
            f"✦ Codigo de {plataforma} recibido.\n"
            f"✦ Espera un momento mientras {OWNER_DISPLAY_NAME} lo activa para ti."
        )
        bot.send_message(
            MI_ID,
            bloque_activacion(plataforma, texto),
            parse_mode='HTML',
            reply_markup=teclado_responder(uid)
        )
        return

    if plataforma and not tiene_codigo:
        # Menciono la plataforma pero no envio codigo todavia — pedir codigo
        esperando_codigo[uid] = plataforma
        save_activation_state(uid, "codigo", plataforma)
        bot.reply_to(message,
            f"✦ Entendido, Por favor envia tu codigo de activacion de {plataforma}.\n"
            "✦ Es el numero que aparece en tu pantalla al momento de activar."
        )
        bot.forward_message(MI_ID, message.chat.id, message.message_id)
        bot.send_message(
            MI_ID,
            f"⏳ <b>Esperando codigo</b> — {plataforma}\n"
            f"╰┈➤<b>𝐈𝐃</b>➟ <code>{uid}</code>\n"
            f"╰┈➤<b>𝐍𝐨𝐦𝐛𝐫𝐞</b>➟ <a href='tg://user?id={uid}'>{html.escape(nombre)}</a>\n"
            f"╰┈➤<b>𝐔𝐬𝐮𝐚𝐫𝐢𝐨</b>➟ {html.escape(username)}",
            parse_mode='HTML',
            reply_markup=teclado_responder(uid)
        )
        return

    # --- Mensaje normal ---
    if not ya_fue_notificado(message.chat.id):
        bot.reply_to(message,
            "✦ Tu mensaje fue recibido.\n"
            f"{OWNER_DISPLAY_NAME} te respondera en cuanto pueda.\n\n"
            "Usa /help para consultar las funciones disponibles.\n"
            "No es necesario escribir de nuevo."
        )
        marcar_notificado(message.chat.id)

    bot.forward_message(MI_ID, message.chat.id, message.message_id)
    bot.send_message(
        MI_ID,
        f"💬 <b>Nuevo mensaje</b> [{hora}]\n"
        f"<a href='tg://user?id={uid}'>{html.escape(nombre)}</a>\n"
        f"ID: <code>{uid}</code>",
        parse_mode='HTML',
        reply_markup=teclado_responder(uid)
    )


# ==========================================
# 13. ARRANQUE
# ==========================================
def run_bot():
    logger.info("Iniciando Flamie Bot: moderación, stickers y activación")
    start_captcha_sweeper(bot)
    start_reminder_worker(bot)
    registrar_comandos_menu()
    bot.infinity_polling(
        timeout=10,
        long_polling_timeout=5,
        allowed_updates=["message", "edited_message", "callback_query", "chat_member", "my_chat_member", "chat_join_request"],
    )
