# `edgefit diagnostic` : vérifie que la carte est prête, avant même d'ouvrir le tableau de bord.
# Chaque vérification renvoie (niveau, message) ; niveaux : OK, INFO, ATTENTION, ERREUR.
# Rien n'est modifié sur la machine : le diagnostic propose les commandes, il ne les lance pas.
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import psutil

from .metriques import detecter_fournisseur, modele_carte
from .metriques.generique import bureau_actif
from .metriques.jetson import analyser_ligne_tegrastats

GIO = 1024 ** 3

# numéro de version L4T (Linux for Tegra) -> version de JetPack
JETPACK = {"32": "4", "35": "5 (Ubuntu 20.04)", "36": "6 (Ubuntu 22.04)", "38": "7 (Ubuntu 24.04)"}


def version_l4t(texte):
    """ "# R36 (release), REVISION: 4.3, ..." -> ("36", "4.3")."""
    m = re.search(r"R(\d+) \(release\), REVISION: ([\d.]+)", texte or "")
    return (m.group(1), m.group(2)) if m else None


def mode_energie(texte):
    """sortie de `nvpmodel -q` -> nom du mode ("MAXN", "15W"...)."""
    m = re.search(r"NV Power Mode:\s*(\S+)", texte or "")
    return m.group(1) if m else None


def _commande(args, delai=5):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=delai).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def moteur_vulkan_ollama(exe=None):
    """Moteur GPU Vulkan d'Ollama (lib/ollama/vulkan/libggml-vulkan.so) s'il est installé, sinon None.
    Ollama ne le livre pas pour ARM : sur une Jetson JetPack 4 (CUDA 10.2, trop ancien pour Ollama),
    l'installateur d'edgefit le compile depuis les sources d'Ollama (option --gpu)."""
    exe = exe or shutil.which("ollama")
    if not exe:
        return None
    dossier = Path(exe).resolve().parent.parent / "lib" / "ollama" / "vulkan"
    trouves = sorted(dossier.glob("libggml-vulkan*.so*"))
    return trouves[0] if trouves else None


def verifier_python():
    v = sys.version_info
    return [("OK", f"Python {v.major}.{v.minor}")]


def verifier_carte():
    carte = modele_carte()
    resultats = [("INFO", f"Carte : {carte or 'non détectée (PC ordinaire ?)'}")]
    release = Path("/etc/nv_tegra_release")
    if release.exists():
        l4t = version_l4t(release.read_text(errors="ignore"))
        if l4t:
            jetpack = JETPACK.get(l4t[0], "inconnue")
            texte = f"L4T R{l4t[0]}.{l4t[1]}, JetPack {jetpack}"
            if l4t[0] != "32":
                resultats.append(("OK", texte))
            elif moteur_vulkan_ollama():
                resultats.append(("OK", texte + " : GPU utilisé par Vulkan (moteur compilé par l'installateur d'edgefit)"))
            else:
                resultats.append(("ATTENTION", texte + " : CUDA trop ancien pour Ollama. Pour utiliser le GPU par Vulkan, "
                                  "relancez l'installateur avec --gpu"))
        mode = mode_energie(_commande(["nvpmodel", "-q"]))
        if mode:
            resultats.append(("INFO", f"Mode d'énergie : {mode} (il change les tokens/s ; liste : sudo nvpmodel -p --verbose)"))
    if carte and "jetson" in carte.lower():
        if not shutil.which("tegrastats"):
            resultats.append(("ATTENTION", "tegrastats introuvable : GPU, températures et puissance ne seront pas affichés"))
        else:
            ligne = _commande(["timeout", "2", "tegrastats", "--interval", "500"]) or ""
            lues = {k: v for k, v in analyser_ligne_tegrastats(ligne.splitlines()[0] if ligne else "").items()
                    if v is not None}
            absentes = {"gpu_pct", "temp_cpu_c", "temp_gpu_c", "puissance_w"} - lues.keys()
            if absentes:
                resultats.append(("ATTENTION", "tegrastats lu, mais champs absents : " + ", ".join(sorted(absentes))
                                  + ". Envoyez la ligne de `tegrastats` pour adapter l'analyse."))
            else:
                resultats.append(("OK", "tegrastats : GPU, températures et puissance lus"))
    if carte and "raspberry pi" in carte.lower():
        sortie = _commande(["vcgencmd", "get_throttled"])
        if sortie and "=0x0" not in sortie:
            resultats.append(("ATTENTION", f"Raspberry Pi bridé ou sous-alimenté ({sortie.strip()}) : "
                                           "utilisez l'alimentation officielle 27 W"))
        elif sortie:
            resultats.append(("OK", "Alimentation du Raspberry Pi correcte"))
    return resultats


