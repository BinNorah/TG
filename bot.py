import os
import telebot
import sqlite3
from datetime import date, datetime
from pathlib import Path
import html
import threading
import io
import re
import time
import random

# ==========================================
# 1. CREDENCIALES Y CONFIGURACION
# ==========================================
BASE_DIR = Path(__file__).resolve().parent


def cargar_env_local():
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for linea in env_path.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        os.environ.setdefault(clave.strip(), valor.strip().strip('"').strip("'"))


cargar_env_local()


def _require_env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Falta la variable de entorno: {name}")
    return value


TOKEN = _require_env("TELEGRAM_BOT_TOKEN")
MI_ID = int(_require_env("TELEGRAM_OWNER_ID"))
# DATA_DIR se puede sobreescribir con variable de entorno para Railway Volume
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "mensajes_flamie.db"

# ==========================================
# 2. BASE DE DATOS
# ==========================================
# Una sola conexion por hilo — evita deadlocks con threading.Lock
def get_db():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    return con

# Migrar/crear tablas de forma segura
def init_db():
    con = get_db()
    cur = con.cursor()
    # Tabla principal — si ya existe sin columna hora, la agrega
    cur.execute('''
        CREATE TABLE IF NOT EXISTS mensajes (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id  INTEGER,
            nombre   TEXT,
            mensaje  TEXT,
            fecha    TEXT,
            hora     TEXT DEFAULT ""
        )
    ''')
    # Agregar columna hora si no existe (compatibilidad con DB vieja)
    try:
        cur.execute("ALTER TABLE mensajes ADD COLUMN hora TEXT DEFAULT ''")
    except sqlite3.OperationalError as exc:
        if "duplicate column name: hora" not in str(exc).lower():
            raise

    # Tabla para recordar a quien ya se le envio la respuesta automatica
    # Persiste entre reinicios del bot
    cur.execute('''
        CREATE TABLE IF NOT EXISTS usuarios_notificados (
            user_id INTEGER PRIMARY KEY
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS chat_users (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            first_name TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (chat_id, user_id)
        )
    ''')
    cur.execute('''
        CREATE INDEX IF NOT EXISTS idx_chat_users_username
        ON chat_users (chat_id, username)
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS custom_commands (
            command TEXT PRIMARY KEY,
            response TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS chat_settings (
            chat_id INTEGER NOT NULL,
            setting TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (chat_id, setting)
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS chat_rules (
            chat_id INTEGER PRIMARY KEY,
            rules TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS warnings (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS group_activity (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            first_name TEXT,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        )
    ''')

    con.commit()
    con.close()

init_db()

def cargar_usuarios_notificados():
    con = get_db()
    try:
        cur = con.execute("SELECT user_id FROM usuarios_notificados")
        return {row[0] for row in cur.fetchall()}
    finally:
        con.close()

# Cache en memoria — se llena una sola vez al iniciar el bot
usuarios_notificados_cache = cargar_usuarios_notificados()

# ==========================================
# 4. BOT
# ==========================================
bot = telebot.TeleBot(TOKEN)
_gif_ids = {}


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
    filas = query("SELECT command FROM custom_commands ORDER BY command LIMIT 40")
    return [telebot.types.BotCommand(str(fila[0]), "Comando personalizado") for fila in filas]


