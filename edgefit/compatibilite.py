# est-ce que ce modèle rentre sur cette carte, avec Ollama ? Et sinon, que faire ?
#
# Différences avec l'estimation générale de llm_modelfit :
#   - les poids ne sont pas estimés (paramètres × octets) : on connaît la taille exacte
#     du fichier GGUF, qui tient compte des quantifications mixtes (Q4_K_M garde
#     certaines couches en 6 bits, d'où ~4,9 bits par poids et pas 4) ;
#   - le cache KV peut être quantifié par Ollama (q8_0, q4_0), formats inconnus de llm_modelfit ;
#   - la mémoire disponible est mesurée maintenant sur la carte, pas lue dans une fiche technique.
# Les formules elles-mêmes (cache KV, buffers) restent celles de llm_modelfit.
#
# Ce module ne fait aucun appel réseau : il reçoit tout en paramètres, ce qui le rend testable.
from llm_modelfit.comparaison import charger_plateformes, verifier_compat
from llm_modelfit.engine import memoire_buffers, memoire_cache_kv, octets_vers_go

GIO = 1024 ** 3

# bits par poids effectifs des formats GGUF (quantifications mixtes comprises), d'après
# les tables de llama.cpp. Utilisés seulement pour estimer une variante pas encore
# téléchargée : pour le fichier choisi, on a sa taille exacte.
BITS_PAR_POIDS = {
    "F32": 32, "F16": 16, "BF16": 16, "Q8_0": 8.5, "Q6_K": 6.56, "Q5_K_M": 5.69, "Q5_K_S": 5.54,
    "Q5_0": 5.5, "Q4_K_M": 4.89, "Q4_K_S": 4.58, "Q4_0": 4.5, "IQ4_XS": 4.25, "Q3_K_M": 3.91, "Q2_K": 3.35,
}
# variantes proposées quand il faut changer de quantification (du plus précis au plus léger)
VARIANTES = ["F16", "Q8_0", "Q6_K", "Q5_K_M", "Q4_K_M", "Q3_K_M", "Q2_K"]
# en dessous de Q4 (~4,5 bits), la qualité baisse nettement : jamais proposé comme "le plus rapide"
BITS_MIN_CONSEILLES = 4.5

# octets par valeur du cache KV selon OLLAMA_KV_CACHE_TYPE (blocs de 32 valeurs + une échelle)
OCTETS_KV = {"f16": 2.0, "q8_0": 34 / 32, "q4_0": 18 / 32}
QUALITE_KV = {"f16": 2, "q8_0": 1, "q4_0": 0}

CONTEXTES = [131072, 65536, 32768, 16384, 8192, 4096, 2048]

# architectures dont Ollama ne garde que la fenêtre pour les couches glissantes.
# Volontairement limité à Gemma 3 (moteur propre d'Ollama) : ailleurs le moteur peut
# réserver le cache complet, et surestimer vaut mieux que promettre ce qui ne tient pas.
MOTIF_GLISSANT = {"gemma3": 6}  # 1 couche globale toutes les 6


def couches_glissantes(arch):
    motif = MOTIF_GLISSANT.get(arch.get("architecture"))
    if not motif or not arch.get("sliding_window"):
        return 0
    return arch["num_layers"] - arch["num_layers"] // motif


def cache_kv_octets(arch, num_ctx, type_kv="f16", parallele=1):
    # Ollama réserve le cache de tout le contexte dès le chargement, pour chaque
    # requête traitée en parallèle (OLLAMA_NUM_PARALLEL)
    return parallele * memoire_cache_kv(
        arch["num_layers"], arch["num_kv_heads"], arch["head_dim"], num_ctx, OCTETS_KV[type_kv],
        nb_couches_glissantes=couches_glissantes(arch), fenetre=arch.get("sliding_window"),
    )


def estimer(arch, poids_octets, num_ctx, type_kv="f16", parallele=1):
    kv = cache_kv_octets(arch, num_ctx, type_kv, parallele)
    buffers = memoire_buffers(poids_octets, kv)
    return {"poids": poids_octets, "cache_kv": kv, "buffers": buffers, "total": poids_octets + kv + buffers}


