"""Juegos, reacciones anime y niveles de actividad para grupos."""
import html
import logging
import random
import re
import time

import requests
import telebot

from flamie.database.db import get_db
from flamie.core.config import BOT_PUBLIC_LINK
from flamie.modules.settings import module_enabled_for_message

logger = logging.getLogger(__name__)
ANIME_ACTIONS = {
    "abrazar": "hug", "acariciar": "pat", "saludar": "wave", "besar": "kiss",
    "aplaudir": "clap", "bailar": "dance", "llorar": "cry", "reir": "laugh",
    "guinar": "wink", "sonrojar": "blush", "golpear": "punch", "morder": "bite",
}
QUIZ = [
    ("¿Cuál es el planeta más grande del sistema solar?", ["Marte", "Júpiter", "Venus", "Mercurio"], 1),
    ("¿Cuántos lados tiene un hexágono?", ["5", "6", "7", "8"], 1),
    ("¿Cuál es el océano más grande?", ["Atlántico", "Índico", "Pacífico", "Ártico"], 2),
    ("¿Qué elemento químico tiene el símbolo O?", ["Oro", "Osmio", "Oxígeno", "Ozono"], 2),
    ("¿Cuántos minutos tiene una hora?", ["30", "45", "60", "90"], 2),
]
TRUTH = ["¿Qué talento te gustaría tener?", "¿Cuál fue tu momento más vergonzoso?", "¿Qué canción te sabes de memoria?", "¿Qué meta quieres cumplir este año?"]
DARE = ["Escribe una frase usando solo emojis.", "Envía un cumplido sincero a alguien del grupo.", "Describe tu día usando tres palabras.", "Escribe tu nombre al revés."]


