# client de l'API locale d'Ollama (http://127.0.0.1:11434 par défaut).
# Pour le téléchargement et le chat, Ollama répond en NDJSON : une ligne JSON par
# événement (progression, morceau de réponse...). Ce module traduit ces réponses en
# événements simples pour l'interface : {"type": "progression" | "token" | "fin" | "erreur", ...}
import json
import re
import time

import httpx

GO = 1024 ** 3
NS = 1e9  # Ollama donne toutes ses durées en nanosecondes

# noms Ollama : "qwen2.5:0.5b", "hf.co/bartowski/Llama-3.2-1B-Instruct-GGUF:Q4_K_M"...
MOTIF_NOM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/:]{0,199}$")


class OllamaIndisponible(Exception):
    pass


class ModeleInconnu(Exception):
    pass


def nom_valide(nom):
    return isinstance(nom, str) and bool(MOTIF_NOM.match(nom))


def expliquer_erreur(message):
    # modèle protégé sur Hugging Face : Ollama ne s'authentifie pas avec un token HF
    # mais avec sa propre clé SSH, à ajouter une fois au compte Hugging Face
    if "401" in message or "unauthorized" in message.lower():
        return (f"{message} — modèle protégé : ajoutez la clé publique de votre Ollama "
                "(~/.ollama/id_ed25519.pub) dans les clés SSH de votre compte Hugging Face, "
                "après avoir accepté la licence du modèle.")
    return message


def _premier(valeur):
    # certains modèles donnent une valeur par couche (liste) au lieu d'une valeur unique
    return max(valeur) if isinstance(valeur, list) else valeur


