import json

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from edgefit.metriques import FournisseurFactice
from edgefit.ollama import ClientOllama
from edgefit.ollama_factice import OllamaFactice
from edgefit.catalogue import CatalogueFactice
from edgefit.registre import RegistreFactice
from edgefit.securite import NOM_COOKIE, origine_autorisee
from edgefit.serveur import creer_app

JETON = "jeton-de-test"
COOKIE = f"{NOM_COOKIE}={JETON}"


@pytest.fixture
def client():
    return TestClient(creer_app(FournisseurFactice(), OllamaFactice(pause_s=0), JETON, RegistreFactice(),
                                CatalogueFactice(), intervalle_s=0.01))


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
    categories = [c["cle"] for c in d["solutions"]["categories"]]
    assert "serveur" in categories and "modele" in categories


def test_compatibilite_modele_inconnu_et_requetes_invalides(connecte):
    r = connecte.post("/api/compatibilite", json={"modele": "inexistant:1b"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "introuvable"
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


def test_recherche_installes_suggestions_et_hugging_face(connecte):
    r = connecte.get("/api/recherche", params={"q": "llama"}).json()
    assert [m["nom"] for m in r["installes"]] == ["llama3.2:3b"]
    assert all(s["nom"] != "llama3.2:3b" for s in r["suggestions"])  # déjà installé : pas proposé deux fois
    assert r["huggingface"][0]["depot"] == "bartowski/Llama-3.2-1B-Instruct-GGUF"


def test_versions_d_un_depot_et_depot_invalide(connecte):
    versions = connecte.get("/api/variantes", params={"depot": "bartowski/Llama-3.2-1B-Instruct-GGUF"}).json()
    assert versions[0]["format"] == "Q3_K_M" and versions[-1]["format"] == "F16"
    assert connecte.get("/api/variantes", params={"depot": "../x"}).status_code == 400
    assert connecte.get("/api/variantes", params={"depot": "inconnu/x"}).status_code == 502


def test_compatibilite_donne_telechargement_et_disque(connecte):
    d = connecte.post("/api/compatibilite", json={"modele": "gemma3:4b", "num_ctx": 4096}).json()
    assert d["projecteur_go"] > 0.7 and d["telechargement_go"] > d["taille_fichier_go"]
    assert d["disque_libre_go"] > 0
    installe = connecte.post("/api/compatibilite", json={"modele": "qwen2.5:0.5b"}).json()
    assert installe["telechargement_go"] == 0


def test_sans_jeton_acces_libre():
    # edgefit --sans-jeton : la page, l'API et le flux de métriques s'ouvrent sans cookie
    c = TestClient(creer_app(FournisseurFactice(), OllamaFactice(pause_s=0), None, RegistreFactice(),
                             CatalogueFactice(), intervalle_s=0.01))
    assert c.get("/").status_code == 200
    assert c.get("/api/metriques").status_code == 200
    with c.websocket_connect("/ws/metriques", headers={"origin": "http://testserver"}) as ws:
        assert ws.receive_json()
