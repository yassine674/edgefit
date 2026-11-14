# voix : les réponses lues à voix haute par Piper, sur la carte, sans internet.
#
# Piper est une synthèse vocale « neuronale » légère : un petit réseau (≈ 60 Mo, format ONNX)
# transforme le texte en son, sur le processeur (pas besoin du GPU, qui reste au modèle de
# langue). Sur un PC il produit ≈ 18 s de parole par seconde de calcul ; une carte ARM est
# plusieurs fois plus lente, mais toujours plus rapide que la parole.
#
# Piper est optionnel (pip install "edgefit[voix]") : sans lui, l'appli marche, sans la voix.
# Les voix ne viennent que de la liste ci-dessous, depuis le dépôt officiel de Piper, et chaque
# fichier est vérifié par son empreinte SHA-256 avant d'être utilisé.
import asyncio
import hashlib
import io
import math
import re
import struct
import threading
import wave
from pathlib import Path

import httpx
from platformdirs import user_data_dir

DEPOT = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
VOIX = {
    "fr": {"nom": "fr_FR-siwis-medium", "chemin": "fr/fr_FR/siwis/medium", "licence": "CC-BY 4.0",
           "fichiers": {".onnx": (63_221_984, "b1d946fd68d0347155fa59bce0a018924abbef3c1561181db45ca463d83963ef"),
                        ".onnx.json": (5_458, "df814aa3dfedd00510a07144442cc0a220ff21e43d03c3b078444fc0be68fc37")}},
    "en": {"nom": "en_US-lessac-medium", "chemin": "en/en_US/lessac/medium", "licence": "Blizzard 2011",
           "fichiers": {".onnx": (63_201_294, "5efe09e69902187827af646e1a6e9d269dee769f9877d17b16b1b46eeaaf019f"),
                        ".onnx.json": (4_885, "efe19c417bed055f2d69908248c6ba650fa135bc868b0e6abb3da181dab690a0")}},
}
LANGUES = tuple(VOIX)
TEXTE_MAX = 1000  # caractères par demande : la page découpe les réponses en phrases


class VoixErreur(Exception):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def moteur_present():
    try:
        import piper  # noqa: F401
        return True
    except ImportError:
        return False


def nettoyer(texte):
    """Ce qu'une voix doit lire : sans blocs de code, sans balises Markdown, sans adresses web."""
    texte = re.sub(r"```.*?(```|$)", " ", texte, flags=re.S)       # blocs de code (même non fermés)
    texte = re.sub(r"`([^`]*)`", r"\1", texte)                      # code en ligne : on garde le mot
    texte = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", texte)        # liens Markdown : le texte seul
    texte = re.sub(r"https?://\S+", " ", texte)
    texte = re.sub(r"^\s{0,3}(#{1,6}|[-*+>]|\d+[.)])\s+", "", texte, flags=re.M)  # titres, listes, citations
    texte = re.sub(r"[*_~|#]+", " ", texte)
    return re.sub(r"\s+", " ", texte).strip()


def taille_mo(langue):
    return sum(o for o, _ in VOIX[langue]["fichiers"].values()) / 1e6


