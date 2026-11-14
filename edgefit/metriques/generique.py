# toute machine : CPU, RAM et température via psutil ; sur PC, le GPU NVIDIA via nvidia-smi s'il existe
import shutil
import subprocess
import time

import psutil

GO = 1024 ** 3

# capteurs de température CPU les plus courants, par ordre de préférence
CAPTEURS_CPU = ("cpu_thermal", "coretemp", "k10temp", "soc_thermal", "cpu-thermal")


def temperature_cpu():
    lire = getattr(psutil, "sensors_temperatures", None)  # absent sous Windows et macOS
    if lire is None:
        return None
    capteurs = lire()
    for nom in CAPTEURS_CPU:
        if capteurs.get(nom):
            return capteurs[nom][0].current
    for mesures in capteurs.values():
        if mesures:
            return mesures[0].current
    return None


# processus d'un bureau graphique : sur une carte, il occupe souvent 0,5 à 1 Go
PROCESSUS_BUREAU = {"Xorg", "Xwayland", "gnome-shell", "gdm3", "lightdm", "sddm", "xfce4-session", "labwc", "wayfire"}


def bureau_actif():
    try:
        return any(p.info["name"] in PROCESSUS_BUREAU for p in psutil.process_iter(["name"]))
    except psutil.Error:
        return False


def lire_nvidia(commande):
    """Charge, température et puissance du premier GPU NVIDIA (PC), ou None."""
    try:
        sortie = subprocess.run([commande, "--query-gpu=utilization.gpu,temperature.gpu,power.draw",
                                 "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=2).stdout
        charge, temp, puissance = (v.strip() for v in sortie.splitlines()[0].split(","))
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return None

    def nombre(v):
        try:
            return float(v)
        except ValueError:  # « [N/A] » sur certains GPU
            return None
    return {"gpu_pct": nombre(charge), "temp_gpu_c": nombre(temp), "puissance_w": nombre(puissance)}


class FournisseurGenerique:
    type_carte = "autre"

    def __init__(self, machine, carte=None):
        self.machine = machine
        self.carte = carte  # nom exact de la carte (device tree), s'il existe
        psutil.cpu_percent(None)  # le 1er appel sert de référence et renvoie toujours 0
        # PC avec une carte NVIDIA : nvidia-smi donne la charge, la température et la puissance du GPU
        self._nvidia = shutil.which("nvidia-smi") if carte is None else None

    def memoire(self):
        # available (MemAvailable) : ce que le système peut donner à un nouveau programme
        # sans swap. Le swap ne compte pas : la mémoire du GPU (CUDA) ne peut pas y aller
        ram = psutil.virtual_memory()
        return {"totale": ram.total, "disponible": ram.available, "bureau_actif": bureau_actif()}

    def lire(self):
        ram = psutil.virtual_memory()
        return {
            "horodatage": time.time(),
            "machine": self.machine,
            "cpu_pct": psutil.cpu_percent(None),
            # total - available : la mémoire vraiment indisponible pour un nouveau modèle
            # (ram.used ignore les caches que le système peut libérer, ou les compte mal)
            "ram_utilisee_go": (ram.total - ram.available) / GO,
            "ram_totale_go": ram.total / GO,
            "temp_cpu_c": temperature_cpu(),
            "gpu_pct": None,
            "temp_gpu_c": None,
            "puissance_w": None,
            "source_puissance": None,
            "alertes": [],
        } | self._gpu_nvidia()

    def _gpu_nvidia(self):
        g = lire_nvidia(self._nvidia) if self._nvidia else None
        if not g:
            return {}
        if g["puissance_w"] is not None:
            g["source_puissance"] = "gpu"  # le GPU seul : ni le processeur ni la RAM
        return {k: v for k, v in g.items() if v is not None}
