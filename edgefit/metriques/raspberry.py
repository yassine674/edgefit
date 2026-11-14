# Raspberry Pi : mêmes mesures que le générique (pas de GPU utilisable pour l'IA),
# plus les alertes d'alimentation et de bridage données par `vcgencmd get_throttled`.
# Une alimentation trop faible fait baisser la fréquence du CPU, donc le débit en tokens/s :
# c'est la première chose à vérifier quand une mesure semble anormalement lente.
import re
import shutil
import subprocess

from .generique import FournisseurGenerique

# bits du registre renvoyé par get_throttled (situation actuelle)
ALERTES_BRIDAGE = {
    0: "sous-tension : alimentation insuffisante",
    1: "fréquence du CPU plafonnée",
    2: "CPU bridé en ce moment",
    3: "limite de température atteinte",
}


_RE_PMIC = re.compile(r"^\s*(\S+)_([AV]) (?:current|volt)\(\d+\)=([\d.]+)[AV]\s*$", re.M)


def puissance_pmic(sortie):
    """Raspberry Pi 5 : `vcgencmd pmic_read_adc` donne courant (…_A) et tension (…_V) de chaque ligne
    d'alimentation ; leur produit, sommé, approche la puissance de la carte (sans les ports USB)."""
    courants, tensions = {}, {}
    for nom, genre, valeur in _RE_PMIC.findall(sortie):
        (courants if genre == "A" else tensions)[nom] = float(valeur)
    total = sum(courants[n] * tensions[n] for n in courants if n in tensions)
    return round(total, 2) if total > 0 else None


def alertes_depuis_registre(valeur):
    return [message for bit, message in ALERTES_BRIDAGE.items() if valeur & (1 << bit)]


class FournisseurRaspberry(FournisseurGenerique):
    type_carte = "raspberry"

    def __init__(self, machine):
        super().__init__(machine, carte=machine)
        self.vcgencmd = shutil.which("vcgencmd")

    def _registre_bridage(self):
        if not self.vcgencmd:
            return 0
        try:
            sortie = subprocess.run([self.vcgencmd, "get_throttled"], capture_output=True, text=True, timeout=2).stdout
            return int(sortie.strip().split("=")[1], 16)  # ex. "throttled=0x50005"
        except (OSError, subprocess.SubprocessError, IndexError, ValueError):
            return 0

    def _puissance(self):
        if not self.vcgencmd:
            return None
        try:
            sortie = subprocess.run([self.vcgencmd, "pmic_read_adc"], capture_output=True, text=True, timeout=2).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        return puissance_pmic(sortie)  # None sur Pi 4 et plus anciens : pas de PMIC lisible

    def lire(self):
        m = super().lire()
        m["alertes"] = alertes_depuis_registre(self._registre_bridage())
        if (p := self._puissance()) is not None:
            m["puissance_w"], m["source_puissance"] = p, "pmic"
        return m
