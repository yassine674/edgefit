# métriques simulées (option --fake) : pour développer et tester l'interface sur un
# PC sans Jetson. Les valeurs bougent doucement, comme sur une vraie carte.
import math
import random
import time


def _marcher(valeur, pas, minimum, maximum):
    return min(maximum, max(minimum, valeur + random.uniform(-pas, pas)))


GIO = 1024 ** 3

# l'Ollama simulé dit ici quand il écrit : la carte simulée chauffe et consomme davantage,
# et ralentit au-delà de 70 °C (bridage thermique), comme une vraie Jetson dans un boîtier fermé
ACTIVITE = {"generation": 0}


def bridage():
    """Facteur de vitesse (1 = pleine vitesse) selon la température simulée."""
    return max(0.6, 1 - max(0.0, ETAT["temp"] - 70) * 0.04)


ETAT = {"temp": 45.0}


class FournisseurFactice:
    machine = "Jetson simulée (--fake)"
    type_carte = "jetson"
    carte = "NVIDIA Jetson Orin Nano Developer Kit"  # pour retrouver sa fiche (bande passante)
    RAM_TOTALE_GO = 7.4

    def __init__(self):
        self._cpu = 20.0
        self._gpu = 10.0
        self._ram = 3.2
        self._temp = 45.0
        self._instant = time.monotonic()

    def lire(self):
        self._cpu = _marcher(self._cpu, 8, 2, 100)
        self._gpu = _marcher(self._gpu, 10, 0, 100)
        self._ram = _marcher(self._ram, 0.1, 2.5, 7.0)
        actif = ACTIVITE["generation"] > 0
        if actif:
            self._gpu = _marcher(85, 8, 60, 100)
        # la température tend vers 78 °C pendant l'écriture, 44 °C au repos (constante de temps : 60 s)
        maintenant = time.monotonic()
        ecart = 1 - math.exp(-(maintenant - self._instant) / 60)
        self._instant = maintenant
        cible = 78 if actif else 44
        self._temp = min(82.0, max(38.0, self._temp + (cible - self._temp) * ecart + random.uniform(-0.15, 0.15)))
        ETAT["temp"] = self._temp
        return {
            "horodatage": time.time(),
            "machine": self.machine,
            "cpu_pct": round(self._cpu, 1),
            "ram_utilisee_go": round(self._ram, 2),
            "ram_totale_go": self.RAM_TOTALE_GO,
            "temp_cpu_c": round(self._temp, 1),
            "gpu_pct": round(self._gpu, 1),
            "temp_gpu_c": round(self._temp - 1.5, 1),
            "puissance_w": round(4 + self._gpu / 100 * 6 + (2.5 if actif else 0), 2),
            "source_puissance": "carte",
            "alertes": ["fréquence du GPU réduite (température)"] if bridage() < 1 else [],
        }

    def memoire(self):
        return {"totale": int(self.RAM_TOTALE_GO * GIO), "disponible": int((self.RAM_TOTALE_GO - self._ram) * GIO),
                "bureau_actif": True}
