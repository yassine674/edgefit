# images : générer des images sur la carte, sans internet, avec stable-diffusion.cpp.
#
# Ollama ne sait pas générer d'images sous Linux. stable-diffusion.cpp (leejet, licence MIT) fait
# pour les images ce que llama.cpp fait pour le texte : un programme compact, qui tourne sur le
# processeur, ou sur le GPU par Vulkan (y compris un GPU intégré Intel), CUDA sur Jetson.
#
# Modèle : SD-Turbo (Stability AI), une version « distillée » de Stable Diffusion 2.1 qui fait une
# image en 1 à 4 étapes au lieu de 25 à 50. En GGUF 8 bits : un seul fichier de 1,9 Go.
# Le moteur et le modèle viennent de leurs dépôts officiels, à une version fixée, vérifiés par SHA-256.
#
# Le moteur est lancé directement (jamais par un shell) : la description de l'image est un argument,
# elle ne peut pas devenir une commande.
import asyncio
import hashlib
import json
import os
import platform
import re
import shutil
import struct
import time
import zipfile
import zlib
from pathlib import Path

import httpx
from platformdirs import user_data_dir

VERSION_MOTEUR = "master-1098-b521db4"
_DEPOT_MOTEUR = f"https://github.com/leejet/stable-diffusion.cpp/releases/download/{VERSION_MOTEUR}"
MOTEURS = {  # binaires officiels : Linux x86_64 seulement (Jetson et Raspberry Pi : à compiler, voir README)
    "cpu": {"fichier": "sd-master-b521db4-bin-Linux-Ubuntu-26.04-x86_64.zip", "octets": 29_068_066,
            "sha256": "533473b653c3d56f91fd549f539ab1c6df5d82231dcb36bab0c6a7f1da3766b3"},
    "vulkan": {"fichier": "sd-master-b521db4-bin-Linux-Ubuntu-26.04-x86_64-vulkan.zip", "octets": 41_947_420,
               "sha256": "ae136316b878b675dae4fecb3b10a16bb1d459a1d601aed3a02be52d78302c82"},
}
MODELE = {"nom": "SD-Turbo", "fichier": "sd_turbo-f16-q8_0.gguf", "octets": 2_023_745_376,
          "sha256": "d50be7655f0a554cf8041c145d88b210bd5f3c545423119dee62ae08cae51580",
          "url": "https://huggingface.co/Green-Sky/SD-Turbo-GGUF/resolve/19a31586d02d64a73b4419bc193b3ecfaf38e1f0/sd_turbo-f16-q8_0.gguf",
          "licence": "Stability AI Community License", "memoire_go": 2.6}
TAILLES = (256, 384, 512, 640, 768)
PROMPT_MAX = 1000
DUREE_MAX_S = 900


class ImagesErreur(Exception):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def binaires_disponibles():
    return platform.system() == "Linux" and platform.machine() in ("x86_64", "AMD64")