def marge_securite(type_carte, totale):
    # réserve pour ce que la formule ne voit pas : contexte CUDA, moteur d'Ollama,
    # pics d'allocation. En absolu plutôt qu'en % : ~0,5-1 Go quelle que soit la carte
    if type_carte == "raspberry":
        return int(0.5 * GIO)  # pas de CUDA sur un Raspberry Pi
    return int(max(1 * GIO, 0.10 * totale))


def budget(type_carte, totale, disponible, liberable=0):
    """Mémoire utilisable pour un nouveau modèle, en octets.
    disponible : MemAvailable (ce que le système peut donner sans swap) ;
    liberable  : modèles déjà chargés par Ollama, qu'il décharge pour faire de la place."""
    marge = marge_securite(type_carte, totale)
    return {"totale": totale, "disponible": disponible, "liberable": liberable, "marge": marge,
            "utilisable": max(0, disponible + liberable - marge)}


def _verdict(total, b):
    if total <= b["disponible"] - b["marge"]:
        return "ok"
    if total <= b["utilisable"]:
        return "ok_apres_dechargement"
    return "trop_gros"


def nb_param_estime(arch, poids_octets, format_actuel):
    if arch.get("nb_param"):
        return arch["nb_param"]
    if format_actuel in BITS_PAR_POIDS:
        return poids_octets * 8 / BITS_PAR_POIDS[format_actuel]
    return None


def nom_variante(nom, format_variante):
    # seuls les dépôts Hugging Face ont des noms de variantes prévisibles (":Q4_K_M") ;
    # la bibliothèque Ollama les nomme au cas par cas ("3b-instruct-q8_0"...)
    if nom.startswith(("hf.co/", "huggingface.co/")):
        return f"{nom.partition(':')[0]}:{format_variante}"
    return None


def candidats(nom, arch, poids_octets, format_actuel):
    """Fichiers possibles : celui choisi (taille exacte) + les autres variantes (estimées)."""
    actuel = {"format": format_actuel or "actuel", "poids": poids_octets, "exact": True, "nom": nom,
              "bits": BITS_PAR_POIDS.get(format_actuel, poids_octets * 8 / arch["nb_param"] if arch.get("nb_param") else 0)}
    liste = [actuel]
    n = nb_param_estime(arch, poids_octets, format_actuel)
    if not n:
        return liste
    # correction par la taille réelle du fichier actuel : sur un petit modèle, l'embedding
    # (gros vocabulaire, gardé en haute précision) pèse plus que "paramètres × bits"
    correction = 1.0
    if format_actuel in BITS_PAR_POIDS:
        correction = poids_octets / (n * BITS_PAR_POIDS[format_actuel] / 8)
    for fmt in VARIANTES:
        if fmt != format_actuel:
            liste.append({"format": fmt, "poids": int(n * BITS_PAR_POIDS[fmt] / 8 * correction), "exact": False,
                          "nom": nom_variante(nom, fmt), "bits": BITS_PAR_POIDS[fmt]})
    return liste


def contextes_possibles(contexte_max, demande):
    valeurs = {c for c in CONTEXTES if not contexte_max or c <= contexte_max} | {demande}
    if contexte_max:
        valeurs.add(contexte_max)
    return sorted(v for v in valeurs if v >= 256)


def explorer(nom, arch, poids_octets, format_actuel, b, demande, parallele=1):
    """Toutes les combinaisons fichier × contexte × type de cache KV qui rentrent.
    Une recherche exhaustive suffit : 8 fichiers × ~8 contextes × 3 types = moins de 200 calculs."""
    resultats = []
    for c in candidats(nom, arch, poids_octets, format_actuel):
        for ctx in contextes_possibles(arch.get("context_max"), demande):
            for kv in OCTETS_KV:
                e = estimer(arch, c["poids"], ctx, kv, parallele)
                if e["total"] <= b["utilisable"]:
                    resultats.append({"fichier": c, "num_ctx": ctx, "type_kv": kv, "estimation": e})
    return resultats