def verifier_memoire():
    ram, swap = psutil.virtual_memory(), psutil.swap_memory()
    resultats = [("INFO", f"Mémoire : {ram.available / GIO:.1f} Go libres sur {ram.total / GIO:.1f} Go "
                          f"(swap {swap.total / GIO:.1f} Go, inutilisable par le GPU)")]
    carte = (modele_carte() or "").lower()
    if bureau_actif() and ("jetson" in carte or "raspberry pi" in carte):
        resultats.append(("INFO", "Bureau graphique actif : sans lui, ~0,5 à 1 Go de plus pour les modèles "
                                  "(sudo systemctl set-default multi-user.target, puis redémarrer)"))
    return resultats


def dossier_modeles():
    """Dossier où mesurer l'espace disque libre pour les modèles d'Ollama.

    Installé par son script officiel, Ollama tourne sous son propre utilisateur et range les modèles
    dans /usr/share/ollama/.ollama/models, illisible pour les autres comptes. Selon la version de
    Python, Path.exists() y lève PermissionError ou répond False : on interroge donc os.stat, qui
    distingue « absent » de « interdit ». Si c'est interdit, on remonte au premier dossier parent
    accessible, qui est sur le même disque."""
    candidats = [os.environ.get("OLLAMA_MODELS"), "/usr/share/ollama/.ollama/models",  # service systemd
                 Path.home() / ".ollama" / "models"]
    for chemin in filter(None, candidats):
        chemin = Path(chemin)
        try:
            os.stat(chemin)
            return chemin
        except PermissionError:
            for parent in chemin.parents:
                try:
                    shutil.disk_usage(parent)
                    return parent
                except OSError:
                    continue
        except OSError:  # absent
            continue
    return Path.home()


def verifier_ollama(url):
    try:
        version = httpx.get(f"{url}/api/version", timeout=3).json()["version"]
    except (httpx.HTTPError, ValueError, KeyError):
        return [("ERREUR", f"Ollama ne répond pas sur {url}. Installation : curl -fsSL https://ollama.com/install.sh | sh")]
    resultats = [("OK", f"Ollama {version} sur {url}")]

    # le port d'Ollama ne doit pas être ouvert au réseau : il n'a pas d'authentification
    try:
        expose = any(c.laddr.port == 11434 and c.laddr.ip in ("0.0.0.0", "::") and c.status == psutil.CONN_LISTEN
                     for c in psutil.net_connections("inet"))
    except (psutil.Error, OSError):
        expose = False
    if expose:
        resultats.append(("ATTENTION", "Ollama écoute sur toutes les interfaces (OLLAMA_HOST=0.0.0.0) : n'importe qui "
                                       "sur le réseau peut l'utiliser. Retirez OLLAMA_HOST (sudo systemctl edit ollama)."))

    try:
        charges = httpx.get(f"{url}/api/ps", timeout=3).json().get("models", [])
    except (httpx.HTTPError, ValueError):
        charges = []
    for m in charges:
        part = m.get("size_vram", 0) / m["size"] if m.get("size") else 0
        if part < 0.99:
            resultats.append(("ATTENTION", f"{m['name']} n'est qu'à {part:.0%} sur le GPU : Ollama tourne en partie sur le CPU "
                                           "(version d'Ollama ou de JetPack non prise en charge ?)"))
        else:
            resultats.append(("OK", f"{m['name']} chargé à 100 % sur le GPU"))
    if not charges:
        resultats.append(("INFO", "Aucun modèle chargé : lancez une question dans le chat puis relancez le diagnostic "
                                  "pour vérifier qu'Ollama utilise bien le GPU"))

    dossier = dossier_modeles()
    libre = shutil.disk_usage(dossier).free / GIO
    niveau = "ATTENTION" if libre < 10 else "OK"
    resultats.append((niveau, f"{libre:.0f} Go libres pour les modèles ({dossier})"))
    return resultats


def verifier_internet():
    try:
        httpx.get("https://registry.ollama.ai/v2/", timeout=5)
        return [("OK", "Registre Ollama joignable (téléchargements et vérification avant téléchargement)")]
    except httpx.HTTPError:
        return [("ATTENTION", "Pas d'accès au registre Ollama : seuls les modèles déjà installés seront utilisables")]


