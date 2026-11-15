# atelier : essayer des optimisations sur un modèle et voir tout de suite ce que chacune change,
# puis fabriquer un modèle Ollama qui garde les réglages choisis.
#
# Chaque catégorie renvoie des OPTIONS chiffrées (mémoire totale, gain, verdict), pas des conseils :
#   poids      autre quantification du même modèle (un autre fichier, vérifié dans le registre) ;
#   cache_kv   précision du cache des clés/valeurs (réglage du serveur Ollama) ;
#   contexte   taille de la fenêtre (gardée dans le modèle créé : PARAMETER num_ctx) ;
#   materiel   le même réglage sur les autres plateformes connues de llm_modelfit.
# Les réglages qui ne changent pas la mémoire (répartition GPU/CPU, vitesse, génération,
# instructions) sont validés ici et écrits dans le modèle créé.
from .compatibilite import (
    BITS_PAR_POIDS, OCTETS_KV, _verdict, candidats, charger_plateformes, commande_kv, contextes_possibles, estimer,
    octets_vers_go, verifier_compat,
)

# paramètres qu'un modèle Ollama peut garder (Modelfile PARAMETER) : nom -> (type, min, max)
PARAMETRES = {
    "num_ctx": (int, 256, 1_048_576),
    # fenêtre glissante : quand le contexte est plein, Ollama garde les num_keep premiers tokens
    # (instructions, début de la conversation) et jette la moitié la plus ancienne du reste
    "num_keep": (int, 0, 1_048_576),
    "num_gpu": (int, 0, 999),          # couches envoyées au GPU ; le reste tourne sur le processeur
    "num_thread": (int, 1, 256),
    "num_batch": (int, 8, 2048),
    "num_predict": (int, -1, 32_768),  # longueur maximale d'une réponse (-1 : pas de limite)
    "temperature": (float, 0.0, 2.0),
    "top_p": (float, 0.0, 1.0),
    "top_k": (int, 1, 1000),
    "repeat_penalty": (float, 0.0, 2.0),
    "seed": (int, 0, 2 ** 31 - 1),
}
# quantifications qu'Ollama sait produire lui-même à partir d'un modèle F16 ou F32
QUANTIFIABLES = ("q4_K_M", "q4_K_S", "q8_0")


class ErreurAtelier(ValueError):
    pass


def valider_parametres(parametres):
    """Ne garde que les paramètres connus, convertis et bornés ; lève ErreurAtelier sinon."""
    propres = {}
    for cle, valeur in (parametres or {}).items():
        if cle not in PARAMETRES:
            raise ErreurAtelier(f"paramètre inconnu : {cle}")
        genre, bas, haut = PARAMETRES[cle]
        if valeur is None or valeur == "":
            continue
        try:
            valeur = genre(valeur)
        except (TypeError, ValueError) as e:
            raise ErreurAtelier(f"{cle} : nombre attendu") from e
        if not bas <= valeur <= haut:
            raise ErreurAtelier(f"{cle} doit être entre {bas} et {haut}")
        propres[cle] = valeur
    return propres


def _option(total, reference, b, **champs):
    return {**champs, "total_go": octets_vers_go(total), "gain_go": octets_vers_go(reference - total),
            "verdict": _verdict(total, b), "reste_go": octets_vers_go(b["utilisable"] - total)}


def coeurs():
    """Cœurs physiques et fils logiques du processeur (pour le réglage num_thread)."""
    import os
    import psutil
    logiques = os.cpu_count() or 1
    return {"physiques": psutil.cpu_count(logical=False) or logiques, "logiques": logiques}


def etudier(nom, arch, poids_octets, format_actuel, b, num_ctx, type_kv, bande_passante=None, rendement=0.6,
            type_carte="autre"):
    """État de l'atelier pour ce modèle et ces choix : estimation et options de chaque catégorie."""
    actuel = estimer(arch, poids_octets, num_ctx, type_kv)
    total = actuel["total"]
    plafond = bande_passante * 1e9 / actuel["poids"] if bande_passante else None

    poids = []
    for c in candidats(nom, arch, poids_octets, format_actuel):
        e = estimer(arch, c["poids"], num_ctx, type_kv)
        poids.append(_option(e["total"], total, b, format=c["format"], nom=c["nom"], bits=round(c["bits"], 2),
                             fichier_go=octets_vers_go(c["poids"]), exact=c["exact"], choisi=c["exact"],
                             plafond_tokens_s=bande_passante * 1e9 / e["poids"] if bande_passante else None))
    poids.sort(key=lambda o: -o["fichier_go"])

    cache = [_option(estimer(arch, poids_octets, num_ctx, kv)["total"], total, b, valeur=kv, choisi=kv == type_kv,
                     kv_go=octets_vers_go(estimer(arch, poids_octets, num_ctx, kv)["cache_kv"]),
                     commande=commande_kv(kv) if kv != "f16" else None)
             for kv in OCTETS_KV]

    contextes = [_option(estimer(arch, poids_octets, c, type_kv)["total"], total, b, valeur=c, choisi=c == num_ctx,
                         kv_go=octets_vers_go(estimer(arch, poids_octets, c, type_kv)["cache_kv"]))
                 for c in sorted(set(contextes_possibles(arch.get("context_max"), num_ctx)) | {num_ctx})]

    total_go = octets_vers_go(total)
    plateformes = []
    for p in charger_plateformes():
        v = verifier_compat(total_go, p)
        plateformes.append({"nom": p["name"], "memoire_go": p["memory_gb"], "watts": p.get("power_watts"),
                            "type_memoire": p.get("memory_type"), "compatible": v["compatible"],
                            "reste_go": round(v["utilisable_go"] - total_go, 2),
                            "plafond_tokens_s": p["bandwidth_gb_s"] * 1e9 / actuel["poids"] if p.get("bandwidth_gb_s") else None})
    plateformes.sort(key=lambda p: (not p["compatible"], p["memoire_go"]))

    return {
        "modele": nom, "format": format_actuel, "num_ctx": num_ctx, "type_kv": type_kv,
        "bits": round(BITS_PAR_POIDS.get(format_actuel, 0), 2) or None,
        "couches": arch.get("num_layers"), "contexte_max": arch.get("context_max"),
        "estimation_go": {k: octets_vers_go(v) for k, v in actuel.items()},
        "budget_go": {k: octets_vers_go(v) for k, v in b.items()},
        "verdict": _verdict(total, b), "reste_go": octets_vers_go(b["utilisable"] - total),
        "plafond_tokens_s": plafond, "vitesse_attendue": plafond * rendement if plafond else None, "rendement": rendement,
        "memoire_unifiee": type_carte in ("jetson", "raspberry"),
        "quantifiable": (format_actuel or "").upper() in ("F16", "F32"),
        "coeurs": coeurs(),
        "categories": {"poids": poids, "cache_kv": cache, "contexte": contextes, "materiel": plateformes},
    }