def _pile(titre, cle, r, b, bande_passante, type_kv_actuel="f16"):
    e = r["estimation"]
    return {
        "titre": titre, "cle": cle,
        "format": r["fichier"]["format"], "nom": r["fichier"]["nom"], "exact": r["fichier"]["exact"],
        "num_ctx": r["num_ctx"], "type_kv": r["type_kv"],
        "total_go": octets_vers_go(e["total"]), "reste_go": octets_vers_go(b["utilisable"] - e["total"]),
        # décodage : chaque token relit tous les poids -> plafond = bande passante / poids
        "debit_max_tokens_s": bande_passante * 1e9 / e["poids"] if bande_passante else None,
        "telechargement": not r["fichier"]["exact"],
        "reglage_serveur": r["type_kv"] != type_kv_actuel,
        "qualite_degradee": r["fichier"]["bits"] < BITS_MIN_CONSEILLES,
    }


def piles(combinaisons, demande, b, bande_passante=None, type_kv_actuel="f16"):
    """Réglages types parmi ce qui rentre : tel quel (si possible), meilleure qualité,
    plus long contexte, plus rapide."""
    if not combinaisons:
        return []
    tel_quel = next((r for r in combinaisons if r["fichier"]["exact"] and r["num_ctx"] == demande
                     and r["type_kv"] == type_kv_actuel), None)
    au_contexte = [r for r in combinaisons if r["num_ctx"] >= demande]
    if not au_contexte:  # même au plus léger, le contexte demandé ne passe pas : le plus grand possible
        plus_grand = max(r["num_ctx"] for r in combinaisons)
        au_contexte = [r for r in combinaisons if r["num_ctx"] == plus_grand]
    ctx_ref = min(r["num_ctx"] for r in au_contexte)
    au_contexte = [r for r in au_contexte if r["num_ctx"] == ctx_ref]

    qualite = max(au_contexte, key=lambda r: (r["fichier"]["bits"], QUALITE_KV[r["type_kv"]], r["fichier"]["exact"]))
    contexte = max(combinaisons, key=lambda r: (r["num_ctx"], r["fichier"]["bits"], QUALITE_KV[r["type_kv"]]))
    conseilles = [r for r in au_contexte if r["fichier"]["bits"] >= BITS_MIN_CONSEILLES] or au_contexte
    rapide = min(conseilles, key=lambda r: (r["fichier"]["poids"], -QUALITE_KV[r["type_kv"]]))

    choix = [("Tel quel", "tel_quel", tel_quel)] if tel_quel else []
    choix += [("Meilleure qualité", "qualite", qualite), ("Plus long contexte", "contexte", contexte),
              ("Plus rapide", "rapide", rapide)]
    resultat, vus = [], set()
    for titre, cle, r in choix:
        signature = (r["fichier"]["format"], r["num_ctx"], r["type_kv"])
        if signature not in vus:
            vus.add(signature)
            resultat.append(_pile(titre, cle, r, b, bande_passante, type_kv_actuel))
    return resultat


# --- solutions classées par catégorie -------------------------------------------------

def commande_kv(type_kv):
    return ("sudo systemctl edit ollama\n"
            "# ajouter dans la zone indiquée :\n"
            "[Service]\n"
            'Environment="OLLAMA_FLASH_ATTENTION=1"\n'
            f'Environment="OLLAMA_KV_CACHE_TYPE={type_kv}"\n'
            "# puis :\n"
            "sudo systemctl restart ollama")


PETITS_MODELES = [  # (nom dans la bibliothèque Ollama, nombre de paramètres)
    ("gemma3:4b", 4.3e9), ("llama3.2:3b", 3.21e9), ("qwen2.5:3b", 3.09e9), ("qwen2.5:1.5b", 1.54e9),
    ("llama3.2:1b", 1.24e9), ("gemma3:1b", 1.0e9), ("qwen2.5:0.5b", 0.49e9),
]

