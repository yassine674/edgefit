# parole : enregistrement -> Whisper -> texte ; certificat pour --https
import io
import json
import math
import struct
import wave

import pytest
from fastapi.testclient import TestClient

from edgefit import ecoute
from edgefit.catalogue import CatalogueFactice
from edgefit.ecoute import EcouteErreur, EcouteFactice
from edgefit.metriques import FournisseurFactice
from edgefit.ollama_factice import OllamaFactice
from edgefit.registre import RegistreFactice
from edgefit.securite import NOM_COOKIE
from edgefit.serveur import creer_app


def client(e):
    c = TestClient(creer_app(FournisseurFactice(), OllamaFactice(pause_s=0), "j", RegistreFactice(), CatalogueFactice(), ecoute=e))
    c.cookies.set(NOM_COOKIE, "j")
    return c


def wav(duree_s, taux=48000):
    tampon = io.BytesIO()
    with wave.open(tampon, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(taux)
        w.writeframes(b"".join(struct.pack("<hh", v, v) for v in
                               (int(8000 * math.sin(2 * math.pi * 440 * i / taux)) for i in range(int(duree_s * taux)))))
    return tampon.getvalue()


def test_telecharger_puis_transcrire():
    e = EcouteFactice()
    c = client(e)
    assert c.get("/api/ecoute").json()["modeles"]["base"]["installe"] is False
    r = c.post("/api/ecoute/transcrire?taille=base&langue=fr", content=wav(0.5), headers={"Content-Type": "audio/wav"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "modele_absent"
    evts = [json.loads(x) for x in c.post("/api/ecoute/telecharger", json={"taille": "base"}).text.splitlines()]
    assert evts[-1]["type"] == "fin" and c.get("/api/ecoute").json()["modeles"]["base"]["installe"]
    r = c.post("/api/ecoute/transcrire?taille=base&langue=fr", content=wav(0.5))
    assert r.status_code == 200 and r.json()["texte"] == EcouteFactice.PHRASE and e.recus


def test_refus():
    c = client(EcouteFactice(moteur=False))
    assert c.post("/api/ecoute/transcrire", content=wav(0.2)).status_code == 501
    c = client(EcouteFactice())
    assert c.post("/api/ecoute/transcrire?taille=enorme", content=b"x" * 100).status_code == 422
    assert c.post("/api/ecoute/telecharger", json={"taille": "large-v3"}).status_code == 422
    trop = c.post("/api/ecoute/transcrire", content=b"x" * (ecoute.AUDIO_MAX_OCTETS + 1))
    assert trop.status_code == 413


def test_decodage_reel_en_16_khz_mono():
    pytest.importorskip("av")
    audio = ecoute.decoder(wav(1.0))  # stéréo 48 kHz -> mono 16 kHz
    assert abs(len(audio) - 16000) < 400 and audio.dtype.name == "float32" and 0.2 < abs(audio).max() <= 1
    with pytest.raises(EcouteErreur):
        ecoute.decoder(b"pas de l'audio du tout")


def test_certificat_https(tmp_path, monkeypatch):
    import shutil
    if not shutil.which("openssl"):
        pytest.skip("openssl absent")
    from edgefit import cli
    monkeypatch.setattr(cli, "user_data_dir", lambda *a, **k: str(tmp_path))
    cert, cle = cli.certificat("192.168.1.42")
    assert cert.exists() and (cle.stat().st_mode & 0o777) == 0o600
    assert cli.certificat("192.168.1.42") == (cert, cle)  # réutilisé tant que l'IP ne change pas
    avant = cert.read_bytes()
    cli.certificat("10.0.0.7")
    assert cert.read_bytes() != avant  # refait pour la nouvelle IP
