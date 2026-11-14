# Ollama simulé (option --fake) : même interface que ClientOllama, pour développer
# l'interface sans Ollama ni Jetson. Les métriques passent par la même fonction
# metriques_inference que le vrai client, avec des durées plausibles pour une Jetson.
import asyncio
import json
import re
import time

from .metriques import factice as carte_simulee
from .ollama import ModeleInconnu, metriques_inference

NS = 1e9

MODELES_INITIAUX = [
    {"nom": "qwen2.5:0.5b", "taille_go": 0.37, "famille": "qwen2", "parametres": "494.03M", "quantification": "Q4_K_M"},
    {"nom": "llama3.2:3b", "taille_go": 1.88, "famille": "llama", "parametres": "3.2B", "quantification": "Q4_K_M"},
]

ARCHITECTURES = {
    "qwen2.5:0.5b": {"architecture": "qwen2", "nb_param": 494_032_768, "num_layers": 24, "num_kv_heads": 2,
                     "head_dim": 64, "context_max": 32768, "sliding_window": None},
    "llama3.2:3b": {"architecture": "llama", "nb_param": 3_212_749_824, "num_layers": 28, "num_kv_heads": 8,
                    "head_dim": 128, "context_max": 131072, "sliding_window": None},
}


def _architecture(nom):
    if nom in ARCHITECTURES:
        return ARCHITECTURES[nom]
    from .catalogue import SUGGESTIONS
    entree = next((e for e in SUGGESTIONS if e["nom"] == nom), None)
    return entree["architecture"] if entree else ARCHITECTURES["qwen2.5:0.5b"]


def _vision(nom):
    from .catalogue import SUGGESTIONS
    entree = next((e for e in SUGGESTIONS if e["nom"] == nom), None)
    return bool(entree and entree.get("vision")) or any(k in nom for k in ("llava", "moondream", "vl", "vision"))


def _appel_simule(question, installes):
    """Outil qu'un vrai modèle demanderait probablement pour cette question (mots-clés grossiers)."""
    q = question.lower()
    calcul = re.search(r"[\d.()]+(?:\s*(?:[-+*/^]|\*\*)\s*[\d.()]+)+", question)
    if calcul:
        return "calculator", {"expression": calcul.group(0).strip()}
    if any(m in q for m in ("tient", "fit", "rentre", "passe")):
        nom = next((m for m in re.findall(r"[a-z0-9.\-]+:[a-z0-9.\-]+", q)), installes[-1]["nom"])
        return "check_model_fit", {"model": nom, "context": "8192" if "8k" in q or "8192" in q else 4096}
    if any(m in q for m in ("température", "temperature", "mémoire", "memoire", "memory", "carte", "board", "gpu")):
        return "board_status", {}
    if any(m in q for m in ("installé", "installe", "installed", "modèles", "models")):
        return "installed_models", {}
    if any(m in q for m in ("heure", "date", "time", "jour")):
        return "current_time", {}
    return None


