# Flamie

Bot de Telegram para administración de grupos y atención de mensajes directos. Usa Python 3.11, pyTelegramBotAPI, SQLite y Pyrogram para el userbot.

## Despliegue en Railway

1. Conecta el repositorio y conserva el proceso de inicio definido en `Procfile` (`python -u main.py`). El supervisor levanta el bot principal y el userbot, y reinicia cada proceso con espera exponencial cuando termina inesperadamente.
2. Añade un volumen y configura `DATA_DIR` con su ruta de montaje, por ejemplo `/data`. La base SQLite y la sesión local de Pyrogram se guardan en esa carpeta.
3. Copia las variables de `.env.example` a Railway. Configura `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_ID`, `PYROGRAM_API_ID`, `PYROGRAM_API_HASH` y `PYROGRAM_SESSION_STRING`.
4. Para generar una sesión, ejecuta localmente `python generar_session_string.py` y guarda la cadena resultante en Railway como `PYROGRAM_SESSION_STRING`. No subas la cadena ni un archivo `.session` al repositorio.
5. Instala las dependencias fijadas por `requirements.txt`. `.python-version` selecciona Python 3.11 en Railpack.

El bot principal necesita permisos de administrador según las funciones habilitadas: borrar mensajes, restringir o expulsar usuarios, fijar mensajes, gestionar invitaciones, cambiar información y enviar registros. El userbot necesita una sesión autorizada de la cuenta personal.

## Variables de entorno

| Variable | Uso |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Token del bot de BotFather. |
| `TELEGRAM_OWNER_ID` | ID numérico del propietario. |
| `PYROGRAM_API_ID`, `PYROGRAM_API_HASH` | Credenciales de la aplicación de Telegram para Pyrogram. |
| `PYROGRAM_SESSION_STRING` | Sesión del userbot para Railway. |
| `PYROGRAM_SESSION_NAME` | Nombre de sesión local si no se usa StringSession. |
| `DATA_DIR` | Directorio persistente para la base y sesión. |
| `BOT_PUBLIC_USERNAME`, `BOT_PUBLIC_LINK` | Alias y enlace del bot mostrado en mensajes. |
| `OWNER_DISPLAY_NAME`, `OWNER_ALIAS` | Nombre y alias mostrados en respuestas de activación. |
| `LIBRETRANSLATE_URL`, `LIBRETRANSLATE_API_KEY` | Instancia y clave opcionales para `/tr`. |

En local, `flamie/core/config.py` carga `.env` si existe; sin `DATA_DIR`, usa la carpeta local `data/`, salvo que ya exista la base histórica en la raíz, que conserva en esa ruta. `.env` y las sesiones están excluidos de Git.

## Comandos

### Información y utilidades

- `/start`, `/help`, `/info`, `/id`, `/chatid`, `/chatlink`, `/staff`, `/rules`
- `/activar` inicia el flujo de solicitud de activación en privado.
- `/tr [idioma] texto`, `/tts texto`, `/clima ciudad`, `/calc expresión`, `/uinfo`, `/wiki tema`
- `/recordar 10m texto`, `/afk [motivo]`, `/encuesta pregunta | opción 1 | opción 2`
- `/idsticker` (responde a un sticker), `/getsticker` (responde a un sticker o indica su `file_id`)
- `/q` (responde a un texto para crear un sticker de cita)

### Diversión y comunidad

- `/8ball pregunta`, `/dado`, `/quiz`, `/ship` (responde a alguien o indica dos IDs), `/verdadoreto`, `/top`
- Reacciones: `/abrazar`, `/acariciar`, `/saludar`, `/besar`, `/aplaudir`, `/bailar`, `/llorar`, `/reir`, `/guinar`, `/sonrojar`, `/golpear`, `/morder`

### Administración del grupo

- Moderación: `/ban`, `/tban`, `/kick`, `/mute`, `/tmute`, `/unmute`, `/restrict`, `/promote`, `/demote`, `/warn`, `/unwarn`, `/warns`, `/resetwarns`, `/warnconfig`, `/unban`, `/purge`
- Mensajes y avisos: `/report`, `/admin`, `@admin`, `/del`, `/pin`, `/unpin`, `/unallpin`
- Ajustes y miembros: `/settings`, `/settitle`, `/setgtitle`, `/setdescr`, `/setwelcome`, `/setgoodbye`, `/captchaconfig`, `/antiraid`
- Antispam y comunidad: `/antilinks`, `/floodconfig`, `/blockword`, `/blockcontent`, `/filter`, `/unfilter`, `/save`, `/get`, `#nombre`, `/setrules`, `/clearrules`
- Comandos personalizados del propietario: `/addcmd nombre | respuesta`, `/delcmd nombre`, `/listcmd`

Los comandos de moderación que ya existían también aceptan los prefijos `.` y `,`. Para guardar o quitar una nota se usa `/save nombre | texto` o `/get nombre` y `#nombre`.

Los módulos de grupo se configuran por chat en `/settings`. Las funciones que modifican el grupo requieren que el bot tenga los permisos correspondientes.

### Propietario (chat privado)

- `/stats`, `/grupos`, `/logs [cantidad]`
- `/broadcast mensaje`
- `/gban ID [motivo]`, `/ungban ID`
- `/backup`
- `/maintenance on|off|estado`
- `/limpiar` para borrar el historial privado del bot tras confirmar

## Persistencia y privacidad

SQLite usa WAL y guarda las migraciones en `schema_version`. Mantén `DATA_DIR` en un volumen persistente para preservar configuraciones, estados, recordatorios, niveles, bloqueos globales y datos del userbot tras reinicios o despliegues.

Los comandos `/tr`, `/tts`, `/clima`, `/wiki` y las reacciones anime consultan servicios externos. No incluyas tokens, credenciales de Pyrogram ni cadenas de sesión en mensajes, capturas o el repositorio.