def usuario_puede_administrar(chat_id, chat_type, user_id):
    if user_id == MI_ID:
        return True
    if chat_type == "private":
        return False
    try:
        return bot.get_chat_member(chat_id, user_id).status in ("administrator", "creator")
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
    ]
    privados = publicos + [("activar", "Solicita una activacion")]
    administradores = [
        ("ban", "Banea a un usuario"), ("tban", "Banea por tiempo"), ("kick", "Expulsa a un usuario"),
        ("mute", "Silencia a un usuario"), ("tmute", "Silencia por tiempo"), ("unmute", "Quita el silencio"),
        ("restrict", "Restringe a un usuario"), ("promote", "Promueve a un administrador"),
        ("demote", "Retira el rol de administrador"), ("warn", "Advierte a un usuario"),
        ("unwarn", "Retira una advertencia"),
        ("del", "Borra el mensaje respondido"), ("pin", "Fija el mensaje respondido"),
        ("unpin", "Desfija un mensaje"), ("unallpin", "Desfija todos los mensajes"),
        ("setrules", "Define las reglas del grupo"), ("clearrules", "Elimina las reglas"),
    ]
    propietario = [
        ("limpiar", "Limpia este historial con confirmacion"),
        ("addcmd", "Crea o actualiza un comando"), ("delcmd", "Elimina un comando personalizado"),
        ("listcmd", "Lista comandos personalizados"),
    ]
    personalizados = comandos_personalizados_menu()
    convertir = lambda pares: [telebot.types.BotCommand(nombre, descripcion) for nombre, descripcion in pares]
    comandos_publicos = convertir(publicos) + personalizados
    comandos_privados = convertir(privados) + personalizados
    comandos_admin = comandos_publicos + convertir(administradores)
    comandos_propietario = comandos_privados + convertir(administradores) + convertir(propietario)
    try:
        bot.set_my_commands(comandos_privados, scope=telebot.types.BotCommandScopeAllPrivateChats())
        bot.set_my_commands(comandos_publicos, scope=telebot.types.BotCommandScopeAllGroupChats())
        bot.set_my_commands(comandos_admin, scope=telebot.types.BotCommandScopeAllChatAdministrators())
        bot.set_my_commands(comandos_propietario, scope=telebot.types.BotCommandScopeChat(chat_id=MI_ID))
    except Exception:
        print("No se pudo actualizar el menu de comandos de Telegram.")


def teclado_ayuda(message, user_id=None, panel="inicio"):
    user_id = user_id or (message.from_user.id if message.from_user else 0)
    chat_id = message.chat.id
    chat_type = message.chat.type
    es_admin = usuario_puede_administrar(chat_id, chat_type, user_id)
    markup = telebot.types.InlineKeyboardMarkup()
    if panel == "inicio":
        markup.row(
            telebot.types.InlineKeyboardButton("Informacion", callback_data="help:info"),
            telebot.types.InlineKeyboardButton("Ocio", callback_data="help:ocio"),
        )
        markup.row(
            telebot.types.InlineKeyboardButton("Stickers", callback_data="help:stickers"),
            telebot.types.InlineKeyboardButton("Reglas", callback_data="help:reglas"),
        )
        markup.row(telebot.types.InlineKeyboardButton("Utilidades", callback_data="help:utilidades"))
        if chat_type == "private":
            markup.row(telebot.types.InlineKeyboardButton("Activacion", callback_data="help:activacion"))
        if es_admin:
            markup.row(
                telebot.types.InlineKeyboardButton("Moderacion", callback_data="help:moderacion"),
                telebot.types.InlineKeyboardButton("Administracion", callback_data="help:adminchat"),
            )
            if user_id == MI_ID:
                markup.row(telebot.types.InlineKeyboardButton("Propietario", callback_data="help:propietario"))
        markup.row(telebot.types.InlineKeyboardButton("Cerrar", callback_data="help:cerrar"))
    else:
        markup.row(
            telebot.types.InlineKeyboardButton("Volver a paneles", callback_data="help:inicio"),
            telebot.types.InlineKeyboardButton("Cerrar", callback_data="help:cerrar"),
        )
    return markup


def texto_panel_ayuda(panel, user_id, chat_id, chat_type):
    es_admin = usuario_puede_administrar(chat_id, chat_type, user_id)
    paneles = {
        "inicio": "Ayuda de Flamie\nElige un panel. Los paneles administrativos solo se muestran a administradores del grupo y al propietario.",
        "info": "Informacion\n/info, /id — Datos e identificadores del chat y usuario.\n/chatid — ID del chat.",
        "ocio": "Ocio\n/8ball — Respuesta aleatoria.\n/dado — Lanza un dado.",
        "stickers": "Stickers\n/q — Responde a un texto para crear sticker de cita.",
        "reglas": "Reglas\n/rules — Consulta las reglas de este grupo.",
        "utilidades": "Utilidades\n/chatlink — Obtiene el enlace del grupo.\n/staff — Lista administradores.\n/activar — Solicita una activacion en privado.",
        "activacion": "Activacion\nPulsa Activacion para indicar la plataforma y enviar una solicitud. Tambien puedes usar /activar.",
        "moderacion": "Moderacion\n/ban, /tban, /kick — Expulsar o banear.\n/mute, /tmute, /unmute, /restrict — Silencios y restricciones.\nResponde a un mensaje o indica ID o @usuario conocido.",
        "adminchat": "Administracion del grupo\n/promote y /demote — Gestion de administradores.\n/setrules y /clearrules — Gestion de reglas.\n/del, /pin, /unpin, /unallpin — Herramientas de mensajes.",
        "propietario": "Panel del propietario\n/limpiar — Limpia el historial privado en tu cuenta tras confirmacion.\n/addcmd nombre | respuesta, /delcmd nombre, /listcmd — Comandos personalizados.",
    }
    paneles_admin = {"moderacion", "adminchat"}
    if panel in paneles_admin and not es_admin:
        return "Acceso denegado. Este panel solo esta disponible para administradores del grupo."
    if panel == "propietario" and user_id != MI_ID:
        return "Acceso denegado. Este panel solo esta disponible para el propietario."
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
        pedir_plataforma_activacion(call.message.chat.id)
        return
    texto = texto_panel_ayuda(panel, user_id, call.message.chat.id, call.message.chat.type)
    markup = teclado_ayuda(call.message, user_id=user_id, panel=panel)
    try:
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
        print(f"[ERROR] No se pudieron registrar los usuarios del chat: {exc}")


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
    pedir_plataforma_activacion(message.chat.id)


