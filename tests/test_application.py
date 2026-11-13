import json

import pytest
from fastapi.testclient import TestClient

from edgefit import banc, stockage
from edgefit.catalogue import SUGGESTIONS, CatalogueFactice
from edgefit.metriques import FournisseurFactice
from edgefit.ollama_factice import OllamaFactice
from edgefit.registre import RegistreFactice
from edgefit.securite import NOM_COOKIE
from edgefit.serveur import creer_app


@pytest.fixture
def client():
    c = TestClient(creer_app(FournisseurFactice(), OllamaFactice(pause_s=0), "j", RegistreFactice(),
                             CatalogueFactice(), intervalle_s=0.01))
    c.cookies.set(NOM_COOKIE, "j")
    return c


def lignes(r):
    return [json.loads(l) for l in r.text.splitlines() if l.strip()]


# --- catalogue -------------------------------------------------------------------------

def test_catalogue_livre_avec_architecture_et_usages():
    for e in SUGGESTIONS:
        a = e["architecture"]
        assert a["num_layers"] and a["num_kv_heads"] and a["head_dim"] and a["context_max"], e["nom"]
        assert e["tags"], e["nom"]


def test_catalogue_un_verdict_par_modele_en_une_requete(client):
    d = client.get("/api/catalogue", params={"num_ctx": 4096}).json()
    assert len(d["modeles"]) == len(SUGGESTIONS)
    petit = next(m for m in d["modeles"] if m["nom"] == "smollm2:135m")
    gros = next(m for m in d["modeles"] if m["nom"] == "qwen3:8b")
    assert petit["verdict"] == "ok" and petit["reste_go"] > 0
    assert gros["verdict"] == "trop_gros" and gros["reste_go"] < 0
    assert next(m for m in d["modeles"] if m["nom"] == "llama3.2:3b")["installe"]
    assert petit["plafond_tokens_s"] > gros["plafond_tokens_s"]  # Jetson simulée : bande passante connue


def test_catalogue_contexte_plus_grand_plus_de_memoire(client):
    a = client.get("/api/catalogue", params={"num_ctx": 2048}).json()
    b = client.get("/api/catalogue", params={"num_ctx": 32768}).json()
    total = lambda d: next(m for m in d["modeles"] if m["nom"] == "llama3.2:3b")["total_go"]
    assert total(b) > total(a)


# --- modèles installés -----------------------------------------------------------------

def test_decharger_et_supprimer(client):
    client.post("/api/chat", json={"modele": "qwen2.5:0.5b", "messages": [{"role": "user", "content": "x"}]})
    assert [m["nom"] for m in client.get("/api/charges").json()] == ["qwen2.5:0.5b"]
    assert client.post("/api/modeles/decharger", json={"nom": "qwen2.5:0.5b"}).status_code == 200
    assert client.get("/api/charges").json() == []
    assert client.post("/api/modeles/supprimer", json={"nom": "qwen2.5:0.5b"}).status_code == 200
    assert all(m["nom"] != "qwen2.5:0.5b" for m in client.get("/api/modeles").json())
    assert client.post("/api/modeles/supprimer", json={"nom": "qwen2.5:0.5b"}).status_code == 404


# --- conversations ------------------------------------------------------------------------

def test_conversation_enregistree_cote_serveur(client):
    conv = client.post("/api/conversations", json={"modele": "qwen2.5:0.5b", "systeme": "Réponds en français."}).json()
    r = client.post("/api/chat", json={"modele": "qwen2.5:0.5b", "conversation_id": conv["id"], "temperature": 0.2,
                                       "systeme": conv["systeme"], "messages": [{"role": "user", "content": "Bonjour à toi"}]})
    assert lignes(r)[-1]["type"] == "fin"
    lue = client.get(f"/api/conversations/{conv['id']}").json()
    assert lue["titre"] == "Bonjour à toi"  # titre automatique : début du premier message
    assert [m["role"] for m in lue["messages"]] == ["user", "assistant"]
    assert lue["messages"][1]["mesures"]["debit_tokens_s"] > 0
    assert conv["id"] in [c["id"] for c in client.get("/api/conversations").json()]
    renommee = client.patch(f"/api/conversations/{conv['id']}", json={"titre": "Essai"}).json()
    assert renommee["titre"] == "Essai"
    assert client.delete(f"/api/conversations/{conv['id']}").json()["ok"]
    assert client.get(f"/api/conversations/{conv['id']}").status_code == 404


