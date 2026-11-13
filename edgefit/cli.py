import argparse
import socket

import httpx
import uvicorn

from .metriques import detecter_fournisseur
from .ollama import ClientOllama
from .ollama_factice import OllamaFactice
from .registre import Registre, RegistreFactice
from .securite import nouveau_jeton
from .serveur import creer_app


def ip_locale():
    # adresse de la machine sur le réseau local. Aucun paquet n'est envoyé : en UDP,
    # connect() choisit seulement l'interface réseau qui servirait à joindre cette adresse
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def etat_ollama(url):
    try:
        return f"Ollama {httpx.get(f'{url}/api/version', timeout=2).json()['version']} sur {url}"
    except (httpx.HTTPError, ValueError, KeyError):
        return f"ATTENTION : Ollama ne répond pas sur {url} (modèles et chat indisponibles tant qu'il n'est pas lancé)"


def main():
    parser = argparse.ArgumentParser(prog="edgefit", description="Tableau de bord LLM pour Jetson et Raspberry Pi.")
    parser.add_argument("commande", nargs="?", choices=["diagnostic"],
                        help="diagnostic : vérifie que la machine est prête (Ollama, GPU, mémoire...) sans lancer le serveur")
    parser.add_argument("--hote", default="0.0.0.0", help="interface d'écoute (défaut : toutes, pour être joignable depuis le réseau local)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--ollama", default="http://127.0.0.1:11434", help="adresse du serveur Ollama local")
    parser.add_argument("--fake", action="store_true", help="métriques et Ollama simulés, pour développer sans Jetson ni Ollama")
    args = parser.parse_args()
    if args.commande == "diagnostic":
        from .diagnostic import lancer
        raise SystemExit(lancer(args.ollama))

    fournisseur = detecter_fournisseur(fake=args.fake)
    ollama = OllamaFactice() if args.fake else ClientOllama(args.ollama)
    jeton = nouveau_jeton()
    app = creer_app(fournisseur, ollama, jeton, RegistreFactice() if args.fake else Registre())

    print(f"edgefit — machine : {fournisseur.machine}")
    print("Ollama simulé (--fake)" if args.fake else etat_ollama(args.ollama))
    print(f"Depuis votre PC, ouvrez : http://{ip_locale()}:{args.port}/?token={jeton}")
    # flush : sans terminal (journal systemd, nohup), Python retarde l'affichage
    # et l'adresse avec le jeton n'apparaîtrait qu'à l'arrêt du serveur
    print("(nouveau jeton à chaque démarrage ; Ctrl+C pour arrêter)", flush=True)
    uvicorn.run(app, host=args.hote, port=args.port, log_level="warning")
