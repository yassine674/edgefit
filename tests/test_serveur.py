import json

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from edgefit.metriques import FournisseurFactice
from edgefit.ollama import ClientOllama
from edgefit.ollama_factice import OllamaFactice
from edgefit.registre import RegistreFactice
from edgefit.securite import NOM_COOKIE, origine_autorisee
from edgefit.serveur import creer_app

JETON = "jeton-de-test"
COOKIE = f"{NOM_COOKIE}={JETON}"


@pytest.fixture
def client():
    return TestClient(creer_app(FournisseurFactice(), OllamaFactice(pause_s=0), JETON, RegistreFactice(),
                                intervalle_s=0.01))


@pytest.fixture
def connecte(client):
    client.cookies.set(NOM_COOKIE, JETON)
    return client


def lignes(reponse):
    return [json.loads(l) for l in reponse.text.splitlines() if l.strip()]


def test_sans_jeton_tout_est_refuse(client):
    assert client.get("/").status_code == 401
    assert client.get("/api/metriques").status_code == 401
    assert client.get("/static/app.js").status_code == 401


def test_mauvais_jeton_refuse(client):
    assert client.get("/?token=faux", follow_redirects=False).status_code == 403


def test_bon_jeton_pose_le_cookie_et_disparait_de_l_url(client):
    r = client.get(f"/?token={JETON}", follow_redirects=False)
    assert r.status_code == 303
    assert "token" not in r.headers["location"]
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


def test_avec_le_cookie_page_api_et_fichiers_accessibles(client):
    client.cookies.set(NOM_COOKIE, JETON)
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    metriques = client.get("/api/metriques").json()
    assert {"cpu_pct", "ram_utilisee_go", "ram_totale_go", "temp_cpu_c", "alertes"} <= metriques.keys()


def test_websocket_meme_origine_recoit_les_metriques(client):
    with client.websocket_connect("/ws/metriques", headers={"origin": "http://testserver", "cookie": COOKIE}) as ws:
        assert "cpu_pct" in ws.receive_json()
        assert "cpu_pct" in ws.receive_json()


@pytest.mark.parametrize("entetes", [
    {"origin": "http://site-malveillant.example", "cookie": COOKIE},  # autre site
    {"origin": "http://testserver"},                                   # pas de cookie
])
def test_websocket_refuse(client, entetes):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/metriques", headers=entetes) as ws:
            ws.receive_json()


def test_api_ollama_protegee_par_le_cookie(client):
    assert client.get("/api/modeles").status_code == 401
    assert client.post("/api/chat", json={"modele": "qwen2.5:0.5b", "messages": []}).status_code == 401


def test_modeles_et_architecture(connecte):
    noms = [m["nom"] for m in connecte.get("/api/modeles").json()]
    assert "qwen2.5:0.5b" in noms
    a = connecte.get("/api/modeles/architecture", params={"nom": "qwen2.5:0.5b"}).json()
    assert a["num_kv_heads"] == 2
    assert connecte.get("/api/modeles/architecture", params={"nom": "absent:1b"}).status_code == 404
    assert connecte.get("/api/modeles/architecture", params={"nom": "../x"}).status_code == 400


def test_telechargement_ajoute_le_modele(connecte):
    evts = lignes(connecte.post("/api/modeles/telecharger", json={"nom": "gemma2:2b"}))
    assert evts[0]["type"] == "progression" and evts[-1] == {"type": "fin"}
    assert "gemma2:2b" in [m["nom"] for m in connecte.get("/api/modeles").json()]


def test_chat_en_flux(connecte):
    r = connecte.post("/api/chat", json={"modele": "qwen2.5:0.5b", "messages": [{"role": "user", "content": "Bonjour"}]})
    assert r.headers["content-type"].startswith("application/x-ndjson")
    evts = lignes(r)
    assert evts[0]["type"] == "token" and evts[-1]["type"] == "fin"
    assert evts[-1]["metriques"]["debit_tokens_s"] > 0


@pytest.mark.parametrize("corps", [
    {"modele": "qwen2.5:0.5b", "messages": []},                                       # aucun message
    {"modele": "a b", "messages": [{"role": "user", "content": "x"}]},                # nom invalide
    {"modele": "qwen2.5:0.5b", "messages": [{"role": "system", "content": "x"}]},     # rôle interdit
])
def test_chat_demandes_invalides(connecte, corps):
    assert connecte.post("/api/chat", json=corps).status_code == 422


def test_ollama_eteint(client):
    def refus(requete):
        raise httpx.ConnectError("connexion refusée")
    app = creer_app(FournisseurFactice(), ClientOllama(transport=httpx.MockTransport(refus)), JETON)
    c = TestClient(app)
    c.cookies.set(NOM_COOKIE, JETON)
    assert c.get("/api/ollama").json()["disponible"] is False
    r = c.get("/api/modeles")
    assert r.status_code == 503 and "Ollama ne répond pas" in r.json()["erreur"]
    evts = lignes(c.post("/api/chat", json={"modele": "qwen2.5:0.5b", "messages": [{"role": "user", "content": "x"}]}))
    assert evts == [{"type": "erreur", "erreur": "Ollama ne répond pas sur http://127.0.0.1:11434 : est-il installé et lancé ?"}]


def test_origine_autorisee():
    assert origine_autorisee("http://192.168.1.42:8000", "192.168.1.42:8000")
    assert not origine_autorisee("http://192.168.1.42:8001", "192.168.1.42:8000")
    assert not origine_autorisee("http://evil.example", "192.168.1.42:8000")
    assert not origine_autorisee(None, "192.168.1.42:8000")


# --- compatibilité (Phase 3) ---

def test_compatibilite_modele_installe(connecte):
    d = connecte.post("/api/compatibilite", json={"modele": "qwen2.5:0.5b", "num_ctx": 4096}).json()
    assert d["source"] == "installé" and d["format"] == "Q4_K_M"
    assert d["verdict"] == "ok" and d["solutions"] is None
    assert d["piles"] and d["plateforme"] == "NVIDIA Jetson Orin Nano 8GB"


def test_compatibilite_modele_a_telecharger_trop_gros(connecte):
    d = connecte.post("/api/compatibilite", json={"modele": "llama3.1:8b", "num_ctx": 8192}).json()
    assert d["source"] == "registre"
    assert d["verdict"] == "trop_gros"
    categories = [c["categorie"] for c in d["solutions"]["categories"]]
    assert "Réglage du serveur Ollama (redémarrage)" in categories
    assert "Autre modèle, plus petit" in categories


def test_compatibilite_modele_inconnu_et_requetes_invalides(connecte):
    assert connecte.post("/api/compatibilite", json={"modele": "inexistant:1b"}).status_code == 422
    assert connecte.post("/api/compatibilite", json={"modele": "qwen2.5:0.5b", "num_ctx": 10}).status_code == 422
    assert connecte.post("/api/compatibilite", json={"modele": "qwen2.5:0.5b", "type_kv": "q2"}).status_code == 422
    assert connecte.post("/api/compatibilite", json={"modele": "a b"}).status_code == 422


def test_chat_envoie_le_contexte_et_rend_la_memoire_chargee(connecte):
    r = connecte.post("/api/chat", json={"modele": "qwen2.5:0.5b", "num_ctx": 8192,
                                         "messages": [{"role": "user", "content": "Bonjour"}]})
    fin = lignes(r)[-1]
    assert fin["type"] == "fin"
    # le faux Ollama calcule sa "mémoire chargée" à partir du num_ctx reçu
    assert fin["metriques"]["memoire_chargee_go"] > 0.37