class OllamaFactice:
    url = "simulé (--fake)"

    def __init__(self, pause_s=0.04, sans_outils=()):
        self._sans_outils = set(sans_outils)  # modèles simulés qui ne savent pas appeler d'outils
        self._installes = list(MODELES_INITIAUX)
        self._pause = pause_s
        self._charge = None  # (nom, num_ctx) du dernier modèle utilisé
        self._bases = {}     # modèle créé dans l'atelier -> modèle de base (même architecture)
        self.crees = {}      # modèle créé -> ce qu'Ollama aurait reçu (pour les tests)

    async def version(self):
        return "simulé"

    async def modeles(self):
        return [{"modifie": "2026-11-10T09:30:00Z", **m} for m in self._installes]

    async def architecture(self, nom):
        if nom not in ARCHITECTURES:
            raise ModeleInconnu(f"model '{nom}' not found")
        return ARCHITECTURES[nom]

    async def infos(self, nom):
        modele = next((m for m in self._installes if m["nom"] == nom), None)
        if modele is None:
            return None
        return {"taille_octets": int(modele["taille_go"] * 1024 ** 3),
                "architecture": _architecture(self._bases.get(nom, nom)),
                "format": modele["quantification"] or "Q4_K_M",
                "capacites": ["completion"] + ([] if nom in self._sans_outils else ["tools"])
                + (["vision"] if _vision(nom) else [])}

    async def charges(self):
        if self._charge is None:
            return []
        nom, num_ctx = self._charge
        modele = next(m for m in self._installes if m["nom"] == nom)
        # comme un vrai Ollama : un peu plus que le fichier (cache KV, buffers)
        taille = int(modele["taille_go"] * 1024 ** 3 * 1.08 + num_ctx * 30_000)
        return [{"nom": nom, "taille": taille, "taille_vram": taille, "num_ctx": num_ctx}]

    async def decharger(self, nom):
        if self._charge and self._charge[0] == nom:
            self._charge = None

    async def supprimer(self, nom):
        if all(m["nom"] != nom for m in self._installes):
            raise ModeleInconnu(f"model '{nom}' not found")
        self._installes = [m for m in self._installes if m["nom"] != nom]
        await self.decharger(nom)

    async def telecharger(self, nom):
        total = 400_000_000
        yield {"type": "progression", "statut": "pulling manifest", "fait": None, "total": None}
        for fait in range(0, total + 1, total // 20):
            await asyncio.sleep(self._pause)
            yield {"type": "progression", "statut": "pulling model", "fait": fait, "total": total}
        if all(m["nom"] != nom for m in self._installes):
            from .catalogue import SUGGESTIONS
            from .registre import RegistreFactice
            # « modele:3b-instruct-q8_0 » : une autre quantification d'un modèle du catalogue
            base, _, quant = nom.partition("-instruct-")
            entree = next((e for e in SUGGESTIONS if e["nom"] == base), None)
            bits = RegistreFactice.VARIANTES.get(quant.upper())
            taille = (entree["taille_octets"] + entree["projecteur_octets"]) if entree else total
            if entree and bits:
                taille = int(entree["taille_octets"] * bits / 4.85)
                self._bases[nom] = base
            self._installes.append({"nom": nom, "taille_go": taille / 1024 ** 3, "famille": entree and entree["famille"],
                                    "parametres": entree and entree["parametres"],
                                    "quantification": quant.upper() if bits else entree and entree["format"]})
        yield {"type": "fin"}

    async def creer(self, nom, base, systeme="", parametres=None, quantifier=None):
        source = next((m for m in self._installes if m["nom"] == base), None)
        if source is None:
            yield {"type": "erreur", "erreur": f"model '{base}' not found"}
            return
        if quantifier and (source["quantification"] or "").upper() not in ("F16", "F32"):
            yield {"type": "erreur", "erreur": "quantization is only supported for F16 and F32 models"}
            return
        for statut in ("reading model metadata", "creating system layer", "creating parameters layer", "writing manifest"):
            await asyncio.sleep(self._pause * 3)
            yield {"type": "progression", "statut": statut, "fait": None, "total": None}
        self._installes = [m for m in self._installes if m["nom"] != nom]
        self._installes.append({**source, "nom": nom, "quantification": quantifier.upper() if quantifier else source["quantification"]})
        self._bases[nom] = self._bases.get(base, base)
        self.crees[nom] = {"base": base, "systeme": systeme, "parametres": parametres or {}, "quantifier": quantifier}
        yield {"type": "fin"}

    async def discuter(self, nom, messages, options=None, outils=None, garder=None):
        if all(m["nom"] != nom for m in self._installes):
            yield {"type": "erreur", "erreur": f"model '{nom}' not found"}
            return
        if outils and nom in self._sans_outils:
            yield {"type": "erreur", "erreur": f"registry.ollama.ai/library/{nom} does not support tools"}
            return
        # outils proposés et question qui s'y prête : le modèle simulé demande un appel au lieu de répondre
        autorises = {o["function"]["name"] for o in outils or []}
        appel = _appel_simule(messages[-1]["content"], self._installes) if messages[-1]["role"] == "user" else None
        if appel and appel[0] in autorises:
            debut = time.perf_counter()
            await asyncio.sleep(self._pause * 5)
            yield {"type": "appels", "appels": [{"nom": appel[0], "arguments": appel[1]}]}
            fin = {"load_duration": 0.01 * NS, "prompt_eval_count": 60, "prompt_eval_duration": 0.2 * NS,
                   "eval_count": 12, "eval_duration": 0.3 * NS}
            yield {"type": "fin", "metriques": metriques_inference(fin, debut, None, time.perf_counter())}
            return
        froid = self._charge is None or self._charge[0] != nom
        self._charge = (nom, (options or {}).get("num_ctx", 4096))
        taille_go = next(m["taille_go"] for m in self._installes if m["nom"] == nom)
        # vitesse simulée plausible : un modèle deux fois plus gros génère deux fois moins vite
        # et la carte simulée ralentit quand elle chauffe (bridage thermique)
        debit = min(80.0, 25.0 / max(taille_go, 0.25)) * carte_simulee.bridage()
        debut = time.perf_counter()
        question = messages[-1]["content"]
        if messages[-1]["role"] == "tool":
            # réponse après un outil : le modèle simulé reprend le résultat
            resultat = json.loads(messages[-1]["content"])
            reponse = (f"D'après l'outil {messages[-1].get('tool_name', '')} : "
                       + " ; ".join(f"{k} = {v}" for k, v in resultat.items() if not isinstance(v, (list, dict)))
                       + ". (Réponse simulée, mode --fake.)").split(" ")
        elif messages[-1].get("images"):
            n = len(messages[-1]["images"])
            reponse = (f"Je vois {n} image{'s' if n > 1 else ''} (réponse simulée de {nom}, mode --fake : "
                       "un vrai modèle de vision décrirait ici son contenu).").split(" ")
        elif re.search(r"exemple|example|code|markdown", question, re.I):
            # réponse mise en forme, comme celles des vrais modèles (titres, listes, tableau, code)
            reponse = (f"## Réponse simulée de {nom}\n\nLe **cache KV** garde, pour chaque token déjà lu, "
                       "ses clés et ses valeurs : le modèle ne les recalcule pas.\n\n"
                       "- par token : `2 × couches × têtes KV × dim tête × octets`\n"
                       "- croît *linéairement* avec le contexte\n  - 4K tokens : 0,44 Go\n  - 32K tokens : 3,5 Go\n\n"
                       "| Contexte | Cache KV |\n|---|---:|\n| 4 096 | 0,44 Go |\n| 32 768 | 3,50 Go |\n\n"
                       "```python\ndef cache_kv(couches, tetes_kv, dim, contexte, octets=2):\n"
                       "    return 2 * couches * tetes_kv * dim * contexte * octets\n```\n\n"
                       "> Mesures inventées (mode --fake). Source : [Ollama](https://ollama.com).").split(" ")
        else:
            reponse = (f"Réponse simulée (mode --fake) de {nom} à : « {question[:80]} ». "
                       "Les mesures affichées ci-dessous sont plausibles pour une Jetson, mais inventées.").split(" ")
        # num_predict (bancs, endurance) : le modèle simulé écrit jusqu'à la limite demandée
        voulu = min((options or {}).get("num_predict") or 0, 2000)
        while len(reponse) < voulu:
            reponse += reponse[: voulu - len(reponse)]
        if froid and self._pause:
            await asyncio.sleep(0.8 + taille_go)  # chargement simulé, compris dans le premier mot comme en vrai
        carte_simulee.ACTIVITE["generation"] += 1
        try:
            await asyncio.sleep(self._pause * 5)  # prefill
            premier = time.perf_counter()
            for i, mot in enumerate(reponse):
                yield {"type": "token", "texte": mot if i == 0 else " " + mot}
                await asyncio.sleep(self._pause / carte_simulee.bridage())
        finally:
            carte_simulee.ACTIVITE["generation"] -= 1
        tokens_prompt = 4 * len(question.split()) + 20
        fin = {
            "load_duration": (0.8 + taille_go) * NS if froid else 0.01 * NS,
            "prompt_eval_count": tokens_prompt,
            "prompt_eval_duration": tokens_prompt / (6 * debit) * NS,
            "eval_count": len(reponse),
            "eval_duration": len(reponse) / debit * NS,
        }
        yield {"type": "fin", "metriques": metriques_inference(fin, debut, premier, time.perf_counter())}
