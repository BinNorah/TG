import os
import pyrogram
from pyrogram import Client, filters
from pyrogram.enums import ParseMode

# --- PARCHE PARA PYROGRAM (Nuevos IDs de Telegram) ---
# Telegram ha expandido los IDs de grupos a rangos que Pyrogram v2.x 
# considera inválidos por defecto, causando "ValueError: Peer id invalid".
from pyrogram import utils
def get_peer_type_new(peer_id: int) -> str:
    peer_id_str = str(peer_id)
    if not peer_id_str.startswith("-"):
        return "user"
    elif peer_id_str.startswith("-100"):
        return "channel"
    else:
        return "chat"
utils.get_peer_type = get_peer_type_new
# -----------------------------------------------------

from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import SessionPasswordNeeded
from pathlib import Path
import time
import requests
import json

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
        print(f"[ERROR] Falta la variable de entorno: {name}")
        raise RuntimeError(f"Falta la variable de entorno: {name}")
    return value


try:
    API_ID = int(_require_env("PYROGRAM_API_ID"))
    API_HASH = _require_env("PYROGRAM_API_HASH")
    MI_BOT = "@Flamie_SpeeddrawBot"
    BOT_TOKEN = _require_env("TELEGRAM_BOT_TOKEN")
    MI_ID = int(_require_env("TELEGRAM_OWNER_ID"))
    BOT_ID = int(BOT_TOKEN.split(':')[0])
except Exception as e:
    print(f"[ERROR] No se pudieron cargar las variables de entorno requeridas: {e}")
    raise

SESSION_NAME = os.getenv("PYROGRAM_SESSION_NAME", "mi_sesion")
if SESSION_NAME.endswith(".session"):
    SESSION_NAME = SESSION_NAME[:-8]

# En servidor (Railway): usa StringSession desde variable de entorno
# En local: usa el archivo .session
SESSION_STRING = os.getenv("PYROGRAM_SESSION_STRING", "").strip()
SESSION_FILE = BASE_DIR / f"{SESSION_NAME}.session"

if SESSION_STRING:
    print("[USERBOT] Modo servidor: usando StringSession desde variable de entorno.")
    app = Client(
        name="bot_session",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STRING,
    )
elif SESSION_FILE.exists():
    print(f"[USERBOT] Modo local: usando archivo de sesion {SESSION_FILE}")
    app = Client(str(BASE_DIR / SESSION_NAME), api_id=API_ID, api_hash=API_HASH)
else:
    print("[ERROR CRITICO] No se encontro sesion de Pyrogram.")
    print("  - En Railway: agrega la variable PYROGRAM_SESSION_STRING")
    print("  - En local: ejecuta generar_session_string.py primero")
    import sys as _sys
    _sys.exit(1)
usuarios_respondidos = set()

# Cache del file_id del GIF de respuesta automatica. Pyrogram, igual que el
# bot, vuelve a subir el archivo local cada vez si no se reutiliza el file_id
# que Telegram devuelve tras la primera subida — eso era lo que generaba la
# demora en la respuesta automatica.
_gif_id_cache = {"FlamieSpeedDraw.gif": None}

# ==========================================
# 2. NOTIFICAR AL BOT (con foto de perfil)
# ==========================================
def _notificar_fallback(user_id, nombre, contenido, tipo):
    """Fallback via Bot API REST si Pyrogram falla."""
    hora = time.strftime('%H:%M:%S')
    iconos = {
        "texto": "💬", "foto": "📷", "video": "🎥",
        "audio": "🎵", "documento": "📎", "sticker": "🎭",
    }
    icono = iconos.get(tipo, "📨")
    cuerpo = (
        f"{icono} <b>Mensaje directo</b>  •  <code>{hora}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>De:</b> <a href='tg://user?id={user_id}'>{nombre}</a>\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n\n"
        f"📝 <b>Contenido:</b> {contenido}\n\n"
        f"↩️ <code>/r {user_id} [respuesta]</code>"
    )
    teclado = {"inline_keyboard": [[{"text": "💬 DM", "callback_data": f"resp:dm:{user_id}"}]]}
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": MI_ID, "text": cuerpo, "parse_mode": "HTML", "reply_markup": teclado},
            timeout=10
        )
    except Exception as e:
        print(f"[ERROR] _notificar_fallback: {e}")


