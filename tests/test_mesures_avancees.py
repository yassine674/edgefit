# énergie par token, endurance, choix par objectif, images pour les modèles de vision
import asyncio
import base64
import itertools
import json

import pytest
from fastapi.testclient import TestClient

from edgefit import endurance, objectif
from edgefit.catalogue import CatalogueFactice
from edgefit.energie import Releveur
from edgefit.metriques import FournisseurFactice
from edgefit.metriques.generique import lire_nvidia
from edgefit.metriques.raspberry import puissance_pmic
from edgefit.ollama_factice import OllamaFactice
from edgefit.registre import RegistreFactice
from edgefit.securite import NOM_COOKIE
from edgefit.serveur import creer_app


def client(ollama=None):
    c = TestClient(creer_app(FournisseurFactice(), ollama or OllamaFactice(pause_s=0), "j", RegistreFactice(),
                             CatalogueFactice(), intervalle_s=0.01))
    c.cookies.set(NOM_COOKIE, "j")
    return c


def lignes(r):
    return [json.loads(x) for x in r.text.splitlines() if x.strip()]


# --- énergie ---------------------------------------------------------------------------------

class Capteur:
    """Carte de 4 W au repos qui passe à 10 W dès la première lecture suivante."""
    def __init__(self, source="carte"):
        self.n = 0
        self.source = source

    def lire(self):
        self.n += 1
        return {"puissance_w": 4.0 if self.n == 1 else 10.0, "source_puissance": self.source}


def test_energie_par_token_brute_et_nette():
    async def scenario():
        r = await Releveur(Capteur(), periode_s=0.01).demarrer()
        await asyncio.sleep(0.05)
        r.token()
        await asyncio.sleep(0.05)
        return r.resumer({"tokens_generes": 100, "debit_tokens_s": 20.0, "duree_totale_s": 6.0})
    e = asyncio.run(scenario())
    assert e["puissance_repos_w"] == 4.0 and e["source_puissance"] == "carte"
    assert e["joules_par_token"] == pytest.approx(10 / 20)            # 10 W pendant 5 s pour 100 tokens
    assert e["joules_par_token_net"] == pytest.approx((10 - 4) / 20)  # au-dessus du repos
    assert e["tokens_par_joule"] == pytest.approx(2.0)


def test_sans_capteur_aucune_energie_inventee():
    class SansCapteur:
        def lire(self):
            return {"puissance_w": None}

    async def scenario():
        r = await Releveur(SansCapteur(), periode_s=0.01).demarrer()
        return r.resumer({"tokens_generes": 10, "debit_tokens_s": 5})
    assert asyncio.run(scenario()) == {}


def test_chat_ajoute_l_energie_aux_mesures():
    evts = lignes(client(OllamaFactice(pause_s=0.01)).post(
        "/api/chat", json={"modele": "qwen2.5:0.5b", "messages": [{"role": "user", "content": "Bonjour"}]}))
    m = evts[-1]["metriques"]
    assert m["source_puissance"] == "carte" and m["joules_par_token"] > 0 and m["energie_j"] > 0


def test_puissance_raspberry_pi_5():
    sortie = """     3V7_WL_SW_A current(0)=0.10000000A
     3V3_SYS_A current(1)=0.20000000A
     VDD_CORE_A current(7)=2.00000000A
     3V7_WL_SW_V volt(8)=3.70000000V
     3V3_SYS_V volt(9)=3.30000000V
     VDD_CORE_V volt(15)=0.80000000V
     EXT5V_V volt(24)=5.10000000V"""
    assert puissance_pmic(sortie) == pytest.approx(0.37 + 0.66 + 1.6, abs=0.01)
    assert puissance_pmic("") is None


def test_gpu_nvidia(tmp_path):
    faux = tmp_path / "nvidia-smi"
    faux.write_text("#!/bin/sh\necho '57, 64, 123.45'\n")
    faux.chmod(0o755)
    assert lire_nvidia(str(faux)) == {"gpu_pct": 57.0, "temp_gpu_c": 64.0, "puissance_w": 123.45}
    faux.write_text("#!/bin/sh\necho '57, 64, [N/A]'\n")
    assert lire_nvidia(str(faux))["puissance_w"] is None
    assert lire_nvidia(str(tmp_path / "absent")) is None


# --- endurance -------------------------------------------------------------------------------

def test_resume_d_endurance_detecte_le_bridage():
    stable = [{"t": 5 * i, "debit": 20.0, "temp": 50, "puissance": 9} for i in range(1, 25)]
    assert endurance.resumer(stable, 120, 2400)["verdict"] == "stable"
    bride = [{"t": 5 * i, "debit": 20.0 if i < 10 else 14.0, "temp": 50 + i, "puissance": 9} for i in range(1, 25)]
    r = endurance.resumer(bride, 120, 2000)
    assert r["verdict"] == "bride" and r["baisse_pct"] == pytest.approx(30) and r["decroche_s"] == 50
    assert r["temp_max"] == 74 and r["joules_par_token"] == pytest.approx(9 / 14, rel=0.5)
    assert endurance.resumer(stable[:2], 120, 10)["verdict"] == "insuffisant"


