import asyncio
import hashlib
import io
import json
import wave

import httpx
import pytest
from fastapi.testclient import TestClient

from edgefit import voix as module_voix
from edgefit.catalogue import CatalogueFactice
from edgefit.metriques import FournisseurFactice
from edgefit.ollama_factice import OllamaFactice
from edgefit.registre import RegistreFactice
from edgefit.securite import NOM_COOKIE
from edgefit.serveur import creer_app
from edgefit.voix import Voix, VoixFactice, nettoyer


def client_avec(v):
    c = TestClient(creer_app(FournisseurFactice(), OllamaFactice(pause_s=0), "j", RegistreFactice(), CatalogueFactice(),
                             intervalle_s=0.01, voix=v))
    c.cookies.set(NOM_COOKIE, "j")
    return c


def lignes(r):
    return [json.loads(l) for l in r.text.splitlines() if l.strip()]


def test_nettoyer_ce_qu_une_voix_doit_lire():
    t = "## Résultat\n- **Le cache KV** grossit.\n```python\nprint(1)\n```\nVoir https://ollama.com et `num_ctx`."
    assert nettoyer(t) == "Résultat Le cache KV grossit. Voir et num ctx."  # « _ » se lit mal
    assert nettoyer("[la doc](https://x.y) ici") == "la doc ici"
    assert nettoyer("```\ncode non fermé") == ""


def test_voix_telechargee_puis_lue():
    v = VoixFactice()
    c = client_avec(v)
    assert c.get("/api/voix").json()["voix"]["fr"]["installee"] is False
    r = c.post("/api/voix/lire", json={"texte": "Bonjour.", "langue": "fr"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "voix_absente"
    evts = lignes(c.post("/api/voix/telecharger", json={"langue": "fr"}))
    assert evts[-1]["type"] == "fin" and evts[-2]["fait"] == evts[-2]["total"]
    assert c.get("/api/voix").json()["voix"]["fr"]["installee"] is True
    r = c.post("/api/voix/lire", json={"texte": "**Bonjour** à toi.", "langue": "fr"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(r.content)) as w:
        assert w.getnframes() > 0
    assert v.lus == ["Bonjour à toi."]


def test_lecture_validee():
    c = client_avec(VoixFactice())
    assert c.post("/api/voix/lire", json={"texte": "x" * 1001, "langue": "fr"}).status_code == 422
    assert c.post("/api/voix/lire", json={"texte": "x", "langue": "de"}).status_code == 422
    assert c.post("/api/voix/telecharger", json={"langue": "../../etc"}).status_code == 422


def test_sans_piper():
    r = client_avec(VoixFactice(moteur=False)).post("/api/voix/lire", json={"texte": "x", "langue": "fr"})
    assert r.status_code == 501 and r.json()["detail"]["code"] == "moteur_absent"


# --- vrai téléchargement, avec un faux dépôt : vérification des empreintes -------------------

def faux_depot(contenus):
    def repondre(requete):
        nom = requete.url.path.rsplit("/", 1)[1]
        return httpx.Response(200, content=contenus[nom]) if nom in contenus else httpx.Response(404)
    return httpx.MockTransport(repondre)


def remplir(v, monkeypatch, alterer=False):
    contenus = {}
    fichiers = {}
    for ext in (".onnx", ".onnx.json"):
        donnees = f"faux {ext}".encode()
        contenus[f"fr_FR-siwis-medium{ext}"] = donnees + (b"!" if alterer and ext == ".onnx" else b"")
        fichiers[ext] = (len(donnees), hashlib.sha256(donnees).hexdigest())
    monkeypatch.setitem(module_voix.VOIX, "fr", {**module_voix.VOIX["fr"], "fichiers": fichiers})
    return faux_depot(contenus)


async def tout(gen):
    return [e async for e in gen]


def test_telechargement_verifie(tmp_path, monkeypatch):
    v = Voix(tmp_path)
    v._transport = remplir(v, monkeypatch)
    evts = asyncio.run(tout(v.telecharger("fr")))
    assert evts[-1]["type"] == "fin" and v.installee("fr")
    assert not list(tmp_path.glob("*.part"))


def test_fichier_altere_refuse_et_supprime(tmp_path, monkeypatch):
    v = Voix(tmp_path)
    v._transport = remplir(v, monkeypatch, alterer=True)
    evts = asyncio.run(tout(v.telecharger("fr")))
    assert evts[-1]["type"] == "erreur" and evts[-1]["code"] == "empreinte"
    assert not v.installee("fr") and not list(tmp_path.glob("*.onnx*.part")) and not (tmp_path / "fr_FR-siwis-medium.onnx").exists()


def test_depot_injoignable(tmp_path):
    def panne(requete):
        raise httpx.ConnectError("hors ligne")
    v = Voix(tmp_path, transport=httpx.MockTransport(panne))
    assert asyncio.run(tout(v.telecharger("fr")))[-1]["code"] == "hors_ligne"