POUR_INFORMATION = [
    ("Quantification GPTQ ou AWQ",
     "Quantifications calibrées sur des données, souvent un peu plus fidèles que le GGUF à taille égale. "
     "Utilisées par vLLM ou TensorRT-LLM, pas par Ollama."),
    ("TensorRT-LLM ou MLC-LLM sur Jetson",
     "Moteurs optimisés pour les GPU NVIDIA, souvent plus rapides qu'Ollama, mais les modèles doivent être convertis."),
    ("PagedAttention (vLLM)",
     "Réserve le cache KV par petits blocs au fil de la conversation au lieu de tout le contexte dès le chargement."),
    ("Décodage spéculatif",
     "Un petit modèle propose plusieurs tokens, le grand les vérifie d'un coup : plus rapide, mais les deux modèles occupent la mémoire."),
]


def solutions(nom, arch, poids_octets, format_actuel, b, demande, type_kv_actuel="f16", parallele=1,
              type_carte="autre", bureau_actif=False):
    """Pistes classées par catégorie, de la plus simple (un réglage) à la plus lourde (changer de carte)."""
    actuel = estimer(arch, poids_octets, demande, type_kv_actuel, parallele)
    manque = actuel["total"] - b["utilisable"]
    categories = []

    def ajouter(categorie, pistes):
        if pistes:
            categories.append({"categorie": categorie, "pistes": pistes})

    def piste(titre, detail, total=None, action=None, commande=None):
        p = {"titre": titre, "detail": detail, "action": action, "commande": commande}
        if total is not None:
            p["gain_go"] = octets_vers_go(actuel["total"] - total)
            p["rentre"] = total <= b["utilisable"]
        return p

    # 1. réglage de la requête : immédiat, rien à télécharger
    pistes = []
    for ctx in sorted(contextes_possibles(arch.get("context_max"), demande), reverse=True):
        if ctx < demande:
            e = estimer(arch, poids_octets, ctx, type_kv_actuel, parallele)
            if e["total"] <= b["utilisable"]:
                pistes.append(piste(f"Réduire le contexte à {ctx} tokens",
                                    "Le cache KV est réservé pour tout le contexte : moins de contexte, moins de mémoire. "
                                    "La conversation sera oubliée plus tôt.",
                                    e["total"], {"type": "contexte", "valeur": ctx}))
                break
    if type_carte == "jetson":
        pistes.append(piste("Pourquoi pas de déport sur le CPU (num_gpu) ?",
                            "Sur une Jetson, le CPU et le GPU partagent la même mémoire : déplacer des couches "
                            "vers le CPU ne libère rien, et ralentit."))
    ajouter("Réglage de la requête (immédiat)", pistes)

    # 2. réglage du serveur Ollama : vaut pour tous les modèles, demande un redémarrage
    pistes = []
    for kv in ("q8_0", "q4_0"):
        if QUALITE_KV[kv] < QUALITE_KV[type_kv_actuel]:
            e = estimer(arch, poids_octets, demande, kv, parallele)
            perte = "perte de qualité quasi nulle" if kv == "q8_0" else "perte de qualité plus visible sur les longs contextes"
            pistes.append(piste(f"Quantifier le cache KV en {kv}",
                                f"Réglage global d'Ollama (demande Flash Attention), {perte}.",
                                e["total"], commande=commande_kv(kv)))
    if parallele > 1:
        e = estimer(arch, poids_octets, demande, type_kv_actuel, 1)
        pistes.append(piste("Une seule requête à la fois (OLLAMA_NUM_PARALLEL=1)",
                            f"Ollama réserve actuellement {parallele} caches KV complets.", e["total"]))
    ajouter("Réglage du serveur Ollama (redémarrage)", pistes)

    # 3. autre quantification : un autre fichier à télécharger
    pistes = []
    for c in candidats(nom, arch, poids_octets, format_actuel)[1:]:
        if c["poids"] < poids_octets:
            e = estimer(arch, c["poids"], demande, type_kv_actuel, parallele)
            nom_c = c["nom"]
            detail = (f"~{c['bits']:.1f} bits par poids, environ {octets_vers_go(c['poids']):.1f} Go à télécharger (estimé). "
                      + ("" if nom_c else "Cherchez le tag correspondant dans la page « Tags » du modèle sur ollama.com."))
            pistes.append(piste(f"Variante {c['format']}", detail, e["total"],
                                {"type": "telecharger", "valeur": nom_c} if nom_c else None))
    ajouter("Autre quantification (téléchargement)", pistes)

    # 4. libérer de la mémoire sur la carte
    pistes = []
    if b["liberable"]:
        pistes.append(piste("Modèles déjà chargés par Ollama",
                            f"{octets_vers_go(b['liberable']):.1f} Go sont occupés par des modèles chargés ; "
                            "Ollama les décharge pour faire de la place (déjà compté ci-dessus)."))
    if bureau_actif and type_carte in ("jetson", "raspberry"):
        pistes.append(piste("Démarrer sans interface graphique",
                            "Le bureau occupe souvent 0,5 à 1 Go. La carte reste pilotable depuis ce tableau de bord. "
                            "Pour revenir au bureau : sudo systemctl set-default graphical.target",
                            commande="sudo systemctl set-default multi-user.target\nsudo reboot"))
    ajouter("Libérer de la mémoire", pistes)

    # 5. autre modèle, plus petit
    n = nb_param_estime(arch, poids_octets, format_actuel)
    pistes = []
    for nom_petit, params in PETITS_MODELES:
        if n and params < 0.8 * n and len(pistes) < 3:
            pistes.append(piste(nom_petit, f"{params / 1e9:.1f} milliards de paramètres, "
                                f"environ {params * BITS_PAR_POIDS['Q4_K_M'] / 8 / GIO:.1f} Go en Q4_K_M.",
                                action={"type": "verifier", "valeur": nom_petit}))
    ajouter("Autre modèle, plus petit", pistes)

    # 6. autre matériel : les plateformes de llm_modelfit (avec sa propre marge), seulement
    # celles qui ont plus de mémoire que cette carte (leur fiche suppose une carte vide)
    total_go = octets_vers_go(actuel["total"])
    compatibles = [p for p in charger_plateformes()
                   if p["memory_gb"] * 1e9 > 1.1 * b["totale"] and verifier_compat(total_go, p)["compatible"]]
    compatibles.sort(key=lambda p: p["memory_gb"])
    ajouter("Autre matériel", [piste(p["name"], f"{p['memory_gb']:g} Go de mémoire, {p['power_watts']:g} W.")
                               for p in compatibles[:4]])

    ajouter("Hors d'Ollama (pour information)", [piste(t, d) for t, d in POUR_INFORMATION])
    return {"manque_go": octets_vers_go(max(0, manque)), "categories": categories}