def test_endurance_complete_et_enregistree(monkeypatch):
    horloge = itertools.count(0, 0.25)  # chaque lecture avance de 0,25 s : une minute simulée en un instant
    monkeypatch.setattr(endurance, "horloge", lambda: next(horloge))
    c = client()
    evts = lignes(c.post("/api/endurance", json={"nom": "qwen2.5:0.5b", "duree_s": 60}))
    points = [e for e in evts if e["type"] == "point"]
    assert evts[0]["type"] == "debut" and evts[-1]["type"] == "fin"
    assert len(points) >= 8 and all(p["debit"] and p["temp"] for p in points)
    assert evts[-1]["resume"]["verdict"] in ("stable", "leger", "bride")
    enregistres = c.get("/api/endurances").json()
    assert enregistres[0]["modele"] == "qwen2.5:0.5b" and len(enregistres[0]["points"]) == len(points)
    assert c.post("/api/endurance", json={"nom": "qwen2.5:0.5b", "duree_s": 7}).status_code == 422  # durée hors liste


# --- choix par objectif ------------------------------------------------------------------------

def test_rendement_appris_sur_les_mesures():
    assert objectif.rendement([]) == (objectif.RENDEMENT_DEFAUT, 0)
    assert objectif.rendement([(30, 60), (40, 80), (None, 50), (500, 10)]) == (0.5, 2)  # 500/10 : aberrant, ignoré


def test_classement_prend_le_plus_gros_qui_remplit_tout():
    modeles = [
        {"nom": "petit", "parametres": "1.5B", "tags": ["chat", "code"], "verdict": "ok", "plafond_tokens_s": 60},
        {"nom": "moyen", "parametres": "3B", "tags": ["chat", "code"], "verdict": "ok", "plafond_tokens_s": 30},
        {"nom": "gros", "parametres": "8B", "tags": ["chat", "code"], "verdict": "trop_gros", "plafond_tokens_s": 12},
        {"nom": "lent", "parametres": "7B", "tags": ["chat", "code"], "verdict": "ok", "plafond_tokens_s": 14},
        {"nom": "mesure", "parametres": "4B", "tags": ["chat"], "verdict": "ok", "plafond_tokens_s": 25, "debit_mesure": 16},
    ]
    r = objectif.classer(modeles, "code", 10, 0.6)
    assert [m["nom"] for m in r["retenus"]] == ["moyen", "petit"]
    assert r["exclus"] == {"memoire": 1, "usage": 1, "lent": 1, "inconnue": 0}
    assert [m["nom"] for m in r["presque"]] == ["lent"]  # 8,4 tok/s pour 10 demandés
    r = objectif.classer(modeles, None, 15, 0.6)
    assert r["retenus"][0]["nom"] == "mesure" and r["retenus"][0]["source_vitesse"] == "mesure"


def test_route_objectif():
    c = client()
    r = c.get("/api/objectif", params={"usage": "code", "debit_min": 5, "num_ctx": 8192}).json()
    assert r["retenus"] and all("code" in m["tags"] for m in r["retenus"])
    assert r["plateforme"] and 0 < r["rendement"] <= 1
    assert c.get("/api/objectif", params={"usage": "shell"}).status_code == 422


# --- images ------------------------------------------------------------------------------------

IMAGE = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64).decode()


def test_image_envoyee_au_modele_de_vision_et_comptee():
    c = client()
    assert "vision" not in c.get("/api/modeles/capacites", params={"nom": "qwen2.5:0.5b"}).json()["capacites"]
    lignes(c.post("/api/modeles/telecharger", json={"nom": "moondream:1.8b"}))
    assert "vision" in c.get("/api/modeles/capacites", params={"nom": "moondream:1.8b"}).json()["capacites"]
    conv = c.post("/api/conversations", json={"modele": "moondream:1.8b"}).json()
    evts = lignes(c.post("/api/chat", json={"modele": "moondream:1.8b", "conversation_id": conv["id"],
                                            "messages": [{"role": "user", "content": "Décris", "images": [IMAGE]}]}))
    assert "1 image" in "".join(e["texte"] for e in evts if e["type"] == "token")
    lue = c.get(f"/api/conversations/{conv['id']}").json()
    assert lue["messages"][0]["mesures"] == {"images": 1}  # le nombre, pas l'image


@pytest.mark.parametrize("images", [["pas du base64 !"], [IMAGE] * 5, ["data:image/png;base64," + IMAGE]])
def test_images_invalides_refusees(images):
    r = client().post("/api/chat", json={"modele": "qwen2.5:0.5b", "messages": [{"role": "user", "content": "x", "images": images}]})
    assert r.status_code == 422


def test_capacites_modele_absent():
    c = client()
    assert c.get("/api/modeles/capacites", params={"nom": "absent:1b"}).status_code == 404
    assert c.get("/api/modeles/capacites", params={"nom": "../x"}).status_code == 400
