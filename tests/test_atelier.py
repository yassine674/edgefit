# atelier : options chiffrées par catégorie, puis création d'un modèle qui garde les réglages
import json

import pytest
from fastapi.testclient import TestClient

from edgefit import atelier
from edgefit.catalogue import CatalogueFactice
from edgefit.metriques import FournisseurFactice
from edgefit.ollama_factice import OllamaFactice
from edgefit.registre import RegistreFactice
from edgefit.securite import NOM_COOKIE
from edgefit.serveur import creer_app


@pytest.fixture
def cas():
    ollama = OllamaFactice(pause_s=0)
    c = TestClient(creer_app(FournisseurFactice(), ollama, "j", RegistreFactice(), CatalogueFactice(), intervalle_s=0.01))
    c.cookies.set(NOM_COOKIE, "j")
    return c, ollama


def lignes(r):
    return [json.loads(x) for x in r.text.splitlines() if x.strip()]


def test_parametres_valides_convertis_et_bornes():
    assert atelier.valider_parametres({"num_ctx": "8192", "temperature": 0.2, "top_k": None}) == {"num_ctx": 8192, "temperature": 0.2}
    for mauvais in ({"commande": 1}, {"temperature": 9}, {"num_ctx": "beaucoup"}, {"num_gpu": -1}):
        with pytest.raises(atelier.ErreurAtelier):
            atelier.valider_parametres(mauvais)


def test_etude_chiffre_chaque_option(cas):
    c, _ = cas
    e = c.post("/api/atelier", json={"modele": "llama3.2:3b", "num_ctx": 8192, "type_kv": "f16"}).json()
    cats = e["categories"]
    assert set(cats) == {"poids", "cache_kv", "contexte", "materiel"} and e["couches"] == 28
    choisi = next(o for o in cats["poids"] if o["choisi"])
    assert choisi["format"] == "Q4_K_M" and choisi["exact"] and choisi["gain_go"] == 0
    # moins de bits : toujours moins de mémoire
    totaux = [o["total_go"] for o in cats["poids"]]
    assert totaux == sorted(totaux, reverse=True)
    kv = {o["valeur"]: o for o in cats["cache_kv"]}
    assert kv["q4_0"]["total_go"] < kv["q8_0"]["total_go"] < kv["f16"]["total_go"] and kv["q8_0"]["gain_go"] > 0
    assert kv["q8_0"]["commande"] and kv["f16"]["commande"] is None
    ctx = {o["valeur"]: o for o in cats["contexte"]}
    assert ctx[2048]["total_go"] < ctx[8192]["total_go"] and ctx[8192]["choisi"]
    assert any(p["compatible"] for p in cats["materiel"]) and e["vitesse_attendue"] > 0
    # le même modèle avec le cache en q4_0 : l'estimation suit le choix
    e2 = c.post("/api/atelier", json={"modele": "llama3.2:3b", "num_ctx": 8192, "type_kv": "q4_0"}).json()
    assert e2["estimation_go"]["total"] == pytest.approx(kv["q4_0"]["total_go"])


def test_creation_garde_les_reglages(cas):
    c, ollama = cas
    evts = lignes(c.post("/api/modeles/creer", json={"nom": "assistant-fr", "base": "qwen2.5:0.5b", "systeme": "Réponds en français.",
                                                     "parametres": {"num_ctx": 8192, "temperature": 0.2, "num_gpu": 12}}))
    assert evts[-1] == {"type": "fin", "nom": "assistant-fr"} and all(e["phase"] == "creation" for e in evts[:-1])
    assert ollama.crees["assistant-fr"] == {"base": "qwen2.5:0.5b", "systeme": "Réponds en français.", "quantifier": None,
                                            "parametres": {"num_ctx": 8192, "temperature": 0.2, "num_gpu": 12}}
    assert "assistant-fr" in [m["nom"] for m in c.get("/api/modeles").json()]
    # le modèle créé s'étudie et discute comme les autres (même architecture que sa base)
    assert c.post("/api/atelier", json={"modele": "assistant-fr"}).json()["couches"] == 24
    assert lignes(c.post("/api/chat", json={"modele": "assistant-fr", "messages": [{"role": "user", "content": "Bonjour"}]}))[-1]["type"] == "fin"