def register_fun(bot):
    @bot.message_handler(commands=["top"])
    def cmd_top(message):
        try:
            if message.chat.type not in ("group", "supergroup"):
                bot.reply_to(message, "Usa /top dentro de un grupo.")
                return
            if not module_enabled_for_message(message, "fun"):
                return
            con = get_db()
            try:
                rows = con.execute(
                    "SELECT user_id,first_name,username,xp,count FROM group_activity "
                    "WHERE chat_id=? ORDER BY xp DESC,count DESC LIMIT 10", (message.chat.id,),
                ).fetchall()
            finally:
                con.close()
            if not rows:
                bot.reply_to(message, "Todavía no hay actividad registrada.")
                return
            lines = ["<b>🏆 Tabla de actividad</b>"]
            for rank, row in enumerate(rows, 1):
                name = row[1] or (f"@{row[2]}" if row[2] else f"Usuario {row[0]}")
                level = int(row[3]) // 100 + 1
                lines.append(f"{rank}. <a href=\"tg://user?id={row[0]}\">{html.escape(name)}</a> · nivel {level} · {row[3]} XP · {row[4]} mensajes")
            bot.reply_to(message, "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            logger.exception("Falló /top")
            bot.reply_to(message, "No pude consultar la tabla de actividad.")

    @bot.message_handler(commands=["quiz"])
    def cmd_quiz(message):
        try:
            if not module_enabled_for_message(message, "fun"):
                return
            question, options, answer = random.choice(QUIZ)
            nonce = int(time.time()) % 1000000
            keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)
            buttons = [telebot.types.InlineKeyboardButton(
                option, callback_data=f"funquiz:{nonce}:{index}:{answer}"
            ) for index, option in enumerate(options)]
            keyboard.add(*buttons)
            bot.send_message(message.chat.id, f"🧠 <b>Quiz</b>\n{html.escape(question)}", parse_mode="HTML", reply_markup=keyboard, reply_to_message_id=message.message_id)
        except Exception:
            logger.exception("Falló /quiz")
            bot.reply_to(message, "No pude iniciar el quiz.")

    @bot.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith("funquiz:")))
    def callback_quiz(call):
        try:
            _, nonce, choice, answer = call.data.split(":")
            if call.message.date < int(time.time()) - 3600:
                bot.answer_callback_query(call.id, "Este quiz ya venció.", show_alert=True)
                return
            if int(choice) == int(answer):
                bot.answer_callback_query(call.id, "¡Correcto! 🎉")
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            else:
                bot.answer_callback_query(call.id, "No era esa. ¡Prueba otra!", show_alert=False)
        except Exception:
            logger.exception("Falló la respuesta del quiz")
            try:
                bot.answer_callback_query(call.id, "No pude procesar tu respuesta.")
            except Exception:
                logger.exception("No se pudo responder callback quiz")

    @bot.message_handler(commands=["ship"])
    def cmd_ship(message):
        try:
            if not module_enabled_for_message(message, "fun"):
                return
            first = message.from_user
            second = message.reply_to_message.from_user if message.reply_to_message and message.reply_to_message.from_user else None
            if not second:
                argument = (message.text or "").partition(" ")[2].strip()
                match = re.fullmatch(r"(\d+)\s+(\d+)", argument)
                if match:
                    a, b = map(int, match.groups())
                    if a == first.id:
                        second = type("User", (), {"id": b, "first_name": f"Usuario {b}"})()
                    else:
                        first = type("User", (), {"id": a, "first_name": f"Usuario {a}"})()
                        second = type("User", (), {"id": b, "first_name": f"Usuario {b}"})()
            if not second:
                bot.reply_to(message, "Responde a alguien con /ship o usa /ship ID1 ID2.")
                return
            low_id, high_id = sorted((first.id, second.id))
            score = (low_id * 31 + high_id * 17) % 101
            names = " + ".join(html.escape(x.first_name or "Usuario") for x in (first, second))
            bot.reply_to(message, f"💞 {names}\nCompatibilidad: <b>{score}%</b>", parse_mode="HTML")
        except Exception:
            logger.exception("Falló /ship")
            bot.reply_to(message, "No pude calcular esa compatibilidad.")

    @bot.message_handler(commands=["verdadoreto"])
    def cmd_truth_or_dare(message):
        try:
            if not module_enabled_for_message(message, "fun"):
                return
            choice = random.choice(("Verdad", "Reto"))
            item = random.choice(TRUTH if choice == "Verdad" else DARE)
            bot.reply_to(message, f"<b>{choice}:</b> {html.escape(item)}", parse_mode="HTML")
        except Exception:
            logger.exception("Falló /verdadoreto")
            bot.reply_to(message, "No pude elegir una opción ahora.")

    @bot.message_handler(func=lambda m: bool(m.text and re.match(r"^/(?:" + "|".join(ANIME_ACTIONS) + r")(?:@[A-Za-z0-9_]+)?(?:\s|$)", m.text, re.IGNORECASE)))
    def cmd_anime_reaction(message):
        try:
            if not module_enabled_for_message(message, "fun"):
                return
            command = message.text.split()[0].split("@")[0].lstrip("/").lower()
            category = ANIME_ACTIONS[command]
            response = requests.get(
                f"https://nekos.best/api/v2/{category}",
                headers={"User-Agent": f"FlamieTelegramBot ({BOT_PUBLIC_LINK})"}, timeout=(3, 10),
            )
            response.raise_for_status()
            results = response.json().get("results") or []
            url = results[0].get("url") if results else None
            if not url or not url.startswith("https://nekos.best/"):
                raise ValueError("URL de reacción no válida")
            actor = html.escape(message.from_user.first_name or "Alguien")
            recipient = message.reply_to_message.from_user.first_name if message.reply_to_message and message.reply_to_message.from_user else "el grupo"
            caption = f"{actor} {command} a {html.escape(recipient)}"
            bot.send_animation(message.chat.id, url, caption=caption, reply_to_message_id=message.message_id)
        except Exception:
            logger.exception("Falló la reacción anime")
            bot.reply_to(message, "No pude cargar esa reacción ahora.")