@bot.message_handler(commands=['info', 'id'])
def cmd_info(message):
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
    bot.reply_to(message, f"ID del chat: <code>{message.chat.id}</code>", parse_mode="HTML")


@bot.message_handler(commands=["chatlink"])
def cmd_chatlink(message):
    try:
        chat = bot.get_chat(message.chat.id)
        enlace = f"https://t.me/{chat.username}" if chat.username else bot.export_chat_invite_link(message.chat.id)
        bot.reply_to(message, html.escape(enlace), parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        bot.reply_to(message, "No pude obtener el enlace. El bot necesita permiso para invitar usuarios.")


@bot.message_handler(commands=["staff"])
def cmd_staff(message):
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
    filas = query("SELECT rules FROM chat_rules WHERE chat_id=?", (message.chat.id,))
    bot.reply_to(message, str(filas[0][0]) if filas else "Este grupo no tiene reglas configuradas.")


@bot.message_handler(commands=["8ball"])
def cmd_8ball(message):
    if not (message.text or "").split(maxsplit=1)[-1].strip() or len((message.text or "").split()) < 2:
        bot.reply_to(message, "Uso: /8ball pregunta")
        return
    bot.reply_to(message, random.choice(["Sí.", "No.", "Es probable.", "No puedo asegurarlo.", "Inténtalo más tarde."]))


@bot.message_handler(commands=["dado"])
def cmd_dado(message):
    bot.reply_to(message, f"Resultado: {random.randint(1, 6)}")


def exigir_admin(message):
    try:
        return message.chat.type != "private" and usuario_es_admin(message.chat.id, message.from_user.id)
    except Exception:
        return False


@bot.message_handler(commands=["setrules"])
def cmd_setrules(message):
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
        bot.reply_to(message, f"No pude borrar el mensaje: {html.escape(str(exc))}")


@bot.message_handler(commands=["pin"])
def cmd_pin(message):
    if not exigir_admin(message) or not message.reply_to_message:
        bot.reply_to(message, "Un administrador debe responder con /pin al mensaje.")
        return
    try:
        bot.pin_chat_message(message.chat.id, message.reply_to_message.message_id)
    except Exception as exc:
        bot.reply_to(message, f"No pude fijar el mensaje: {html.escape(str(exc))}")


@bot.message_handler(commands=["unpin"])
def cmd_unpin(message):
    if not exigir_admin(message):
        bot.reply_to(message, "Solo un administrador del grupo puede desfijar mensajes.")
        return
    try:
        if message.reply_to_message:
            bot.unpin_chat_message(message.chat.id, message.reply_to_message.message_id)
        else:
            bot.unpin_chat_message(message.chat.id)
    except Exception as exc:
        bot.reply_to(message, f"No pude desfijar el mensaje: {html.escape(str(exc))}")


@bot.message_handler(commands=["unallpin"])
def cmd_unallpin(message):
    if not exigir_admin(message):
        bot.reply_to(message, "Solo un administrador del grupo puede desfijar mensajes.")
        return
    try:
        bot.unpin_all_chat_messages(message.chat.id)
        bot.reply_to(message, "Se desfijaron los mensajes del grupo.")
    except Exception as exc:
        bot.reply_to(message, f"No pude desfijar los mensajes: {html.escape(str(exc))}")


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
                "⬩➤MsTarantulaᓚᘏᗢ\n"
                f"⟡{message.text}\n"
                "⟡Eɴᴠɪᴀ Cᴀᴘᴛᴜʀᴀ O Fᴏᴛᴏ Aɢʀᴀᴅᴇᴄɪᴇɴᴅᴏ Cᴏɴ Mɪ Aʟɪᴀs @MsTarantula"
            )
        else:
            texto = f"⬩➤MsTarantulaᓚᘏᗢ\n{message.text}"
        bot.send_message(int(uid), texto, disable_web_page_preview=True)
        bot.send_message(MI_ID, f"✅ Enviado a <code>{uid}</code>.", parse_mode='HTML')
    except Exception as e:
        bot.send_message(MI_ID, f"No se pudo enviar: {e}")

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
    r"^\s*[/.,]\s*(ban|tban|mute|tmute|unmute|restrict|kick|promote|demote|warn|unwarn)(?:@[A-Za-z0-9_]+)?(?:\s+|$)(.*)$",
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
    miembro = bot.get_chat_member(chat_id, user_id)
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