def test_creation_telecharge_la_base_absente(cas):
    c, ollama = cas
    evts = lignes(c.post("/api/modeles/creer", json={"nom": "gemma-perso", "base": "gemma3:1b", "parametres": {"num_ctx": 2048}}))
    phases = [e.get("phase") for e in evts[:-1]]
    assert phases.index("telechargement") < phases.index("creation") and evts[-1]["type"] == "fin"
    assert {"gemma3:1b", "gemma-perso"} <= {m["nom"] for m in c.get("/api/modeles").json()}


def test_creation_refusee(cas):
    c, _ = cas
    creer = lambda **k: c.post("/api/modeles/creer", json={"nom": "x", "base": "qwen2.5:0.5b", **k})  # noqa: E731
    assert creer(parametres={"shell": 1}).status_code == 422          # paramètre hors liste
    assert creer(parametres={"temperature": 5}).status_code == 422    # hors bornes
    assert creer(nom="qwen2.5:0.5b").status_code == 422               # même nom que la base
    assert creer(quantifier="q1").status_code == 422                  # quantification inconnue
    assert creer(nom="../x").status_code == 422
    assert creer(nom="llama3.2:3b").status_code == 409                # existe déjà
    assert lignes(creer(nom="llama3.2:3b", remplacer=True))[-1]["type"] == "fin"
    # Ollama ne recompresse que du F16 : son refus arrive comme un événement lisible
    assert lignes(creer(nom="y", quantifier="q4_K_M"))[-1]["type"] == "erreur"


def test_vrais_noms_des_autres_quantifications(cas):
    from edgefit.registre import tags_par_format
    tags = ["3b", "3b-instruct-q8_0", "3b-text-q8_0", "3b-instruct-fp16", "1b-instruct-q8_0", "latest"]
    assert tags_par_format("llama3.2:3b", tags) == {"Q8_0": "llama3.2:3b-instruct-q8_0", "F16": "llama3.2:3b-instruct-fp16"}
    assert tags_par_format("llama3.2", tags) == {} and tags_par_format("mon-modele:latest", tags) == {}
    c, _ = cas
    poids = {o["format"]: o for o in c.post("/api/atelier", json={"modele": "llama3.2:3b"}).json()["categories"]["poids"]}
    assert poids["Q8_0"]["nom"] == "llama3.2:3b-instruct-q8_0" and poids["Q6_K"]["nom"] is None  # non publiée : pas cliquable
    # choisir cette version : taille exacte lue dans le registre, plus grosse que le Q4_K_M
    e = c.post("/api/atelier", json={"modele": "llama3.2:3b-instruct-q8_0"}).json()
    assert e["format"] == "Q8_0" and e["source"] == "registre" and e["estimation_go"]["poids"] > poids["Q4_K_M"]["fichier_go"]


def test_gpu_detecte_seulement_s_il_existe(cas):
    from edgefit.metriques.generique import FournisseurGenerique
    c, _ = cas
    assert c.post("/api/atelier", json={"modele": "qwen2.5:0.5b"}).json()["gpu_detecte"] is True  # Jetson simulée
    pc = FournisseurGenerique("pc-test")
    pc._nvidia = None  # PC sans carte NVIDIA (GPU intégré Intel) : Ollama tourne sur le processeur
    c2 = TestClient(creer_app(pc, OllamaFactice(pause_s=0), "j", RegistreFactice(), CatalogueFactice()))
    c2.cookies.set(NOM_COOKIE, "j")
    assert c2.post("/api/atelier", json={"modele": "qwen2.5:0.5b"}).json()["gpu_detecte"] is False


def test_le_chat_garde_le_modele_charge():
    import asyncio
    import httpx
    from edgefit.ollama import ClientOllama
    recus = []

    def repondre(requete):
        recus.append(json.loads(requete.content))
        return httpx.Response(200, text=json.dumps({"done": True, "message": {"content": ""}, "eval_count": 1, "eval_duration": 1}) + "\n")

    async def scenario():
        client = ClientOllama(transport=httpx.MockTransport(repondre))
        return [e async for e in client.discuter("m", [{"role": "user", "content": "x"}], {}, None, "30m")]
    asyncio.run(scenario())
    assert recus[0]["keep_alive"] == "30m"