def plateforme_correspondante(carte):
    """Fiche llm_modelfit de cette carte (pour sa bande passante), d'après le nom du device tree."""
    if not carte:
        return None
    mots_carte = set(carte.lower().replace("-", " ").split())
    for p in charger_plateformes():
        mots = {m for m in p["name"].lower().split() if m != "nvidia" and not m.endswith("gb")}
        if mots and mots <= mots_carte:
            return p
    return None


def analyser(nom, arch, poids_octets, format_actuel, b, demande, type_kv_actuel="f16", parallele=1,
             type_carte="autre", bureau_actif=False, carte=None):
    actuel = estimer(arch, poids_octets, demande, type_kv_actuel, parallele)
    verdict = _verdict(actuel["total"], b)
    plateforme = plateforme_correspondante(carte)
    bande_passante = plateforme.get("bandwidth_gb_s") if plateforme else None
    combinaisons = explorer(nom, arch, poids_octets, format_actuel, b, demande, parallele)
    resultat = {
        "modele": nom, "format": format_actuel, "num_ctx": demande, "type_kv": type_kv_actuel,
        "architecture": arch,
        "estimation_go": {k: octets_vers_go(v) for k, v in actuel.items()},
        "budget_go": {k: octets_vers_go(v) for k, v in b.items()},
        "verdict": verdict,
        "contexte_max": arch.get("context_max"),
        "couches_glissantes": couches_glissantes(arch),
        "plateforme": plateforme["name"] if plateforme else None,
        "piles": piles(combinaisons, demande, b, bande_passante, type_kv_actuel),
        "solutions": None,
    }
    if verdict == "trop_gros":
        resultat["solutions"] = solutions(nom, arch, poids_octets, format_actuel, b, demande, type_kv_actuel,
                                          parallele, type_carte, bureau_actif)
    return resultat
