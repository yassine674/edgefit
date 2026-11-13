# trouver un modèle : recherche avec suggestions pendant la frappe, et versions d'un dépôt.
#
# Trois sources, dans cet ordre :
#   1. les modèles déjà installés dans Ollama (instantané, marche sans internet) ;
#   2. une liste de modèles adaptés aux petites cartes, livrée avec l'application
#      (donnees/suggestions.json, tailles exactes relevées par scripts/maj_suggestions.py) ;
#   3. la recherche de Hugging Face, limitée aux dépôts GGUF (le format qu'Ollama sait lire).
# La bibliothèque d'Ollama n'a pas d'API de recherche : d'où la liste livrée (2).
#
# La recherche ne fait que proposer des noms : la lecture du modèle sur le registre (calcul
# de compatibilité) n'a lieu qu'une fois un modèle choisi.
import json
import re
import time
from pathlib import Path

import httpx

SUGGESTIONS = json.loads((Path(__file__).parent / "donnees" / "suggestions.json").read_text())

URL_HF = "https://huggingface.co/api"
# modèles de texte ou image+texte ; exclut la synthèse vocale, les embeddings...
TACHES_CHAT = {None, "text-generation", "image-text-to-text"}
# suffixe de quantification dans un nom de fichier : "...-Q4_K_M.gguf", "..._q8_0.gguf", "...-UD-Q4_K_XL.gguf"
MOTIF_QUANT = re.compile(r"(?:^|[-_.])((?:UD-)?(?:I?Q\d(?:_[A-Z0-9]+)*|BF16|F16|FP16|F32))$", re.IGNORECASE)
MOTIF_FRAGMENT = re.compile(r"-\d{5}-of-\d{5}$", re.IGNORECASE)  # fichier découpé en morceaux
MOTIF_DEPOT = re.compile(r"^[A-Za-z0-9][\w.\-]{0,95}/[\w.\-]{1,96}$")

GIO = 1024 ** 3


def depot_valide(depot):
    return bool(MOTIF_DEPOT.match(depot or "")) and ".." not in depot


def chercher_suggestions(q, limite=8):
    q = q.strip().lower()
    if not q:
        return SUGGESTIONS[:limite]
    trouves = [e for e in SUGGESTIONS if q in e["nom"] or q in (e.get("famille") or "") or q in e["description"].lower()]
    return trouves[:limite]


def quant_depuis_fichier(chemin):
    """'dossier/Llama-3.2-1B-Instruct-Q4_K_M.gguf' -> ('Q4_K_M', fragmenté ?) ; None si inconnu."""
    nom = chemin.rsplit("/", 1)[-1]
    if not nom.lower().endswith(".gguf") or "mmproj" in nom.lower():
        return None
    base = nom[:-5]
    fragment = bool(MOTIF_FRAGMENT.search(base))
    base = MOTIF_FRAGMENT.sub("", base)
    m = MOTIF_QUANT.search(base)
    return (m.group(1).upper(), fragment) if m else None


def variantes_depuis_arbre(depot, fichiers):
    """Liste des fichiers d'un dépôt HF -> versions téléchargeables par Ollama, de la plus légère à la plus lourde."""
    versions, projecteurs = {}, []
    for f in fichiers:
        if f.get("type") != "file":
            continue
        taille = (f.get("lfs") or {}).get("size") or f.get("size", 0)
        if "mmproj" in f["path"].lower() and f["path"].lower().endswith(".gguf"):
            projecteurs.append(taille)
            continue
        quant = quant_depuis_fichier(f["path"])
        if quant is None:
            continue
        nom_quant, fragment = quant
        v = versions.setdefault(nom_quant, {"nom": f"hf.co/{depot}:{nom_quant}", "format": nom_quant,
                                            "taille_octets": 0, "fragmente": False})
        v["taille_octets"] += taille
        v["fragmente"] |= fragment
    projecteur = min(projecteurs, default=0)  # HF joint le plus petit projecteur du dépôt
    for v in versions.values():
        v["projecteur_octets"] = projecteur
        v["telechargeable"] = not v["fragmente"]  # Ollama ne sait pas tirer un GGUF en plusieurs morceaux
    return sorted(versions.values(), key=lambda v: v["taille_octets"])


