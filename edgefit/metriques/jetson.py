# Jetson : CPU et RAM via psutil, GPU / températures / puissance via tegrastats.
# tegrastats est livré avec JetPack et toujours présent ; NVML (nvidia-smi) ne
# fonctionne pas sur les puces Tegra, et jtop demande d'installer un service root.
# tegrastats écrit une ligne d'état à chaque intervalle : on le lance une seule fois
# en arrière-plan et on garde la dernière ligne analysée.
import re
import subprocess
import threading

from .generique import FournisseurGenerique

_RE_GPU = re.compile(r"\bGR3D_FREQ (\d+)%")
_RE_TEMP = re.compile(r"\b(CPU|GPU)@(-?[\d.]+)C", re.IGNORECASE)
# Orin / Xavier : "VDD_IN 4296mW/4296mW" ; Jetson Nano : "POM_5V_IN 2400/2400"
_RE_PUISSANCE = re.compile(r"\b(?:VDD_IN|POM_5V_IN) (\d+)(?:mW)?/")


def analyser_ligne_tegrastats(ligne):
    resultat = {"gpu_pct": None, "temp_cpu_c": None, "temp_gpu_c": None, "puissance_w": None}
    if m := _RE_GPU.search(ligne):
        resultat["gpu_pct"] = float(m.group(1))
    for nom, valeur in _RE_TEMP.findall(ligne):
        temp = float(valeur)
        if temp > -200:  # -256C : capteur éteint
            resultat["temp_cpu_c" if nom.upper() == "CPU" else "temp_gpu_c"] = temp
    if m := _RE_PUISSANCE.search(ligne):
        resultat["puissance_w"] = int(m.group(1)) / 1000
    return resultat


class FournisseurJetson(FournisseurGenerique):
    type_carte = "jetson"

    def __init__(self, machine, intervalle_ms=1000):
        super().__init__(machine, carte=machine)
        self._derniere = {}
        self._processus = subprocess.Popen(
            ["tegrastats", "--interval", str(intervalle_ms)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        threading.Thread(target=self._suivre, daemon=True).start()

    def _suivre(self):
        for ligne in self._processus.stdout:
            self._derniere = analyser_ligne_tegrastats(ligne)

    def lire(self):
        m = super().lire()
        # psutil reste la source pour le CPU et la RAM ; tegrastats complète le reste
        m.update({cle: valeur for cle, valeur in self._derniere.items() if valeur is not None})
        if m.get("puissance_w") is not None:
            m["source_puissance"] = "carte"  # VDD_IN : toute la carte (CPU, GPU, mémoire)
        return m
