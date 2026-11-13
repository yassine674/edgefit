# banc d'essai et score edgefit : « est-ce que ce modèle tourne bien sur CETTE carte ? »
#
# Protocole (version 1), toujours le même pour que les scores se comparent :
#   1. décharger le modèle, puis une première requête « à froid » : mesure le temps de chargement ;
#   2. deux requêtes « à chaud » avec deux textes différents de même longueur (un texte identique
#      profiterait du cache du prompt d'Ollama et donnerait un premier mot trop flatteur) ;
#   3. 128 tokens générés, température 0, graine fixe, contexte 4096 ;
#   4. mémoire réellement occupée lue dans /api/ps, puissance de la carte relevée pendant les essais.
#
# Le score n'est pas stocké : il est recalculé à la lecture à partir des mesures brutes, pour
# qu'un changement de formule n'oblige pas à refaire les essais.
import math
import statistics

PROTOCOLE = 1
NUM_CTX = 4096
NUM_PREDICT = 128

_DEBUT = ("Voici un extrait d'un rapport technique sur l'exécution de modèles de langage sur des cartes "
          "embarquées. ")
_SUJETS = [
    "La mémoire unifiée d'une Jetson partage la même RAM entre le processeur et le GPU, ce qui change "
    "la façon de compter la place disponible pour les poids et pour le cache des clés et des valeurs. ",
    "Un Raspberry Pi n'a pas de GPU utilisable pour l'inférence : tout passe par le processeur, et la "
    "bande passante mémoire devient le facteur qui limite le nombre de tokens générés par seconde. ",
    "La quantification réduit la taille des poids en les stockant sur moins de bits, au prix d'une petite "
    "erreur d'arrondi qui reste faible tant qu'on ne descend pas sous quatre bits par poids. ",
]
_FIN = "Résume ce texte en une dizaine de phrases simples, puis propose trois questions à poser à son auteur."


def textes():
    """Trois textes de même structure et de longueur proche (environ 300 tokens chacun)."""
    return [_DEBUT + (s * 4) + _FIN for s in _SUJETS]


def _borne(x):
    return max(0.0, min(1.0, x))


def score(mesures):
    """Score sur 100 et sa décomposition, à partir des mesures brutes d'un essai.
    - vitesse : 0 à 2 tokens/s, 1 à 20 tokens/s (environ 4 fois la vitesse de lecture), échelle log ;
    - réactivité : premier mot à chaud, 0 à 10 s, 1 à 0,5 s, échelle log ;
    - marge mémoire : une condition, pas un terme ; sous 10 % de mémoire libre, le score est plafonné à 60."""
    tps, ttft = mesures.get("debit_tokens_s"), mesures.get("ttft_s")
    if not tps or not ttft:
        return None
    s_vitesse = _borne(math.log(tps / 2) / math.log(20 / 2))
    s_reactivite = _borne(math.log(10 / ttft) / math.log(10 / 0.5))
    valeur = round(100 * (0.6 * s_vitesse + 0.4 * s_reactivite))
    marge = mesures.get("marge_libre")
    plafonne = marge is not None and marge < 0.10
    if plafonne:
        valeur = min(valeur, 60)
    return {"score": valeur, "vitesse": round(100 * s_vitesse), "reactivite": round(100 * s_reactivite),
            "plafonne": plafonne}


def resumer(essais, chargement_s, memoire_go, marge_libre, puissance_w):
    """Mesures brutes d'un banc : médiane des essais à chaud."""
    debit = statistics.median(e["debit_tokens_s"] for e in essais if e.get("debit_tokens_s"))
    ttft = statistics.median(e["ttft_mesure_s"] for e in essais if e.get("ttft_mesure_s"))
    prefill = statistics.median(e["debit_prefill_tokens_s"] for e in essais if e.get("debit_prefill_tokens_s"))
    m = {"debit_tokens_s": debit, "ttft_s": ttft, "debit_prefill_tokens_s": prefill,
         "chargement_s": chargement_s, "memoire_go": memoire_go, "marge_libre": marge_libre,
         "puissance_w": puissance_w}
    # tokens par joule : énergie de la carte entière (VDD_IN), pas seulement du GPU
    m["tokens_par_joule"] = debit / puissance_w if puissance_w else None
    return m
