"""Démarre l'app et affiche l'adresse à ouvrir depuis le téléphone.

Trois erreurs faciles que ce script évite :

1. Ouvrir `localhost` depuis le téléphone. `localhost` désigne *l'appareil
   courant* : sur un iPhone, c'est l'iPhone, où rien ne tourne. Il faut l'IP
   de la machine qui héberge.
2. Lancer uvicorn sans `--host 0.0.0.0`. Par défaut il n'écoute que sur
   127.0.0.1 et refuse toute connexion venant du réseau, même avec la bonne
   adresse.
3. Croire que les notifications vont marcher. Elles exigent un contexte
   sécurisé (HTTPS, ou localhost) : en HTTP sur une IP locale, le navigateur
   désactive les service workers et le push est impossible.

Usage :  python scripts/demarrer.py [--port 8000]
"""
from __future__ import annotations

import argparse
import socket
import subprocess
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent


def ip_locale() -> str | None:
    """IP de cette machine sur le réseau local.

    On ouvre une socket UDP vers une adresse extérieure sans rien envoyer :
    le système choisit alors l'interface qui sert à sortir, et son adresse est
    celle que les autres appareils du réseau peuvent joindre. `gethostname()`
    renverrait souvent 127.0.0.1, inutilisable depuis le téléphone.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 1))  # réseau de documentation, aucun trafic
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Démarre l'app de trading")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    backend = RACINE / "backend"
    if not (backend / "app" / "main.py").is_file():
        print(f"❌ backend introuvable sous {backend}", file=sys.stderr)
        return 1

    env = backend / ".env"
    if not env.is_file():
        print(f"⚠️  {env} absent — copie backend/.env.example et renseigne "
              f"SAXO_ACCESS_TOKEN, sinon l'app démarrera sans pouvoir "
              f"interroger le courtier.\n")

    ip = ip_locale()
    print("=" * 64)
    print("  Sur CETTE machine      :  http://localhost:%d" % args.port)
    if ip:
        print("  Depuis ton téléphone   :  http://%s:%d" % (ip, args.port))
        print("                            (même réseau Wi-Fi, PC allumé)")
    else:
        print("  Adresse réseau         :  introuvable — vérifie ta connexion")
    print()
    print("  ⚠️  N'ouvre pas « localhost » sur le téléphone : ça désigne le")
    print("      téléphone lui-même, où rien ne tourne.")
    print()
    print("  ⚠️  Les notifications push ne marcheront PAS par cette adresse :")
    print("      elles exigent HTTPS. L'app te le dira clairement à l'écran.")
    print("      Pour les avoir : un tunnel (Cloudflare Tunnel, ngrok) ou un")
    print("      hébergeur, qui donnent une URL HTTPS sans changer le code.")
    print("=" * 64)
    print()

    # `--host 0.0.0.0` : indispensable pour être joignable depuis le réseau.
    return subprocess.call(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "0.0.0.0", "--port", str(args.port)],
        cwd=backend,
    )


if __name__ == "__main__":
    sys.exit(main())
