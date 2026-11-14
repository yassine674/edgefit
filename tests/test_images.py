# images : installation vérifiée (SHA-256, archive sûre), génération par un moteur lancé sans shell
import hashlib
import io
import json
import sys
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from edgefit import images as mod
from edgefit.catalogue import CatalogueFactice
from edgefit.images import Images, ImagesFactice, png_degrade
from edgefit.metriques import FournisseurFactice
from edgefit.ollama_factice import OllamaFactice
from edgefit.registre import RegistreFactice
from edgefit.securite import NOM_COOKIE
from edgefit.serveur import creer_app

# faux moteur : un script Python qui se comporte comme sd-cli (barre de progression, PNG écrit dans --output)
FAUX_SD = f"""#!{sys.executable}
import sys, struct, zlib
a = sys.argv
sortie = a[a.index("--output") + 1]
etapes = int(a[a.index("--steps") + 1])
open(sortie + ".prompt", "w").write(a[a.index("--prompt") + 1])
for n in range(1, etapes + 1):
    print(f"  |{{'=' * 10 * n}}| {{n}}/{{etapes}} - 0.50s/it", end="\\r", flush=True)
sys.path.insert(0, {str(__import__('pathlib').Path(mod.__file__).parent.parent)!r})
from edgefit.images import png_degrade
open(sortie, "wb").write(png_degrade(8, 8, 1))
"""


def client(im):
    c = TestClient(creer_app(FournisseurFactice(), OllamaFactice(pause_s=0), "j", RegistreFactice(), CatalogueFactice(), images=im))
    c.cookies.set(NOM_COOKIE, "j")
    return c


def lignes(r):
    return [json.loads(x) for x in r.text.splitlines() if x.strip()]


@pytest.fixture
def installe(tmp_path):
    im = Images(tmp_path)
    (tmp_path / "moteur-cpu").mkdir()
    sd = tmp_path / "moteur-cpu" / "sd-cli"
    sd.write_text(FAUX_SD)
    sd.chmod(0o755)
    (tmp_path / mod.MODELE["fichier"]).write_bytes(b"gguf")
    return im


def test_generation_avec_le_moteur(installe):
    c = client(installe)
    prompt = "un chat ; rm -rf / $(reboot) `id`"  # passé tel quel : jamais interprété par un shell
    evts = lignes(c.post("/api/images/generer", json={"prompt": prompt, "etapes": 4, "graine": 7, "largeur": 256, "hauteur": 256}))
    types = [e["type"] for e in evts]
    assert types[0] == "debut" and types.count("etape") == 4 and types[-1] == "fin"
    fin = evts[-1]
    assert fin["graine"] == 7 and fin["prompt"] == prompt
    assert (installe.galerie_dossier() / (fin["fichier"] + ".prompt")).read_text() == prompt
    assert c.get(f"/api/images/fichier/{fin['fichier']}").content.startswith(b"\x89PNG")
    assert c.get("/api/images/galerie").json()[0]["fichier"] == fin["fichier"]
    assert c.delete(f"/api/images/fichier/{fin['fichier']}").status_code == 200
    assert c.get("/api/images/galerie").json() == []


def test_noms_de_fichiers_et_parametres_refuses(installe):
    c = client(installe)
    assert c.get("/api/images/fichier/..%2F..%2Fetc%2Fpasswd").status_code == 404
    assert c.get("/api/images/fichier/x.json").status_code == 404
    for mauvais in ({"prompt": ""}, {"prompt": "x", "largeur": 999}, {"prompt": "x", "etapes": 50}, {"prompt": "x" * 1001}):
        assert c.post("/api/images/generer", json=mauvais).status_code == 422


def test_moteur_absent(tmp_path):
    evts = lignes(client(Images(tmp_path)).post("/api/images/generer", json={"prompt": "x"}))
    assert evts[-1]["code"] == "absent"


def test_installation_verifiee(tmp_path, monkeypatch):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("sd-cli", "#!/bin/sh\n")
        z.writestr("libggml.so", "x")
        z.writestr("../evasion.txt", "non")      # chemin hors du dossier : ignoré
    archive = archive.getvalue()
    modele = b"gguf" * 100
    monkeypatch.setattr(mod, "binaires_disponibles", lambda: True)
    monkeypatch.setitem(mod.MOTEURS, "cpu", {"fichier": "sd.zip", "octets": len(archive), "sha256": hashlib.sha256(archive).hexdigest()})
    monkeypatch.setattr(mod, "MODELE", {**mod.MODELE, "octets": len(modele), "sha256": hashlib.sha256(modele).hexdigest(), "url": "https://hf.test/m.gguf"})
    altere = {"actif": False}

    def repondre(req):
        if req.url.path.endswith("sd.zip"):
            return httpx.Response(200, content=archive)
        return httpx.Response(200, content=modele[:-1] + b"X" if altere["actif"] else modele)
    im = Images(tmp_path, transport=httpx.MockTransport(repondre))
    altere["actif"] = True
    evts = lignes(client(im).post("/api/images/installer", json={"variante": "cpu"}))
    assert evts[-1]["code"] == "empreinte" and not im.modele().exists()      # modèle altéré : refusé, supprimé
    altere["actif"] = False
    evts = lignes(client(im).post("/api/images/installer", json={"variante": "cpu"}))
    assert evts[-1]["type"] == "fin" and im.moteur()[0] == "cpu" and im.modele().exists()
    assert not (tmp_path / "evasion.txt").exists() and not (tmp_path.parent / "evasion.txt").exists()
    assert (tmp_path / "moteur-cpu" / "sd-cli").stat().st_mode & 0o111


def test_mode_simule(tmp_path):
    c = client(ImagesFactice(tmp_path, pause_s=0))
    assert lignes(c.post("/api/images/installer", json={"variante": "cpu"}))[-1]["type"] == "fin"
    fin = lignes(c.post("/api/images/generer", json={"prompt": "test", "etapes": 2}))[-1]
    assert fin["type"] == "fin" and c.get(f"/api/images/fichier/{fin['fichier']}").content == png_degrade(256, 256, fin["graine"])