def test_nom_exact_d_apres_l_empreinte():
    import asyncio
    import httpx
    from edgefit.registre import Registre, tags_de_la_page
    page = ('<a>qwen:latest</a> <span>d53d04290064</span> • 2.3GB • 32K <a>qwen:4b-chat</a> <span>d53d04290064</span> • 2.3GB '
            '<a>qwen:4b-chat-v1.5-q4_0</a> <span>d53d04290064</span> • 2.3GB <a>qwen:7b</a> <span>2091ee8c8d8f</span> • 4.5GB')
    assert tags_de_la_page("qwen", page)[:2] == [("latest", "d53d04290064"), ("4b-chat", "d53d04290064")]
    r = Registre(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=page)))
    empreinte = "d53d04290064" + "0" * 52
    assert asyncio.run(r.nom_exact("qwen:latest", empreinte)) == "qwen:4b-chat-v1.5-q4_0"
    assert asyncio.run(r.nom_exact("qwen:4b-chat-v1.5-q4_0", empreinte)) is None   # déjà exact
    assert asyncio.run(r.nom_exact("mon-modele:latest", "abc")) is None
    hors_ligne = Registre(transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(httpx.ConnectError("x"))))
    assert asyncio.run(hors_ligne.nom_exact("qwen:latest", empreinte)) is None


def test_liste_des_modeles_avec_nom_exact(cas):
    c, ollama = cas
    ollama._installes.append({"nom": "qwen:latest", "taille_go": 2.1, "famille": "qwen2", "parametres": "4B", "quantification": "Q4_0"})
    noms = {m["nom"]: m["nom_exact"] for m in c.get("/api/modeles").json()}
    assert noms["qwen:latest"] == "qwen:4b-chat-v1.5-q4_0" and noms["qwen2.5:0.5b"] is None


def test_creation_garde_l_identite_et_la_recette(cas):
    c, _ = cas
    from edgefit import stockage
    stockage.supprimer_profil("llama3.2:3b")  # la base de test est commune : un autre test a pu créer ce nom
    avatar = "data:image/webp;base64,UklGRg=="
    r = c.post("/api/modeles/creer", json={"nom": "atlas", "base": "llama3.2:3b", "parametres": {"num_ctx": 2048, "num_keep": 256},
                                           "avatar": avatar, "couleur": "#c6f24e", "description": "Tuteur de maths", "type_kv": "q8_0"})
    assert lignes(r)[-1]["type"] == "fin"
    m = next(x for x in c.get("/api/modeles").json() if x["nom"].startswith("atlas"))
    assert m["profil"]["avatar"] == avatar and m["profil"]["description"] == "Tuteur de maths" and m["profil"]["base"] == "llama3.2:3b"
    assert m["profil"]["recette"]["parametres"] == {"num_ctx": 2048, "num_keep": 256} and m["profil"]["recette"]["type_kv"] == "q8_0"
    # un modèle sans profil n'en invente pas ; un avatar qui n'est pas une image « data: » est refusé
    assert next(x for x in c.get("/api/modeles").json() if x["nom"] == "llama3.2:3b")["profil"] is None
    assert c.post("/api/modeles/profil", json={"nom": "llama3.2:3b", "avatar": "https://exemple.org/a.png"}).status_code == 422
    assert c.post("/api/modeles/profil", json={"nom": "llama3.2:3b", "avatar": avatar, "description": "Le généraliste"}).status_code == 200
    assert c.post("/api/modeles/profil", json={"nom": "absent:1b"}).status_code == 404
    # supprimer le modèle supprime son profil
    assert c.post("/api/modeles/supprimer", json={"nom": m["nom"]}).status_code == 200
    assert m["nom"] not in stockage.lire_profils()


def test_atelier_donne_les_coeurs_et_accepte_num_keep(cas):
    c, _ = cas
    e = c.post("/api/atelier", json={"modele": "llama3.2:3b"}).json()
    assert 1 <= e["coeurs"]["physiques"] <= e["coeurs"]["logiques"]
    assert atelier.valider_parametres({"num_keep": "512"}) == {"num_keep": 512}