async def notificar_con_foto(client, message: Message, user_id, nombre, contenido, tipo):
    """
    Notifica al dueño via Bot API con la foto de perfil del remitente.
    La foto de perfil se descarga con Pyrogram y se sube via Bot API.
    NOTE: client.send_* envía a Mensajes Guardados, NO al chat del bot.
          Por eso SIEMPRE usamos Bot API (BOT_TOKEN) para las notificaciones.
    """
    hora = time.strftime('%H:%M:%S')

    # --- Armar caption estético ---
    iconos_tipo = {
        "texto":     ("💬", "Mensaje directo"),
        "foto":      ("📷", "Foto directa"),
        "video":     ("🎥", "Video directo"),
        "audio":     ("🎵", "Audio directo"),
        "documento": ("📎", "Archivo directo"),
        "sticker":   ("🎭", "Sticker directo"),
    }
    icono, etiqueta = iconos_tipo.get(tipo, ("📨", "Mensaje directo"))

    if tipo == "texto":
        linea_contenido = f"💬 <b>Dice:</b>\n<blockquote>{contenido}</blockquote>"
    else:
        linea_contenido = f"{icono} <b>Contenido:</b> {contenido}"

    caption = (
        f"{icono} <b>{etiqueta}</b>  ·  <code>{hora}</code>\n"
        f"────────────────────\n"
        f"👤 <a href='tg://user?id={user_id}'><b>{nombre}</b></a>\n"
        f"🆔 <code>{user_id}</code>\n\n"
        f"{linea_contenido}\n\n"
        f"↩️ <code>/r {user_id} [respuesta]</code>"
    )
    teclado = {"inline_keyboard": [[{"text": "💬 DM", "callback_data": f"resp:dm:{user_id}"}]]}

    # --- Descargar foto de perfil con Pyrogram (en memoria) ---
    foto_bytes = None
    try:
        async for foto in client.get_chat_photos(user_id, limit=1):
            foto_bytes = await client.download_media(foto, in_memory=True)
            break
    except Exception as e:
        print(f"[DEBUG] Sin foto de perfil para {user_id}: {e}")

    # --- Enviar notificación via Bot API (va al chat del bot, no a Guardados) ---
    try:
        if foto_bytes:
            foto_bytes.seek(0)
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                data={
                    "chat_id": MI_ID,
                    "caption": caption,
                    "parse_mode": "HTML",
                    "reply_markup": json.dumps(teclado),
                },
                files={"photo": ("profile.jpg", foto_bytes, "image/jpeg")},
                timeout=15,
            )
            print(f"[{hora}] Notificación con foto enviada: {r.status_code}")
            if r.status_code != 200:
                print(f"[DEBUG] Error Bot API: {r.text}")
        else:
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": MI_ID,
                    "text": caption,
                    "parse_mode": "HTML",
                    "reply_markup": teclado,
                },
                timeout=10,
            )
            print(f"[{hora}] Notificación texto enviada: {r.status_code}")
            if r.status_code != 200:
                print(f"[DEBUG] Error Bot API: {r.text}")
    except Exception as e:
        print(f"[{hora}] Error en notificar_con_foto: {e}")

    # --- Reenviar el archivo real si es multimedia (descarga + re-subida via Bot API) ---
    if tipo in ("foto", "video", "audio", "documento", "sticker"):
        try:
            media_bytes = await client.download_media(message, in_memory=True)
            media_bytes.seek(0)
            url_base = f"https://api.telegram.org/bot{BOT_TOKEN}"
            data_base = {"chat_id": MI_ID}
            if message.photo:
                requests.post(f"{url_base}/sendPhoto",
                    data=data_base, files={"photo": ("foto.jpg", media_bytes, "image/jpeg")}, timeout=30)
            elif message.video:
                requests.post(f"{url_base}/sendVideo",
                    data=data_base, files={"video": ("video.mp4", media_bytes, "video/mp4")}, timeout=60)
            elif message.voice:
                requests.post(f"{url_base}/sendVoice",
                    data=data_base, files={"voice": ("voz.ogg", media_bytes, "audio/ogg")}, timeout=30)
            elif message.audio:
                requests.post(f"{url_base}/sendAudio",
                    data=data_base, files={"audio": ("audio.mp3", media_bytes, "audio/mpeg")}, timeout=30)
            elif message.document:
                fname = getattr(message.document, 'file_name', None) or "archivo"
                requests.post(f"{url_base}/sendDocument",
                    data=data_base, files={"document": (fname, media_bytes, "application/octet-stream")}, timeout=60)
            elif message.sticker:
                requests.post(f"{url_base}/sendSticker",
                    data=data_base, files={"sticker": ("sticker.webp", media_bytes, "image/webp")}, timeout=30)
            print(f"[{hora}] Media reenviada al dueño via Bot API.")
        except Exception as e:
            print(f"[{hora}] Error al reenviar media: {e}")

