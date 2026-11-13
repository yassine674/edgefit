import asyncio
import json

import httpx
import pytest

from edgefit.ollama import (
    ClientOllama, ModeleInconnu, OllamaIndisponible,
    architecture_depuis_model_info, metriques_inference, nom_valide,
)

# extrait de /api/show pour qwen2.5:0.5b (métadonnées GGUF)
INFO_QWEN = {
    "general.architecture": "qwen2", "general.parameter_count": 494032768,
    "qwen2.block_count": 24, "qwen2.attention.head_count": 14, "qwen2.attention.head_count_kv": 2,
    "qwen2.embedding_length": 896, "qwen2.context_length": 32768,
}

FLUX_CHAT = [
    {"message": {"role": "assistant", "content": "Bon"}, "done": False},
    {"message": {"role": "assistant", "content": "jour"}, "done": False},
    {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
     "load_duration": 2_000_000_000, "prompt_eval_count": 30, "prompt_eval_duration": 500_000_000,
     "eval_count": 2, "eval_duration": 100_000_000},
]

FLUX_PULL = [
    {"status": "pulling manifest"},
    {"status": "pulling abc123", "digest": "sha256:abc123", "total": 1000, "completed": 500},
    {"status": "success"},
]


def ndjson(lignes):
    return "".join(json.dumps(l) + "\n" for l in lignes)


def faux_ollama(requete: httpx.Request):
    chemin = requete.url.path
    if chemin == "/api/version":
        return httpx.Response(200, json={"version": "0.12.0"})
    if chemin == "/api/tags":
        return httpx.Response(200, json={"models": [{"name": "qwen2.5:0.5b", "size": 397_821_319, "details": {
            "family": "qwen2", "parameter_size": "494.03M", "quantization_level": "Q4_K_M"}}]})
    corps = json.loads(requete.content)
    if corps.get("model") == "inconnu":
        return httpx.Response(404, json={"error": "model 'inconnu' not found"})
    if chemin == "/api/show":
        return httpx.Response(200, json={"model_info": INFO_QWEN})
    if chemin == "/api/pull":
        if corps["model"].startswith("hf.co/prive"):
            return httpx.Response(200, text=ndjson([{"status": "pulling manifest"},
                                                    {"error": "pull model manifest: 401: Unauthorized"}]))
        return httpx.Response(200, text=ndjson(FLUX_PULL))
    if chemin == "/api/chat":
        return httpx.Response(200, text=ndjson(FLUX_CHAT))
    return httpx.Response(500)


def client():
    return ClientOllama(transport=httpx.MockTransport(faux_ollama))


async def tout(generateur):
    return [evt async for evt in generateur]


def test_version_et_modeles():
    c = client()
    assert asyncio.run(c.version()) == "0.12.0"
    (m,) = asyncio.run(c.modeles())
    assert m["nom"] == "qwen2.5:0.5b" and m["quantification"] == "Q4_K_M"
    assert m["taille_go"] == pytest.approx(0.37, abs=0.01)


def test_architecture_depuis_show():
    a = asyncio.run(client().architecture("qwen2.5:0.5b"))
    # head_dim absent des métadonnées : déduit de embedding_length / head_count = 896 / 14
    assert a == {"architecture": "qwen2", "nb_param": 494032768, "num_layers": 24, "num_kv_heads": 2,
                 "head_dim": 64, "context_max": 32768, "sliding_window": None}


def test_architecture_fenetre_glissante_et_valeurs_par_couche():
    info = {"general.architecture": "gemma2", "gemma2.block_count": 26, "gemma2.attention.head_count": 8,
            "gemma2.attention.head_count_kv": [4] * 26, "gemma2.attention.key_length": 256,
            "gemma2.embedding_length": 2304, "gemma2.attention.sliding_window": 4096}
    a = architecture_depuis_model_info(info)
    assert a["num_kv_heads"] == 4 and a["head_dim"] == 256 and a["sliding_window"] == 4096


def test_modele_inconnu():
    with pytest.raises(ModeleInconnu):
        asyncio.run(client().architecture("inconnu"))


def test_telechargement():
    evts = asyncio.run(tout(client().telecharger("qwen2.5:0.5b")))
    assert [e["type"] for e in evts] == ["progression", "progression", "fin"]
    assert evts[1]["fait"] == 500 and evts[1]["total"] == 1000


def test_telechargement_modele_protege_explique_la_cle_ssh():
    evts = asyncio.run(tout(client().telecharger("hf.co/prive/modele:Q4_K_M")))
    assert evts[-1]["type"] == "erreur" and "id_ed25519.pub" in evts[-1]["erreur"]


def test_chat_tokens_puis_metriques():
    evts = asyncio.run(tout(client().discuter("qwen2.5:0.5b", [{"role": "user", "content": "Salut"}])))
    assert [e["texte"] for e in evts if e["type"] == "token"] == ["Bon", "jour"]
    m = evts[-1]["metriques"]
    assert m["chargement_s"] == 2.0 and m["tokens_generes"] == 2
    assert m["debit_tokens_s"] == pytest.approx(20.0)        # 2 tokens en 0,1 s
    assert m["debit_prefill_tokens_s"] == pytest.approx(60.0)  # 30 tokens en 0,5 s
    assert m["ttft_mesure_s"] is not None


def test_chat_modele_inconnu_donne_une_erreur():
    evts = asyncio.run(tout(client().discuter("inconnu", [{"role": "user", "content": "x"}])))
    assert evts == [{"type": "erreur", "erreur": "model 'inconnu' not found"}]


def test_ollama_eteint():
    def refus(requete):
        raise httpx.ConnectError("connexion refusée")
    c = ClientOllama(transport=httpx.MockTransport(refus))
    with pytest.raises(OllamaIndisponible):
        asyncio.run(c.modeles())
    with pytest.raises(OllamaIndisponible):
        asyncio.run(tout(c.discuter("qwen2.5:0.5b", [{"role": "user", "content": "x"}])))


def test_metriques_sans_premier_token():
    m = metriques_inference({"eval_count": 0, "eval_duration": 0}, 0.0, None, 1.0)
    assert m["ttft_mesure_s"] is None and m["debit_tokens_s"] is None and m["duree_totale_s"] == 1.0


@pytest.mark.parametrize("nom,attendu", [
    ("qwen2.5:0.5b", True), ("hf.co/bartowski/Llama-3.2-1B-Instruct-GGUF:Q4_K_M", True),
    ("", False), ("../etc/passwd", False), ("a b", False), ("x" * 300, False),
])
def test_nom_valide(nom, attendu):
    assert nom_valide(nom) is attendu
