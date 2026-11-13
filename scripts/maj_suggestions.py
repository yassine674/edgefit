"""Met à jour edgefit/donnees/suggestions.json : taille exacte, format, nombre de paramètres et
architecture (couches, têtes KV, contexte...) de chaque modèle suggéré, lus sur le registre
d'Ollama (manifeste, petit fichier de config et premier mégaoctet du fichier GGUF).
Les champs "description" et "tags" s'écrivent à la main.

    .venv/bin/python scripts/maj_suggestions.py

Pour ajouter un modèle : l'ajouter dans edgefit/donnees/suggestions.json avec au moins
"nom" et "description", puis relancer ce script, qui complète le reste.
"""
import asyncio
import json
from pathlib import Path

import httpx

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from edgefit.registre import Registre  # noqa: E402

FICHIER = Path(__file__).resolve().parent.parent / "edgefit" / "donnees" / "suggestions.json"
ACCEPT = {"Accept": "application/vnd.docker.distribution.manifest.v2+json"}


async def completer(client, entree):
    nom, tag = entree["nom"].split(":")
    base = f"https://registry.ollama.ai/v2/library/{nom}"
    manifeste = (await client.get(f"{base}/manifests/{tag}", headers=ACCEPT)).json()
    config = (await client.get(f"{base}/blobs/{manifeste['config']['digest']}")).json()
    couches = {}
    for couche in manifeste["layers"]:
        type_couche = couche["mediaType"].rsplit(".", 1)[-1]  # model, projector, template...
        couches[type_couche] = couches.get(type_couche, 0) + couche["size"]
    entree.update({
        "taille_octets": couches["model"], "projecteur_octets": couches.get("projector", 0),
        "format": config.get("file_type"), "famille": config.get("model_family"),
        "parametres": config.get("model_type"), "vision": couches.get("projector", 0) > 0,
    })


CHAMPS_ARCHI = ("architecture", "nb_param", "num_layers", "num_kv_heads", "head_dim", "context_max", "sliding_window")


async def architecture(registre, entree):
    arch = (await registre.infos(entree["nom"]))["architecture"]
    entree["architecture"] = {k: arch.get(k) for k in CHAMPS_ARCHI}


async def main():
    entrees = json.loads(FICHIER.read_text())
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        await asyncio.gather(*(completer(client, e) for e in entrees))
    registre = Registre()
    await asyncio.gather(*(architecture(registre, e) for e in entrees))
    FICHIER.write_text(json.dumps(entrees, indent=1, ensure_ascii=False) + "\n")
    print(f"{len(entrees)} modèles mis à jour dans {FICHIER}")


if __name__ == "__main__":
    asyncio.run(main())
