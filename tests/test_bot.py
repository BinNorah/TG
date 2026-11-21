"""Pruebas de regresión del bot (sin red). Ejecuta:  python -m unittest tests.test_bot -v"""
import time
import unittest

from tests import fake_telegram as ft
from tests.fake_telegram import (
    ADMIN, CALLS, GROUP, MEMBER, MEMBER2, OWNER, bot, click, make_message, methods, raw_message,
    raw_update, send, texts, user_json, chat_json,
)

L = ft.L
from flamie.modules.settings import module_enabled


def enable(*modules):
    for module in modules:
        L.set_chat_setting(GROUP, f"module:{module}", "1")


def say(text, **kw):
    calls, errors = send(text, **kw)
    return calls, errors


class BaseCase(unittest.TestCase):
    def assertClean(self, errors):
        self.assertEqual(errors, [], "Se registraron errores: %s" % errors)


class TestPublicCommands(BaseCase):
    def test_help_y_start_muestran_menu(self):
        for cmd in ("/start", "/help"):
            calls, errors = say(cmd)
            self.assertClean(errors)
            # Con los GIF presentes se envía una animación con el menú; si faltan, texto.
            self.assertTrue({"sendMessage", "sendAnimation"} & set(methods(calls)), cmd)

    def test_comandos_basicos_responden(self):
        for cmd in ("/info", "/id", "/chatid", "/rules", "/dado", "/8ball ¿sí?", "/calc 2+2*3",
                    "/ship", "/verdadoreto", "/top", "/staff"):
            calls, errors = say(cmd)
            self.assertClean(errors)
            self.assertTrue(texts(calls) or "sendPoll" in methods(calls), cmd)

    def test_calc(self):
        calls, _ = say("/calc 2+2*3")
        self.assertIn("8", texts(calls)[0])

    def test_tr_no_confunde_palabras_cortas_con_idiomas(self):
        # «yo» no es un idioma: no debe tratarse como destino (antes fallaba).
        import flamie.modules.utilities as u
        self.assertNotIn("yo", u.LANGUAGE_CODES)
        self.assertIn("en", u.LANGUAGE_CODES)


class TestAdminDefaults(BaseCase):
    def test_comandos_de_modulos_apagados_responden_y_activan(self):
        calls, errors = say("/filter hola | respuesta", frm=ADMIN)
        self.assertClean(errors)
        joined = " ".join(texts(calls))
        self.assertIn("Activé automáticamente", joined)
        self.assertIn("Filtro guardado", joined)

    def test_filter_y_save_aceptan_sintaxis_natural(self):
        enable("filters")
        calls, _ = say("/filter adios chao", frm=ADMIN)
        self.assertIn("Filtro guardado", texts(calls)[0])
        calls, _ = say("/save nota2 contenido dos", frm=ADMIN)
        self.assertIn("guardada", texts(calls)[0])
        calls, _ = say("/get nota2")
        self.assertIn("contenido dos", texts(calls)[0])
        calls, _ = say("#nota2")
        self.assertIn("contenido dos", texts(calls)[0])

    def test_miembro_no_puede_configurar(self):
        enable("filters")
        calls, _ = say("/filter x | y", frm=MEMBER)
        self.assertIn("Solo los administradores", texts(calls)[0])
        calls, _ = say("/ban 3001", frm=MEMBER)
        self.assertIn("Solo los administradores", texts(calls)[0])

    def test_aviso_de_modulo_desactivado_para_todos(self):
        L.set_chat_setting(GROUP, "module:fun", "0")
        calls, _ = say("/dado")
        self.assertIn("desactivado", texts(calls)[0])
        L.set_chat_setting(GROUP, "module:fun", "1")


