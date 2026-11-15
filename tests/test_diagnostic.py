from edgefit.diagnostic import moteur_vulkan_ollama


def faux_ollama(tmp_path):
    # même disposition que l'installation officielle : <préfixe>/bin/ollama et <préfixe>/lib/ollama/
    exe = tmp_path / "bin" / "ollama"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    (tmp_path / "lib" / "ollama").mkdir(parents=True)
    return exe


def test_moteur_vulkan_absent(tmp_path):
    assert moteur_vulkan_ollama(faux_ollama(tmp_path)) is None


def test_moteur_vulkan_present(tmp_path):
    exe = faux_ollama(tmp_path)
    dossier = tmp_path / "lib" / "ollama" / "vulkan"
    dossier.mkdir()
    (dossier / "libggml-vulkan.so").write_bytes(b"")
    assert moteur_vulkan_ollama(exe) == dossier / "libggml-vulkan.so"
