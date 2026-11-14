# objectif : « je veux tel usage, au moins N tokens/s, tel contexte : que dois-je installer ? »
#
# Pour chaque modèle du catalogue on connaît déjà la mémoire nécessaire (verdict) et le plafond
# théorique de vitesse : bande passante mémoire / taille des poids (chaque token relit tous les
# poids). Une vraie carte n'atteint qu'une partie de ce plafond. Cette part (le rendement) est
# apprise sur la carte elle-même : médiane de « débit mesuré / plafond » sur les bancs d'essai
# déjà faits. Sans aucune mesure, on prend 60 %, une valeur typique de llama.cpp sur GPU embarqué,
# et la page le dit.
#
# Parmi les modèles qui tiennent en mémoire, font l'usage voulu et atteignent la vitesse, le
# meilleur choix est le plus gros (plus de paramètres : en général de meilleures réponses).
import re
import statistics

RENDEMENT_DEFAUT = 0.6
USAGES = ("chat", "code", "raisonnement", "vision", "multilingue")


def milliards(parametres):
    """« 3.2B » -> 3.2 ; « 494M » -> 0.494 ; inconnu -> 0."""
    m = re.match(r"\s*([\d.]+)\s*([MBK]?)", str(parametres or ""), re.I)
    if not m:
        return 0.0
    v = float(m.group(1))
    return {"M": v / 1e3, "K": v / 1e6}.get(m.group(2).upper(), v)


def rendement(paires):
    """(rendement, nombre de mesures) à partir de paires (débit mesuré, plafond théorique)."""
    ratios = [mesure / plafond for mesure, plafond in paires if mesure and plafond]
    ratios = [r for r in ratios if 0.05 < r < 1.5]  # une mesure aberrante ne doit pas tout fausser
    if not ratios:
        return RENDEMENT_DEFAUT, 0
    return statistics.median(ratios), len(ratios)


def vitesse(m, rend):
    """(tokens/s attendus, source) : la mesure si elle existe, sinon le plafond × rendement."""
    if m.get("debit_mesure"):
        return m["debit_mesure"], "mesure"
    if m.get("plafond_tokens_s"):
        return m["plafond_tokens_s"] * rend, "estimation"
    return None, "inconnue"


def classer(modeles, usage, debit_min, rend):
    retenus, presque, exclus = [], [], {"memoire": 0, "usage": 0, "lent": 0, "inconnue": 0}
    for m in modeles:
        v, source = vitesse(m, rend)
        entree = {**m, "vitesse_attendue": v, "source_vitesse": source}
        if m.get("verdict") == "trop_gros":
            exclus["memoire"] += 1
        elif usage and usage not in (m.get("tags") or []) and not (usage == "vision" and m.get("vision")):
            exclus["usage"] += 1
        elif debit_min and v is None:
            exclus["inconnue"] += 1
        elif debit_min and v < debit_min:
            exclus["lent"] += 1
            if v >= 0.75 * debit_min:
                presque.append(entree)
        else:
            retenus.append(entree)
    ordre = lambda e: (milliards(e.get("parametres")), e.get("vitesse_attendue") or 0)  # noqa: E731
    retenus.sort(key=ordre, reverse=True)
    presque.sort(key=lambda e: e.get("vitesse_attendue") or 0, reverse=True)
    return {"retenus": retenus, "presque": presque[:3], "exclus": exclus}