class TestModeration(BaseCase):
    def test_warn_hasta_el_limite_aplica_ban(self):
        for i in range(3):
            calls, errors = say("/warn spam", frm=ADMIN, reply_to=MEMBER2)
            self.assertClean(errors)
        self.assertIn("banChatMember", methods(calls))
        L.query("SELECT 1")

    def test_ban_kick_mute_unmute(self):
        for cmd, expected in (("/ban motivo", "banChatMember"), ("/kick", "unbanChatMember"),
                              ("/mute", "restrictChatMember"), ("/unmute", "restrictChatMember"),
                              (".ban x", "banChatMember")):
            calls, errors = say(cmd, frm=ADMIN, reply_to=MEMBER)
            self.assertClean(errors)
            self.assertIn(expected, methods(calls), cmd)

    def test_unmute_respeta_permisos_del_grupo(self):
        calls, _ = say("/unmute", frm=ADMIN, reply_to=MEMBER)
        params = [p for n, p in calls if n == "restrictChatMember"][0]
        self.assertIn("permissions", params)

    def test_no_se_sanciona_a_admins_ni_a_uno_mismo(self):
        calls, _ = say("/ban", frm=ADMIN, reply_to=OWNER)
        self.assertIn("administrador", texts(calls)[0])
        calls, _ = say("/ban 2000", frm=ADMIN)
        self.assertIn("ti mismo", texts(calls)[0])

    def test_resolucion_de_usuarios_por_arroba(self):
        say("hola grupo", frm=MEMBER)  # el middleware registra al usuario
        rows = L.query("SELECT user_id FROM chat_users WHERE chat_id=? AND username='member_u'", (GROUP,))
        self.assertEqual(len(rows), 1)
        calls, errors = say("/mute @member_u 10m", frm=ADMIN)
        self.assertClean(errors)
        self.assertIn("restrictChatMember", methods(calls))

    def test_promote_demote(self):
        for cmd in ("/promote delete_messages pin_messages", "/demote"):
            calls, errors = say(cmd, frm=ADMIN, reply_to=MEMBER)
            self.assertClean(errors)
            self.assertIn("promoteChatMember", methods(calls))


class TestPanels(BaseCase):
    def test_todos_los_paneles_de_ayuda(self):
        for panel in ("info", "ocio", "anime", "stickers", "reglas", "utilidades",
                      "moderacion", "adminchat", "inicio"):
            calls, errors = click(f"help:{panel}", frm=ADMIN, animation=True)
            self.assertClean(errors)
            self.assertIn("editMessageCaption", methods(calls), panel)

    def test_panel_admin_denegado_a_miembros(self):
        calls, _ = click("help:adminchat", frm=MEMBER)
        self.assertIn("Acceso denegado", texts(calls)[0])

    def test_settings_alterna_modulos(self):
        before = module_enabled(GROUP, "fun")
        calls, errors = click("settings:toggle:fun", frm=ADMIN)
        self.assertClean(errors)
        self.assertNotEqual(before, module_enabled(GROUP, "fun"))
        click("settings:toggle:fun", frm=ADMIN)
        self.assertEqual(before, module_enabled(GROUP, "fun"))

    def test_settings_rechaza_a_miembros(self):
        calls, _ = click("settings:toggle:fun", frm=MEMBER)
        self.assertIn("administradores", texts(calls)[0])


class TestGroupEvents(BaseCase):
    def setUp(self):
        enable("welcome", "captcha", "join_requests")

    def tearDown(self):
        for m in ("welcome", "captcha", "join_requests", "antispam"):
            L.set_chat_setting(GROUP, f"module:{m}", "0")

    def test_bienvenida_y_captcha(self):
        msg = make_message(None, frm=ADMIN, content={"new_chat_members": [user_json(MEMBER2)]})
        calls, errors = raw_message(msg)
        self.assertClean(errors)
        out = " ".join(texts(calls))
        self.assertIn("Bienvenido", out)
        self.assertIn("pulsa el botón", out)
        calls, _ = click(f"captcha:{GROUP}:{MEMBER2}", frm=MEMBER)
        self.assertIn("otra persona", texts(calls)[0])
        calls, errors = click(f"captcha:{GROUP}:{MEMBER2}", frm=MEMBER2)
        self.assertClean(errors)
        self.assertIn("restrictChatMember", methods(calls))

    def test_despedida_no_usa_texto_de_bienvenida(self):
        msg = make_message(None, frm=MEMBER2, content={"left_chat_member": user_json(MEMBER2)})
        calls, _ = raw_message(msg)
        out = " ".join(texts(calls))
        self.assertIn("Hasta pronto", out)
        self.assertNotIn("Bienvenido", out)

    def test_solicitudes_de_ingreso_aprobar_y_rechazar(self):
        calls, errors = raw_update({"chat_join_request": {
            "chat": chat_json(GROUP), "from": user_json(MEMBER2),
            "user_chat_id": MEMBER2, "date": int(time.time())}})
        self.assertClean(errors)
        for action, api in (("approve", "approveChatJoinRequest"), ("decline", "declineChatJoinRequest")):
            calls, errors = click(f"join:{action}:{GROUP}:{MEMBER2}", frm=ADMIN)
            self.assertClean(errors)
            self.assertIn(api, methods(calls))

    def test_antispam_enlaces_y_palabras(self):
        enable("antispam")
        say("/antilinks on", frm=ADMIN)
        calls, _ = say("mira https://spam.com", frm=MEMBER2)
        self.assertIn("deleteMessage", methods(calls))
        calls, _ = say("mira https://ok.com", frm=ADMIN)
        self.assertNotIn("deleteMessage", methods(calls))
        say("/antilinks off", frm=ADMIN)
        say("/blockword add feo", frm=ADMIN)
        time.sleep(0.01)
        calls, _ = say("eso es feo", frm=MEMBER)
        self.assertIn("deleteMessage", methods(calls))
        say("/blockword del feo", frm=ADMIN)


