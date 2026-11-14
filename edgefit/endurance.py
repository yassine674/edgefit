# endurance : la vitesse tient-elle dans la durée ?
#
# Un banc d'essai dure moins d'une minute : la carte est encore froide. Sur une Jetson ou un
# Raspberry Pi sans ventilation suffisante, la puce chauffe en quelques minutes d'écriture
# continue, baisse sa fréquence (bridage thermique) et le débit chute. Un robot ou un assistant
# qui tourne toute la journée vit ce régime-là, pas celui des 30 premières secondes.
#
# Protocole : le modèle écrit sans arrêt (des requêtes de 512 tokens enchaînées) pendant la
# durée choisie. Toutes les 5 secondes, on note le débit d'écriture de la fenêtre, la
# température, la puissance et les alertes de bridage. Le résumé compare le début et la fin.
import statistics
import time

horloge = time.monotonic  # remplacée dans les tests (dix minutes simulées en un instant)

DUREES_S = (60, 180, 300, 600, 1200)
FENETRE_S = 5.0
NUM_PREDICT = 512
NUM_CTX = 2048
CONSIGNE = ("Écris un long texte détaillé, en plusieurs parties, sur l'histoire des ordinateurs embarqués, "
            "des premiers microcontrôleurs aux cartes actuelles pour l'intelligence artificielle. "
            "Continue sans conclure. (Partie {n})")


class Fenetre:
    """Débit d'écriture sur une fenêtre de temps : tokens / temps passé à écrire. Les pauses entre
    deux requêtes (lecture du message, envoi) ne comptent pas : on mesure la vitesse de la puce."""

    def __init__(self, debut):
        self.debut = debut
        self.tokens = 0
        self.temps = 0.0

    def ajouter(self, intervalle_s):
        self.tokens += 1
        self.temps += intervalle_s

    def debit(self):
        return self.tokens / self.temps if self.temps > 0 else None


def point(t_s, debit, mesure):
    temps = [v for v in (mesure.get("temp_cpu_c"), mesure.get("temp_gpu_c")) if v is not None]
    return {"t": round(t_s, 1), "debit": round(debit, 2) if debit else None,
            "temp": max(temps) if temps else None, "puissance": mesure.get("puissance_w"),
            "gpu": mesure.get("gpu_pct"), "bridage": bool(mesure.get("alertes"))}


def _mediane(valeurs):
    valeurs = [v for v in valeurs if v is not None]
    return statistics.median(valeurs) if valeurs else None


def resumer(points, duree_s, tokens):
    """Début contre fin, baisse en %, moment où le débit décroche, énergie, verdict."""
    utiles = [p for p in points if p.get("debit")]
    if len(utiles) < 3:
        return {"verdict": "insuffisant", "points": len(utiles)}
    n = max(1, len(utiles) // 5)  # premier et dernier cinquième
    debut = _mediane(p["debit"] for p in utiles[:n])
    fin = _mediane(p["debit"] for p in utiles[-n:])
    baisse = max(0.0, (debut - fin) / debut * 100) if debut else 0.0
    # décrochage : premier instant où la médiane glissante de 3 points passe sous 90 % du début
    decroche = None
    for i in range(2, len(utiles)):
        fenetre = utiles[i - 2:i + 1]
        if _mediane(p["debit"] for p in fenetre) < 0.9 * debut:
            decroche = next(p["t"] for p in fenetre if p["debit"] < 0.9 * debut)  # première mesure lente
            break
    temps = [p["temp"] for p in points if p.get("temp") is not None]
    puissances = [p["puissance"] for p in points if p.get("puissance") is not None]
    debit_moyen = _mediane(p["debit"] for p in utiles)
    puissance = sum(puissances) / len(puissances) if puissances else None
    bridage_signale = any(p.get("bridage") for p in points)
    verdict = "stable" if baisse < 5 and not bridage_signale else "leger" if baisse < 15 else "bride"
    return {
        "verdict": verdict, "debit_debut": debut, "debit_fin": fin, "debit_median": debit_moyen,
        "baisse_pct": baisse, "decroche_s": decroche, "bridage_signale": bridage_signale,
        "temp_debut": temps[0] if temps else None, "temp_max": max(temps) if temps else None,
        "puissance_moy_w": puissance,
        "joules_par_token": puissance / debit_moyen if puissance and debit_moyen else None,
        "tokens": tokens, "duree_s": duree_s, "points": len(utiles),
    }
