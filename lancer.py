#!/usr/bin/env python3
"""
Lance le serveur PDF puis ouvre l'interface dans le navigateur.
Usage : python lancer.py  (ou python3 lancer.py)
"""

import os, sys, time, socket, signal, subprocess, webbrowser
from pathlib import Path

BASE        = Path(__file__).parent.resolve()
VENV_PYTHON = BASE / 'venv' / 'bin' / 'python'
SERVER      = BASE / 'serveur_pdf.py'

# Re-lance avec le Python du venv si on n'est pas déjà dedans
# (compare sys.prefix pour détecter l'activation du venv, pas le binaire)
if VENV_PYTHON.exists() and Path(sys.prefix) != VENV_PYTHON.parent.parent:
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON)] + sys.argv)
HTML    = BASE / 'index.html'
PORT    = int(os.environ.get('PDF_SERVER_PORT', 5050))


def port_libre(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) != 0


def attendre_serveur(port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_libre(port):
            return True
        time.sleep(0.2)
    return False


def main():
    proc = None

    if port_libre(PORT):
        print(f"⚙  Démarrage du serveur sur le port {PORT}…")
        python = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
        proc = subprocess.Popen(
            [python, str(SERVER)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not attendre_serveur(PORT):
            print("❌  Le serveur n'a pas démarré dans les délais.")
            proc.terminate()
            sys.exit(1)
        print(f"✓  Serveur prêt (PID {proc.pid})")
    else:
        print(f"✓  Serveur déjà actif sur le port {PORT}")

    webbrowser.open(HTML.as_uri() + '?admin=1')
    print(f"✓  Interface ouverte : {HTML.name}")

    if proc is None:
        return

    print("   Ctrl+C pour arrêter le serveur.\n")

    def _stop(sig, frame):
        print("\nArrêt du serveur…")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _stop)
    signal.signal(signal.SIGTERM, _stop)
    proc.wait()


if __name__ == '__main__':
    main()
