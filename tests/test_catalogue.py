import asyncio

import httpx

from edgefit.catalogue import (
    SUGGESTIONS, Catalogue, chercher_suggestions, depot_valide, quant_depuis_fichier, variantes_depuis_arbre,
)


def test_suggestions_livrees_completes():
    assert len(SUGGESTIONS) >= 20
    for e in SUGGESTIONS:
        assert e["taille_octets"] > 0 and e["description"] and ":" in e["nom"]
    assert any(e["vision"] for e in SUGGESTIONS)
    assert [e["nom"] for e in chercher_suggestions("gemma3")][:2] == ["gemma3:270m", "gemma3:1b"]
    assert chercher_suggestions("code")[0]["nom"] == "qwen2.5-coder:1.5b"  # cherche aussi dans la description


def test_quantification_lue_dans_le_nom_de_fichier():
    assert quant_depuis_fichier("Llama-3.2-1B-Instruct-Q4_K_M.gguf") == ("Q4_K_M", False)
    assert quant_depuis_fichier("gemma-3-4b-it-UD-Q4_K_XL.gguf") == ("UD-Q4_K_XL", False)
    assert quant_depuis_fichier("Q8_0/model-q8_0-00001-of-00002.gguf") == ("Q8_0", True)
    assert quant_depuis_fichier("mmproj-F16.gguf") is None
    assert quant_depuis_fichier("README.md") is None
    assert quant_depuis_fichier("model.gguf") is None


def test_versions_d_un_depot():
    arbre = [
        {"type": "file", "path": "m-Q8_0.gguf", "size": 800, "lfs": {"size": 800}},
        {"type": "file", "path": "m-Q4_K_M.gguf", "size": 400},
        {"type": "file", "path": "m-F16-00001-of-00002.gguf", "size": 900},
        {"type": "file", "path": "m-F16-00002-of-00002.gguf", "size": 700},
        {"type": "file", "path": "mmproj-m-f16.gguf", "size": 300},
        {"type": "directory", "path": "Q8_0"},
    ]
    versions = variantes_depuis_arbre("a/m-GGUF", arbre)
    assert [v["format"] for v in versions] == ["Q4_K_M", "Q8_0", "F16"]
    f16 = versions[-1]
    assert f16["taille_octets"] == 1600 and f16["fragmente"] and not f16["telechargeable"]
    assert versions[0]["nom"] == "hf.co/a/m-GGUF:Q4_K_M" and versions[0]["projecteur_octets"] == 300


def test_depot_valide():
    assert depot_valide("bartowski/Llama-3.2-1B-Instruct-GGUF")
    assert not depot_valide("../etc/passwd") and not depot_valide("sans-slash") and not depot_valide("a/b/c")


def _catalogue(repondre):
    return Catalogue(transport=httpx.MockTransport(repondre))


def test_recherche_hf_filtre_les_modeles_qui_ne_parlent_pas():
    def repondre(requete):
        assert requete.url.params["filter"] == "gguf"
        return httpx.Response(200, json=[
            {"id": "a/chat-GGUF", "downloads": 10, "gated": False, "pipeline_tag": "text-generation"},
            {"id": "a/voix-GGUF", "downloads": 99, "gated": False, "pipeline_tag": "text-to-speech"},
            {"id": "a/protege-GGUF", "downloads": 5, "gated": "manual"},
        ])
    r = asyncio.run(_catalogue(repondre).rechercher("chat", [{"nom": "chat:1b"}]))
    assert [m["depot"] for m in r["huggingface"]] == ["a/chat-GGUF", "a/protege-GGUF"]
    assert r["huggingface"][1]["protege"] and r["installes"] == [{"nom": "chat:1b"}]


def test_recherche_hors_ligne_ne_reessaie_pas_a_chaque_touche():
    appels = []

    def repondre(requete):
        appels.append(requete)
        raise httpx.ConnectError("pas de réseau")
    c = _catalogue(repondre)
    r1 = asyncio.run(c.rechercher("qwen", []))
    r2 = asyncio.run(c.rechercher("qwen2", []))
    assert r1["erreur_hf"] == "hors_ligne" and r1["suggestions"]  # la liste locale reste disponible
    assert r2["erreur_hf"] and len(appels) == 1


def test_recherche_limite_de_requetes_et_cache():
    appels = []

    def repondre(requete):
        appels.append(requete)
        return httpx.Response(429) if requete.url.params["search"] == "trop" else httpx.Response(200, json=[])
    c = _catalogue(repondre)
    assert asyncio.run(c.rechercher("trop", []))["erreur_hf"] == "sature"
    asyncio.run(c.rechercher("llama", []))
    asyncio.run(c.rechercher("llama", []))
    assert len(appels) == 2  # la 2e recherche "llama" vient du cache


def test_un_seul_caractere_ne_lance_pas_hugging_face():
    def repondre(requete):
        raise AssertionError("aucun appel attendu")
    assert asyncio.run(_catalogue(repondre).rechercher("q", []))["huggingface"] == []
