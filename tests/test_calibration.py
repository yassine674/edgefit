import pytest
from fastapi.testclient import TestClient

from edgefit import calibration
from edgefit.catalogue import CatalogueFactice
from edgefit.metriques import FournisseurFactice
from edgefit.ollama_factice import OllamaFactice
from edgefit.registre import RegistreFactice
from edgefit.securite import NOM_COOKIE
from edgefit.serveur import creer_app


def test_sans_mesure_rien_ne_change():
    assert calibration.resume([])["active"] is False
    un_point = calibration.ajuster([(2.0, 0.5, 2.75)])  # exactement la formule de départ
    assert un_point == pytest.approx(list(calibration.DEPART), abs=1e-6)


def test_mesures_coherentes_les_coefficients_convergent():
    # vraie loi de cette carte : 0,4 Go fixes + 1,05 × poids + 1,3 × KV
    vrai = lambda p, k: 0.4 + 1.05 * p + 1.3 * k
    points = [(p, k, vrai(p, k)) for p in (0.4, 1.0, 2.0, 3.5) for k in (0.1, 0.5, 1.0)] * 5
    r = calibration.resume([{"poids_go": p, "kv_go": k, "mesure_go": y} for p, k, y in points])
    assert r["active"] and r["n"] == 60
    assert r["coef"]["fixe_go"] == pytest.approx(0.4, abs=0.05)
    assert r["coef"]["a"] == pytest.approx(1.05, abs=0.03)
    assert r["coef"]["b"] == pytest.approx(1.3, abs=0.05)
    assert r["erreur_corrigee_pct"] < r["erreur_brute_pct"]


def test_peu_de_mesures_correction_prudente():
    # un seul point très éloigné ne doit pas emmener les coefficients n'importe où
    coef = calibration.ajuster([(1.0, 0.2, 3.0)])
    assert abs(coef[1] - 1.1) < 1.0 and abs(coef[2] - 1.1) < 1.0


@pytest.fixture
def client():
    c = TestClient(creer_app(FournisseurFactice(), OllamaFactice(pause_s=0), "j", RegistreFactice(),
                             CatalogueFactice(), intervalle_s=0.01))
    c.cookies.set(NOM_COOKIE, "j")
    c.delete("/api/calibration")
    return c


def test_le_chat_alimente_la_calibration_puis_corrige_le_verdict(client):
    for num_ctx in (2048, 4096, 8192):
        client.post("/api/chat", json={"modele": "qwen2.5:0.5b", "num_ctx": num_ctx, "type_kv": "f16",
                                       "messages": [{"role": "user", "content": "x"}]})
    cal = client.get("/api/calibration").json()
    assert cal["n"] == 3 and cal["active"]
    assert all(o["mesure_go"] > 0 and o["estime_go"] > 0 for o in cal["observations"])
    d = client.post("/api/compatibilite", json={"modele": "qwen2.5:0.5b", "num_ctx": 4096}).json()
    assert d["calibration"]["active"] and d["calibration"]["total_go"] > 0
    assert "verdict_brut" in d["calibration"]
    cat = client.get("/api/catalogue").json()
    assert cat["calibration"]["active"]
    assert all("total_brut_go" in m for m in cat["modeles"])


def test_meme_reglage_compte_une_seule_fois(client):
    for _ in range(3):
        client.post("/api/chat", json={"modele": "qwen2.5:0.5b", "num_ctx": 4096,
                                       "messages": [{"role": "user", "content": "x"}]})
    assert client.get("/api/calibration").json()["n"] == 1
