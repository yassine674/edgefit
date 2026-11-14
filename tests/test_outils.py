import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from edgefit import compatibilite, outils
from edgefit.catalogue import CatalogueFactice
from edgefit.metriques import FournisseurFactice
from edgefit.ollama_factice import OllamaFactice
from edgefit.registre import RegistreFactice
from edgefit.securite import NOM_COOKIE
from edgefit.serveur import creer_app

TOUS = list(outils.NOMS)


def client_avec(ollama):
    c = TestClient(creer_app(FournisseurFactice(), ollama, "j", RegistreFactice(), CatalogueFactice(), intervalle_s=0.01))
    c.cookies.set(NOM_COOKIE, "j")
    return c


@pytest.fixture
def client():
    return client_avec(OllamaFactice(pause_s=0))


def lignes(r):
    return [json.loads(l) for l in r.text.splitlines() if l.strip()]


# --- calculatrice : de l'arithmétique, jamais du code ------------------------------------------

def test_calculatrice_exacte():
    assert outils.calculer("2 * 28 * 8 * 128 * 4096 * 2 / 1024**3")["result"] == 0.4375
    assert outils.calculer("sqrt(16) + 2^3")["result"] == 12
    assert outils.calculer("round(pi, 2)")["result"] == 3.14


@pytest.mark.parametrize("expression", [
    "__import__('os').system('ls')", "open('/etc/passwd')", "(1).__class__", "x + 1", "[1, 2]",
    "9**9**9", "10**100000", "'a' * 10", "sqrt(x=4)", "1" * 300,
])
def test_calculatrice_refuse_tout_le_reste(expression):
    with pytest.raises(outils.ErreurOutil):
        outils.calculer(expression)


def test_division_par_zero_devient_une_erreur_lisible():
    with pytest.raises(outils.ErreurOutil):
        outils.calculer("1 / 0")


# --- liste blanche ------------------------------------------------------------------------------

def boite():
    async def budget():  # Jetson de 8 Go dont 4 Go libres
        return compatibilite.budget("jetson", 8 * 1024 ** 3, 4 * 1024 ** 3), [], {}
    return outils.Boite(FournisseurFactice(), OllamaFactice(pause_s=0), budget)


def test_outil_non_coche_ou_inconnu_jamais_execute():
    b = boite()
    assert "not allowed" in asyncio.run(b.executer("calculator", {"expression": "1+1"}, {"current_time"}))["error"]
    assert "unknown tool" in asyncio.run(b.executer("shell", {"cmd": "rm -rf /"}, set(TOUS)))["error"]
    assert [d["function"]["name"] for d in b.definitions({"calculator"})] == ["calculator"]


def test_arguments_verifies():
    b = boite()
    assert "missing argument" in asyncio.run(b.executer("check_model_fit", {}, set(TOUS)))["error"]
    assert "integer" in asyncio.run(b.executer("check_model_fit", {"model": "qwen3:8b", "context": "beaucoup"}, set(TOUS)))["error"]
    r = asyncio.run(b.executer("check_model_fit", {"model": "qwen2.5:0.5b", "context": "8192", "invente": 1}, set(TOUS)))
    assert r["context"] == 8192 and r["verdict"] == "fits" and r["source"] == "installed"
    assert "not installed" in asyncio.run(b.executer("check_model_fit", {"model": "qwen3:8b"}, set(TOUS)))["source"]
    assert "neither installed" in asyncio.run(b.executer("check_model_fit", {"model": "inconnu:1b"}, set(TOUS)))["error"]


def test_chat_refuse_un_nom_d_outil_hors_liste(client):
    r = client.post("/api/chat", json={"modele": "qwen2.5:0.5b", "outils": ["shell"],
                                       "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 422


# --- boucle modèle -> outil -> modèle ---------------------------------------------------------

def test_chat_avec_outil_puis_reponse_enregistree(client):
    conv = client.post("/api/conversations", json={"modele": "qwen2.5:0.5b"}).json()
    evts = lignes(client.post("/api/chat", json={"modele": "qwen2.5:0.5b", "conversation_id": conv["id"], "outils": TOUS,
                                                 "messages": [{"role": "user", "content": "Combien font 17 * 23 ?"}]}))
    types = [e["type"] for e in evts]
    assert types.index("outil") < types.index("outil_resultat") < types.index("token") < types.index("fin")
    assert evts[types.index("outil_resultat")]["resultat"]["result"] == 391
    fin = evts[-1]
    assert fin["metriques"]["outils"][0]["nom"] == "calculator"
    assert "391" in "".join(e["texte"] for e in evts if e["type"] == "token")
    lue = client.get(f"/api/conversations/{conv['id']}").json()
    assert lue["messages"][1]["mesures"]["outils"][0]["resultat"]["result"] == 391  # visible au rechargement


def test_etat_de_la_carte(client):
    evts = lignes(client.post("/api/chat", json={"modele": "qwen2.5:0.5b", "outils": ["board_status"],
                                                 "messages": [{"role": "user", "content": "Quelle est la température de la carte ?"}]}))
    res = next(e for e in evts if e["type"] == "outil_resultat")["resultat"]
    assert res["memory_total_gb"] and "cpu_temperature_c" in res


def test_sans_outils_coches_le_modele_repond_directement(client):
    evts = lignes(client.post("/api/chat", json={"modele": "qwen2.5:0.5b",
                                                 "messages": [{"role": "user", "content": "Combien font 17 * 23 ?"}]}))
    assert "outil" not in [e["type"] for e in evts] and evts[-1]["type"] == "fin"


def test_modele_qui_ne_sait_pas_appeler_d_outils():
    c = client_avec(OllamaFactice(pause_s=0, sans_outils={"llama3.2:3b"}))
    evts = lignes(c.post("/api/chat", json={"modele": "llama3.2:3b", "outils": TOUS,
                                            "messages": [{"role": "user", "content": "Combien font 17 * 23 ?"}]}))
    assert evts[0]["type"] == "outils_indisponibles" and evts[-1]["type"] == "fin"


def test_ollama_ancien_qui_refuse_les_outils_en_cours_de_route():
    ollama = OllamaFactice(pause_s=0, sans_outils={"llama3.2:3b"})

    async def infos_sans_capacites(nom, _infos=ollama.infos):
        i = await _infos(nom)
        return i and {**i, "capacites": None}
    ollama.infos = infos_sans_capacites
    evts = lignes(client_avec(ollama).post("/api/chat", json={"modele": "llama3.2:3b", "outils": TOUS,
                                                               "messages": [{"role": "user", "content": "17 * 23"}]}))
    types = [e["type"] for e in evts]
    assert "outils_indisponibles" in types and types[-1] == "fin" and "erreur" not in types
