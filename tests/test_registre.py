import asyncio
import json
import struct

import httpx
import pytest

from edgefit.registre import (
    Registre, RegistreErreur, adresse, format_fichier, lire_entete_gguf, parametres_depuis_texte,
)


def _chaine(texte):
    b = texte.encode()
    return struct.pack("<Q", len(b)) + b


def gguf(meta):
    """Fabrique un en-tête GGUF minimal (types : u32 = 4, chaîne = 8, tableau de chaînes = 9)."""
    corps = b""
    for cle, valeur in meta.items():
        corps += _chaine(cle)
        if isinstance(valeur, str):
            corps += struct.pack("<I", 8) + _chaine(valeur)
        elif isinstance(valeur, list):
            corps += struct.pack("<II", 9, 8) + struct.pack("<Q", len(valeur)) + b"".join(_chaine(v) for v in valeur)
        else:
            corps += struct.pack("<II", 4, valeur)
    return b"GGUF" + struct.pack("<IQQ", 3, 0, len(meta)) + corps


META_LLAMA = {"general.architecture": "llama", "llama.block_count": 28, "llama.attention.head_count": 24,
              "llama.attention.head_count_kv": 8, "llama.attention.key_length": 128,
              "llama.context_length": 131072, "llama.embedding_length": 3072, "general.file_type": 15,
              "tokenizer.ggml.tokens": ["<s>", "a", "b"]}


def test_lecture_entete_complet():
    meta, complet = lire_entete_gguf(gguf(META_LLAMA))
    assert complet and meta["llama.block_count"] == 28 and meta["tokenizer.ggml.tokens"] == ["<s>", "a", "b"]


def test_entete_coupe_garde_ce_qui_a_ete_lu():
    octets = gguf(META_LLAMA)
    meta, complet = lire_entete_gguf(octets[:len(octets) - 5])
    assert not complet and meta["llama.block_count"] == 28


def test_fichier_qui_n_est_pas_un_gguf():
    with pytest.raises(RegistreErreur):
        lire_entete_gguf(b"PK\x03\x04" + b"\x00" * 30)


def test_adresses_des_deux_registres():
    assert adresse("qwen2.5:0.5b")[:3] == ("https://registry.ollama.ai", "library/qwen2.5", "0.5b")
    assert adresse("llama3.2")[2] == "latest"
    base, depot, tag, entetes = adresse("hf.co/bartowski/Llama-3.2-1B-Instruct-GGUF:Q4_K_M")
    assert (base, depot, tag) == ("https://huggingface.co", "bartowski/Llama-3.2-1B-Instruct-GGUF", "Q4_K_M")
    assert "ollama" in entetes["User-Agent"]
    with pytest.raises(RegistreErreur):
        adresse("hf.co/../../etc:latest")


def test_parametres_et_format():
    assert parametres_depuis_texte("999.89M") == 999_890_000
    assert parametres_depuis_texte("1.24B") == 1_240_000_000
    assert parametres_depuis_texte("") is None
    assert format_fichier({"file_type": "Q4_K_M"}, {}, "1b") == "Q4_K_M"
    assert format_fichier({"file_type": "unknown"}, {"general.file_type": 7}, "x") == "Q8_0"
    assert format_fichier({}, {}, "q5_k_m") == "Q5_K_M"
    assert format_fichier({}, {}, "latest") is None


def _faux_registre(requetes):
    entete = gguf(META_LLAMA)
    config = {"file_type": "Q4_K_M", "model_type": "3.2B"}

    def repondre(requete):
        requetes.append(requete)
        chemin = requete.url.path
        if chemin.endswith("/manifests/3b"):
            return httpx.Response(200, json={"config": {"digest": "sha256:cfg"}, "layers": [
                {"mediaType": "application/vnd.ollama.image.model", "digest": "sha256:mod", "size": 2_019_377_376},
                {"mediaType": "application/vnd.ollama.image.projector", "digest": "sha256:proj", "size": 851_251_104},
                {"mediaType": "application/vnd.ollama.image.template", "digest": "sha256:tpl", "size": 1_000}]})
        if chemin.endswith("/blobs/sha256:cfg"):
            return httpx.Response(200, content=json.dumps(config).encode())
        if chemin.endswith("/blobs/sha256:mod"):
            debut, fin = map(int, requete.headers["range"].removeprefix("bytes=").split("-"))
            return httpx.Response(206, content=entete[debut:fin + 1])
        return httpx.Response(404)
    return httpx.MockTransport(repondre)


def test_registre_lit_taille_architecture_et_format_sans_tout_telecharger():
    requetes = []
    infos = asyncio.run(Registre(transport=_faux_registre(requetes)).infos("llama3.2:3b"))
    assert infos["taille_octets"] == 2_019_377_376 and infos["format"] == "Q4_K_M"
    assert infos["architecture"]["num_layers"] == 28 and infos["architecture"]["nb_param"] == 3_200_000_000
    lecture = next(r for r in requetes if r.url.path.endswith("sha256:mod"))
    assert lecture.headers["range"] == "bytes=0-1048575"  # le premier Mo seulement
    # le registre d'Ollama refuse les requêtes qui se présentent comme Ollama sans signature
    assert all("ollama" not in r.headers.get("user-agent", "") for r in requetes)


def test_registre_compte_le_projecteur_des_modeles_vision():
    infos = asyncio.run(Registre(transport=_faux_registre([])).infos("llama3.2:3b"))
    assert infos["projecteur_octets"] == 851_251_104
    assert infos["architecture"]["projecteur_octets"] == 851_251_104
    assert infos["telechargement_octets"] == 2_019_377_376 + 851_251_104 + 1_000


def test_registre_garde_les_reponses_en_cache():
    requetes = []
    registre = Registre(transport=_faux_registre(requetes))

    async def deux_fois_en_meme_temps():
        return await asyncio.gather(registre.infos("llama3.2:3b"), registre.infos("llama3.2:3b"))
    a, b = asyncio.run(deux_fois_en_meme_temps())
    n = len(requetes)
    asyncio.run(registre.infos("llama3.2:3b"))
    assert a == b and len(requetes) == n == 3  # manifeste + config + en-tête, une seule fois


def test_registre_modele_introuvable():
    with pytest.raises(RegistreErreur):
        asyncio.run(Registre(transport=_faux_registre([])).infos("llama3.2:70b"))
