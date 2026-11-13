# choisit la façon de lire les métriques selon la machine. Tous les fournisseurs
# ont la même interface : un attribut `machine` et une méthode `lire()` qui renvoie
# un dict aux mêmes clés (None quand une mesure n'existe pas sur cette machine).
import platform
import shutil
from pathlib import Path

from .factice import FournisseurFactice
from .generique import FournisseurGenerique
from .jetson import FournisseurJetson
from .raspberry import FournisseurRaspberry


def modele_carte():
    # Jetson et Raspberry Pi exposent le nom exact de la carte dans le device tree Linux
    chemin = Path("/proc/device-tree/model")
    if chemin.exists():
        return chemin.read_bytes().decode(errors="ignore").strip("\x00").strip() or None
    return None


def detecter_fournisseur(fake=False):
    if fake:
        return FournisseurFactice()
    carte = modele_carte()
    if carte and "jetson" in carte.lower():
        if shutil.which("tegrastats"):
            return FournisseurJetson(carte)
        fournisseur = FournisseurGenerique(carte, carte=carte)  # Jetson sans tegrastats : pas de GPU lu
        fournisseur.type_carte = "jetson"
        return fournisseur
    if carte and "raspberry pi" in carte.lower():
        return FournisseurRaspberry(carte)
    return FournisseurGenerique(carte or platform.node(), carte=carte)
