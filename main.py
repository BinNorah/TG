#!/usr/bin/env python3
"""
Archivo principal para Railway. Orquesta bot.py y userbot.py sin conflictos.
"""
import subprocess
import sys
import signal
import atexit
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Procesos a manejar
bot_proc = None
userbot_proc = None

def cleanup():
    """Termina ambos procesos limpiamente."""
    global bot_proc, userbot_proc
    for proc in [bot_proc, userbot_proc]:
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except:
                proc.kill()

def main():
    global bot_proc, userbot_proc
    
    atexit.register(cleanup)
    signal.signal(signal.SIGINT, lambda s, f: (cleanup(), sys.exit(0)))
    signal.signal(signal.SIGTERM, lambda s, f: (cleanup(), sys.exit(0)))
    
    print("=" * 50)
    print("INICIANDO SISTEMA FLAMIE")
    print("=" * 50)
    
    try:
        # Lanzar userbot en BACKGROUND (subprocess)
        print("[MAIN] Iniciando Recepcionista (userbot)...")
        userbot_proc = subprocess.Popen(
            [sys.executable, str(BASE_DIR / "userbot.py")],
            cwd=str(BASE_DIR),
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        
        # Esperar a que userbot se conecte (Pyrogram establece conexión)
        print("[MAIN] Esperando a que userbot se conecte (5s)...")
        time.sleep(5)
        
        # Verificar que el userbot no haya crasheado antes de continuar
        if userbot_proc.poll() is not None:
            print(f"[ERROR] El userbot terminó prematuramente (código: {userbot_proc.returncode}).")
            print("[MAIN] Continuando de todas formas con el bot principal...")
        else:
            print("[MAIN] Userbot corriendo correctamente.")
        
        # Lanzar bot en FOREGROUND (bloquea aquí)
        print("[MAIN] Iniciando Bot Principal (bot.py)...\n")
        bot_proc = subprocess.Popen(
            [sys.executable, str(BASE_DIR / "bot.py")],
            cwd=str(BASE_DIR),
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        
        # Esperar a que bot termine
        bot_proc.wait()
        
    except KeyboardInterrupt:
        print("\n[MAIN] Sistema detenido por usuario.")
    except Exception as e:
        print(f"[ERROR] {e}")
        import traceback
        traceback.print_exc()
    finally:
        cleanup()
        print("[MAIN] Sistema apagado.")

if __name__ == "__main__":
    main()
