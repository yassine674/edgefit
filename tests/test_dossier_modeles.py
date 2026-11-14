# Ollama installé par son script officiel range ses modèles dans un dossier illisible pour
# l'utilisateur : edgefit doit quand même voir Ollama et mesurer le disque.
import os

import pytest
from fastapi.testclient import TestClient

from edgefit.catalogue import CatalogueFactice
from edgefit.diagnostic import dossier_modeles
from edgefit.metriques import FournisseurFactice
from edgefit.ollama_factice import OllamaFactice
from edgefit.registre import RegistreFactice
from edgefit.securite import NOM_COOKIE
from edgefit.serveur import creer_app


@pytest.fixture
def dossier_illisible(tmp_path, monkeypatch):
    service = tmp_path / "ollama"
    (service / ".ollama" / "models").mkdir(parents=True)
    service.chmod(0o750)
    os.chmod(service, 0)  # comme /usr/share/ollama (drwxr-x--- ollama:ollama) vu par un autre compte
    monkeypatch.setenv("OLLAMA_MODELS", str(service / ".ollama" / "models"))
    yield service
    os.chmod(service, 0o750)


@pytest.mark.skipif(os.geteuid() == 0, reason="root lit tous les dossiers")
def test_dossier_illisible_remonte_au_parent(dossier_illisible):
    assert dossier_modeles() == dossier_illisible


@pytest.mark.skipif(os.geteuid() == 0, reason="root lit tous les dossiers")
def test_ollama_vu_meme_si_le_dossier_des_modeles_est_illisible(dossier_illisible):
    c = TestClient(creer_app(FournisseurFactice(), OllamaFactice(pause_s=0), "j", RegistreFactice(), CatalogueFactice()))
    c.cookies.set(NOM_COOKIE, "j")
    r = c.get("/api/ollama")
    assert r.status_code == 200 and r.json()["disponible"] is True and r.json()["disque"]["total_go"] > 0