# Chats donde el dueño de la cuenta escribio primero (no responder automaticamente)
chats_iniciados_por_mi = set()

# ==========================================
# 3. INTERCEPTAR MENSAJES ENVIADOS POR MI (para saber que YO inicie el chat)
# ==========================================
@app.on_message(filters.private & filters.me & filters.outgoing)
async def registrar_mis_mensajes(client, message: Message):
    """Registra cuando el dueno envia un mensaje — ese chat no recibe respuesta automatica."""
    if message.chat.id not in chats_iniciados_por_mi:
        chats_iniciados_por_mi.add(message.chat.id)


@app.on_message(filters.private & filters.incoming & filters.user(BOT_ID))
async def limpiar_historial_confirmado(client, message: Message):
    """Limpia solo la copia local del chat con el bot tras la confirmacion del propietario."""
    if message.chat.id != BOT_ID or message.text != "/__limpiar_confirmado":
        return

    try:
        ids_mensajes = []
        async for mensaje in client.get_chat_history(BOT_ID):
            ids_mensajes.append(mensaje.id)

        for inicio in range(0, len(ids_mensajes), 100):
            await client.delete_messages(
                BOT_ID,
                ids_mensajes[inicio:inicio + 100],
                revoke=False,
            )
    except Exception as exc:
        print(f"[ERROR] No se pudo limpiar el historial del bot: {type(exc).__name__}")