class TestPrivateFlows(BaseCase):
    def test_foto_en_grupo_no_se_reenvia_al_dueno(self):
        photo = make_message(None, frm=MEMBER, content={"photo": [
            {"file_id": "p", "file_unique_id": "pu", "width": 1, "height": 1}]})
        calls, errors = raw_message(photo)
        self.assertClean(errors)
        self.assertNotIn("forwardMessage", methods(calls))
        self.assertEqual(texts(calls), [])

    def test_foto_privada_si_se_reenvia(self):
        photo = make_message(None, frm=MEMBER, chat="private", content={"photo": [
            {"file_id": "p", "file_unique_id": "pu", "width": 1, "height": 1}]})
        calls, errors = raw_message(photo)
        self.assertClean(errors)
        self.assertIn("forwardMessage", methods(calls))

    def test_voz_y_stickers_privados_se_reenvian(self):
        payloads = [
            {"voice": {"file_id": "v", "file_unique_id": "vu", "duration": 1}},
            {"sticker": {"file_id": "s", "file_unique_id": "su", "type": "regular", "width": 1,
                         "height": 1, "is_animated": False, "is_video": False}},
        ]
        for payload in payloads:
            calls, errors = raw_message(make_message(None, frm=MEMBER2, chat="private", content=payload))
            self.assertClean(errors)
            self.assertIn("forwardMessage", methods(calls))

    def test_flujo_de_activacion(self):
        say("/activar", frm=MEMBER, chat="private")
        calls, _ = say("Netflix", frm=MEMBER, chat="private")
        self.assertIn("codigo de activacion", texts(calls)[0])
        calls, _ = say("12345678", frm=MEMBER, chat="private")
        self.assertIn("recibido", texts(calls)[0])
        self.assertTrue(any("Plataforma: Netflix" in t for t in texts(calls)))

    def test_comando_r_del_propietario(self):
        calls, errors = say("/r 3000 hola desde el dueño", frm=OWNER, chat="private")
        self.assertClean(errors)
        sent = [p for n, p in calls if n == "sendMessage" and int(p["chat_id"]) == MEMBER]
        self.assertEqual(len(sent), 1)
        calls, _ = say("/r 3000 x", frm=MEMBER, chat="private")
        self.assertEqual(texts(calls), [])

    def test_respuesta_del_dueno_con_foto_se_copia(self):
        click("resp:dm:3000", frm=OWNER, chat="private")
        photo = make_message(None, frm=OWNER, chat="private", content={"photo": [
            {"file_id": "p", "file_unique_id": "pu", "width": 1, "height": 1}]})
        calls, errors = raw_message(photo)
        self.assertClean(errors)
        self.assertIn("copyMessage", methods(calls))


class TestOwner(BaseCase):
    def test_comandos_del_dueno(self):
        for cmd in ("/stats", "/grupos", "/logs", "/maintenance estado", "/backup", "/listcmd"):
            calls, errors = say(cmd, frm=OWNER, chat="private")
            self.assertClean(errors)
            self.assertTrue(calls, cmd)

    def test_comandos_del_dueno_no_responden_a_otros(self):
        calls, _ = say("/stats", frm=MEMBER, chat="private")
        self.assertEqual(texts(calls), [])

    def test_comandos_personalizados(self):
        say("/addcmd saludo | Hola {first_name}", frm=OWNER, chat="private")
        calls, _ = say("/saludo", frm=MEMBER)
        self.assertIn("Hola Member", texts(calls)[0])
        say("/delcmd saludo", frm=OWNER, chat="private")

    def test_menu_de_comandos_no_supera_100(self):
        captured = []
        original = bot.set_my_commands
        bot.set_my_commands = lambda cmds, scope=None, **k: captured.append(len(cmds)) or True
        try:
            for i in range(60):
                L.query("SELECT 1")
                con = L.get_db()
                con.execute("INSERT OR REPLACE INTO custom_commands(command,response) VALUES(?,?)", (f"cmd{i:02d}", "x"))
                con.commit(); con.close()
            L.registrar_comandos_menu()
        finally:
            bot.set_my_commands = original
        self.assertTrue(captured and max(captured) <= 100, captured)


if __name__ == "__main__":
    unittest.main()
