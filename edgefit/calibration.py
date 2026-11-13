# calibration : l'estimation de mémoire apprend des mesures réelles de CETTE carte.
#
# Après chaque réponse, Ollama donne la mémoire réellement occupée par le modèle chargé
# (/api/ps). On la garde à côté de l'estimation détaillée (poids, cache KV) et on ajuste :
#
#     mémoire réelle ≈ fixe + a × poids + b × cache KV
#
# La formule de départ (llm_modelfit) correspond à fixe = 0, a = b = 1,1 (10 % de buffers).
# L'ajustement part de là (régression « ridge » : chaque coefficient est tiré vers sa valeur
# de départ), si bien qu'avec peu de mesures la correction reste prudente, et qu'elle ne
# s'en écarte que si les mesures le justifient. Pas de numpy : un système 3x3 résolu à la main.
GIO = 1024 ** 3
DEPART = (0.0, 1.1, 1.1)      # fixe (Go), a, b
FORCE_DEPART = 0.5            # plus c'est grand, plus il faut de mesures pour s'éloigner du départ
MIN_POINTS = 3                # en dessous, on ne corrige pas le verdict


def _resoudre(m, v):
    """Résout m · x = v (3x3) par élimination de Gauss avec pivot."""
    a = [row[:] + [v[i]] for i, row in enumerate(m)]
    n = len(a)
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(a[r][c]))
        if abs(a[p][c]) < 1e-12:
            return None
        a[c], a[p] = a[p], a[c]
        for r in range(n):
            if r != c:
                k = a[r][c] / a[c][c]
                a[r] = [x - k * y for x, y in zip(a[r], a[c])]
    return [a[i][n] / a[i][i] for i in range(n)]


def ajuster(points):
    """points : [(poids_go, kv_go, mesure_go)] -> coefficients (fixe, a, b) ou None.
    Minimise Σ (mesure - fixe - a·poids - b·kv)² + λ · Σ (coef - départ)²."""
    if not points:
        return None
    lam = FORCE_DEPART
    lignes = [(1.0, p, k) for p, k, _ in points]
    mt_m = [[sum(l[i] * l[j] for l in lignes) + (lam if i == j else 0) for j in range(3)] for i in range(3)]
    mt_y = [sum(l[i] * y for l, (_, _, y) in zip(lignes, points)) + lam * DEPART[i] for i in range(3)]
    return _resoudre(mt_m, mt_y)


def predire(coef, poids_go, kv_go):
    fixe, a, b = coef
    return fixe + a * poids_go + b * kv_go


def resume(observations):
    """Coefficients, erreur moyenne avant/après et nombre de points, pour la page et pour le verdict."""
    points = [(o["poids_go"], o["kv_go"], o["mesure_go"]) for o in observations]
    coef = ajuster(points)
    if coef is None:
        return {"n": 0, "coef": None, "erreur_brute_pct": None, "erreur_corrigee_pct": None, "active": False}
    def erreur(f):
        return 100 * sum(abs(f(p, k) - y) / y for p, k, y in points) / len(points)
    return {
        "n": len(points), "coef": {"fixe_go": coef[0], "a": coef[1], "b": coef[2]},
        "erreur_brute_pct": erreur(lambda p, k: predire(DEPART, p, k)),
        "erreur_corrigee_pct": erreur(lambda p, k: predire(coef, p, k)),
        "active": len(points) >= MIN_POINTS,
    }