class Images:
    def __init__(self, dossier=None, transport=None):
        self.dossier = Path(dossier or Path(user_data_dir("edgefit", appauthor=False)) / "images")
        self._transport = transport
        self._verrou = asyncio.Lock()   # une image à la fois : elle occupe tout le processeur
        self._telechargement = False

    # --- état ---------------------------------------------------------------------------------
    def _moteur(self, variante):
        return self.dossier / f"moteur-{variante}" / "sd-cli"

    def moteur(self):
        """(variante, chemin) du moteur à utiliser : EDGEFIT_SD (compilé à la main), sinon Vulkan, sinon processeur."""
        perso = os.environ.get("EDGEFIT_SD")
        if perso and Path(perso).is_file():
            return "perso", Path(perso)
        for v in ("vulkan", "cpu"):
            if self._moteur(v).is_file():
                return v, self._moteur(v)
        return None, None

    def modele(self):
        return self.dossier / MODELE["fichier"]

    def galerie_dossier(self):
        return self.dossier / "galerie"

    def etat(self):
        variante, _ = self.moteur()
        return {"moteur": variante, "binaires": binaires_disponibles(), "version": VERSION_MOTEUR,
                "moteurs": {v: {"installe": self._moteur(v).is_file(), "taille_mo": round(m["octets"] / 1e6)} for v, m in MOTEURS.items()},
                "modele": {"nom": MODELE["nom"], "installe": self.modele().is_file(), "taille_mo": round(MODELE["octets"] / 1e6),
                           "licence": MODELE["licence"], "memoire_go": MODELE["memoire_go"]},
                "en_cours": self._telechargement, "tailles": TAILLES}

    # --- installation -------------------------------------------------------------------------
    async def _telecharger_fichier(self, http, url, final, octets, empreinte, deja, total):
        partiel = final.with_name(final.name + ".part")
        h = hashlib.sha256()
        fait = 0
        async with http.stream("GET", url) as r:
            if r.status_code >= 400:
                raise ImagesErreur(f"le dépôt répond {r.status_code}", "indisponible")
            with open(partiel, "wb") as sortie:
                async for morceau in r.aiter_bytes(1 << 17):
                    sortie.write(morceau)
                    h.update(morceau)
                    fait += len(morceau)
                    yield deja + fait
        if h.hexdigest() != empreinte or fait != octets:
            partiel.unlink(missing_ok=True)
            raise ImagesErreur(f"{final.name} altéré (empreinte SHA-256 différente) : supprimé", "empreinte")
        partiel.replace(final)

    async def installer(self, variante="cpu"):
        """Moteur (variante) + modèle. Événements progression {fait, total} puis fin / erreur."""
        if self._telechargement:
            yield {"type": "erreur", "erreur": "installation déjà en cours", "code": "en_cours"}
            return
        if not binaires_disponibles() and variante != "perso":
            yield {"type": "erreur", "code": "plateforme",
                   "erreur": f"pas de moteur tout prêt pour {platform.machine()} : compilez stable-diffusion.cpp (voir README) et indiquez-le avec EDGEFIT_SD"}
            return
        self._telechargement = True
        m = MOTEURS[variante]
        a_faire = []
        if not self._moteur(variante).is_file():
            a_faire.append(("moteur", f"{_DEPOT_MOTEUR}/{m['fichier']}", self.dossier / m["fichier"], m["octets"], m["sha256"]))
        if not self.modele().is_file():
            a_faire.append(("modele", MODELE["url"], self.modele(), MODELE["octets"], MODELE["sha256"]))
        total = sum(x[3] for x in a_faire)
        deja = 0
        try:
            self.dossier.mkdir(parents=True, exist_ok=True)
            async with httpx.AsyncClient(transport=self._transport, follow_redirects=True, timeout=httpx.Timeout(20, read=120)) as http:
                for quoi, url, final, octets, empreinte in a_faire:
                    dernier = 0
                    async for fait in self._telecharger_fichier(http, url, final, octets, empreinte, deja, total):
                        if fait - dernier > 4_000_000 or fait == deja + octets:
                            dernier = fait
                            yield {"type": "progression", "quoi": quoi, "fait": fait, "total": total}
                    deja += octets
                    if quoi == "moteur":
                        await asyncio.to_thread(self._extraire, final, variante)
            yield {"type": "fin"}
        except ImagesErreur as e:
            yield {"type": "erreur", "erreur": str(e), "code": e.code}
        except httpx.HTTPError:
            yield {"type": "erreur", "erreur": "pas d'accès à github.com ou huggingface.co", "code": "hors_ligne"}
        finally:
            self._telechargement = False

    def _extraire(self, archive, variante):
        cible = self.dossier / f"moteur-{variante}"
        temporaire = cible.with_name(cible.name + ".tmp")
        shutil.rmtree(temporaire, ignore_errors=True)
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                nom = Path(info.filename).name  # archive à plat : on refuse tout chemin (../, /...)
                if not nom or info.is_dir() or nom != info.filename:
                    continue
                z.extract(info, temporaire)
        if not (temporaire / "sd-cli").is_file():
            shutil.rmtree(temporaire, ignore_errors=True)
            raise ImagesErreur("archive du moteur sans sd-cli", "archive")
        (temporaire / "sd-cli").chmod(0o755)
        shutil.rmtree(cible, ignore_errors=True)
        temporaire.rename(cible)
        archive.unlink(missing_ok=True)

    # --- génération ---------------------------------------------------------------------------
    async def generer(self, prompt, negatif="", largeur=512, hauteur=512, etapes=1, graine=None):
        """Événements : debut, etape {n, total}, fin {fichier, duree_s, ...} ou erreur."""
        variante, chemin = self.moteur()
        if chemin is None or not self.modele().is_file():
            yield {"type": "erreur", "erreur": "moteur d'images non installé", "code": "absent"}
            return
        if self._verrou.locked():
            yield {"type": "erreur", "erreur": "une image est déjà en cours", "code": "occupe"}
            return
        async with self._verrou:
            graine = graine if graine is not None else int.from_bytes(os.urandom(3), "big")
            galerie = self.galerie_dossier()
            galerie.mkdir(parents=True, exist_ok=True)
            nom = f"{time.strftime('%Y%m%d-%H%M%S')}-{graine}.png"
            sortie = galerie / nom
            args = [str(chemin), "--model", str(self.modele()), "--prompt", prompt, "--width", str(largeur),
                    "--height", str(hauteur), "--steps", str(etapes), "--cfg-scale", "1.0", "--sampling-method", "euler_a",
                    "--seed", str(graine), "--threads", str(max(1, (os.cpu_count() or 2) // 2)), "--output", str(sortie)]
            if negatif:
                args[5:5] = ["--negative-prompt", negatif]
            debut = time.perf_counter()
            yield {"type": "debut", "moteur": variante, "graine": graine}
            proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                        env={**os.environ, "LD_LIBRARY_PATH": str(chemin.parent)})
            journal = []
            try:
                async with asyncio.timeout(DUREE_MAX_S):
                    tampon = b""
                    while chunk := await proc.stdout.read(512):
                        tampon += chunk
                        # la barre de progression du moteur se réécrit avec \r : « |====| 1/4 - 3.20s/it »
                        *lignes, tampon = re.split(rb"[\r\n]", tampon)
                        for ligne in lignes:
                            texte = ligne.decode(errors="replace").strip()
                            if not texte:
                                continue
                            journal.append(texte)
                            if m := re.search(r"\|\s*(\d+)/(\d+)\s*-", texte):
                                yield {"type": "etape", "n": int(m.group(1)), "total": int(m.group(2))}
                    code = await proc.wait()
            except TimeoutError:
                proc.kill()
                yield {"type": "erreur", "erreur": f"plus de {DUREE_MAX_S} s : arrêté", "code": "delai"}
                return
            except asyncio.CancelledError:  # page fermée : on n'abandonne pas un processus qui tourne
                proc.kill()
                raise
            if code != 0 or not sortie.is_file():
                yield {"type": "erreur", "erreur": "le moteur a échoué : " + " / ".join(journal[-3:])[:400], "code": "moteur"}
                return
            infos = {"fichier": nom, "prompt": prompt, "negatif": negatif, "largeur": largeur, "hauteur": hauteur,
                     "etapes": etapes, "graine": graine, "moteur": variante, "duree_s": time.perf_counter() - debut,
                     "cree": time.time()}
            sortie.with_suffix(".json").write_text(json.dumps(infos, ensure_ascii=False))
            yield {"type": "fin", **infos}

    def galerie(self, limite=60):
        d = self.galerie_dossier()
        if not d.is_dir():
            return []
        res = []
        for j in sorted(d.glob("*.json"), reverse=True)[:limite]:
            try:
                infos = json.loads(j.read_text())
            except (OSError, ValueError):
                continue
            if (d / infos.get("fichier", "")).is_file():
                res.append(infos)
        return res

    def chemin_image(self, nom):
        if not re.fullmatch(r"[\w.-]+\.png", nom or ""):
            return None
        p = self.galerie_dossier() / nom
        return p if p.is_file() else None

    def supprimer(self, nom):
        p = self.chemin_image(nom)
        if p is None:
            return False
        p.unlink()
        p.with_suffix(".json").unlink(missing_ok=True)
        return True


def png_degrade(largeur, hauteur, graine):
    """Image simulée (--fake, tests) : un dégradé qui dépend de la graine."""
    r0, g0, b0 = (graine * 37) % 256, (graine * 91) % 256, (graine * 53) % 256
    lignes = b"".join(b"\x00" + bytes(v for x in range(largeur) for v in
                                       ((r0 + x * 255 // largeur) % 256, (g0 + y * 255 // hauteur) % 256, b0))
                      for y in range(hauteur))

    def bloc(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + bloc(b"IHDR", struct.pack(">IIBBBBB", largeur, hauteur, 8, 2, 0, 0, 0))
            + bloc(b"IDAT", zlib.compress(lignes, 6)) + bloc(b"IEND", b""))


class ImagesFactice(Images):
    """--fake et tests : installation instantanée, des dégradés au lieu d'images, sans moteur."""

    def __init__(self, dossier, pause_s=0.15):
        super().__init__(dossier)
        self._pause = pause_s
        self._installe = False

    def moteur(self):
        return ("cpu", Path("sd-cli-simule")) if self._installe else (None, None)

    def etat(self):
        e = super().etat()
        e["binaires"] = True
        e["moteurs"]["cpu"]["installe"] = self._installe
        e["modele"]["installe"] = self._installe
        return e

    def modele(self):
        return Path(__file__) if self._installe else Path("/absent")

    async def installer(self, variante="cpu"):
        total = MOTEURS[variante]["octets"] + MODELE["octets"]
        for i in range(1, 6):
            await asyncio.sleep(self._pause / 3)
            yield {"type": "progression", "quoi": "modele", "fait": total * i // 5, "total": total}
        self._installe = True
        yield {"type": "fin"}

    async def generer(self, prompt, negatif="", largeur=512, hauteur=512, etapes=1, graine=None):
        if not self._installe:
            yield {"type": "erreur", "erreur": "moteur d'images non installé", "code": "absent"}
            return
        graine = graine if graine is not None else int.from_bytes(os.urandom(3), "big")
        debut = time.perf_counter()
        yield {"type": "debut", "moteur": "cpu", "graine": graine}
        for n in range(1, etapes + 1):
            await asyncio.sleep(self._pause)
            yield {"type": "etape", "n": n, "total": etapes}
        galerie = self.galerie_dossier()
        galerie.mkdir(parents=True, exist_ok=True)
        nom = f"{time.strftime('%Y%m%d-%H%M%S')}-{graine}.png"
        (galerie / nom).write_bytes(png_degrade(min(largeur, 256), min(hauteur, 256), graine))
        infos = {"fichier": nom, "prompt": prompt, "negatif": negatif, "largeur": largeur, "hauteur": hauteur, "etapes": etapes,
                 "graine": graine, "moteur": "cpu", "duree_s": time.perf_counter() - debut, "cree": time.time()}
        (galerie / nom).with_suffix(".json").write_text(json.dumps(infos, ensure_ascii=False))
        yield {"type": "fin", **infos}
