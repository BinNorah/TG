#!/usr/bin/env python3
"""
Script de UN SOLO USO para generar la StringSession de Pyrogram.
Ejecutar SOLO LOCALMENTE, nunca en el servidor.

Pasos:
1. python generar_session_string.py
2. Ingresar tu número de teléfono y el código que te llega
3. Copiar la cadena que aparece al final
4. Pegarla en Railway como variable: PYROGRAM_SESSION_STRING
"""
import os
from pathlib import Path

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
DATA_DIR = Path(os.getenv("DATA_DIR") or (BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

API_ID   = os.getenv("PYROGRAM_API_ID")
API_HASH = os.getenv("PYROGRAM_API_HASH")

if not API_ID or not API_HASH:
    print("ERROR: Falta PYROGRAM_API_ID o PYROGRAM_API_HASH en .env")
    exit(1)

from pyrogram import Client

SESSION_TEMP = str(DATA_DIR / "_temp_gen_session")

print("=" * 55)
print("  GENERADOR DE STRINGSESSION — FLAMIE BOT")
print("=" * 55)
print("Este proceso te pedira tu numero de Telegram y un codigo.")
print("Al terminar, copia la cadena y agregala en Railway.\n")

try:
    with Client(
        name=SESSION_TEMP,
        api_id=int(API_ID),
        api_hash=API_HASH,
    ) as app:
        session_string = app.export_session_string()
finally:
    # Limpiar archivos temporales de sesión
    import os as _os
    for ext in [".session", ".session-journal"]:
        p = DATA_DIR / f"_temp_gen_session{ext}"
        if p.exists():
            _os.remove(p)

print("\n" + "=" * 55)
print("StringSession generada exitosamente!")
print("=" * 55)
print("\nCopia TODA la linea de abajo (es larga, selecciona todo):\n")
print(f"PYROGRAM_SESSION_STRING={session_string}")
print("\n" + "=" * 55)
print("Pegala en Railway -> Service -> Variables")
print("Nombre de variable: PYROGRAM_SESSION_STRING")
print("Valor: la cadena larga que ves arriba")
print("=" * 55)
