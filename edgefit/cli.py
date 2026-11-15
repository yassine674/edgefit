import argparse
import json
import os
import shutil
import subprocess
import tempfile
import socket
from pathlib import Path

from platformdirs import user_data_dir

import httpx
import uvicorn

from .metriques import detecter_fournisseur
from .ollama import ClientOllama
from .ollama_factice import OllamaFactice
from .catalogue import Catalogue, CatalogueFactice
from .registre import Registre, RegistreFactice
from .securite import nouveau_jeton
from .voix import Voix, VoixFactice
from .ecoute import Ecoute, EcouteFactice
from .images import Images, ImagesFactice
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


def nom_tailscale():
    # nom de la carte sur Tailscale (ex. "nano") : il ne change pas, même si l'adresse IP change.
    # None si Tailscale n'est pas installé ou pas connecté
    if not shutil.which("tailscale"):
        return None
    try:
        sortie = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5)
        moi = json.loads(sortie.stdout)
    except (subprocess.SubprocessError, ValueError, OSError):
        return None
    if moi.get("BackendState") != "Running":
        return None
    nom = (moi.get("Self") or {}).get("DNSName", "").split(".")[0]
    return nom or None


def port_libre(hote, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((hote, port))
            return True
        except OSError:
            return False


def etat_ollama(url):
    try:
        return f"Ollama {httpx.get(f'{url}/api/version', timeout=2).json()['version']} sur {url}"
    except (httpx.HTTPError, ValueError, KeyError):
        return f"ATTENTION : Ollama ne répond pas sur {url} (modèles et chat indisponibles tant qu'il n'est pas lancé)"


def fichier_adresse():
    return Path(user_data_dir("edgefit", appauthor=False)) / "adresse.txt"


def ecrire_adresse(adresse):
    # l'adresse contient le jeton : lisible seulement par l'utilisateur (0600)
    chemin = fichier_adresse()
    chemin.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(chemin, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(adresse + "\n")


def certificat(ip):
    """Certificat auto-signé pour --https (le micro du navigateur n'est permis que sur une page sûre).
    Créé avec openssl, valable pour l'IP de la carte et localhost ; refait si l'IP change."""
    dossier = Path(user_data_dir("edgefit", appauthor=False)) / "tls"
    cert, cle, pour = dossier / "cert.pem", dossier / "cle.pem", dossier / "ip.txt"
    if cert.exists() and cle.exists() and pour.exists() and pour.read_text().strip() == ip:
        return cert, cle
    if not shutil.which("openssl"):
        raise SystemExit("--https demande openssl : sudo apt install openssl")
    dossier.mkdir(parents=True, exist_ok=True)
    os.chmod(dossier, 0o700)
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "825", "-subj", "/CN=edgefit",
                    "-keyout", str(cle), "-out", str(cert), "-addext", f"subjectAltName=IP:{ip},IP:127.0.0.1,DNS:localhost"],
                   check=True, capture_output=True)
    os.chmod(cle, 0o600)
    pour.write_text(ip)
    return cert, cle


def main():
    parser = argparse.ArgumentParser(prog="edgefit", description="Tableau de bord LLM pour Jetson et Raspberry Pi.")
    parser.add_argument("commande", nargs="?", choices=["diagnostic", "adresse"],
                        help="diagnostic : vérifie que la machine est prête (Ollama, GPU, mémoire...) sans lancer le serveur ; "
                             "adresse : réaffiche l'adresse (avec son jeton) de l'edgefit en cours")
    parser.add_argument("--hote", default="0.0.0.0", help="interface d'écoute (défaut : toutes, pour être joignable depuis le réseau local)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--ollama", default="http://127.0.0.1:11434", help="adresse du serveur Ollama local")
    parser.add_argument("--https", action="store_true",
                        help="servir en https (certificat auto-signé) : nécessaire pour le micro depuis un téléphone")
    parser.add_argument("--fake", action="store_true", help="métriques et Ollama simulés, pour développer sans Jetson ni Ollama")
    parser.add_argument("--sans-jeton", action="store_true",
                        help="pas de jeton dans l'adresse : toute machine qui joint la carte ouvre l'appli "
                             "(à réserver à un réseau privé, par exemple Tailscale)")
    args = parser.parse_args()
    if args.commande == "diagnostic":
        from .diagnostic import lancer
        raise SystemExit(lancer(args.ollama))
    if args.commande == "adresse":
        chemin = fichier_adresse()
        if not chemin.exists():
            raise SystemExit("edgefit n'a pas encore été lancé sur cette machine.")
        print(chemin.read_text().strip())
        return

    if args.fake and not os.environ.get("EDGEFIT_DB"):
        # mode simulé : jamais dans la vraie base (des mesures inventées s'y mêleraient aux vraies)
        os.environ["EDGEFIT_DB"] = str(Path(tempfile.mkdtemp(prefix="edgefit-fake-")) / "edgefit.db")
    fournisseur = detecter_fournisseur(fake=args.fake)
    ollama = OllamaFactice() if args.fake else ClientOllama(args.ollama)
    if not port_libre(args.hote, args.port):
        raise SystemExit(f"Le port {args.port} est déjà utilisé : edgefit tourne sans doute déjà "
                         f"(son adresse : edgefit adresse). Sinon, choisissez un autre port avec --port.")
    jeton = None if args.sans_jeton else nouveau_jeton()
    app = creer_app(fournisseur, ollama, jeton,
                    RegistreFactice() if args.fake else Registre(),
                    CatalogueFactice() if args.fake else Catalogue(),
                    voix=VoixFactice() if args.fake else Voix(),
                    ecoute=EcouteFactice() if args.fake else Ecoute(),
                    images=ImagesFactice(Path(tempfile.mkdtemp(prefix="edgefit-images-"))) if args.fake else Images())

    print(f"edgefit — machine : {fournisseur.machine}")
    print("Ollama simulé (--fake)" if args.fake else etat_ollama(args.ollama))
    ip = ip_locale()
    tls = certificat(ip) if args.https else None
    schema = "https" if tls else "http"
    suffixe = "" if jeton is None else f"/?token={jeton}"
    nom = nom_tailscale()
    adresses = ([f"{schema}://{nom}:{args.port}{suffixe}"] if nom else []) + [f"{schema}://{ip}:{args.port}{suffixe}"]
    if not args.fake:  # le mode simulé ne remplace pas l'adresse du vrai serveur
        ecrire_adresse("\n".join(adresses))
    if nom:
        print(f"Depuis votre PC, de n'importe où (Tailscale) : {adresses[0]}")
        print(f"Sur le même réseau local : {adresses[1]}")
    else:
        print(f"Depuis votre PC, ouvrez : {adresses[0]}")
    print("(cette adresse se réaffiche avec : edgefit adresse)")
    if jeton is None:
        print("(sans jeton : toute machine qui joint la carte ouvre l'appli)")
    # flush : sans terminal (journal systemd, nohup), Python retarde l'affichage
    # et l'adresse avec le jeton n'apparaîtrait qu'à l'arrêt du serveur
    if tls:
        print("(certificat auto-signé : le navigateur affiche un avertissement la première fois, acceptez-le)")
    print("(Ctrl+C pour arrêter)" if jeton is None else "(nouveau jeton à chaque démarrage ; Ctrl+C pour arrêter)", flush=True)
    uvicorn.run(app, host=args.hote, port=args.port, log_level="warning",
                **({"ssl_certfile": str(tls[0]), "ssl_keyfile": str(tls[1])} if tls else {}))
