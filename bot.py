import os
import telebot
import sqlite3
from datetime import date, datetime
from pathlib import Path
import html
import threading
import google.genai as genai

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
DATA_DIR.mkdir(exist_ok=True)
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
    except sqlite3.OperationalError:
        pass  # Ya existe, no hay problema

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
    except sqlite3.OperationalError:
        pass

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