"""Simulador de la API de Telegram para probar el bot sin red ni token real.

Sustituye telebot.apihelper._make_request, registra cada llamada saliente (CALLS)
y los errores registrados en logs (ERRORS), y permite enviar mensajes/clics al bot.
"""
import itertools
import json
import logging
import os
import shutil
import sys
import time

import tempfile
TEST_DIR = tempfile.mkdtemp(prefix="flamie_test_")
os.environ.update(
    TELEGRAM_BOT_TOKEN="123456:TESTTOKEN",
    TELEGRAM_OWNER_ID="1000",
    DATA_DIR=TEST_DIR,
)
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

import telebot
from telebot import apihelper, types

BOT_ID = 123456
OWNER, ADMIN, MEMBER, MEMBER2 = 1000, 2000, 3000, 3001
GROUP = -1001234567890
USERS = {
    OWNER: ("Owner", "owner_u"), ADMIN: ("Admin", "admin_u"),
    MEMBER: ("Member", "member_u"), MEMBER2: ("Member2", "member2_u"),
}
ADMIN_IDS = {ADMIN}
CALLS = []
_mid = itertools.count(1000)
BOT_CAN = {}  # permisos del bot sobrescribibles por prueba


def user_json(uid):
    if uid == BOT_ID:
        return {"id": BOT_ID, "is_bot": True, "first_name": "Flamie", "username": "Flamie_SpeeddrawBot"}
    name, uname = USERS.get(uid, ("User%d" % uid, "u%d" % uid))
    return {"id": uid, "is_bot": False, "first_name": name, "username": uname}


def chat_json(cid):
    if cid > 0:
        return {"id": cid, "type": "private", "first_name": USERS.get(cid, ("U",))[0]}
    if cid == GROUP:
        return {"id": cid, "type": "supergroup", "title": "Grupo Test"}
    return {"id": cid, "type": "channel", "title": "Canal Logs"}


ADMIN_FLAGS = [
    "can_be_edited", "can_manage_chat", "can_delete_messages", "can_manage_video_chats",
    "can_restrict_members", "can_promote_members", "can_change_info", "can_invite_users",
    "can_post_messages", "can_edit_messages", "can_pin_messages", "is_anonymous",
    "can_manage_topics", "can_post_stories", "can_edit_stories", "can_delete_stories",
    "can_manage_direct_messages", "can_manage_tags",
]


def member_json(chat_id, uid):
    if uid == BOT_ID:
        d = {"status": "administrator", "user": user_json(uid)}
        for f in ADMIN_FLAGS:
            d[f] = True
        d["is_anonymous"] = False
        d.update(BOT_CAN)
        return d
    if uid == OWNER or uid in ADMIN_IDS:
        status = "creator" if uid == OWNER else "administrator"
        d = {"status": status, "user": user_json(uid)}
        if status == "creator":
            d["is_anonymous"] = False
        else:
            for f in ADMIN_FLAGS:
                d[f] = True
            d["is_anonymous"] = False
        return d
    return {"status": "member", "user": user_json(uid)}


def msg_json(chat_id, text=None, **extra):
    d = {"message_id": next(_mid), "date": int(time.time()), "chat": chat_json(chat_id),
         "from": user_json(BOT_ID)}
    if text is not None:
        d["text"] = text
    d.update(extra)
    return d


def fake_request(token, method_name, method="get", params=None, files=None, **kw):
    params = params or {}
    CALLS.append((method_name, dict(params)))
    m = method_name
    if m == "getMe":
        return user_json(BOT_ID)
    if m == "getChatMember":
        return member_json(int(params["chat_id"]), int(params["user_id"]))
    if m == "getChat":
        c = chat_json(int(params["chat_id"]))
        return c
    if m == "getChatAdministrators":
        return [member_json(GROUP, u) for u in [OWNER, *ADMIN_IDS]]
    if m == "getChatMemberCount":
        return 5
    if m == "getUserProfilePhotos":
        return {"total_count": 0, "photos": []}
    if m == "getFile":
        return {"file_id": params.get("file_id", "x"), "file_unique_id": "u", "file_path": "x/y.webp"}
    if m in ("sendAnimation",):
        return msg_json(int(params["chat_id"]), animation={
            "file_id": "ANIM_FILE_ID", "file_unique_id": "au", "width": 1, "height": 1, "duration": 1})
    if m in ("sendMessage", "sendPhoto", "sendSticker", "sendVoice", "sendDocument", "sendPoll",
             "sendVideo", "sendAudio", "forwardMessage", "copyMessage"):
        return msg_json(int(params["chat_id"]), params.get("text") or params.get("caption"))
    if m in ("editMessageText", "editMessageCaption", "editMessageReplyMarkup"):
        return msg_json(int(params["chat_id"]), params.get("text") or params.get("caption"))
    if m == "exportChatInviteLink":
        return "https://t.me/+abcdef"
    if m == "createChatInviteLink":
        return {"invite_link": "https://t.me/+abcdef", "creator": user_json(BOT_ID),
                "creates_join_request": False, "is_primary": False, "is_revoked": False}
    if m == "getMyCommands":
        return []
    return True


apihelper._make_request = fake_request

ERRORS = []


