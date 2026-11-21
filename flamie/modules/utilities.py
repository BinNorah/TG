"""Utilidades: traducción, voz, clima, cálculo, notas de usuario y recordatorios."""
import ast
import html
import logging
import math
import os
import re
import threading
import time
from urllib.parse import quote
from io import BytesIO

import requests
from flamie.database.db import get_db, query
from flamie.modules.settings import chat_language, get_chat_setting, module_enabled_for_message
from flamie.utils.cache import get_chat_member_cached

logger = logging.getLogger(__name__)
_reminder_lock = threading.Lock()
_reminder_started = False
_stop_reminders = threading.Event()

# Códigos de idioma aceptados como destino en /tr. Se valida contra esta lista porque
# palabras cortas en español («yo», «la», «me») también encajan con el patrón [a-z]{2,3}.
LANGUAGE_CODES = {
    "af", "sq", "ar", "hy", "az", "eu", "be", "bn", "bs", "bg", "ca", "zh", "hr", "cs", "da",
    "nl", "en", "eo", "et", "fi", "fr", "gl", "ka", "de", "el", "gu", "ht", "he", "hi", "hu",
    "is", "id", "ga", "it", "ja", "kn", "kk", "ko", "ku", "la", "lv", "lt", "mk", "ms", "mt",
    "mr", "mn", "ne", "no", "fa", "pl", "pt", "pa", "ro", "ru", "sr", "sk", "sl", "es", "sw",
    "sv", "tl", "ta", "te", "th", "tr", "uk", "ur", "uz", "vi", "cy", "yi", "zu",
}

HTTP_HEADERS = {"User-Agent": "FlamieBot/1.0 (Telegram utility commands)"}
WMO_CODES = {
    0: "Despejado", 1: "Mayormente despejado", 2: "Parcialmente nublado", 3: "Nublado",
    45: "Niebla", 48: "Niebla con escarcha", 51: "Llovizna ligera", 53: "Llovizna",
    55: "Llovizna intensa", 56: "Llovizna helada ligera", 57: "Llovizna helada intensa",
    61: "Lluvia ligera", 63: "Lluvia", 65: "Lluvia intensa", 66: "Lluvia helada ligera",
    67: "Lluvia helada intensa", 71: "Nieve ligera", 73: "Nieve", 75: "Nieve intensa",
    77: "Granos de nieve", 80: "Chubascos ligeros", 81: "Chubascos", 82: "Chubascos intensos",
    85: "Chubascos de nieve", 86: "Chubascos de nieve intensos", 95: "Tormenta",
    96: "Tormenta con granizo ligero", 99: "Tormenta con granizo intenso",
}


def _arguments(message):
    return (message.text or "").partition(" ")[2].strip()


def _reply_text(message):
    replied = getattr(message, "reply_to_message", None)
    if not replied:
        return ""
    return (replied.text or replied.caption or "").strip()


def _translate_with_fallback(text, target):
    """Traducción automática de respaldo cuando la instancia LibreTranslate falla."""
    from langdetect import DetectorFactory, detect

    DetectorFactory.seed = 0
    source = detect(text)
    target = target.split("-", 1)[0].lower()
    if source == target:
        return text
    if len(text.encode("utf-8")) > 500:
        raise ValueError("El servicio alternativo admite hasta 500 bytes por consulta.")
    response = requests.get(
        "https://api.mymemory.translated.net/get",
        params={"q": text, "langpair": f"{source}|{target}", "mt": "1"},
        headers=HTTP_HEADERS,
        timeout=(3, 12),
    )
    response.raise_for_status()
    data = response.json()
    translated = (data.get("responseData") or {}).get("translatedText", "").strip()
    if data.get("responseStatus") != 200 or not translated:
        raise ValueError("El proveedor alternativo no devolvió una traducción.")
    return translated