def test_chat_conversation_inconnue(client):
    r = client.post("/api/chat", json={"modele": "qwen2.5:0.5b", "conversation_id": 99999,
                                       "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 404


def test_stockage_direct():
    conv = stockage.creer_conversation("m:1b", num_ctx=2048)
    stockage.ajouter_message(conv["id"], "user", "  une   question  ")
    assert stockage.lire_conversation(conv["id"])["titre"] == "une question"
    assert stockage.modifier_conversation(conv["id"], inconnu="x", num_ctx=8192)["num_ctx"] == 8192


# --- banc d'essai et score -------------------------------------------------------------

def test_banc_complet_puis_score_dans_le_catalogue(client):
    evenements = lignes(client.post("/api/banc", json={"nom": "qwen2.5:0.5b"}))
    phases = [e.get("phase") for e in evenements if e["type"] == "phase"]
    assert phases == ["dechargement", "froid", "chaud", "chaud"]
    fin = evenements[-1]
    assert fin["type"] == "fin" and 0 <= fin["score"]["score"] <= 100
    assert fin["mesures"]["chargement_s"] > fin["mesures"]["ttft_s"] * 0  # le froid mesure un chargement
    assert fin["mesures"]["puissance_w"] and fin["mesures"]["tokens_par_joule"]
    liste = client.get("/api/bancs").json()
    assert liste[0]["modele"] == "qwen2.5:0.5b" and liste[0]["score"]["score"] == fin["score"]["score"]
    carte = next(m for m in client.get("/api/catalogue").json()["modeles"] if m["nom"] == "qwen2.5:0.5b")
    assert carte["score"]["score"] == fin["score"]["score"]


def test_banc_modele_absent(client):
    fin = lignes(client.post("/api/banc", json={"nom": "absent:1b"}))[-1]
    assert fin["type"] == "erreur"


def test_formule_du_score():
    assert banc.score({"debit_tokens_s": 20, "ttft_s": 0.5})["score"] == 100
    assert banc.score({"debit_tokens_s": 2, "ttft_s": 10})["score"] == 0
    milieu = banc.score({"debit_tokens_s": 6.32, "ttft_s": 2.24})  # milieux géométriques des deux échelles
    assert 45 <= milieu["score"] <= 55
    assert banc.score({"debit_tokens_s": 50, "ttft_s": 0.2, "marge_libre": 0.05}) == {
        "score": 60, "vitesse": 100, "reactivite": 100, "plafonne": True}
    assert banc.score({"debit_tokens_s": None, "ttft_s": 1}) is None


def test_textes_du_banc_differents_et_de_meme_longueur():
    t = banc.textes()
    assert len(set(t)) == 3 and max(map(len, t)) - min(map(len, t)) < 0.1 * len(t[0])


def test_diagnostic_export_et_effacement(client):
    d = client.get("/api/diagnostic").json()
    assert {"carte", "disque_libre_go", "ollama_version", "base"} <= d["faits"].keys()
    assert all({"niveau", "code", "params"} <= v.keys() for v in d["verifications"])
    assert client.get("/api/ollama").json()["disque"]["libre_go"] > 0
    conv = client.post("/api/conversations", json={"modele": "qwen2.5:0.5b"}).json()
    client.post("/api/chat", json={"modele": "qwen2.5:0.5b", "conversation_id": conv["id"],
                                   "messages": [{"role": "user", "content": "x"}]})
    export = client.get("/api/export").json()
    assert any(c["id"] == conv["id"] and len(c["messages"]) == 2 for c in export["conversations"])
    assert client.delete("/api/conversations").json()["effaces"] >= 1
    assert client.get("/api/conversations").json() == []
    assert client.delete("/api/bancs").status_code == 200
    assert client.get("/api/modeles").json()[0]["modifie"]
