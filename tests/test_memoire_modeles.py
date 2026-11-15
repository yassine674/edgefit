# installé (sur le disque) ≠ chargé (en mémoire) : activer un modèle, n'en garder qu'un seul en mémoire
import json

import pytest
from fastapi.testclient import TestClient

from edgefit.catalogue import CatalogueFactice
from edgefit.metriques import FournisseurFactice
from edgefit.ollama_factice import OllamaFactice
from edgefit.registre import RegistreFactice
from edgefit.securite import NOM_COOKIE
from edgefit.serveur import creer_app


class OllamaPlusieurs(OllamaFactice):
    """Ollama simulé qui peut garder plusieurs modèles en mémoire, comme le vrai."""

    def __init__(self):
        super().__init__(pause_s=0)
        self.en_memoire = {}
        self.garde = {}

    async def charges(self):
        return [{"nom": n, "taille": 2 * 1024 ** 3, "taille_vram": 0, "num_ctx": c, "expire": None} for n, c in self.en_memoire.items()]

    async def charger(self, nom, num_ctx=None, garder=None):
        await super().charger(nom, num_ctx, garder)
        self.en_memoire[nom] = num_ctx
        self.garde[nom] = garder

    async def decharger(self, nom):
        self.en_memoire.pop(nom, None)

    async def discuter(self, nom, messages, options=None, outils=None, garder=None):
        self.en_memoire[nom] = (options or {}).get("num_ctx")
        self.garde[nom] = garder
        async for e in super().discuter(nom, messages, options, outils, garder):
            yield e


@pytest.fixture
def cas():
    ollama = OllamaPlusieurs()
    c = TestClient(creer_app(FournisseurFactice(), ollama, "j", RegistreFactice(), CatalogueFactice(), intervalle_s=0.01))
    c.cookies.set(NOM_COOKIE, "j")
    return c, ollama


def test_activer_libere_les_autres(cas):
    c, ollama = cas
    ollama.en_memoire = {"qwen2.5:0.5b": 4096}
    r = c.post("/api/modeles/activer", json={"nom": "llama3.2:3b", "num_ctx": 2048, "garder": "1m"})
    assert r.status_code == 200 and r.json()["liberes"] == ["qwen2.5:0.5b"]
    assert ollama.en_memoire == {"llama3.2:3b": 2048} and ollama.garde["llama3.2:3b"] == "1m"
    assert r.json()["taille_go"] == pytest.approx(2.0)


def test_activer_sans_un_seul_garde_les_autres(cas):
    c, ollama = cas
    ollama.en_memoire = {"qwen2.5:0.5b": 4096}
    r = c.post("/api/modeles/activer", json={"nom": "llama3.2:3b", "un_seul": False})
    assert r.json()["liberes"] == [] and set(ollama.en_memoire) == {"qwen2.5:0.5b", "llama3.2:3b"}


def test_activer_modele_inconnu_ou_duree_invalide(cas):
    c, _ = cas
    assert c.post("/api/modeles/activer", json={"nom": "absent:1b"}).status_code == 404
    assert c.post("/api/modeles/activer", json={"nom": "llama3.2:3b", "garder": "jamais"}).status_code == 422


def test_le_chat_libere_les_autres_et_le_dit(cas):
    c, ollama = cas
    ollama.en_memoire = {"qwen2.5:0.5b": 4096}
    r = c.post("/api/chat", json={"modele": "llama3.2:3b", "garder": "30m", "messages": [{"role": "user", "content": "bonjour"}]})
    evts = [json.loads(x) for x in r.text.splitlines() if x.strip()]
    assert evts[0] == {"type": "liberes", "noms": ["qwen2.5:0.5b"]}
    assert set(ollama.en_memoire) == {"llama3.2:3b"} and ollama.garde["llama3.2:3b"] == "30m"
    # deuxième question au même modèle : rien à libérer, pas d'événement
    r = c.post("/api/chat", json={"modele": "llama3.2:3b", "messages": [{"role": "user", "content": "encore"}]})
    assert all(json.loads(x)["type"] != "liberes" for x in r.text.splitlines() if x.strip())
    assert ollama.garde["llama3.2:3b"] == "5m"  # durée par défaut


def test_le_chat_sans_un_seul_ne_libere_rien(cas):
    c, ollama = cas
    ollama.en_memoire = {"qwen2.5:0.5b": 4096}
    c.post("/api/chat", json={"modele": "llama3.2:3b", "un_seul": False, "messages": [{"role": "user", "content": "x"}]})
    assert set(ollama.en_memoire) == {"qwen2.5:0.5b", "llama3.2:3b"}


def test_charger_envoie_contexte_et_duree():
    import asyncio
    import httpx
    from edgefit.ollama import ClientOllama
    recus = []

    def repondre(requete):
        recus.append(json.loads(requete.content))
        return httpx.Response(200, json={"done": True})

    asyncio.run(ClientOllama(transport=httpx.MockTransport(repondre)).charger("m:1b", 8192, "5m"))
    assert recus == [{"model": "m:1b", "stream": False, "options": {"num_ctx": 8192}, "keep_alive": "5m"}]
