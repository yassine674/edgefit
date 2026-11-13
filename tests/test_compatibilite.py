import pytest

from edgefit.compatibilite import (
    GIO, OCTETS_KV, analyser, budget, cache_kv_octets, candidats, couches_glissantes, estimer,
    marge_securite, nom_variante, piles, explorer, plateforme_correspondante,
)

LLAMA_3B = {"architecture": "llama", "nb_param": 3_212_749_824, "num_layers": 28, "num_kv_heads": 8,
            "head_dim": 128, "context_max": 131072, "sliding_window": None}
GEMMA3_1B = {"architecture": "gemma3", "nb_param": 999_885_952, "num_layers": 26, "num_kv_heads": 1,
             "head_dim": 256, "context_max": 32768, "sliding_window": 512}
POIDS_3B_Q4 = 2_019_377_376  # taille réelle de llama3.2:3b (Q4_K_M) sur le registre Ollama


def test_cache_kv_llama_3b_comme_la_formule_du_cours():
    # 2 (K et V) × 28 couches × 8 têtes KV × 128 × 4096 tokens × 2 octets = 448 Mio
    assert cache_kv_octets(LLAMA_3B, 4096) == 2 * 28 * 8 * 128 * 4096 * 2


def test_cache_kv_quantifie_et_parallele():
    f16 = cache_kv_octets(LLAMA_3B, 4096, "f16")
    assert cache_kv_octets(LLAMA_3B, 4096, "q8_0") == pytest.approx(f16 * 34 / 64)
    assert cache_kv_octets(LLAMA_3B, 4096, "f16", parallele=2) == 2 * f16


def test_gemma3_fenetre_glissante_une_couche_globale_sur_six():
    assert couches_glissantes(GEMMA3_1B) == 26 - 26 // 6
    assert couches_glissantes(LLAMA_3B) == 0
    # au-delà de la fenêtre, seules les 4 couches globales continuent de grandir
    ecart = cache_kv_octets(GEMMA3_1B, 8192) - cache_kv_octets(GEMMA3_1B, 4096)
    assert ecart == 2 * 1 * 256 * 4 * 4096 * 2


def test_autres_architectures_glissantes_comptees_en_entier_par_prudence():
    mistral = {**LLAMA_3B, "architecture": "llama", "sliding_window": 4096}
    assert couches_glissantes(mistral) == 0


def test_marge_absolue_selon_la_carte():
    assert marge_securite("raspberry", 8 * GIO) == 0.5 * GIO
    assert marge_securite("jetson", 8 * GIO) == 1 * GIO
    assert marge_securite("jetson", 64 * GIO) == pytest.approx(6.4 * GIO, rel=1e-6)


def test_budget_compte_les_modeles_que_ollama_peut_decharger():
    b = budget("jetson", 8 * GIO, 3 * GIO, liberable=2 * GIO)
    assert b["utilisable"] == 4 * GIO


def test_verdicts():
    petit = analyser("llama3.2:3b", LLAMA_3B, POIDS_3B_Q4, "Q4_K_M", budget("jetson", 8 * GIO, 6 * GIO), 4096)
    assert petit["verdict"] == "ok"
    apres = analyser("llama3.2:3b", LLAMA_3B, POIDS_3B_Q4, "Q4_K_M",
                     budget("jetson", 8 * GIO, 2 * GIO, liberable=3 * GIO), 4096)
    assert apres["verdict"] == "ok_apres_dechargement"
    gros = analyser("llama3.2:3b", LLAMA_3B, POIDS_3B_Q4, "Q4_K_M", budget("jetson", 8 * GIO, 3 * GIO), 65536)
    assert gros["verdict"] == "trop_gros" and gros["solutions"]["manque_go"] > 0


def test_candidats_fichier_exact_puis_variantes_estimees():
    liste = candidats("llama3.2:3b", LLAMA_3B, POIDS_3B_Q4, "Q4_K_M")
    assert liste[0]["exact"] and liste[0]["poids"] == POIDS_3B_Q4
    formats = [c["format"] for c in liste]
    assert "Q4_K_M" not in formats[1:] and "Q8_0" in formats
    q8 = next(c for c in liste if c["format"] == "Q8_0")
    assert q8["poids"] == pytest.approx(3.4e9, rel=0.05)  # ~3,4 Go réels pour llama3.2:3b-q8_0


def test_nom_des_variantes_seulement_pour_hugging_face():
    assert nom_variante("hf.co/bartowski/X-GGUF:Q8_0", "Q4_K_M") == "hf.co/bartowski/X-GGUF:Q4_K_M"
    assert nom_variante("llama3.2:3b", "Q8_0") is None