# ==========================================
# 4. INTERCEPTAR MENSAJES PRIVADOS ENTRANTES
# ==========================================
@app.on_message(filters.private & ~filters.me & filters.incoming)
async def interceptar(client, message: Message):
    try:
        user_id = message.from_user.id
        print(f"[DEBUG] Mensaje privado interceptado: user_id={user_id}, MI_ID={MI_ID}")

        # ANTI-BUCLE: ignorar mensajes del propio bot
        if user_id == BOT_ID:
            print(f"[DEBUG] Ignorado: es del bot")
            return
        
        # Ignorar si el mensaje lo envio el propio dueno de la cuenta
        if user_id == MI_ID:
            print(f"[DEBUG] Ignorado: es del dueno de la cuenta")
            return

        nombre   = message.from_user.first_name
        username = message.from_user.username
        nombre_completo = f"{nombre} (@{username})" if username else nombre

        # Si YO inicie este chat, no enviar respuesta automatica
        yo_inicie = message.chat.id in chats_iniciados_por_mi

        # Clasificar contenido
        if message.text:
            tipo = "texto"
            contenido = message.text
        elif message.photo:
            tipo = "foto"
            contenido = "[Foto]"
        elif message.video:
            tipo = "video"
            contenido = "[Video]"
        elif message.audio or message.voice:
            tipo = "audio"
            contenido = "[Audio/Voz]"
        elif message.document:
            tipo = "documento"
            nombre_archivo = getattr(message.document, 'file_name', 'sin nombre')
            contenido = f"[Archivo: {nombre_archivo}]"
        elif message.sticker:
            tipo = "sticker"
            emoji = getattr(message.sticker, 'emoji', '')
            contenido = f"[Sticker {emoji}]"
        else:
            tipo = "texto"
            contenido = "[Mensaje desconocido]"

        print(f"[{time.strftime('%H:%M:%S')}] Directo de {nombre_completo}: {contenido}")

        # Notificar al dueño con foto de perfil + reenviar media si aplica
        await notificar_con_foto(client, message, user_id, nombre_completo, contenido, tipo)

        # Respuesta automatica solo la primera vez Y solo si ellos iniciaron el chat
        if user_id not in usuarios_respondidos and not yo_inicie:
            ARCHIVO_GIF = "FlamieSpeedDraw.gif"
            ruta_gif = str(BASE_DIR / ARCHIVO_GIF)
            texto_respuesta = (
                "【✦】•━【𝙵𝚕𝚊𝚖𝚒𝚎 𝚂𝚙𝚎𝚎𝚍𝚍𝚛𝚊𝚠】━•【✦】\n"
                "╭┈➤Nᴏ Aᴄᴇᴘᴛᴏ Mᴇɴsᴀᴊᴇs Dɪʀᴇᴄᴛᴏs.\n"
                "│━━━━━━━━━━━━━━━━━━━━\n"
                "│Sɪ Tɪᴇɴᴇs Uɴᴀ Sᴏʟɪᴄɪᴛᴜᴅ\n"
                "│Esᴄʀɪʙᴇ A Mɪ Bᴏᴛ Oғɪᴄɪᴀʟ \n"
                "│✧๑⋅@Flamie_SpeeddrawBot⋅๑✧\n"
                "│━━━━━━━━━━━━━━━━━━━━\n"
                "╰┈➤Tᴜ Mᴇɴsᴀᴊᴇ Nᴏ Sᴇʀᴀ Lᴇɪᴅᴀ Aqᴜɪ.\n"
                "【✦】•━【𝙵𝚕𝚊𝚖𝚒𝚎 𝚂𝚙𝚎𝚎𝚍𝚍𝚛𝚊𝚠】━•【✦】"
            )
            try:
                file_id = _gif_id_cache.get(ARCHIVO_GIF)
                enviado = await message.reply_animation(
                    animation=file_id or ruta_gif,
                    caption=texto_respuesta
                )
                if not file_id and enviado and enviado.animation:
                    _gif_id_cache[ARCHIVO_GIF] = enviado.animation.file_id
            except Exception as e:
                print(f"No se pudo enviar el GIF: {e}")
                try:
                    await message.reply_text(texto_respuesta)
                except Exception as e2:
                    print(f"Error al enviar texto: {e2}")

            usuarios_respondidos.add(user_id)
            print(f"[{time.strftime('%H:%M:%S')}] Respuesta automatica enviada a {nombre_completo}")
        else:
            if yo_inicie:
                print(f"[{time.strftime('%H:%M:%S')}] {nombre_completo} responde a un chat que TU iniciaste. Sin respuesta automatica.")
            else:
                print(f"[{time.strftime('%H:%M:%S')}] {nombre_completo} ya fue respondido. Solo notificado.")
    
    except Exception as e:
        print(f"[ERROR] Error en handler interceptar: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()

# ==========================================
# 4. ARRANQUE
# ==========================================
def iniciar_userbot():
    """Inicia el userbot con Pyrogram."""
    try:
        print("Iniciando el Recepcionista (Cuenta Personal)...")
        print(f"Ignorando mensajes del bot ID: {BOT_ID}")
        print("Credenciales requeridas cargadas.")
        app.run()  # Ejecuta app.start() + idle()
        
    except Exception as e:
        print(f"[ERROR] Fallo al iniciar userbot: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        raise

if __name__ == "__main__":
    iniciar_userbot()