def procesar_comando_moderacion(message):
    coincidencia = MODERATION_RE.match(message.text or "")
    if not coincidencia:
        return
    comando = coincidencia.group(1).lower()
    args = coincidencia.group(2).strip()
    if message.chat.type == "private":
        bot.reply_to(message, "Este comando debe usarse dentro del grupo o canal.")
        return

    try:
        if not usuario_es_admin(message.chat.id, message.from_user.id):
            bot.reply_to(message, "Solo los administradores del chat pueden usar este comando.")
            return
        objetivo, resto = resolver_objetivo(message, args)
        if objetivo is None:
            bot.reply_to(
                message,
                "Uso: responde a un mensaje o indica /" + comando + " ID o @usuario.",
            )
            return
        if objetivo == message.from_user.id:
            bot.reply_to(message, "No puedes aplicar esta accion sobre ti mismo.")
            return

        if comando in ("warn", "unwarn"):
            miembro_objetivo = bot.get_chat_member(message.chat.id, objetivo)
            if miembro_objetivo.status in ("administrator", "creator"):
                bot.reply_to(message, "No se puede cambiar las advertencias de un administrador.")
                return
            con = get_db()
            try:
                if comando == "warn":
                    con.execute("INSERT INTO warnings(chat_id,user_id,count) VALUES(?,?,1) ON CONFLICT(chat_id,user_id) DO UPDATE SET count=count+1", (message.chat.id, objetivo))
                    con.commit()
                    fila = con.execute("SELECT count FROM warnings WHERE chat_id=? AND user_id=?", (message.chat.id, objetivo)).fetchone()
                    cuenta = int(fila[0])
                else:
                    con.execute("DELETE FROM warnings WHERE chat_id=? AND user_id=?", (message.chat.id, objetivo))
                    con.commit()
                    cuenta = 0
            finally:
                con.close()
            if comando == "warn" and cuenta >= 3:
                bot.ban_chat_member(message.chat.id, objetivo)
                bot.reply_to(message, f"Advertencia {cuenta}/3. Usuario {objetivo} expulsado al alcanzar el limite.")
            else:
                bot.reply_to(message, f"Advertencias de {objetivo}: {cuenta}/3." if comando == "warn" else f"Advertencias de {objetivo} eliminadas.")
            return

        if comando in ("ban", "tban"):
            duracion = duracion_a_segundos(resto.split()[0]) if resto.split() else None
            if comando == "tban" and not duracion:
                raise ValueError("Uso: /tban 1d ID o responde al mensaje con /tban 1d.")
            hasta = int(time.time()) + duracion if duracion else None
            bot.ban_chat_member(message.chat.id, objetivo, until_date=hasta)
            respuesta = f"Usuario {objetivo} baneado" + (f" por {resto.split()[0]}." if duracion else ".")
        elif comando == "kick":
            bot.ban_chat_member(message.chat.id, objetivo)
            bot.unban_chat_member(message.chat.id, objetivo)
            respuesta = f"Usuario {objetivo} expulsado."
        elif comando == "unmute":
            permisos = telebot.types.ChatPermissions(
                can_send_messages=True, can_send_media_messages=True, can_send_audios=True,
                can_send_documents=True, can_send_photos=True, can_send_videos=True,
                can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True,
                can_send_other_messages=True, can_add_web_page_previews=True,
            )
            bot.restrict_chat_member(message.chat.id, objetivo, permissions=permisos)
            respuesta = f"Se retiro el silencio de {objetivo}."
        elif comando in ("mute", "tmute", "restrict"):
            duracion = duracion_a_segundos(resto.split()[0]) if resto.split() else None
            if comando == "tmute" and not duracion:
                raise ValueError("Uso: /tmute 30m ID o responde al mensaje con /tmute 30m.")
            if resto.split() and not duracion:
                raise ValueError("Duracion invalida. Usa s, m, h o d; por ejemplo 30m o 2h.")
            hasta = int(time.time()) + duracion if duracion else None
            permisos = telebot.types.ChatPermissions(
                can_send_messages=False,
                can_send_media_messages=False,
                can_send_audios=False,
                can_send_documents=False,
                can_send_photos=False,
                can_send_videos=False,
                can_send_video_notes=False,
                can_send_voice_notes=False,
                can_send_polls=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False,
            )
            bot.restrict_chat_member(
                message.chat.id, objetivo, until_date=hasta, permissions=permisos
            )
            sufijo = f" por {resto.split()[0]}" if duracion else " sin limite de tiempo"
            respuesta = f"Usuario {objetivo} restringido{sufijo}."
        elif comando == "demote":
            derechos = {nombre_api: False for nombre_api in PERMISOS_ADMIN.values()}
            bot.promote_chat_member(message.chat.id, objetivo, **derechos)
            respuesta = f"Se retiraron los permisos de administrador a {objetivo}."
        elif comando == "promote":
            derechos = {nombre_api: False for nombre_api in PERMISOS_ADMIN.values()}
            palabras = [p.strip().lower() for p in resto.replace(",", " ").split()]
            for palabra in palabras:
                normalizado = palabra.removeprefix("can_")
                if normalizado not in PERMISOS_ADMIN:
                    bot.reply_to(
                        message,
                        "Permisos validos: " + ", ".join(sorted(PERMISOS_ADMIN)) + ".",
                    )
                    return
                derechos[PERMISOS_ADMIN[normalizado]] = True
            if not palabras:
                derechos["can_manage_chat"] = True
            bot.promote_chat_member(message.chat.id, objetivo, **derechos)
            lista = ", ".join(k.removeprefix("can_") for k in derechos)
            respuesta = f"Usuario {objetivo} promovido. Permisos: {lista}."
        else:
            return
        bot.reply_to(message, respuesta)
    except ValueError as exc:
        bot.reply_to(message, str(exc))
    except Exception as exc:
        bot.reply_to(message, f"No se pudo completar la accion: {html.escape(str(exc))}")


