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
import re
import struct

import httpx

from .ollama import architecture_depuis_model_info

# numéro "general.file_type" de l'en-tête GGUF -> nom du format (énumération de llama.cpp)
TYPES_FICHIER = {0: "F32", 1: "F16", 2: "Q4_0", 7: "Q8_0", 8: "Q5_0", 10: "Q2_K", 12: "Q3_K_M", 14: "Q4_K_S",
                 15: "Q4_K_M", 16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K", 30: "IQ4_XS", 32: "BF16"}

TYPE_MANIFESTE = "application/vnd.docker.distribution.manifest.v2+json"
TYPE_COUCHE_MODELE = "application/vnd.ollama.image.model"
# Hugging Face ne sert le format Ollama qu'à un client qui se présente comme Ollama ;
# le registre d'Ollama, lui, exige alors une requête signée : l'en-tête n'est envoyé qu'à HF
AGENT_HF = "ollama/0.12.0 (edgefit)"
LECTURE_INITIALE = 1024 * 1024
LECTURE_MAX = 32 * 1024 * 1024


class RegistreErreur(Exception):
    pass


def adresse(nom):
    """(url de base, dépôt, tag, en-têtes) pour un nom de modèle Ollama."""
    if ".." in nom:
        raise RegistreErreur("nom de modèle invalide")
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
            raise RegistreErreur("ce fichier n'est pas un modèle GGUF")
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
    def __init__(self, transport=None):
        self._http = httpx.AsyncClient(transport=transport, timeout=20, follow_redirects=True)

    async def _get(self, url, entetes):
        try:
            r = await self._http.get(url, headers=entetes)
        except httpx.TransportError as e:
            raise RegistreErreur("registre injoignable : la carte a-t-elle accès à internet ?") from e
        if r.status_code in (401, 403):
            raise RegistreErreur("modèle protégé ou introuvable sur le registre (licence à accepter sur Hugging Face ?)")
        if r.status_code == 404:
            raise RegistreErreur("modèle ou variante introuvable sur le registre")
        if r.status_code >= 400:
            raise RegistreErreur(f"le registre a répondu {r.status_code}")
        return r

    async def infos(self, nom):
        base, depot, tag, entetes = adresse(nom)
        manifeste = (await self._get(f"{base}/v2/{depot}/manifests/{tag}", {**entetes, "Accept": TYPE_MANIFESTE})).json()
        couche = next((c for c in manifeste.get("layers", []) if c.get("mediaType") == TYPE_COUCHE_MODELE), None)
        if couche is None:
            raise RegistreErreur("le manifeste ne contient pas de fichier modèle")
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
            raise RegistreErreur("architecture absente de l'en-tête du fichier GGUF")
        arch = architecture_depuis_model_info(meta)
        arch["nb_param"] = arch["nb_param"] or parametres_depuis_texte(
            config.get("model_type") or config.get("config", {}).get("parameters"))
        return {"taille_octets": couche["size"], "architecture": arch, "format": format_fichier(config, meta, tag)}


class RegistreFactice:
    """Pour --fake : deux modèles connus, sans réseau."""

    MODELES = {
        "gemma3:1b": {"taille_octets": 815_319_791, "format": "Q4_K_M", "architecture": {
            "architecture": "gemma3", "nb_param": 999_885_952, "num_layers": 26, "num_kv_heads": 1,
            "head_dim": 256, "context_max": 32768, "sliding_window": 512}},
        "llama3.1:8b": {"taille_octets": 4_920_738_944, "format": "Q4_K_M", "architecture": {
            "architecture": "llama", "nb_param": 8_030_261_248, "num_layers": 32, "num_kv_heads": 8,
            "head_dim": 128, "context_max": 131072, "sliding_window": None}},
    }

    async def infos(self, nom):
        if nom not in self.MODELES:
            raise RegistreErreur("modèle introuvable (mode --fake : essayez gemma3:1b ou llama3.1:8b)")
        return self.MODELES[nom]