def architecture_depuis_model_info(info):
    # /api/show renvoie les métadonnées GGUF, préfixées par le nom de l'architecture :
    # "llama.block_count", "qwen2.attention.head_count_kv"... Les clés produites sont
    # celles des specs de llm_modelfit, pour pouvoir réutiliser ses formules.
    arch = info.get("general.architecture")

    def champ(nom):
        return _premier(info.get(f"{arch}.{nom}"))

    nb_tetes = champ("attention.head_count")
    dim = champ("embedding_length")
    return {
        "architecture": arch,
        "nb_param": info.get("general.parameter_count"),
        "num_layers": champ("block_count"),
        "num_kv_heads": champ("attention.head_count_kv") or nb_tetes,
        "head_dim": champ("attention.key_length") or (dim // nb_tetes if dim and nb_tetes else None),
        "context_max": champ("context_length"),
        "sliding_window": champ("attention.sliding_window"),
    }


def metriques_inference(fin, debut, premier_token, arrivee_fin):
    # `fin` : dernier message d'Ollama (done=true), qui contient ses propres mesures.
    # Le TTFT mesuré ici est celui que ressent l'utilisateur : il inclut le chargement
    # du modèle en mémoire quand celui-ci n'était pas déjà chargé ("démarrage à froid"),
    # d'où le temps de chargement donné à part pour ne pas confondre les deux.
    prefill_s = fin.get("prompt_eval_duration", 0) / NS
    decodage_s = fin.get("eval_duration", 0) / NS
    tokens_prompt = fin.get("prompt_eval_count", 0)
    tokens_generes = fin.get("eval_count", 0)
    return {
        "ttft_mesure_s": premier_token - debut if premier_token is not None else None,
        "chargement_s": fin.get("load_duration", 0) / NS,
        "prefill_s": prefill_s,
        "tokens_prompt": tokens_prompt,
        "tokens_generes": tokens_generes,
        "debit_prefill_tokens_s": tokens_prompt / prefill_s if prefill_s else None,
        "debit_tokens_s": tokens_generes / decodage_s if decodage_s else None,
        "duree_totale_s": arrivee_fin - debut,
    }


class ClientOllama:
    def __init__(self, url="http://127.0.0.1:11434", transport=None):
        self.url = url.rstrip("/")
        # read=None : pendant un chat, le premier morceau peut mettre longtemps à
        # arriver (chargement du modèle sur une Jetson) sans que ce soit une panne
        self._http = httpx.AsyncClient(
            base_url=self.url, transport=transport, timeout=httpx.Timeout(10, read=None),
        )

    async def _requete(self, methode, chemin, timeout=30, **kwargs):
        try:
            r = await self._http.request(methode, chemin, timeout=timeout, **kwargs)
        except httpx.TransportError as e:
            raise OllamaIndisponible(self.url) from e
        if r.status_code == 404:
            raise ModeleInconnu(r.json().get("error", "modèle introuvable"))
        r.raise_for_status()
        return r.json() if r.content else {}

    async def version(self):
        return (await self._requete("GET", "/api/version"))["version"]

    async def modeles(self):
        donnees = await self._requete("GET", "/api/tags")
        return [
            {
                "nom": m["name"],
                "taille_go": m["size"] / GO,
                "famille": m.get("details", {}).get("family"),
                "parametres": m.get("details", {}).get("parameter_size"),
                "quantification": m.get("details", {}).get("quantization_level"),
                "modifie": m.get("modified_at"),
                "empreinte": m.get("digest"),
            }
            for m in donnees.get("models", [])
        ]

    async def architecture(self, nom):
        donnees = await self._requete("POST", "/api/show", json={"model": nom})
        return architecture_depuis_model_info(donnees.get("model_info", {}))

    async def infos(self, nom):
        """Modèle installé : taille exacte du fichier et architecture, mêmes clés que Registre.infos."""
        tags = await self._requete("GET", "/api/tags")
        modele = next((m for m in tags.get("models", []) if m["name"] == nom or m.get("model") == nom), None)
        if modele is None:
            return None
        show = await self._requete("POST", "/api/show", json={"model": nom})
        return {"taille_octets": modele["size"], "architecture": architecture_depuis_model_info(show.get("model_info", {})),
                "format": (modele.get("details", {}).get("quantization_level") or "").upper() or None,
                # ["completion", "tools", "vision"...] ; absent sur les Ollama anciens (on ne sait pas)
                "capacites": show.get("capabilities")}

    async def charges(self):
        # modèles chargés en ce moment : `size` est la mémoire réellement occupée
        # (poids + cache KV + buffers), `size_vram` la part sur le GPU
        donnees = await self._requete("GET", "/api/ps")
        return [{"nom": m["name"], "taille": m.get("size", 0), "taille_vram": m.get("size_vram", 0),
                 "num_ctx": m.get("context_length"), "expire": m.get("expires_at")}
                for m in donnees.get("models", [])]

    async def decharger(self, nom):
        # keep_alive 0 : Ollama libère la mémoire du modèle tout de suite
        await self._requete("POST", "/api/generate", json={"model": nom, "keep_alive": 0, "stream": False})

    async def charger(self, nom, num_ctx=None, garder=None):
        # requête sans texte : Ollama lit le fichier du disque et le met en mémoire, sans rien générer.
        # Lire plusieurs Go depuis une carte SD peut prendre plus d'une minute.
        corps = {"model": nom, "stream": False}
        if num_ctx:
            corps["options"] = {"num_ctx": num_ctx}
        if garder:
            corps["keep_alive"] = garder
        await self._requete("POST", "/api/generate", timeout=600, json=corps)

    async def supprimer(self, nom):
        await self._requete("DELETE", "/api/delete", json={"model": nom})

    async def _flux(self, chemin, corps):
        try:
            async with self._http.stream("POST", chemin, json=corps) as r:
                if r.status_code >= 400:
                    texte = (await r.aread()).decode(errors="ignore")
                    try:
                        texte = json.loads(texte).get("error", texte)
                    except ValueError:
                        pass
                    yield {"error": texte}
                    return
                async for ligne in r.aiter_lines():
                    if ligne.strip():
                        yield json.loads(ligne)
        except httpx.TransportError as e:
            raise OllamaIndisponible(self.url) from e

    async def telecharger(self, nom):
        async for evt in self._flux("/api/pull", {"model": nom, "stream": True}):
            if "error" in evt:
                yield {"type": "erreur", "erreur": expliquer_erreur(evt["error"])}
                return
            if evt.get("status") == "success":
                yield {"type": "fin"}
                return
            yield {"type": "progression", "statut": evt.get("status"),
                   "fait": evt.get("completed"), "total": evt.get("total")}

    async def creer(self, nom, base, systeme="", parametres=None, quantifier=None):
        """Fabrique un modèle à partir d'un autre (API /api/create) : mêmes poids, plus des instructions
        et des paramètres gardés dans le modèle. `quantifier` (q4_K_M, q8_0...) recompresse les poids ;
        Ollama ne l'accepte que si la base est en F16 ou F32."""
        corps = {"model": nom, "from": base, "stream": True}
        if systeme.strip():
            corps["system"] = systeme
        if parametres:
            corps["parameters"] = parametres
        if quantifier:
            corps["quantize"] = quantifier
        async for evt in self._flux("/api/create", corps):
            if "error" in evt:
                yield {"type": "erreur", "erreur": expliquer_erreur(evt["error"])}
                return
            if evt.get("status") == "success":
                yield {"type": "fin"}
                return
            yield {"type": "progression", "statut": evt.get("status"), "fait": evt.get("completed"), "total": evt.get("total")}

    async def discuter(self, nom, messages, options=None, outils=None, garder=None):
        debut = time.perf_counter()
        premier = None
        corps = {"model": nom, "messages": messages, "stream": True, "options": options or {}}
        if garder:
            corps["keep_alive"] = garder  # durée pendant laquelle Ollama garde le modèle chargé après la réponse
        if outils:
            corps["tools"] = outils
        async for evt in self._flux("/api/chat", corps):
            if "error" in evt:
                yield {"type": "erreur", "erreur": expliquer_erreur(evt["error"])}
                return
            # le modèle demande d'exécuter un ou plusieurs outils (avant la fin du flux)
            appels = evt.get("message", {}).get("tool_calls")
            if appels:
                yield {"type": "appels", "appels": [{"nom": a.get("function", {}).get("name", ""),
                                                     "arguments": a.get("function", {}).get("arguments") or {}}
                                                    for a in appels]}
            if evt.get("done"):
                yield {"type": "fin", "metriques": metriques_inference(evt, debut, premier, time.perf_counter())}
                return
            texte = evt.get("message", {}).get("content", "")
            if texte and premier is None:
                premier = time.perf_counter()
            yield {"type": "token", "texte": texte}