class Voix:
    def __init__(self, dossier=None, transport=None):
        self.dossier = Path(dossier or Path(user_data_dir("edgefit", appauthor=False)) / "voix")
        self._transport = transport
        self._chargees = {}
        self._verrou = threading.Lock()  # une synthèse à la fois : Piper occupe déjà tous les cœurs
        self._telechargements = set()

    def _fichier(self, langue, ext):
        return self.dossier / f"{VOIX[langue]['nom']}{ext}"

    def installee(self, langue):
        return all(self._fichier(langue, ext).exists() for ext in VOIX[langue]["fichiers"])

    def etat(self):
        return {"moteur": moteur_present(), "voix": {
            l: {"nom": v["nom"], "licence": v["licence"], "installee": self.installee(l), "taille_mo": round(taille_mo(l)),
                "en_cours": l in self._telechargements} for l, v in VOIX.items()}}

    async def telecharger(self, langue):
        """Événements {"type": "progression", "fait", "total"} puis {"type": "fin"} ou {"type": "erreur"}."""
        if langue in self._telechargements:
            yield {"type": "erreur", "erreur": "téléchargement déjà en cours", "code": "en_cours"}
            return
        self._telechargements.add(langue)
        v = VOIX[langue]
        total = sum(o for o, _ in v["fichiers"].values())
        fait = 0
        try:
            self.dossier.mkdir(parents=True, exist_ok=True)
            async with httpx.AsyncClient(transport=self._transport, follow_redirects=True,
                                         timeout=httpx.Timeout(15, read=60)) as http:
                # le petit fichier de réglages d'abord, le gros réseau ensuite
                for ext in sorted(v["fichiers"], key=lambda e: v["fichiers"][e][0]):
                    octets, empreinte = v["fichiers"][ext]
                    final = self._fichier(langue, ext)
                    if final.exists():
                        fait += octets
                        continue
                    partiel = final.with_name(final.name + ".part")
                    h = hashlib.sha256()
                    async with http.stream("GET", f"{DEPOT}/{v['chemin']}/{v['nom']}{ext}") as r:
                        if r.status_code >= 400:
                            raise VoixErreur(f"le dépôt des voix répond {r.status_code}", "indisponible")
                        with open(partiel, "wb") as sortie:
                            async for morceau in r.aiter_bytes(1 << 16):
                                sortie.write(morceau)
                                h.update(morceau)
                                fait += len(morceau)
                                yield {"type": "progression", "fait": fait, "total": total}
                    if h.hexdigest() != empreinte:
                        partiel.unlink(missing_ok=True)
                        raise VoixErreur(f"fichier {final.name} altéré (empreinte SHA-256 différente) : supprimé", "empreinte")
                    partiel.replace(final)
            yield {"type": "fin"}
        except VoixErreur as e:
            yield {"type": "erreur", "erreur": str(e), "code": e.code}
        except httpx.HTTPError:
            yield {"type": "erreur", "erreur": "pas d'accès à huggingface.co", "code": "hors_ligne"}
        finally:
            self._telechargements.discard(langue)

    def _voix(self, langue):
        if langue not in self._chargees:
            from piper import PiperVoice
            self._chargees[langue] = PiperVoice.load(self._fichier(langue, ".onnx"))
        return self._chargees[langue]

    def _synthese(self, texte, langue):
        with self._verrou:
            voix = self._voix(langue)
            tampon = io.BytesIO()
            with wave.open(tampon, "wb") as w:
                voix.synthesize_wav(texte, w)
            return tampon.getvalue()

    async def lire(self, texte, langue):
        """Fichier WAV de `texte` lu par la voix de `langue`."""
        if not moteur_present():
            raise VoixErreur("Piper n'est pas installé", "moteur_absent")
        if not self.installee(langue):
            raise VoixErreur(f"voix {VOIX[langue]['nom']} non téléchargée", "voix_absente")
        texte = nettoyer(texte)
        if not texte:
            raise VoixErreur("rien à lire", "vide")
        return await asyncio.to_thread(self._synthese, texte, langue)


def wav_bip(duree_s, frequence=440, taux=16000):
    """Son court et doux (mode --fake) : la page joue un vrai fichier WAV, sans Piper."""
    n = int(duree_s * taux)
    echantillons = (int(3000 * math.sin(2 * math.pi * frequence * i / taux) * min(1, i / 400, (n - i) / 400))
                    for i in range(n))
    tampon = io.BytesIO()
    with wave.open(tampon, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(taux)
        w.writeframes(b"".join(struct.pack("<h", e) for e in echantillons))
    return tampon.getvalue()


class VoixFactice:
    """Voix simulée (--fake, tests) : même interface, des bips au lieu de la parole."""

    def __init__(self, moteur=True):
        self._moteur = moteur
        self._installees = set()
        self.lus = []

    def etat(self):
        return {"moteur": self._moteur, "voix": {
            l: {"nom": v["nom"], "licence": v["licence"], "installee": l in self._installees, "taille_mo": round(taille_mo(l)),
                "en_cours": False} for l, v in VOIX.items()}}

    def installee(self, langue):
        return langue in self._installees

    async def telecharger(self, langue):
        total = sum(o for o, _ in VOIX[langue]["fichiers"].values())
        for i in range(1, 11):
            await asyncio.sleep(0.02)
            yield {"type": "progression", "fait": total * i // 10, "total": total}
        self._installees.add(langue)
        yield {"type": "fin"}

    async def lire(self, texte, langue):
        if not self._moteur:
            raise VoixErreur("Piper n'est pas installé", "moteur_absent")
        if langue not in self._installees:
            raise VoixErreur(f"voix {VOIX[langue]['nom']} non téléchargée", "voix_absente")
        texte = nettoyer(texte)
        if not texte:
            raise VoixErreur("rien à lire", "vide")
        self.lus.append(texte)
        return wav_bip(min(2.0, 0.15 + len(texte) * 0.004))
