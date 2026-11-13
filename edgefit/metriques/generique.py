# toute machine : CPU, RAM et température via psutil (pas de GPU détectable ici)
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


class FournisseurGenerique:
    type_carte = "autre"

    def __init__(self, machine, carte=None):
        self.machine = machine
        self.carte = carte  # nom exact de la carte (device tree), s'il existe
        psutil.cpu_percent(None)  # le 1er appel sert de référence et renvoie toujours 0

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
            "alertes": [],
        }
