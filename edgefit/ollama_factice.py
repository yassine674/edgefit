# Ollama simulé (option --fake) : même interface que ClientOllama, pour développer
# l'interface sans Ollama ni Jetson. Les métriques passent par la même fonction
# metriques_inference que le vrai client, avec des durées plausibles pour une Jetson.
import asyncio
import time

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


class OllamaFactice:
    url = "simulé (--fake)"

    def __init__(self, pause_s=0.04):
        self._installes = list(MODELES_INITIAUX)
        self._pause = pause_s
        self._charge = None  # (nom, num_ctx) du dernier modèle utilisé

    async def version(self):
        return "simulé"

    async def modeles(self):
        return list(self._installes)

    async def architecture(self, nom):
        if nom not in ARCHITECTURES:
            raise ModeleInconnu(f"model '{nom}' not found")
        return ARCHITECTURES[nom]

    async def infos(self, nom):
        modele = next((m for m in self._installes if m["nom"] == nom), None)
        if modele is None:
            return None
        return {"taille_octets": int(modele["taille_go"] * 1024 ** 3),
                "architecture": ARCHITECTURES.get(nom, ARCHITECTURES["qwen2.5:0.5b"]),
                "format": modele["quantification"] or "Q4_K_M"}

    async def charges(self):
        if self._charge is None:
            return []
        nom, num_ctx = self._charge
        modele = next(m for m in self._installes if m["nom"] == nom)
        # comme un vrai Ollama : un peu plus que le fichier (cache KV, buffers)
        taille = int(modele["taille_go"] * 1024 ** 3 * 1.08 + num_ctx * 30_000)
        return [{"nom": nom, "taille": taille, "taille_vram": taille, "num_ctx": num_ctx}]

    async def telecharger(self, nom):
        total = 400_000_000
        yield {"type": "progression", "statut": "pulling manifest", "fait": None, "total": None}
        for fait in range(0, total + 1, total // 20):
            await asyncio.sleep(self._pause)
            yield {"type": "progression", "statut": "pulling model", "fait": fait, "total": total}
        if all(m["nom"] != nom for m in self._installes):
            self._installes.append({"nom": nom, "taille_go": total / 1024 ** 3, "famille": None,
                                    "parametres": None, "quantification": None})
        yield {"type": "fin"}

    async def discuter(self, nom, messages, options=None):
        if all(m["nom"] != nom for m in self._installes):
            yield {"type": "erreur", "erreur": f"model '{nom}' not found"}
            return
        self._charge = (nom, (options or {}).get("num_ctx", 4096))
        debut = time.perf_counter()
        question = messages[-1]["content"]
        reponse = (f"Réponse simulée (mode --fake) de {nom} à : « {question[:80]} ». "
                   "Les mesures affichées ci-dessous sont plausibles pour une Jetson, mais inventées.").split(" ")
        await asyncio.sleep(self._pause * 5)  # prefill
        premier = time.perf_counter()
        for i, mot in enumerate(reponse):
            yield {"type": "token", "texte": mot if i == 0 else " " + mot}
            await asyncio.sleep(self._pause)
        # durées annoncées fixes (~20 tokens/s), indépendantes du rythme d'affichage simulé
        tokens_prompt = 4 * len(question.split()) + 20
        fin = {
            "load_duration": 0.8 * NS,
            "prompt_eval_count": tokens_prompt,
            "prompt_eval_duration": tokens_prompt / 150 * NS,
            "eval_count": len(reponse),
            "eval_duration": len(reponse) * 0.05 * NS,
        }
        yield {"type": "fin", "metriques": metriques_inference(fin, debut, premier, time.perf_counter())}