def _calc_node(node, depth=0):
    if depth > 20:
        raise ValueError("La expresión es demasiado compleja.")
    if isinstance(node, ast.Expression):
        return _calc_node(node.body, depth + 1)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        result = node.value
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _calc_node(node.operand, depth + 1)
        result = value if isinstance(node.op, ast.UAdd) else -value
    elif isinstance(node, ast.BinOp):
        left, right = _calc_node(node.left, depth + 1), _calc_node(node.right, depth + 1)
        operations = {
            ast.Add: lambda: left + right, ast.Sub: lambda: left - right,
            ast.Mult: lambda: left * right, ast.Div: lambda: left / right,
            ast.FloorDiv: lambda: left // right, ast.Mod: lambda: left % right,
            ast.Pow: lambda: left ** right,
        }
        operation = operations.get(type(node.op))
        if operation is None:
            raise ValueError("Operador no permitido.")
        if isinstance(node.op, ast.Pow) and abs(right) > 10:
            raise ValueError("El exponente máximo permitido es 10.")
        result = operation()
    else:
        raise ValueError("Solo se permiten números y operaciones aritméticas básicas.")
    if not math.isfinite(float(result)) or abs(result) > 1e100:
        raise ValueError("El resultado excede el rango permitido.")
    return result


def _calculate(expression):
    if not expression or len(expression) > 120:
        raise ValueError("Escribe una expresión de hasta 120 caracteres.")
    tree = ast.parse(expression, mode="eval")
    return _calc_node(tree)


def _format_duration(seconds):
    if seconds < 60:
        return f"{seconds} segundos"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} horas"
    if seconds % 60 == 0:
        return f"{seconds // 60} minutos"
    return f"{seconds} segundos"


def _parse_reminder(argument):
    match = re.fullmatch(r"\s*(\d{1,5})(s|m|h|d)\s+(.{1,2500})\s*", argument, re.DOTALL | re.IGNORECASE)
    if not match:
        raise ValueError("Uso: /recordar 10m texto, con un plazo de 30 segundos a 30 días.")
    value, unit, text = match.groups()
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit.lower()]
    seconds = int(value) * multiplier
    if not 30 <= seconds <= 30 * 86400:
        raise ValueError("El recordatorio debe programarse entre 30 segundos y 30 días.")
    return seconds, text.strip()


def _deliver_due_reminders(bot):
    con = get_db()
    try:
        rows = con.execute(
            "SELECT id,chat_id,user_id,text,attempts FROM reminders "
            "WHERE status='pending' AND due_at<=? ORDER BY due_at LIMIT 30",
            (int(time.time()),),
        ).fetchall()
    finally:
        con.close()
    for reminder_id, chat_id, user_id, text, attempts in rows:
        try:
            body = f"⏰ <a href=\"tg://user?id={user_id}\">Tienes un recordatorio</a>:\n{html.escape(text)}"
            bot.send_message(chat_id, body, parse_mode="HTML")
            con = get_db()
            try:
                con.execute("UPDATE reminders SET status='sent' WHERE id=? AND status='pending'", (reminder_id,))
                con.commit()
            finally:
                con.close()
        except Exception:
            logger.exception("No se pudo entregar reminder_id=%s", reminder_id)
            con = get_db()
            try:
                if attempts >= 4:
                    con.execute("UPDATE reminders SET status='failed',attempts=attempts+1 WHERE id=?", (reminder_id,))
                else:
                    con.execute("UPDATE reminders SET attempts=attempts+1,due_at=? WHERE id=?", (int(time.time()) + 60, reminder_id))
                con.commit()
            finally:
                con.close()


def _reminder_worker(bot):
    while not _stop_reminders.wait(10):
        try:
            _deliver_due_reminders(bot)
        except Exception:
            logger.exception("Falló el procesador de recordatorios")


def start_reminder_worker(bot):
    global _reminder_started
    with _reminder_lock:
        if _reminder_started:
            return
        _reminder_started = True
        threading.Thread(target=_reminder_worker, args=(bot,), name="reminder-worker", daemon=True).start()