@bot.message_handler(func=lambda m: bool(m.text and MODERATION_RE.match(m.text)))
def comandos_moderacion(message):
    procesar_comando_moderacion(message)


# ==========================================
# 11.2 COMANDOS PERSONALIZADOS PERSISTENTES
# ==========================================
COMANDOS_RESERVADOS = {
    "start", "help", "activar", "info", "id", "limpiar", "q",
    "uinfo", "chatid", "chatlink", "staff", "rules", "sticker", "idsticker", "getsticker",
    "quiz", "8ball", "dado", "top", "ban", "tban", "mute", "tmute", "unmute",
    "restrict", "kick", "promote", "demote", "warn", "unwarn", "settitle", "setgtitle",
    "setdescr", "del", "pin", "unpin", "unallpin", "admin", "msg", "copy", "forward",
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
        bot.reply_to(message, f"No se pudo crear el sticker: {html.escape(str(exc))}")

# Estado de espera de codigo por usuario: {user_id: plataforma_detectada_o_None}
esperando_codigo = {}

# Usuarios que eligieron el boton "Activacion" en /help y deben indicar
# todavia para que plataforma es (paso previo a esperando_codigo)
esperando_plataforma = set()

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
            bot.reply_to(message,
                f"✦ Codigo de {plataforma_pendiente} recibido.\n"
                "✦ Espera un momento mientras MsTarantula lo activa para ti."
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
            "✦ Espera un momento mientras MsTarantula lo activa para ti."
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
            "MsTarantula te respondera en cuanto pueda.\n\n"
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
if __name__ == "__main__":
    try:
        print("Iniciando Flamie Bot...")
        print("Moderacion | Stickers | Flujo de activacion\n")
        registrar_comandos_menu()
        bot.infinity_polling(timeout=10, long_polling_timeout=5)
    except KeyboardInterrupt:
        print("\nBot detenido.")
    except Exception as e:
        print(f"[ERROR] Error en bot principal: {e}")
        import traceback
        traceback.print_exc()
