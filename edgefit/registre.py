# infos d'un modèle AVANT de le télécharger : taille exacte et architecture.
#
# Ollama télécharge ses modèles depuis un registre au format des images Docker :
#   - le manifeste (petit JSON) liste les fichiers du modèle et leur taille exacte ;
#   - le fichier principal est un GGUF, qui commence par un en-tête de métadonnées
#     (nombre de couches, têtes KV, contexte...) avant les poids eux-mêmes.
# En lisant le manifeste puis seulement le premier mégaoctet du fichier (requête HTTP
# "Range"), on obtient tout le nécessaire au calcul de mémoire sans télécharger des Go.
#
# Deux registres parlent ce protocole :
#   "qwen2.5:0.5b", "utilisateur/modele:tag"    -> registry.ollama.ai
#   "hf.co/<auteur>/<dépôt GGUF>:Q4_K_M"         -> huggingface.co (qui l'imite pour Ollama)
import asyncio
import re
import struct
import time

import httpx

from .ollama import architecture_depuis_model_info

# numéro "general.file_type" de l'en-tête GGUF -> nom du format (énumération de llama.cpp)
TYPES_FICHIER = {0: "F32", 1: "F16", 2: "Q4_0", 7: "Q8_0", 8: "Q5_0", 10: "Q2_K", 12: "Q3_K_M", 14: "Q4_K_S",
                 15: "Q4_K_M", 16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K", 30: "IQ4_XS", 32: "BF16"}

TYPE_MANIFESTE = "application/vnd.docker.distribution.manifest.v2+json"
TYPE_COUCHE_MODELE = "application/vnd.ollama.image.model"
TYPE_COUCHE_PROJECTEUR = "application/vnd.ollama.image.projector"
# Hugging Face ne sert le format Ollama qu'à un client qui se présente comme Ollama ;
# le registre d'Ollama, lui, exige alors une requête signée : l'en-tête n'est envoyé qu'à HF
AGENT_HF = "ollama/0.12.0 (edgefit)"
LECTURE_INITIALE = 1024 * 1024
LECTURE_MAX = 8 * 1024 * 1024


class RegistreErreur(Exception):
    """Erreur expliquée : `code` permet à la page de l'afficher dans la langue choisie."""

    def __init__(self, message, code="registre"):
        super().__init__(message)
        self.code = code


def adresse(nom):
    """(url de base, dépôt, tag, en-têtes) pour un nom de modèle Ollama."""
    if ".." in nom:
        raise RegistreErreur("nom de modèle invalide", "nom")
    nom, _, tag = nom.partition(":")
    tag = tag or "latest"
    for prefixe in ("hf.co/", "huggingface.co/"):
        if nom.startswith(prefixe):
            return "https://huggingface.co", nom[len(prefixe):], tag, {"User-Agent": AGENT_HF}
    depot = nom if "/" in nom else f"library/{nom}"
    return "https://registry.ollama.ai", depot, tag, {}


# --- lecture de l'en-tête GGUF --------------------------------------------------------
# format : "GGUF", version (u32), nb de tenseurs (u64), nb de métadonnées (u64), puis
# chaque métadonnée = clé (chaîne) + type (u32) + valeur. Chaîne = longueur u64 + octets.

def tags_de_la_page(famille, html):
    """Paires (tag, empreinte) de la page publique des tags : « qwen:4b-chat d53d04290064 • 2.3GB »."""
    texte = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    return list(dict.fromkeys(re.findall(re.escape(famille) + r":([A-Za-z0-9._-]+) ([0-9a-f]{12}) •", texte)))


def tags_par_format(nom, tags):
    """{quantification: nom complet} parmi les tags publiés d'un modèle de la bibliothèque Ollama, pour la
    même taille que `nom` (« llama3.2:3b » -> {"Q8_0": "llama3.2:3b-instruct-q8_0", ...}). Quand plusieurs
    tags donnent la même quantification, la version « instruct » (celle du tag court) passe avant « text »."""
    famille, _, tag = nom.partition(":")
    taille = tag.split("-")[0]
    if not taille or taille == "latest":
        return {}
    trouves = {}
    for t in tags:
        morceaux = t.split("-")
        if morceaux[0] != taille or len(morceaux) < 2:
            continue
        quant = morceaux[-1].upper().replace("FP16", "F16")
        if not re.fullmatch(r"F16|F32|Q\d\w*", quant):
            continue
        milieu = morceaux[1:-1]
        rang = 0 if not milieu else 1 if milieu == ["instruct"] else 3 if "text" in milieu else 2
        if quant not in trouves or rang < trouves[quant][0]:
            trouves[quant] = (rang, f"{famille}:{t}")
    return {q: n for q, (_, n) in trouves.items()}


class _Fin(Exception):
    """L'en-tête continue au-delà des octets déjà lus."""


_FORMATS = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
TYPE_CHAINE, TYPE_TABLEAU = 8, 9


class _Lecteur:
    def __init__(self, octets):
        self.octets, self.pos = octets, 0

    def lire(self, n):
        if self.pos + n > len(self.octets):
            raise _Fin
        morceau = self.octets[self.pos:self.pos + n]
        self.pos += n
        return morceau

    def nombre(self, fmt):
        return struct.unpack(fmt, self.lire(struct.calcsize(fmt)))[0]

    def chaine(self):
        return self.lire(self.nombre("<Q")).decode("utf-8", "replace")

    def valeur(self, type_valeur):
        if type_valeur == TYPE_CHAINE:
            return self.chaine()
        if type_valeur == TYPE_TABLEAU:
            type_elements, n = self.nombre("<I"), self.nombre("<Q")
            if type_elements in _FORMATS:
                # tableaux de nombres (scores du tokenizer...) : sautés, inutiles ici
                self.lire(struct.calcsize(_FORMATS[type_elements]) * n)
                return None
            return [self.valeur(type_elements) for _ in range(n)]
        if type_valeur not in _FORMATS:
            raise RegistreErreur(f"type de métadonnée GGUF inconnu : {type_valeur}")
        return self.nombre(_FORMATS[type_valeur])


def lire_entete_gguf(octets):
    """Métadonnées GGUF lisibles dans `octets` -> (dict, complet). Tolère un en-tête coupé."""
    lecteur = _Lecteur(octets)
    try:
        if lecteur.lire(4) != b"GGUF":
            raise RegistreErreur("ce fichier n'est pas un modèle GGUF", "gguf")
        lecteur.nombre("<I")  # version du format
        lecteur.nombre("<Q")  # nombre de tenseurs
        nb_meta = lecteur.nombre("<Q")
    except _Fin:
        return {}, False
    meta = {}
    try:
        for _ in range(nb_meta):
            cle = lecteur.chaine()
            meta[cle] = lecteur.valeur(lecteur.nombre("<I"))
    except _Fin:
        return meta, False
    return meta, True


def parametres_depuis_texte(texte):
    """ "999.89M" -> 999 890 000, "1.24B" -> 1 240 000 000 (format des configs Ollama)."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([KMBT])\s*", texte or "", re.IGNORECASE)
    if not m:
        return None
    return int(float(m.group(1)) * {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[m.group(2).upper()])


def format_fichier(config, meta, tag):
    """Nom du format (Q4_K_M...) : config Ollama, sinon en-tête GGUF, sinon le tag (dépôts HF)."""
    candidat = (config.get("file_type") or "").upper()
    if candidat and candidat != "UNKNOWN":
        return candidat
    if meta.get("general.file_type") in TYPES_FICHIER:
        return TYPES_FICHIER[meta["general.file_type"]]
    return tag.upper() if re.fullmatch(r"(I?Q\d\w*|F16|BF16|F32)", tag, re.IGNORECASE) else None


def _architecture_complete(meta):
    arch = meta.get("general.architecture")
    return bool(arch) and all(f"{arch}.{c}" in meta for c in ("block_count", "attention.head_count", "context_length"))


# --- accès réseau ---------------------------------------------------------------------

class Registre:
    """Lecture des registres, avec un cache : choisir deux fois le même modèle (ou changer
    seulement le contexte) ne relit pas le registre."""

    DUREE_CACHE_S = 15 * 60

    def __init__(self, transport=None):
        # 8 s : assez pour une connexion lente, assez court pour une page qui attend la réponse
        self._http = httpx.AsyncClient(transport=transport, timeout=8, follow_redirects=True)
        self._cache = {}     # nom -> (instant, infos)
        self._en_cours = {}  # nom -> tâche : deux demandes simultanées ne lisent le registre qu'une fois

    async def variantes_ollama(self, nom):
        """Autres quantifications publiées pour ce modèle de la bibliothèque Ollama : {format: nom}.
        Le registre n'a pas de liste de tags : on lit la page publique ollama.com/library/<modèle>/tags.
        Vide pour un dépôt Hugging Face (noms prévisibles), un modèle personnel, ou hors ligne."""
        page = await self._page_tags(nom.partition(":")[0])
        return tags_par_format(nom, sorted({t for t, _ in page})) if page else {}

    async def _page_tags(self, famille):
        """[(tag, empreinte sur 12 caractères)] lus sur ollama.com/library/<famille>/tags ; [] hors ligne."""
        if "/" in famille or not re.fullmatch(r"[A-Za-z0-9._-]+", famille):
            return []
        cle = f"tags:{famille}"
        en_cache = self._cache.get(cle)
        if en_cache and time.monotonic() - en_cache[0] < self.DUREE_CACHE_S:
            return en_cache[1]
        try:
            r = await self._http.get(f"https://ollama.com/library/{famille}/tags")
        except httpx.HTTPError:
            return []
        page = tags_de_la_page(famille, r.text) if r.status_code == 200 else []
        self._cache[cle] = (time.monotonic(), page)
        return page

    async def nom_exact(self, nom, empreinte):
        """« qwen:latest » -> « qwen:4b-chat-v1.5-q4_0 » : le tag le plus précis qui désigne le même fichier
        (même empreinte). None si rien de plus précis n'est trouvé (modèle personnel, hors ligne...)."""
        if not empreinte or "/" in nom.partition(":")[0]:
            return None
        famille, _, tag = nom.partition(":")
        memes = [t for t, e in await self._page_tags(famille) if empreinte.startswith(e)]
        meilleur = max(memes, key=len, default=None)
        return f"{famille}:{meilleur}" if meilleur and len(meilleur) > len(tag or "latest") else None

    async def _get(self, url, entetes):
        try:
            r = await self._http.get(url, headers=entetes)
        except httpx.TransportError as e:
            raise RegistreErreur("registre injoignable : la carte a-t-elle accès à internet ?", "hors_ligne") from e
        if r.status_code in (401, 403):
            raise RegistreErreur("modèle protégé : acceptez sa licence sur Hugging Face (ou nom introuvable)", "protege")
        if r.status_code == 404:
            raise RegistreErreur("modèle ou version introuvable sur le registre", "introuvable")
        if r.status_code == 400 and "shard" in r.text.lower():
            raise RegistreErreur("cette version est découpée en plusieurs fichiers, ce qu'Ollama ne sait pas télécharger", "fragmente")
        if r.status_code == 429:
            raise RegistreErreur("registre momentanément saturé : réessayez dans une minute", "sature")
        if r.status_code >= 400:
            raise RegistreErreur(f"le registre a répondu {r.status_code}")
        return r

    async def infos(self, nom):
        trouve = self._cache.get(nom)
        if trouve and time.monotonic() - trouve[0] < self.DUREE_CACHE_S:
            return trouve[1]
        if nom not in self._en_cours:
            self._en_cours[nom] = asyncio.ensure_future(self._lire(nom))
        try:
            infos = await asyncio.shield(self._en_cours[nom])
        finally:
            tache = self._en_cours.get(nom)
            if tache is not None and tache.done():
                del self._en_cours[nom]
        self._cache[nom] = (time.monotonic(), infos)
        return infos

    async def _lire(self, nom):
        base, depot, tag, entetes = adresse(nom)
        manifeste = (await self._get(f"{base}/v2/{depot}/manifests/{tag}", {**entetes, "Accept": TYPE_MANIFESTE})).json()
        couches = manifeste.get("layers", [])
        couche = next((c for c in couches if c.get("mediaType") == TYPE_COUCHE_MODELE), None)
        if couche is None:
            raise RegistreErreur("le manifeste ne contient pas de fichier modèle", "gguf")
        # modèles "vision" : un second fichier (le projecteur, qui transforme une image en
        # tokens) est téléchargé et chargé en mémoire avec le modèle
        projecteur = sum(c.get("size", 0) for c in couches if c.get("mediaType") == TYPE_COUCHE_PROJECTEUR)
        # petit fichier de config (quelques centaines d'octets) : format et nombre de paramètres
        config = {}
        if manifeste.get("config", {}).get("digest"):
            try:
                config = (await self._get(f"{base}/v2/{depot}/blobs/{manifeste['config']['digest']}", entetes)).json()
            except (RegistreErreur, ValueError):
                pass

        url = f"{base}/v2/{depot}/blobs/{couche['digest']}"
        n = LECTURE_INITIALE
        while True:
            octets = (await self._get(url, {**entetes, "Range": f"bytes=0-{n - 1}"})).content
            meta, complet = lire_entete_gguf(octets)
            # en pratique l'architecture tient dans le premier Mo ; on élargit sinon
            if complet or _architecture_complete(meta) or n >= LECTURE_MAX or len(octets) < n:
                break
            n *= 4
        if not _architecture_complete(meta):
            raise RegistreErreur("architecture absente de l'en-tête du fichier GGUF", "gguf")
        arch = architecture_depuis_model_info(meta)
        arch["nb_param"] = arch["nb_param"] or parametres_depuis_texte(
            config.get("model_type") or config.get("config", {}).get("parameters"))
        arch["projecteur_octets"] = projecteur
        return {"taille_octets": couche["size"], "projecteur_octets": projecteur,
                "telechargement_octets": sum(c.get("size", 0) for c in couches),
                "architecture": arch, "format": format_fichier(config, meta, tag)}


def _architecture_simulee(entree):
    # mode --fake : architecture plausible déduite du nombre de paramètres (pas de réseau)
    n = parametres_depuis_texte(entree["parametres"]) or 1e9
    couches, tetes_kv, dim = (24, 2, 64) if n < 1e9 else (28, 4, 128) if n < 4.5e9 else (32, 8, 128)
    return {"architecture": entree.get("famille") or "llama", "nb_param": int(n), "num_layers": couches,
            "num_kv_heads": tetes_kv, "head_dim": dim, "context_max": 32768, "sliding_window": None,
            "projecteur_octets": entree.get("projecteur_octets", 0)}


class RegistreFactice:
    """Pour --fake : les modèles suggérés (tailles réelles, architecture simulée) et un dépôt
    Hugging Face d'exemple, sans réseau."""

    MODELES = {
        "gemma3:1b": {"taille_octets": 806_058_400, "format": "Q4_K_M", "architecture": {
            "architecture": "gemma3", "nb_param": 999_885_952, "num_layers": 26, "num_kv_heads": 1,
            "head_dim": 256, "context_max": 32768, "sliding_window": 512}},
        "llama3.1:8b": {"taille_octets": 4_920_738_944, "format": "Q4_K_M", "architecture": {
            "architecture": "llama", "nb_param": 8_030_261_248, "num_layers": 32, "num_kv_heads": 8,
            "head_dim": 128, "context_max": 131072, "sliding_window": None}},
    }
    LLAMA_1B = {"architecture": "llama", "nb_param": 1_235_814_400, "num_layers": 16, "num_kv_heads": 8,
                "head_dim": 64, "context_max": 131072, "sliding_window": None}

    VARIANTES = {"Q8_0": 8.5, "Q3_K_M": 3.9}  # quantifications « publiées » en mode --fake : bits par poids

    async def nom_exact(self, nom, empreinte):
        return "qwen:4b-chat-v1.5-q4_0" if nom == "qwen:latest" else None

    async def variantes_ollama(self, nom):
        from .catalogue import SUGGESTIONS
        base = nom.split("-instruct-")[0]
        if not any(e["nom"] == base for e in SUGGESTIONS):
            return {}
        return {"Q4_K_M": base, **{q: f"{base}-instruct-{q.lower()}" for q in self.VARIANTES}}

    async def infos(self, nom):
        from .catalogue import SUGGESTIONS, VARIANTES_FACTICES
        base, _, quant = nom.partition("-instruct-")
        if quant.upper() in self.VARIANTES and any(e["nom"] == base for e in SUGGESTIONS):
            entree = next(e for e in SUGGESTIONS if e["nom"] == base)
            taille = int(entree["taille_octets"] * self.VARIANTES[quant.upper()] / 4.85)
            return {"taille_octets": taille, "format": quant.upper(), "architecture": _architecture_simulee(entree),
                    "projecteur_octets": 0, "telechargement_octets": taille}
        if nom in self.MODELES:
            m = self.MODELES[nom]
        elif nom in VARIANTES_FACTICES:
            m = {"taille_octets": VARIANTES_FACTICES[nom], "format": nom.rsplit(":", 1)[1], "architecture": self.LLAMA_1B}
        else:
            entree = next((e for e in SUGGESTIONS if e["nom"] == nom), None)
            if entree is None:
                raise RegistreErreur("modèle introuvable (mode --fake : choisissez un modèle dans les suggestions)", "introuvable")
            m = {"taille_octets": entree["taille_octets"], "format": entree["format"],
                 "architecture": _architecture_simulee(entree)}
        projecteur = m["architecture"].get("projecteur_octets", 0)
        return {**m, "projecteur_octets": projecteur, "telechargement_octets": m["taille_octets"] + projecteur}
