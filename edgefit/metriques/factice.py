# métriques simulées (option --fake) : pour développer et tester l'interface sur un
# PC sans Jetson. Les valeurs bougent doucement, comme sur une vraie carte.
import random
import time


def _marcher(valeur, pas, minimum, maximum):
    return min(maximum, max(minimum, valeur + random.uniform(-pas, pas)))


GIO = 1024 ** 3


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

    def lire(self):
        self._cpu = _marcher(self._cpu, 8, 2, 100)
        self._gpu = _marcher(self._gpu, 10, 0, 100)
        self._ram = _marcher(self._ram, 0.1, 2.5, 7.0)
        self._temp = _marcher(self._temp, 0.8, 38, 75)
        return {
            "horodatage": time.time(),
            "machine": self.machine,
            "cpu_pct": round(self._cpu, 1),
            "ram_utilisee_go": round(self._ram, 2),
            "ram_totale_go": self.RAM_TOTALE_GO,
            "temp_cpu_c": round(self._temp, 1),
            "gpu_pct": round(self._gpu, 1),
            "temp_gpu_c": round(self._temp - 1.5, 1),
            "puissance_w": round(4 + self._gpu / 100 * 6, 2),
            "alertes": [],
        }

    def memoire(self):
        return {"totale": int(self.RAM_TOTALE_GO * GIO), "disponible": int((self.RAM_TOTALE_GO - self._ram) * GIO),
                "bureau_actif": True}