def test_piles_tout_rentre_et_sont_differentes():
    b = budget("jetson", 8 * GIO, 4 * GIO)
    combinaisons = explorer("llama3.2:3b", LLAMA_3B, POIDS_3B_Q4, "Q4_K_M", b, 8192)
    assert all(r["estimation"]["total"] <= b["utilisable"] for r in combinaisons)
    resultat = piles(combinaisons, 8192, b, bande_passante=102)
    signatures = {(p["format"], p["num_ctx"], p["type_kv"]) for p in resultat}
    assert len(signatures) == len(resultat) >= 2
    rapide = next(p for p in resultat if p["cle"] == "rapide")
    qualite = next(p for p in resultat if p["cle"] == "qualite")
    assert rapide["debit_max_tokens_s"] > qualite["debit_max_tokens_s"]
    assert rapide["format"] not in ("Q3_K_M", "Q2_K")  # jamais une quantification trop dégradée


def test_rien_ne_rentre_pas_de_pile():
    b = budget("jetson", 2 * GIO, 1 * GIO)
    assert piles(explorer("llama3.2:3b", LLAMA_3B, POIDS_3B_Q4, "Q4_K_M", b, 4096), 4096, b) == []


def test_solutions_contexte_reduit_et_cache_quantifie():
    b = budget("jetson", 8 * GIO, 4 * GIO)
    d = analyser("llama3.2:3b", LLAMA_3B, POIDS_3B_Q4, "Q4_K_M", b, 131072, type_carte="jetson", bureau_actif=True)
    categories = {c["categorie"]: c["pistes"] for c in d["solutions"]["categories"]}
    reduire = categories["Réglage de la requête (immédiat)"][0]
    assert reduire["action"]["type"] == "contexte" and reduire["rentre"]
    kv = categories["Réglage du serveur Ollama (redémarrage)"]
    assert "OLLAMA_KV_CACHE_TYPE=q8_0" in kv[0]["commande"] and kv[1]["gain_go"] > kv[0]["gain_go"]
    assert any("graphique" in p["titre"] for p in categories["Libérer de la mémoire"])


def test_autre_materiel_seulement_plus_grand_que_la_carte():
    b = budget("jetson", int(7.4 * GIO), 4 * GIO)
    d = analyser("llama3.2:3b", LLAMA_3B, POIDS_3B_Q4, "Q4_K_M", b, 131072, type_carte="jetson")
    materiel = next(c["pistes"] for c in d["solutions"]["categories"] if c["categorie"] == "Autre matériel")
    assert all("8GB" not in p["titre"] for p in materiel)


def test_plateforme_reconnue_depuis_le_device_tree():
    assert plateforme_correspondante("NVIDIA Jetson Orin Nano Developer Kit")["name"] == "NVIDIA Jetson Orin Nano 8GB"
    assert plateforme_correspondante("Jetson AGX Orin")["name"] == "NVIDIA Jetson Orin AGX 64GB"
    assert plateforme_correspondante("Raspberry Pi 5 Model B Rev 1.0")["name"] == "Raspberry Pi 5 8GB"
    assert plateforme_correspondante("mon-pc") is None


def test_types_kv_connus():
    assert set(OCTETS_KV) == {"f16", "q8_0", "q4_0"}
    assert estimer(LLAMA_3B, POIDS_3B_Q4, 4096)["total"] > POIDS_3B_Q4


def test_tel_quel_propose_en_premier_quand_ca_rentre():
    b = budget("jetson", 8 * GIO, 6 * GIO)
    resultat = piles(explorer("llama3.2:3b", LLAMA_3B, POIDS_3B_Q4, "Q4_K_M", b, 4096), 4096, b)
    assert resultat[0]["cle"] == "tel_quel"
    assert not resultat[0]["telechargement"] and not resultat[0]["reglage_serveur"]


def test_quantification_trop_forte_signalee():
    b = budget("jetson", 8 * GIO, int(2.9 * GIO))
    resultat = piles(explorer("llama3.2:3b", LLAMA_3B, POIDS_3B_Q4, "Q4_K_M", b, 4096), 4096, b)
    assert all(p["qualite_degradee"] == (p["format"] in ("Q3_K_M", "Q2_K")) for p in resultat)


def test_variantes_coherentes_avec_le_fichier_reel():
    # qwen2.5:0.5b Q4_K_M : 398 Mo réels, bien plus que 494 M × 4,89 bits (embedding en haute précision)
    qwen = {"architecture": "qwen2", "nb_param": 494_032_768, "num_layers": 24, "num_kv_heads": 2,
            "head_dim": 64, "context_max": 32768, "sliding_window": None}
    liste = candidats("qwen2.5:0.5b", qwen, 397_807_936, "Q4_K_M")
    plus_precis = [c for c in liste if c["bits"] > 4.89]
    assert all(c["poids"] > 397_807_936 for c in plus_precis)