def register_utilities(bot, owner_id):
    @bot.message_handler(commands=["tr"])
    def cmd_translate(message):
        try:
            if not module_enabled_for_message(message, "utilities"):
                return
            argument = _arguments(message)
            # Si no se indica destino, traducir al idioma configurado para este chat.
            target = chat_language(message.chat.id)
            parts = argument.split(maxsplit=1)
            if parts and re.fullmatch(r"[a-z]{2,3}(?:-[A-Z]{2})?", parts[0]) and parts[0].split("-")[0] in LANGUAGE_CODES:
                target = parts[0]
                text = parts[1] if len(parts) > 1 else _reply_text(message)
            else:
                text = argument or _reply_text(message)
            if not text or len(text) > 4000:
                bot.reply_to(message, "Uso: responde a un texto con /tr [idioma] o escribe /tr idioma texto.")
                return
            base_url = os.getenv("LIBRETRANSLATE_URL", "https://libretranslate.com").rstrip("/")
            payload = {"q": text, "source": "auto", "target": target, "format": "text"}
            api_key = os.getenv("LIBRETRANSLATE_API_KEY", "").strip()
            if api_key:
                payload["api_key"] = api_key
            try:
                response = requests.post(
                    f"{base_url}/translate", json=payload, headers=HTTP_HEADERS, timeout=(3, 12)
                )
                response.raise_for_status()
                translated = response.json().get("translatedText", "")
                if not translated:
                    raise ValueError("La API no devolvió una traducción.")
            except Exception:
                logger.warning("LibreTranslate no respondió; se probará el proveedor alternativo.", exc_info=True)
                translated = _translate_with_fallback(text, target)
            bot.reply_to(message, html.escape(translated[:4000]), parse_mode="HTML")
        except Exception:
            logger.exception("Falló /tr")
            bot.reply_to(message, "No pude traducirlo. El servicio alternativo puede estar temporalmente limitado; inténtalo de nuevo o indica un idioma destino, por ejemplo /tr es hello.")

    @bot.message_handler(commands=["tts"])
    def cmd_tts(message):
        try:
            if not module_enabled_for_message(message, "utilities"):
                return
            text = _arguments(message) or _reply_text(message)
            if not text or len(text) > 2500:
                bot.reply_to(message, "Uso: /tts texto o responde a un mensaje con /tts. Máximo 2500 caracteres.")
                return
            from gtts import gTTS
            lang = "es" if chat_language(message.chat.id) == "es" else "en"
            try:
                from langdetect import DetectorFactory, detect
                DetectorFactory.seed = 0
                detected = detect(text).split("-", 1)[0]
                if detected in LANGUAGE_CODES:
                    lang = detected
            except Exception:
                logger.debug("No se pudo detectar el idioma del texto para /tts", exc_info=True)
            audio = BytesIO()
            gTTS(text=text, lang=lang, timeout=(3, 12)).write_to_fp(audio)
            audio.seek(0)
            audio.name = "flamie-tts.mp3"
            bot.send_audio(message.chat.id, audio, reply_to_message_id=message.message_id)
        except Exception:
            logger.exception("Falló /tts")
            bot.reply_to(message, "No pude generar el audio. Inténtalo de nuevo más tarde.")

    @bot.message_handler(commands=["clima"])
    def cmd_weather(message):
        try:
            if not module_enabled_for_message(message, "utilities"):
                return
            city = _arguments(message)
            if not city or len(city) > 100:
                bot.reply_to(message, "Uso: /clima ciudad")
                return
            geo_response = requests.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": city, "count": 1, "language": "es", "format": "json"},
                headers=HTTP_HEADERS,
                timeout=(3, 10),
            )
            geo_response.raise_for_status()
            places = geo_response.json().get("results") or []
            if not places:
                bot.reply_to(message, "No encontré esa ciudad.")
                return
            place = places[0]
            weather_response = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": place["latitude"], "longitude": place["longitude"],
                    "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
                    "timezone": "auto",
                },
                headers=HTTP_HEADERS,
                timeout=(3, 10),
            )
            weather_response.raise_for_status()
            current = weather_response.json()["current"]
            code = int(current.get("weather_code", -1))
            region = ", ".join(x for x in [place.get("admin1"), place.get("country")] if x)
            body = (
                f"<b>Clima de {html.escape(place['name'])}</b> · {html.escape(region)}\n"
                f"{html.escape(WMO_CODES.get(code, 'Condición no especificada'))}\n"
                f"Temperatura: <b>{current.get('temperature_2m')} °C</b> · sensación: {current.get('apparent_temperature')} °C\n"
                f"Humedad: {current.get('relative_humidity_2m')}% · viento: {current.get('wind_speed_10m')} km/h\n"
                "Fuente: Open-Meteo"
            )
            bot.reply_to(message, body, parse_mode="HTML")
        except Exception:
            logger.exception("Falló /clima")
            bot.reply_to(message, "No pude consultar el clima de esa ciudad.")

    @bot.message_handler(commands=["calc"])
    def cmd_calc(message):
        try:
            if not module_enabled_for_message(message, "utilities"):
                return
            expression = _arguments(message)
            try:
                result = _calculate(expression)
            except (ValueError, SyntaxError, ZeroDivisionError, OverflowError):
                bot.reply_to(message, "Expresión no válida. Ejemplo: /calc (12 + 8) * 3 ** 2")
                return
            if isinstance(result, float) and result.is_integer():
                result = int(result)
            bot.reply_to(message, f"<code>{html.escape(expression)}</code> = <b>{html.escape(str(result))}</b>", parse_mode="HTML")
        except Exception:
            logger.exception("Falló /calc")
            bot.reply_to(message, "No pude calcular esa expresión.")

    @bot.message_handler(commands=["uinfo"])
    def cmd_uinfo(message):
        try:
            if not module_enabled_for_message(message, "utilities"):
                return
            if message.chat.type == "private":
                bot.reply_to(message, "Usa /uinfo dentro de un grupo, respondiendo a un mensaje o indicando ID/@usuario.")
                return
            target_id = message.reply_to_message.from_user.id if message.reply_to_message and message.reply_to_message.from_user else None
            argument = _arguments(message)
            if target_id is None and argument:
                target = argument.split()[0]
                if target.isdecimal():
                    target_id = int(target)
                elif target.startswith("@"):
                    rows = query(
                        "SELECT user_id FROM chat_users WHERE chat_id=? AND lower(username)=? ORDER BY updated_at DESC LIMIT 2",
                        (message.chat.id, target.lstrip("@").casefold()),
                    )
                    if len(rows) == 1:
                        target_id = int(rows[0][0])
            if target_id is None:
                target_id = message.from_user.id
            member = get_chat_member_cached(bot, message.chat.id, target_id)
            user = member.user
            full_name = " ".join(x for x in [user.first_name, user.last_name] if x) or "Usuario"
            nickname = get_chat_setting(message.chat.id, f"user_tag:{target_id}")
            username = f"@{user.username}" if user.username else "Sin username"
            try:
                chat_member = bot.get_chat_member(message.chat.id, target_id)
                status = chat_member.status
            except Exception:
                status = member.status
            body = (
                f"<b>Información del usuario</b>\nNombre: {html.escape(full_name)}\n"
                + (f"Apodo local: <b>{html.escape(nickname)}</b>\n" if nickname else "")
                + f"Username: {html.escape(username)}\nID: <code>{target_id}</code>\n"
                + f"Estado en el grupo: {html.escape(status)}"
            )
            bot.reply_to(message, body, parse_mode="HTML")
        except Exception:
            logger.exception("Falló /uinfo")
            bot.reply_to(message, "No pude obtener la información de ese usuario.")

    @bot.message_handler(commands=["wiki"])
    def cmd_wiki(message):
        try:
            if not module_enabled_for_message(message, "utilities"):
                return
            term = _arguments(message)
            if not term or len(term) > 200:
                bot.reply_to(message, "Uso: /wiki tema")
                return
            language = "en" if chat_language(message.chat.id) == "en" else "es"
            domain = f"{language}.wikipedia.org"
            search = requests.get(
                f"https://{domain}/w/rest.php/v1/search/page",
                params={"q": term, "limit": 1}, headers=HTTP_HEADERS, timeout=(3, 10),
            )
            search.raise_for_status()
            pages = search.json().get("pages") or []
            if not pages:
                bot.reply_to(message, "No encontré ese tema en Wikipedia.")
                return
            page = pages[0]
            title = page.get("title", term)
            key = page.get("key", title.replace(" ", "_"))
            summary_response = requests.get(
                f"https://{domain}/api/rest_v1/page/summary/{quote(key, safe='')}",
                headers=HTTP_HEADERS, timeout=(3, 10),
            )
            summary_response.raise_for_status()
            summary = summary_response.json()
            extract = summary.get("extract") or page.get("excerpt", "")
            fallback_url = f"https://{domain}/wiki/{quote(key, safe='')}"
            url = summary.get("content_urls", {}).get("desktop", {}).get("page", fallback_url)
            if not isinstance(url, str) or not url.startswith(f"https://{domain}/wiki/"):
                url = fallback_url
            body = f"<b>{html.escape(title)}</b>\n{html.escape(extract[:3000])}\n\n<a href=\"{html.escape(url, quote=True)}\">Leer en Wikipedia</a>"
            bot.reply_to(message, body, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            logger.exception("Falló /wiki")
            bot.reply_to(message, "No pude consultar Wikipedia ahora.")

    @bot.message_handler(commands=["recordar"])
    def cmd_remind(message):
        try:
            if not module_enabled_for_message(message, "utilities"):
                return
            seconds, text = _parse_reminder(_arguments(message))
            due_at = int(time.time()) + seconds
            con = get_db()
            try:
                con.execute(
                    "INSERT INTO reminders(chat_id,user_id,text,due_at) VALUES(?,?,?,?)",
                    (message.chat.id, message.from_user.id, text, due_at),
                )
                con.commit()
            finally:
                con.close()
            bot.reply_to(message, f"Listo, te recordaré en {_format_duration(seconds)}.")
        except ValueError as exc:
            bot.reply_to(message, html.escape(str(exc)))
        except Exception:
            logger.exception("Falló /recordar")
            bot.reply_to(message, "No pude guardar el recordatorio.")

    @bot.message_handler(commands=["afk"])
    def cmd_afk(message):
        try:
            if message.chat.type not in ("group", "supergroup"):
                bot.reply_to(message, "Usa /afk dentro de un grupo.")
                return
            if not module_enabled_for_message(message, "utilities"):
                return
            reason = _arguments(message)[:300]
            con = get_db()
            try:
                if reason.lower() in {"off", "no", "volver"}:
                    con.execute("DELETE FROM chat_afk WHERE chat_id=? AND user_id=?", (message.chat.id, message.from_user.id))
                else:
                    con.execute(
                        "INSERT INTO chat_afk(chat_id,user_id,reason) VALUES(?,?,?) "
                        "ON CONFLICT(chat_id,user_id) DO UPDATE SET reason=excluded.reason,created_at=CURRENT_TIMESTAMP",
                        (message.chat.id, message.from_user.id, reason),
                    )
                con.commit()
            finally:
                con.close()
            if reason.lower() in {"off", "no", "volver"}:
                bot.reply_to(message, "Estado AFK quitado.")
            else:
                bot.reply_to(message, "Quedas marcado como AFK." + (f" Motivo: {html.escape(reason)}" if reason else ""), parse_mode="HTML")
        except Exception:
            logger.exception("Falló /afk")
            bot.reply_to(message, "No pude activar el estado AFK.")

    @bot.message_handler(commands=["encuesta"])
    def cmd_poll(message):
        try:
            if not module_enabled_for_message(message, "utilities"):
                return
            parts = [part.strip() for part in _arguments(message).split("|")]
            if len(parts) < 3 or not parts[0] or any(not option for option in parts[1:]) or len(parts) > 11:
                bot.reply_to(message, "Uso: /encuesta pregunta | opción 1 | opción 2 (hasta 10 opciones).")
                return
            question, options = parts[0], parts[1:]
            if len(question) > 300 or any(len(option) > 100 for option in options):
                bot.reply_to(message, "La pregunta admite 300 caracteres y cada opción 100.")
                return
            bot.send_poll(message.chat.id, question, options, is_anonymous=True, reply_to_message_id=message.message_id)
        except Exception:
            logger.exception("Falló /encuesta")
            bot.reply_to(message, "No pude crear la encuesta.")

    @bot.message_handler(commands=["idsticker"])
    def cmd_id_sticker(message):
        try:
            if not module_enabled_for_message(message, "quote"):
                return
            source = message.reply_to_message
            sticker = getattr(source, "sticker", None) if source else None
            if not sticker:
                bot.reply_to(message, "Responde a un sticker con /idsticker.")
                return
            body = f"<b>file_id</b>\n<code>{html.escape(sticker.file_id)}</code>\n<b>file_unique_id</b>\n<code>{html.escape(sticker.file_unique_id)}</code>"
            bot.reply_to(message, body, parse_mode="HTML")
        except Exception:
            logger.exception("Falló /idsticker")
            bot.reply_to(message, "No pude leer los datos de ese sticker.")

    @bot.message_handler(commands=["getsticker"])
    def cmd_get_sticker(message):
        try:
            if not module_enabled_for_message(message, "quote"):
                return
            source = message.reply_to_message
            sticker = getattr(source, "sticker", None) if source else None
            sticker_id = sticker.file_id if sticker else _arguments(message)
            if not sticker_id or len(sticker_id) > 1024:
                bot.reply_to(message, "Responde a un sticker o usa /getsticker <file_id>.")
                return
            bot.send_sticker(message.chat.id, sticker_id, reply_to_message_id=message.message_id)
        except Exception:
            logger.exception("Falló /getsticker")
            bot.reply_to(message, "No pude enviar ese sticker. Comprueba el file_id.")
