from edgefit.metriques import FournisseurFactice, FournisseurGenerique
from edgefit.metriques.jetson import analyser_ligne_tegrastats
from edgefit.metriques.raspberry import alertes_depuis_registre

# lignes au format documenté de tegrastats (non capturées sur une vraie carte)
LIGNE_ORIN = (
    "11-13-2026 10:00:00 RAM 2263/7620MB (lfb 3x4MB) SWAP 0/3810MB (cached 0MB) "
    "CPU [2%@729,0%@729,1%@729,0%@729,off,off] EMC_FREQ 0%@2133 GR3D_FREQ 37%@[305] "
    "NVDEC off NVJPG off VIC off OFA off APE 174 cpu@46.875C soc2@44.468C soc0@45.031C "
    "gpu@45.093C tj@46.875C soc1@44.312C VDD_IN 4296mW/4296mW VDD_CPU_GPU_CV 494mW/494mW "
    "VDD_SOC 1295mW/1295mW"
)
LIGNE_NANO = (
    "RAM 1543/3964MB (lfb 104x4MB) SWAP 0/1982MB (cached 0MB) IRAM 0/252kB(lfb 252kB) "
    "CPU [3%@1479,1%@1479,0%@1479,1%@1479] EMC_FREQ 0%@1600 GR3D_FREQ 12%@76 APE 25 "
    "PLL@25C CPU@28.5C PMIC@100C GPU@27C AO@34.5C thermal@27.75C "
    "POM_5V_IN 2276/2276 POM_5V_GPU 0/0 POM_5V_CPU 357/357"
)


def test_tegrastats_orin():
    m = analyser_ligne_tegrastats(LIGNE_ORIN)
    assert m == {"gpu_pct": 37.0, "temp_cpu_c": 46.875, "temp_gpu_c": 45.093, "puissance_w": 4.296}


def test_tegrastats_nano():
    m = analyser_ligne_tegrastats(LIGNE_NANO)
    assert m == {"gpu_pct": 12.0, "temp_cpu_c": 28.5, "temp_gpu_c": 27.0, "puissance_w": 2.276}


def test_tegrastats_capteur_eteint_et_ligne_vide():
    assert analyser_ligne_tegrastats("GPU@-256C")["temp_gpu_c"] is None
    assert analyser_ligne_tegrastats("") == {"gpu_pct": None, "temp_cpu_c": None, "temp_gpu_c": None, "puissance_w": None}


def test_alertes_raspberry():
    assert alertes_depuis_registre(0) == []
    # 0x50005 : sous-tension (bit 0) et bridage (bit 2) en ce moment, + bits « déjà arrivé » (16, 18) ignorés
    assert alertes_depuis_registre(0x50005) == ["sous-tension : alimentation insuffisante", "CPU bridé en ce moment"]


def test_fournisseurs_meme_interface():
    cles = FournisseurGenerique("test").lire().keys()
    assert FournisseurFactice().lire().keys() == cles


def test_diagnostic_lit_versions_jetson():
    from edgefit.diagnostic import mode_energie, version_l4t
    texte = "# R36 (release), REVISION: 4.3, GCID: 38968081, BOARD: generic, EABI: aarch64, DATE: Wed Jan  8 01:49:37 UTC 2025"
    assert version_l4t(texte) == ("36", "4.3")
    assert version_l4t("") is None
    assert mode_energie("NV Power Mode: MAXN_SUPER\n2") == "MAXN_SUPER"
    assert mode_energie(None) is None
