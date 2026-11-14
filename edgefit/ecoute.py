# ecoute : la parole transformée en texte par Whisper, sur la carte, sans internet.
#
# Avec la voix (voix.py, Piper), c'est la conversation à l'oral : on parle, Whisper écrit la
# question, le modèle répond, Piper lit la réponse.
#
# faster-whisper est une version de Whisper (OpenAI) réécrite pour tourner vite sur processeur
# (CTranslate2, calculs en entiers 8 bits). Mesuré sur un i7-8650U sur batterie, pour 6 s de parole :
#   base  (≈ 145 Mo) : 2,8 s, quelques mots mal compris ;
#   small (≈ 485 Mo) : 8,1 s, phrase exacte.
# Optionnel (pip install "edgefit[parole]") : sans lui, l'appli marche, sans le micro.
# Les modèles viennent des dépôts officiels Systran sur Hugging Face, à une révision fixée.
import asyncio
import io
import os
import threading
import time
from pathlib import Path

from platformdirs import user_data_dir

MODELES = {
    "base": {"depot": "Systran/faster-whisper-base", "revision": "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66", "taille_mo": 145},
    "small": {"depot": "Systran/faster-whisper-small", "revision": "536b0662742c02347bc0e980a01041f333bce120", "taille_mo": 485},
}
FICHIERS = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")
TAILLES = tuple(MODELES)
AUDIO_MAX_OCTETS = 8 * 1024 * 1024   # ≈ 8 minutes d'Opus, bien plus qu'une question
DUREE_MAX_S = 120


class EcouteErreur(Exception):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def moteur_present():
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def decoder(octets):
    """Audio du navigateur (webm/opus, ogg, wav...) -> échantillons 16 kHz mono en float32.
    Décodé ici avec PyAV plutôt que par faster-whisper, dont la fonction suit mal les versions de PyAV."""
    import av
    import numpy as np
    morceaux = []
    try:
        with av.open(io.BytesIO(octets)) as conteneur:
            reechantillonneur = av.AudioResampler(format="s16", layout="mono", rate=16000)
            for trame in conteneur.decode(audio=0):
                morceaux += [t.to_ndarray().reshape(-1) for t in reechantillonneur.resample(trame)]
            morceaux += [t.to_ndarray().reshape(-1) for t in reechantillonneur.resample(None)]
    except (av.error.FFmpegError, ValueError, IndexError, StopIteration) as e:
        raise EcouteErreur(f"audio illisible : {e}", "audio") from e
    if not morceaux:
        raise EcouteErreur("aucun son dans l'enregistrement", "vide")
    audio = np.concatenate(morceaux).astype(np.float32) / 32768.0
    if len(audio) > DUREE_MAX_S * 16000:
        raise EcouteErreur(f"enregistrement de plus de {DUREE_MAX_S} s", "trop_long")
    return audio


class Ecoute:
    def __init__(self, dossier=None):
        self.dossier = Path(dossier or Path(user_data_dir("edgefit", appauthor=False)) / "whisper")
        self._charges = {}
        self._verrou = threading.Lock()  # une transcription à la fois : elle occupe tous les cœurs
        self._telechargements = set()

    def _chemin(self, taille):
        return self.dossier / taille

    def installe(self, taille):
        return all((self._chemin(taille) / f).exists() for f in FICHIERS)

    def etat(self):
        return {"moteur": moteur_present(), "modeles": {
            t: {"taille_mo": m["taille_mo"], "installe": self.installe(t), "en_cours": t in self._telechargements}
            for t, m in MODELES.items()}}

    async def telecharger(self, taille):
        """Événements {"type": "debut"} puis {"type": "fin"} ou {"type": "erreur"}."""
        if taille in self._telechargements:
            yield {"type": "erreur", "erreur": "téléchargement déjà en cours", "code": "en_cours"}
            return
        self._telechargements.add(taille)
        m = MODELES[taille]
        try:
            yield {"type": "debut", "taille_mo": m["taille_mo"]}
            os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")  # pas de barre dans le journal du serveur
            from huggingface_hub import snapshot_download
            from huggingface_hub.errors import HfHubHTTPError
            try:
                await asyncio.to_thread(snapshot_download, m["depot"], revision=m["revision"],
                                        local_dir=self._chemin(taille), allow_patterns=list(FICHIERS))
            except (HfHubHTTPError, OSError) as e:
                raise EcouteErreur(f"pas d'accès à huggingface.co ({type(e).__name__})", "hors_ligne") from e
            yield {"type": "fin"}
        except EcouteErreur as e:
            yield {"type": "erreur", "erreur": str(e), "code": e.code}
        finally:
            self._telechargements.discard(taille)

    def _modele(self, taille):
        if taille not in self._charges:
            from faster_whisper import WhisperModel
            self._charges[taille] = WhisperModel(str(self._chemin(taille)), device="cpu", compute_type="int8")
        return self._charges[taille]

    def _transcrire(self, audio, taille, langue):
        with self._verrou:
            debut = time.perf_counter()
            segments, info = self._modele(taille).transcribe(audio, language=langue, beam_size=1, vad_filter=True)
            texte = " ".join(s.text.strip() for s in segments).strip()
            return {"texte": texte, "langue": info.language, "duree_s": len(audio) / 16000,
                    "calcul_s": time.perf_counter() - debut, "modele": taille}

    async def transcrire(self, octets, taille="base", langue=None):
        if not moteur_present():
            raise EcouteErreur("faster-whisper n'est pas installé", "moteur_absent")
        if not self.installe(taille):
            raise EcouteErreur(f"modèle Whisper {taille} non téléchargé", "modele_absent")
        audio = await asyncio.to_thread(decoder, octets)
        return await asyncio.to_thread(self._transcrire, audio, taille, langue)


class EcouteFactice:
    """Pour --fake et les tests : même interface, une phrase fixe au lieu de la vraie transcription."""
    PHRASE = "Est-ce que llama3.2:3b tient sur cette carte avec un contexte de 8192 tokens ?"

    def __init__(self, moteur=True):
        self._moteur = moteur
        self._installes = set()
        self.recus = []

    def installe(self, taille):
        return taille in self._installes

    def etat(self):
        return {"moteur": self._moteur, "modeles": {t: {"taille_mo": m["taille_mo"], "installe": t in self._installes,
                                                        "en_cours": False} for t, m in MODELES.items()}}

    async def telecharger(self, taille):
        yield {"type": "debut", "taille_mo": MODELES[taille]["taille_mo"]}
        await asyncio.sleep(0.05)
        self._installes.add(taille)
        yield {"type": "fin"}

    async def transcrire(self, octets, taille="base", langue=None):
        if not self._moteur:
            raise EcouteErreur("faster-whisper n'est pas installé", "moteur_absent")
        if taille not in self._installes:
            raise EcouteErreur(f"modèle Whisper {taille} non téléchargé", "modele_absent")
        if len(octets) < 16:
            raise EcouteErreur("aucun son dans l'enregistrement", "vide")
        self.recus.append(len(octets))
        await asyncio.sleep(0.2)
        return {"texte": self.PHRASE, "langue": langue or "fr", "duree_s": 3.0, "calcul_s": 0.2, "modele": taille}