class Capture(logging.Handler):
    def emit(self, record):
        if record.levelno >= logging.ERROR:
            txt = record.getMessage()
            if record.exc_info:
                import traceback
                tb = "".join(traceback.format_exception(*record.exc_info)).strip().splitlines()
                txt += " | " + " / ".join(tb[-3:])
            ERRORS.append(f"[{record.name}] {txt}")


_cap = Capture()
logging.getLogger().addHandler(_cap)
logging.getLogger().setLevel(logging.WARNING)
telebot.logger.setLevel(logging.ERROR)

from flamie.modules import legacy_handlers as L  # noqa: E402

bot = L.bot
bot.threaded = False

_uid = itertools.count(1)


def make_message(text=None, frm=MEMBER, chat=GROUP, reply_to=None, content=None, entities=None,
                 reply_extra=None):
    cid = chat if chat != "private" else frm
    d = {"message_id": next(_mid), "date": int(time.time()), "chat": chat_json(cid),
         "from": user_json(frm)}
    if text is not None:
        d["text"] = text
        if text.startswith("/"):
            cmd = text.split()[0]
            d["entities"] = [{"type": "bot_command", "offset": 0, "length": len(cmd)}]
    if entities:
        d["entities"] = d.get("entities", []) + entities
    if content:
        d.update(content)
    if reply_to is not None:
        r = {"message_id": next(_mid), "date": int(time.time()), "chat": chat_json(cid),
             "from": user_json(reply_to), "text": "mensaje respondido de prueba"}
        if reply_extra:
            r.update(reply_extra)
        d["reply_to_message"] = r
    return d


def send(text=None, frm=MEMBER, chat=GROUP, **kw):
    """Envía un mensaje y devuelve (llamadas_api, errores)."""
    CALLS.clear()
    ERRORS.clear()
    m = make_message(text, frm, chat, **kw)
    upd = types.Update.de_json({"update_id": next(_uid), "message": m})
    try:
        bot.process_new_updates([upd])
    except Exception as exc:  # no debería escapar
        ERRORS.append(f"[ESCAPÓ] {type(exc).__name__}: {exc}")
    return list(CALLS), list(ERRORS)


def click(data, frm=ADMIN, chat=GROUP, message_text="panel", animation=False):
    CALLS.clear()
    ERRORS.clear()
    cid = chat if chat != "private" else frm
    base = {"message_id": next(_mid), "date": int(time.time()), "chat": chat_json(cid),
            "from": user_json(BOT_ID)}
    if animation:
        base["animation"] = {"file_id": "A", "file_unique_id": "a", "width": 1, "height": 1, "duration": 1}
        base["caption"] = message_text
    else:
        base["text"] = message_text
    cb = {"id": str(next(_uid)), "from": user_json(frm), "chat_instance": "ci",
          "message": base, "data": data}
    upd = types.Update.de_json({"update_id": next(_uid), "callback_query": cb})
    try:
        bot.process_new_updates([upd])
    except Exception as exc:
        ERRORS.append(f"[ESCAPÓ] {type(exc).__name__}: {exc}")
    return list(CALLS), list(ERRORS)


def summarize(calls, errors, maxlen=110):
    out = []
    for name, p in calls:
        if name in ("getMe", "getChatMember", "getChatAdministrators", "getChat", "setMyCommands",
                    "getUserProfilePhotos"):
            continue
        t = p.get("text") or p.get("caption") or ""
        if name == "answerCallbackQuery":
            t = p.get("text") or ""
        extra = ""
        if name in ("sendMessage", "editMessageText") and p.get("reply_markup"):
            try:
                kb = json.loads(p["reply_markup"])["inline_keyboard"]
                extra = " KB=" + "|".join(b.get("text", "?") for row in kb for b in row)[:90]
            except Exception:
                pass
        out.append(f"    -> {name}: {str(t)[:maxlen]!r}{extra}")
    for e in errors:
        out.append(f"    !! {e[:300]}")
    if not out:
        out.append("    (sin respuesta ni errores)")
    return "\n".join(out)


def t(label, text=None, **kw):
    calls, errors = send(text, **kw)
    print(f"[{label}] {text!r}")
    print(summarize(calls, errors))
    return calls, errors


def c(label, data, **kw):
    calls, errors = click(data, **kw)
    print(f"[{label}] click {data!r}")
    print(summarize(calls, errors))
    return calls, errors


def texts(calls):
    """Textos de los mensajes que el bot envió/editó (sin llamadas internas)."""
    out = []
    for name, p in calls:
        if name in ("sendMessage", "editMessageText", "editMessageCaption", "answerCallbackQuery"):
            out.append(str(p.get("text") or p.get("caption") or ""))
    return out


def methods(calls):
    return [name for name, _ in calls]


def raw_message(message_dict):
    CALLS.clear()
    ERRORS.clear()
    upd = types.Update.de_json({"update_id": next(_uid), "message": message_dict})
    bot.process_new_updates([upd])
    return list(CALLS), list(ERRORS)


def raw_update(update_fields):
    CALLS.clear()
    ERRORS.clear()
    bot.process_new_updates([types.Update.de_json({"update_id": next(_uid), **update_fields})])
    return list(CALLS), list(ERRORS)
