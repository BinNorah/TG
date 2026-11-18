import os
import telebot
import sqlite3
from datetime import date, datetime
from pathlib import Path
import html
import threading
import google.genai as genai
import io
import re
import time

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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
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

    cur.execute('''
        CREATE TABLE IF NOT EXISTS alertas_directas (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id  INTEGER,
            nombre   TEXT,
            mensaje  TEXT,
            fecha    TEXT,
            hora     TEXT DEFAULT ""
        )
    ''')
    try:
        cur.execute("ALTER TABLE alertas_directas ADD COLUMN hora TEXT DEFAULT ''")
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
        CREATE TABLE IF NOT EXISTS config (
            clave TEXT PRIMARY KEY,
            valor TEXT
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
usuarios_silenciados = set()


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
# 4.1 CACHE DE ANIMACIONES (GIFs)
# ==========================================
# Telegram devuelve un file_id la primera vez que subes un archivo. Si se
# vuelve a mandar el mismo archivo local cada vez, Telegram lo vuelve a subir
# desde cero (eso es lo que generaba la demora en /start y /cmds). Guardando
# el file_id la primera vez, los siguientes envios son casi instantaneos.
_gif_ids = {}


def resolver_ruta_media(nombre_archivo):
    return BASE_DIR / nombre_archivo


def enviar_animacion_cacheada(chat_id, ruta_gif, caption=None, parse_mode=None, reply_markup=None):
    ruta_absoluta = str(resolver_ruta_media(ruta_gif))
    file_id = _gif_ids.get(ruta_absoluta)
    try:
        if file_id:
            msg = bot.send_animation(
                chat_id, file_id, caption=caption, parse_mode=parse_mode, reply_markup=reply_markup
            )
        else:
            with open(ruta_absoluta, "rb") as f:
                msg = bot.send_animation(
                    chat_id, f, caption=caption, parse_mode=parse_mode, reply_markup=reply_markup
                )
            if msg and msg.animation:
                _gif_ids[ruta_absoluta] = msg.animation.file_id
        return msg
    except FileNotFoundError:
        return None

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

def guardar_alerta(user_id, nombre, mensaje):
    fecha = date.today().strftime("%Y-%m-%d")
    hora  = datetime.now().strftime("%H:%M:%S")
    con = get_db()
    try:
        con.execute(
            "INSERT INTO alertas_directas (user_id, nombre, mensaje, fecha, hora) VALUES (?,?,?,?,?)",
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
    if message.chat.id == MI_ID:
        bot.reply_to(message, "Flamie Bot en línea.")
        return
    # Usuario externo
    if message.chat.id in usuarios_silenciados:
        usuarios_silenciados.remove(message.chat.id)
    texto = (
        "Has llegado a la puerta del dominio de MsTarantula.\n"
        "Deja tu mensaje y espera respuesta.\n\n"
        "Usa /cmds para ver los comandos disponibles."
    )
    try:
        msg = enviar_animacion_cacheada(message.chat.id, "FlamieBot.gif", caption=texto)
        if msg is None:
            bot.send_message(message.chat.id, texto)
    except Exception:
        bot.send_message(message.chat.id, texto)

@bot.message_handler(commands=['cmds'])
def cmd_cmds(message):
    if message.chat.id == MI_ID:
        texto = (
            "<b>Panel de Administracion - MsTarantula</b>\n\n"
            "<b>Reportes:</b>\n"
            "/hoy — Mensajes recibidos hoy (bot + cuenta personal)\n"
            "/stats — Estadisticas generales\n"
            "/resumen — Analisis IA del dia\n\n"
            "<b>Gestion:</b>\n"
            "/reload — Reinicia el sistema (limpia silenciados y notificados)\n"
            "/borrar [Nombre] — Elimina registros de un usuario\n"
            "/silenciar [ID] — Silencia manualmente a un usuario\n\n"
            "<b>Dinamicas:</b>\n"
            "/setdinamica [texto] — Activa una nueva dinamica\n"
            "/enddinamica — Cierra la dinamica actual"
        )
    else:
        texto = (
            "Sᴏʏ Lᴀ Asɪsᴛᴇɴᴛᴇ Pᴇʀsᴏɴᴀʟ Dᴇ @MsTarantula\n\n"
            "<b>Comandos Disponibles:</b>\n\n"
            "/me — Muestra tu informacion de perfil\n"
            "/estado — Verifica el estado de tu mensaje\n\n"
            "O elige una opcion abajo:"          
        )
        try:
            msg = enviar_animacion_cacheada(
                message.chat.id, "FlamieCmds.gif",
                caption=texto, parse_mode='HTML', reply_markup=teclado_cmds()
            )
            if msg is None:
                bot.reply_to(message, texto, parse_mode='HTML', reply_markup=teclado_cmds())
        except Exception:
            bot.reply_to(message, texto, parse_mode='HTML', reply_markup=teclado_cmds())
        return
    bot.reply_to(message, texto, parse_mode='HTML')

@bot.message_handler(commands=['reload'])
def cmd_reload(message):
    if message.chat.id != MI_ID:
        return
    usuarios_silenciados.clear()
    usuarios_notificados_cache.clear()
    con = get_db()
    try:
        con.execute("DELETE FROM usuarios_notificados")
        con.commit()
    finally:
        con.close()
    bot.reply_to(message, "✦ Sistema reiniciado.\nUsuarios silenciados: limpiados.\nTodos los usuarios recibiran la respuesta automatica de nuevo.")

@bot.message_handler(commands=['silenciar'])
def cmd_silenciar(message):
    if message.chat.id != MI_ID:
        return
    partes = message.text.split(maxsplit=1)
    if len(partes) < 2:
        bot.reply_to(message, "Formato: /silenciar [ID numerico]")
        return
    try:
        usuarios_silenciados.add(int(partes[1]))
        bot.reply_to(message, f"Usuario {partes[1]} silenciado.")
    except ValueError:
        bot.reply_to(message, "El ID debe ser un numero.")

@bot.message_handler(commands=['borrar'])
def cmd_borrar(message):
    if message.chat.id != MI_ID:
        return
    partes = message.text.split(maxsplit=1)
    if len(partes) < 2:
        bot.reply_to(message, "Formato: /borrar [NombreDelUsuario]")
        return
    nombre = partes[1]
    con = get_db()
    try:
        cur = con.execute("DELETE FROM mensajes WHERE nombre LIKE ?", (f'%{nombre}%',))
        borrados = cur.rowcount
        con.commit()
    finally:
        con.close()
    if borrados:
        bot.reply_to(message, f"Eliminados {borrados} mensajes de '{nombre}'.")
    else:
        bot.reply_to(message, f"No se encontraron mensajes de '{nombre}'.")



@bot.message_handler(commands=['stats'])
def cmd_stats(message):
    if message.chat.id != MI_ID:
        return
    total_msgs   = query("SELECT COUNT(*) FROM mensajes")[0][0]
    total_users  = query("SELECT COUNT(DISTINCT user_id) FROM mensajes")[0][0]
    total_alerta = query("SELECT COUNT(*) FROM alertas_directas")[0][0]
    top          = query("SELECT nombre, COUNT(*) as c FROM mensajes GROUP BY user_id ORDER BY c DESC LIMIT 5")
    por_dia      = query("SELECT fecha, COUNT(*) as c FROM mensajes GROUP BY fecha ORDER BY fecha DESC LIMIT 7")

    texto = (
        "<b>Estadisticas Generales</b>\n\n"
        f"Total mensajes al bot: <b>{total_msgs}</b>\n"
        f"Usuarios unicos: <b>{total_users}</b>\n"
        f"Alertas de mensajes directos: <b>{total_alerta}</b>\n\n"
        "<b>Top 5 usuarios mas activos:</b>\n"
    )
    for nombre, count in top:
        texto += f"  {html.escape(str(nombre))}: {count} mensajes\n"
    texto += "\n<b>Mensajes por dia (ultimos 7):</b>\n"
    for fecha, count in por_dia:
        texto += f"  {fecha}: {count}\n"
    bot.reply_to(message, texto, parse_mode='HTML')

@bot.message_handler(commands=['hoy'])
def cmd_hoy(message):
    if message.chat.id != MI_ID:
        return
    fecha = date.today().strftime("%Y-%m-%d")
    total_msgs  = query("SELECT COUNT(*), COUNT(DISTINCT user_id) FROM mensajes WHERE fecha=?", (fecha,))[0]
    total_alerta = query("SELECT COUNT(*) FROM alertas_directas WHERE fecha=?", (fecha,))[0][0]
    registros   = query("SELECT nombre, mensaje, user_id, hora FROM mensajes WHERE fecha=? ORDER BY hora", (fecha,))
    alertas     = query("SELECT nombre, mensaje, user_id, hora FROM alertas_directas WHERE fecha=? ORDER BY hora", (fecha,))

    texto = (
        f"<b>Reporte del {fecha}</b>\n\n"
        f"Usuarios unicos al bot: <b>{total_msgs[1]}</b>\n"
        f"Mensajes al bot: <b>{total_msgs[0]}</b>\n"
        f"Mensajes directos a tu cuenta: <b>{total_alerta}</b>\n"
    )
    if registros:
        texto += "\n<b>Mensajes recibidos por el bot:</b>\n"
        for nombre, msg, uid, hora in registros:
            hora_str = hora or "--:--"
            texto += f"[{hora_str}] <a href='tg://user?id={uid}'>{html.escape(str(nombre))}</a>: {html.escape(str(msg))}\n"
    if alertas:
        texto += "\n<b>Mensajes directos a tu cuenta personal:</b>\n"
        for nombre, msg, uid, hora in alertas:
            hora_str = hora or "--:--"
            texto += f"[{hora_str}] <a href='tg://user?id={uid}'>{html.escape(str(nombre))}</a>: {html.escape(str(msg))}\n"
    if not registros and not alertas:
        texto += "\nNo hay actividad registrada hoy."

    for i in range(0, len(texto), 4000):
        bot.send_message(MI_ID, texto[i:i+4000], parse_mode='HTML')

@bot.message_handler(commands=['resumen'])
def cmd_resumen(message):
    if message.chat.id != MI_ID:
        return
    if not GEMINI_API_KEY:
        bot.reply_to(message, "La IA no está configurada en este servidor. Define GEMINI_API_KEY para usar /resumen.")
        return
    client = genai.Client(api_key=GEMINI_API_KEY)
    bot.reply_to(message, "Procesando con IA, un momento...")
    fecha = date.today().strftime("%Y-%m-%d")
    bot_msgs     = query("SELECT nombre, mensaje, hora FROM mensajes WHERE fecha=?", (fecha,))
    direct_msgs  = query("SELECT nombre, mensaje, hora FROM alertas_directas WHERE fecha=?", (fecha,))

    if not bot_msgs and not direct_msgs:
        bot.send_message(MI_ID, "No hay actividad registrada hoy.")
        return

    prompt = (
        "Eres el asistente ejecutivo de MsTarantula, una desarrolladora. "
        "Analiza los mensajes recibidos hoy y genera un reporte con:\n"
        "1. RESUMEN GENERAL (2-3 oraciones)\n"
        "2. URGENTE (si hay algo que requiere atencion inmediata, sino escribe Ninguno)\n"
        "3. CATEGORIAS (clasifica las solicitudes)\n"
        "4. USUARIOS DESTACADOS (quien contacto mas o con solicitudes importantes)\n"
        "5. RECOMENDACION (que deberia priorizar responder)\n\n"
    )
    if bot_msgs:
        prompt += "MENSAJES AL BOT:\n"
        for nombre, msg, hora in bot_msgs:
            prompt += f"- [{hora}] {nombre}: '{msg}'\n"
    if direct_msgs:
        prompt += "\nMENSAJES DIRECTOS A SU CUENTA PERSONAL:\n"
        for nombre, msg, hora in direct_msgs:
            prompt += f"- [{hora}] {nombre}: '{msg}'\n"

    try:
        respuesta = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        bot.send_message(MI_ID, f"<b>Analisis IA del dia:</b>\n\n{respuesta.text}", parse_mode='HTML')
    except Exception as e:
        bot.send_message(MI_ID, f"Error de IA: {e}")

# ==========================================
# 7. HELPERS — CONFIG Y DINAMICA
# ==========================================
def get_config(clave):
    con = get_db()
    try:
        cur = con.execute("SELECT valor FROM config WHERE clave=?", (clave,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        con.close()

def set_config(clave, valor):
    con = get_db()
    try:
        con.execute("INSERT OR REPLACE INTO config (clave, valor) VALUES (?,?)", (clave, valor))
        con.commit()
    finally:
        con.close()

def del_config(clave):
    con = get_db()
    try:
        con.execute("DELETE FROM config WHERE clave=?", (clave,))
        con.commit()
    finally:
        con.close()

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
        telebot.types.InlineKeyboardButton("✉️ Dinámica", callback_data=f"resp:din:{user_id}"),
        telebot.types.InlineKeyboardButton("🔑 Activación", callback_data=f"resp:act:{user_id}")
    )
    return markup

def teclado_cmds():
    """Botones que se muestran a un usuario externo en /cmds para que elija
    si viene por una dinamica o si necesita una activacion para su TV."""
    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton("🎁 Dinámica", callback_data="cmds:din"),
        telebot.types.InlineKeyboardButton("🔑 Activación", callback_data="cmds:act")
    )
    return markup

def mostrar_dinamica(chat_id):
    """Texto de la dinamica activa (o aviso de que no hay ninguna), reutilizado
    tanto por /dinamica como por el boton del menu /cmds."""
    info = get_config("dinamica")
    if info is None:
        bot.send_message(
            chat_id,
            "【✦】•━【𝙵𝚕𝚊𝚖𝚒𝚎 𝚂𝚙𝚎𝚎𝚍𝚍𝚛𝚊𝚠】━•【✦】\n"
            "╰┈➤ No hay ninguna dinamica activa en este momento.\n"
        )
        return
    bot.send_message(
        chat_id,
        "【✦】•━【𝙵𝚕𝚊𝚖𝚒𝚎 𝚂𝚙𝚎𝚎𝚍𝚍𝚛𝚊𝚠】━•【✦】\n"
        f"╰┈➤{info}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "╰┈➤ Pᴀʀᴀ Rᴇᴄʟᴀᴍᴀʀ Tᴜ Pʀᴇᴍɪᴏ\n"
        "╰┈➤ Eɴᴠɪᴀ Tᴜs Pʀᴜᴇʙᴀs Aqᴜɪ Cᴏᴍᴏ Fᴏᴛᴏ\n"
        "╰┈➤ Esᴘᴇʀᴀ Cᴏɴғɪʀᴍᴀᴄɪᴏɴ\n"
        "【✦】•━【𝙵𝚕𝚊𝚖𝚒𝚎 𝚂𝚙𝚎𝚎𝚍𝚍𝚛𝚊𝚠】━•【✦】"
    )

def pedir_plataforma_activacion(chat_id):
    """Pide al usuario que indique la plataforma cuando elige 'Activación'
    desde el menu de botones (en vez de escribirla directamente en el texto)."""
    bot.send_message(
        chat_id,
        "✦ ¿Para qué plataforma necesitas la activación?\n"
        "Escríbela (ej: Netflix, Disney+, HBO Max...)."
    )

# ==========================================
# 8.1 CALLBACK — MENU /cmds (Dinamica / Activacion)
# ==========================================
@bot.callback_query_handler(func=lambda call: call.data.startswith("cmds:"))
def callback_menu_cmds(call):
    opcion = call.data.split(":")[1]   # "din" o "act"
    chat_id = call.message.chat.id
    uid = call.from_user.id

    if opcion == "din":
        bot.answer_callback_query(call.id, "Dinámica")
        mostrar_dinamica(chat_id)
    else:
        bot.answer_callback_query(call.id, "Activación")
        esperando_plataforma.add(uid)
        pedir_plataforma_activacion(chat_id)

# ==========================================
# 8. CALLBACK — BOTON RESPONDER
# ==========================================
@bot.callback_query_handler(func=lambda call: call.data.startswith("resp:"))
def callback_responder(call):
    if call.from_user.id != MI_ID:
        bot.answer_callback_query(call.id)
        return
    partes = call.data.split(":")
    tipo = partes[1]   # "din" o "act"
    uid  = partes[2]
    etiqueta = "Dinámica" if tipo == "din" else "Activación"
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
# 9. COMANDOS — ADMIN DINAMICA
# ==========================================
@bot.message_handler(commands=['setdinamica'])
def cmd_setdinamica(message):
    if message.chat.id != MI_ID:
        return
    partes = message.text.split(maxsplit=1)
    if len(partes) < 2:
        bot.reply_to(message,
            "Formato: /setdinamica [descripcion]\n\n"
            "Ejemplo:\n/setdinamica 🎉 Si ganaste la dinamica, envia tu captura de pantalla y que es lo que ganaste."
        )
        return
    set_config("dinamica", partes[1])
    bot.reply_to(message, "✦ Dinamica activada. Los usuarios ya pueden usar /dinamica.")

@bot.message_handler(commands=['enddinamica'])
def cmd_enddinamica(message):
    if message.chat.id != MI_ID:
        return
    if get_config("dinamica") is None:
        bot.reply_to(message, "No hay ninguna dinamica activa.")
        return
    del_config("dinamica")
    bot.reply_to(message, "✦ Dinamica cerrada.")

# ==========================================
# 10. COMANDOS — USUARIO
# ==========================================
@bot.message_handler(commands=['me'])
def cmd_me(message):
    u = message.from_user
    user_id  = u.id
    nombre   = u.first_name or ""
    username = f"@{u.username}" if u.username else "Sin usuario"
    texto = (
        "【✦】•━【𝙵𝚕𝚊𝚖𝚒𝚎 𝚂𝚙𝚎𝚎𝚍𝚍𝚛𝚊𝚠】━•【✦】\n"
        f"╰┈➤ 𝐈𝐃 ➟ <code>{user_id}</code>\n"
        f"╰┈➤ 𝐍𝐨𝐦𝐛𝐫𝐞 ➟ {html.escape(nombre)}\n"
        f"╰┈➤ 𝐔𝐬𝐮𝐚𝐫𝐢𝐨 ➟ {html.escape(username)}\n"
        "【✦】•━【𝙵𝚕𝚊𝚖𝚒𝚎 𝚂𝚙𝚎𝚎𝚍𝚍𝚛𝚊𝚠】━•【✦】"
    )
    bot.reply_to(message, texto, parse_mode='HTML')

@bot.message_handler(commands=['estado'])
def cmd_estado(message):
    if message.chat.id == MI_ID:
        return
    fecha = date.today().strftime("%Y-%m-%d")
    count = query(
        "SELECT COUNT(*) FROM mensajes WHERE user_id=? AND fecha=?",
        (message.from_user.id, fecha)
    )[0][0]
    if count:
        bot.reply_to(message,
            "✦ Tu mensaje fue recibido correctamente.\n"
            "MsTarantula te respondera en cuanto pueda.\n"
            "No es necesario escribir de nuevo."
        )
    else:
        bot.reply_to(message,
            "✦ Aun no hay mensajes tuyos registrados hoy.\n"
            "Puedes escribir tu consulta y sera atendida."
        )

@bot.message_handler(commands=['dinamica'])
def cmd_dinamica(message):
    if message.chat.id == MI_ID:
        info = get_config("dinamica")
        bot.reply_to(message, f"Dinamica activa:\n\n{info}" if info else "No hay dinamica activa. Usa /setdinamica para crear una.")
    # Usuarios externos: ya no se responde aqui, solo pueden ver la dinamica
    # tocando el boton "🎁 Dinámica" en /cmds.

# ==========================================
# 11. ARCHIVOS MULTIMEDIA
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
    r"^\s*[/.,]\s*(ban|mute|restrict|kick|promote)(?:@[A-Za-z0-9_]+)?(?:\s+|$)(.*)$",
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

        if comando == "ban":
            bot.ban_chat_member(message.chat.id, objetivo)
            respuesta = f"Usuario {objetivo} baneado."
        elif comando == "kick":
            bot.ban_chat_member(message.chat.id, objetivo)
            bot.unban_chat_member(message.chat.id, objetivo)
            respuesta = f"Usuario {objetivo} expulsado."
        elif comando in ("mute", "restrict"):
            duracion = duracion_a_segundos(resto.split()[0]) if resto.split() else None
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
        else:
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
    "start", "cmds", "reload", "silenciar", "borrar", "stats", "hoy",
    "resumen", "setdinamica", "enddinamica", "me", "estado", "dinamica",
    "ban", "mute", "restrict", "kick", "promote", "q", "addcmd",
    "delcmd", "listcmd",
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

# Usuarios que eligieron el boton "Activacion" en /cmds y deben indicar
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
        bot.send_message(MI_ID, "Comando no reconocido. Usa /cmds para ver las opciones.")
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
            "Usa /estado para verificar el estado de tu mensaje.\n"
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
        print("Anti-spam activo | Analisis IA | Respuesta directa | Espera de codigos\n")
        bot.infinity_polling(timeout=10, long_polling_timeout=5)
    except KeyboardInterrupt:
        print("\nBot detenido.")
    except Exception as e:
        print(f"[ERROR] Error en bot principal: {e}")
        import traceback
        traceback.print_exc()