class Catalogue:
    DUREE_CACHE_S = 10 * 60
    PAUSE_HORS_LIGNE_S = 30

    def __init__(self, transport=None):
        self._http = httpx.AsyncClient(transport=transport, timeout=3, follow_redirects=True)
        self._cache = {}
        self._hors_ligne_jusqua = 0.0

    def _en_cache(self, cle):
        trouve = self._cache.get(cle)
        if trouve and time.monotonic() - trouve[0] < self.DUREE_CACHE_S:
            return trouve[1]
        return None

    def _garder(self, cle, valeur):
        if len(self._cache) > 500:
            self._cache.pop(next(iter(self._cache)))
        self._cache[cle] = (time.monotonic(), valeur)
        return valeur

    async def _hf(self, chemin, params):
        """Appel à l'API Hugging Face -> (données, code d'erreur : hors_ligne, sature, introuvable, indisponible)."""
        if time.monotonic() < self._hors_ligne_jusqua:
            return None, "hors_ligne"
        try:
            r = await self._http.get(f"{URL_HF}{chemin}", params=params)
        except httpx.TransportError:
            # mémorisé 30 s : inutile de réessayer à chaque touche tapée
            self._hors_ligne_jusqua = time.monotonic() + self.PAUSE_HORS_LIGNE_S
            return None, "hors_ligne"
        if r.status_code == 429:
            return None, "sature"
        if r.status_code == 404:
            return None, "introuvable"
        if r.status_code >= 400:
            return None, "indisponible"
        return r.json(), None

    async def chercher_hf(self, q, limite=8):
        cle = ("recherche", q)
        if (trouve := self._en_cache(cle)) is not None:
            return trouve, None
        params = [("search", q), ("filter", "gguf"), ("sort", "downloads"), ("limit", limite * 2),
                  ("expand[]", "downloads"), ("expand[]", "gated"), ("expand[]", "pipeline_tag")]
        donnees, erreur = await self._hf("/models", params)
        if erreur:
            return [], erreur
        resultats = [{"depot": m["id"], "telechargements": m.get("downloads", 0), "protege": bool(m.get("gated"))}
                     for m in donnees if m.get("pipeline_tag") in TACHES_CHAT][:limite]
        return self._garder(cle, resultats), None

    async def rechercher(self, q, installes):
        q = q.strip()
        ql = q.lower()
        locaux = [m for m in installes if ql in m["nom"].lower()] if ql else list(installes)
        noms_installes = {m["nom"] for m in installes}
        suggestions = [e for e in chercher_suggestions(q) if e["nom"] not in noms_installes]
        hf, erreur = ([], None)
        if len(q) >= 2:
            hf, erreur = await self.chercher_hf(q)
        return {"installes": locaux[:8], "suggestions": suggestions, "huggingface": hf, "erreur_hf": erreur}

    async def variantes(self, depot):
        cle = ("variantes", depot)
        if (trouve := self._en_cache(cle)) is not None:
            return trouve, None
        donnees, erreur = await self._hf(f"/models/{depot}/tree/main", {"recursive": "true"})
        if erreur:
            return [], erreur
        return self._garder(cle, variantes_depuis_arbre(depot, donnees)), None


# --- mode --fake ----------------------------------------------------------------------

DEPOT_FACTICE = "bartowski/Llama-3.2-1B-Instruct-GGUF"
VARIANTES_FACTICES = {f"hf.co/{DEPOT_FACTICE}:{q}": t for q, t in
                      {"Q3_K_M": 691_356_192, "Q4_K_M": 807_694_464, "Q5_K_M": 911_503_008,
                       "Q6_K": 1_021_800_576, "Q8_0": 1_321_083_008, "F16": 2_479_595_648}.items()}


class CatalogueFactice(Catalogue):
    """Pour --fake : pas de réseau, un seul dépôt Hugging Face d'exemple."""

    async def chercher_hf(self, q, limite=8):
        if q.lower() in DEPOT_FACTICE.lower():
            return [{"depot": DEPOT_FACTICE, "telechargements": 138010, "protege": False}], None
        return [], None

    async def variantes(self, depot):
        if depot != DEPOT_FACTICE:
            return [], "introuvable"
        fichiers = [{"type": "file", "path": f"Llama-3.2-1B-Instruct-{nom.rsplit(':', 1)[1]}.gguf", "size": t}
                    for nom, t in VARIANTES_FACTICES.items()]
        return variantes_depuis_arbre(depot, fichiers), None
