# Cambios de la reparación

Método: se simuló la API de Telegram y se ejecutó cada comando, panel y flujo (ver `tests/`). Contra el código original, 12 pruebas fallaban; ahora pasan las 32.

## Errores corregidos
1. **`@usuario` nunca funcionaba** (`/ban @x`, `/mute @x`, `/uinfo @x`, avisos AFK): el listener recibía `Message` y buscaba `update.message`, así que `chat_users` quedaba vacía. Ahora el middleware registra a los usuarios.
2. **Botones Aprobar/Rechazar de solicitudes de ingreso** siempre fallaban (`split(":", 2)[1:]` daba 2 valores para 3 variables).
3. **Comandos mudos**: `/filter`, `/save`, `/get`, `/antilinks`, `/floodconfig`, `/blockword`, `/blockcontent`, `/setwelcome`, `/captchaconfig` no respondían nada con el módulo apagado (el estado por defecto). Ahora los comandos de configuración activan su módulo y avisan; el resto avisa que el módulo está desactivado.
4. **Fotos y documentos en grupos** se reenviaban al propietario y el bot contestaba «Imagen recibida» en el grupo. Ahora solo en privado.
5. **Voz, vídeo, audio, stickers, vídeo circular, GIF, contactos y ubicaciones** enviados al bot por privado se ignoraban; ahora se reenvían al propietario.
6. **Despedida** usaba el texto de bienvenida por defecto («¡Bienvenido!» al salir alguien).
7. **`/filter` y `/save`** exigían `|`; ahora también valen `/filter palabra respuesta` y `/save nombre texto`.
8. **`/tr yo soy`** trataba «yo» como idioma destino; ahora se valida contra una lista de códigos.
9. **`/unmute` y captcha** concedían todos los permisos, saltándose los límites del grupo; ahora restauran los permisos por defecto del chat.
10. **Caché de roles (30 s)**: tras `/promote`, `/demote` o `/ban` el siguiente comando veía el rol anterior; ahora se invalida.
11. **Los avisos del userbot sugerían `/r ID texto`**, que no existía; añadido (solo propietario).
12. **Respuesta del propietario con foto/archivo** enviaba el texto «None»; ahora copia el mensaje.
13. **Menú de comandos**: con muchos comandos personalizados se superaba el límite de 100 de Telegram y el menú del propietario no se actualizaba.
14. **Userbot**: los chats iniciados por ti se perdían al reiniciar (volvía a auto-responder a gente a la que ya escribiste). Ahora persisten (migración v8).
15. Otros: texto del captcha con `&amp;`, `/afk` con mensaje equivocado, `/tts` con idioma fijo (ahora detecta), reenvíos automáticos del canal vinculado ya no pasan por el antispam, comandos del propietario fuera de su chat privado ahora avisan, finales de línea CRLF/LF mezclados normalizados (el `Procfile` tenía `\r`).

## Añadido
- `tests/`: simulador de Telegram + 32 pruebas de regresión (`python -m unittest tests.test_bot -v`).
- `.gitattributes`, `.gitignore` ampliado, README actualizado.

## Fuera del alcance (requieren red real)
`/clima`, `/wiki`, `/tr`, `/tts` y las reacciones anime dependen de servicios externos; en el entorno de pruebas se verificó que fallan con elegancia (mensaje al usuario, sin caídas), pero no se pudo probar la respuesta real.