def lancer(url_ollama):
    print("edgefit — diagnostic de la machine\n")
    erreurs = 0
    for titre, verification in (("Système", verifier_python), ("Carte", verifier_carte), ("Mémoire", verifier_memoire),
                                ("Ollama", lambda: verifier_ollama(url_ollama)), ("Réseau", verifier_internet)):
        print(titre)
        for niveau, message in verification():
            erreurs += niveau == "ERREUR"
            print(f"  [{niveau}] {message}")
        print()
    fournisseur = detecter_fournisseur()
    print(f"Métriques lues par : {type(fournisseur).__name__}")
    return 1 if erreurs else 0


# --- même diagnostic pour la page web : des codes et des valeurs, la page écrit les phrases ---

def etat_carte(url_ollama):
    """Identité de la carte et vérifications, sous forme de données (traduites par la page)."""
    carte = modele_carte()
    faits = {"carte": carte, "python": f"{sys.version_info.major}.{sys.version_info.minor}",
             "demarrage": psutil.boot_time(), "l4t": None, "jetpack": None, "mode": None,
             "ollama_version": None, "ollama_url": url_ollama}
    verifs = []

    def ajout(niveau, code, **params):
        verifs.append({"niveau": niveau, "code": code, "params": params})

    release = Path("/etc/nv_tegra_release")
    if release.exists():
        l4t = version_l4t(release.read_text(errors="ignore"))
        if l4t:
            faits["l4t"], faits["jetpack"] = f"R{l4t[0]}.{l4t[1]}", JETPACK.get(l4t[0])
            if l4t[0] != "32":
                ajout("OK", "jetpack", l4t=faits["l4t"], jetpack=faits["jetpack"])
            elif moteur_vulkan_ollama():
                ajout("OK", "jetpack_vulkan", l4t=faits["l4t"], jetpack=faits["jetpack"])
            else:
                ajout("ATTENTION", "jetpack_ancien", l4t=faits["l4t"], jetpack=faits["jetpack"])
        faits["mode"] = mode_energie(_commande(["nvpmodel", "-q"]))
    carte_min = (carte or "").lower()
    if "jetson" in carte_min:
        ajout("OK" if shutil.which("tegrastats") else "ATTENTION", "tegrastats" if shutil.which("tegrastats") else "tegrastats_absent")
    if "raspberry pi" in carte_min:
        sortie = _commande(["vcgencmd", "get_throttled"])
        if sortie:
            ajout("OK" if "=0x0" in sortie else "ATTENTION", "pi_ok" if "=0x0" in sortie else "pi_bride", valeur=sortie.strip())
    try:
        faits["ollama_version"] = httpx.get(f"{url_ollama}/api/version", timeout=3).json()["version"]
        ajout("OK", "ollama", version=faits["ollama_version"])
    except (httpx.HTTPError, ValueError, KeyError):
        ajout("ERREUR", "ollama_absent", url=url_ollama)
    try:
        if any(c.laddr.port == 11434 and c.laddr.ip in ("0.0.0.0", "::") and c.status == psutil.CONN_LISTEN
               for c in psutil.net_connections("inet")):
            ajout("ATTENTION", "ollama_expose")
    except (psutil.Error, OSError):
        pass
    if faits["ollama_version"]:
        try:
            for m in httpx.get(f"{url_ollama}/api/ps", timeout=3).json().get("models", []):
                part = m.get("size_vram", 0) / m["size"] if m.get("size") else 0
                if "jetson" in carte_min or part:
                    ajout("OK" if part >= 0.99 else "ATTENTION", "gpu_ok" if part >= 0.99 else "gpu_partiel",
                          nom=m["name"], part=round(part * 100))
        except (httpx.HTTPError, ValueError):
            pass
    dossier = dossier_modeles()
    disque = shutil.disk_usage(dossier)
    faits.update(dossier=str(dossier), disque_libre_go=disque.free / GIO, disque_total_go=disque.total / GIO)
    ajout("ATTENTION" if disque.free / GIO < 10 else "OK", "disque", libre_go=round(disque.free / GIO, 1))
    if bureau_actif() and ("jetson" in carte_min or "raspberry pi" in carte_min):
        ajout("INFO", "bureau")
    try:
        httpx.get("https://registry.ollama.ai/v2/", timeout=4)
        ajout("OK", "internet")
    except httpx.HTTPError:
        ajout("ATTENTION", "internet_absent")
    return {"faits": faits, "verifications": verifs}
