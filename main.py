#!/usr/bin/env python3
"""Supervisa y reinicia los procesos del sistema Flamie en Railway."""
import atexit
import logging
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("flamie.supervisor")
PROCESSES = {}
SHUTTING_DOWN = False
RESTART_DELAY = 5
MAX_RESTART_DELAY = 60
STOP_EVENT = threading.Event()


def start_process(name, script):
    logger.info("Iniciando %s (%s)", name, script)
    return subprocess.Popen(
        [sys.executable, str(BASE_DIR / script)],
        cwd=str(BASE_DIR),
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


def cleanup():
    """Detiene los procesos secundarios cuando Railway apaga el servicio."""
    for name, proc in list(PROCESSES.items()):
        if proc.poll() is None:
            logger.info("Deteniendo %s", name)
            proc.terminate()
    for proc in list(PROCESSES.values()):
        if proc.poll() is None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def request_shutdown(_signum, _frame):
    global SHUTTING_DOWN
    SHUTTING_DOWN = True
    STOP_EVENT.set()


def main():
    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)
    atexit.register(cleanup)

    logger.info("Iniciando sistema Flamie")

    scripts = {"Recepcionista (userbot)": "userbot.py", "Bot Principal": "bot.py"}
    for name, script in scripts.items():
        PROCESSES[name] = start_process(name, script)

    attempts = {name: 0 for name in scripts}
    restart_at = {name: 0.0 for name in scripts}
    started_at = {name: time.monotonic() for name in scripts}
    while not SHUTTING_DOWN:
        now = time.monotonic()
        for name, script in scripts.items():
            proc = PROCESSES[name]
            return_code = proc.poll()
            if return_code is not None and not SHUTTING_DOWN:
                if restart_at[name] == 0.0:
                    delay = min(RESTART_DELAY * (2 ** min(attempts[name], 8)), MAX_RESTART_DELAY)
                    attempts[name] += 1
                    restart_at[name] = now + delay
                    logger.error("%s terminó con código %s; reintentará en %ss", name, return_code, delay)
                elif now >= restart_at[name] and not SHUTTING_DOWN:
                    PROCESSES[name] = start_process(name, script)
                    restart_at[name] = 0.0
                    started_at[name] = now
            elif return_code is None and now - started_at[name] >= 60:
                attempts[name] = 0
        STOP_EVENT.wait(1)

    logger.info("Apagado solicitado; deteniendo procesos")
    cleanup()


if __name__ == "__main__":
    main()
