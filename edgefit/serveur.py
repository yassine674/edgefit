# l'application web : une seule appli FastAPI qui sert à la fois l'API et la page
# du tableau de bord. La page ne passe que par l'API et le WebSocket, jamais par le
# code interne : elle pourrait être servie ailleurs sans rien changer côté serveur.
import asyncio
import json
from contextlib import aclosing
import os
import shutil
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AfterValidator, BaseModel, Field

from . import atelier, banc, calibration, endurance, objectif, outils, stockage
from .energie import Releveur
from .catalogue import SUGGESTIONS, Catalogue, depot_valide
from .diagnostic import dossier_modeles
from .compatibilite import OCTETS_KV, _verdict, analyser, budget, estimer, plateforme_correspondante, solutions, verdict_rapide
from .ollama import GO, ModeleInconnu, OllamaIndisponible, nom_valide
from .registre import Registre, RegistreErreur
from .voix import LANGUES, TEXTE_MAX, Voix, VoixErreur
from .ecoute import AUDIO_MAX_OCTETS, TAILLES, Ecoute, EcouteErreur
from .images import MODELE as MODELE_IMAGES, MOTEURS, PROMPT_MAX, TAILLES as TAILLES_IMAGES, Images
from .securite import NOM_COOKIE, jeton_valide, origine_autorisee

DOSSIER_STATIC = Path(__file__).parent / "static"

PAGE_ACCES_REFUSE = """<!doctype html><meta charset="utf-8"><title>Accès refusé</title>
<body style="font-family:system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem">
<h1>Accès refusé</h1>
<p>Ouvrez l'adresse complète affichée dans le terminal au démarrage d'edgefit
(elle se termine par <code>?token=...</code>).</p></body>"""


def _verifier_nom(nom):
    if not nom_valide(nom):
        raise ValueError("nom de modèle invalide")
    return nom


NomModele = Annotated[str, AfterValidator(_verifier_nom)]


class DemandeTelechargement(BaseModel):
    nom: NomModele


class DemandeCreation(BaseModel):
    nom: NomModele                      # nom du modèle fabriqué
    base: NomModele                     # modèle de départ (installé, ou téléchargé d'abord)
    systeme: str = Field(default="", max_length=8000)
    parametres: dict[str, float | int | None] = Field(default={}, max_length=20)  # vérifiés par atelier.py
    quantifier: Literal[atelier.QUANTIFIABLES] | None = None
    remplacer: bool = False


# image jointe : base64 brut (sans « data:image/...; »), au plus ≈ 6 Mo ; la page les réduit à 1024 px
ImageB64 = Annotated[str, Field(min_length=16, max_length=8_000_000, pattern=r"^[A-Za-z0-9+/]+={0,2}$")]


# Ollama décharge un modèle 5 min après la dernière réponse ; le recharger coûte 5 à 10 s (plus sur
# une carte SD). Pendant une conversation, on le garde 30 min.
GARDER_CHARGE = "30m"


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=20_000)
    images: list[ImageB64] = Field(default=[], max_length=4)  # modèles de vision (moondream, llava, qwen2.5vl...)


# bornes larges : Ollama accepte jusqu'au contexte maximal du modèle (128K pour Llama 3.1...)
Contexte = Annotated[int, Field(ge=256, le=1_048_576)]
TypeKV = Literal[tuple(OCTETS_KV)]
NomOutil = Literal[outils.NOMS]  # liste blanche : seuls les outils écrits dans outils.py existent


class DemandeChat(BaseModel):
    modele: NomModele
    messages: list[Message] = Field(min_length=1, max_length=200)
    # toujours envoyé à Ollama : sa valeur par défaut change d'une version à l'autre,
    # et c'est elle qui fixe la taille du cache KV réservé
    num_ctx: Contexte = 4096
    systeme: str = Field(default="", max_length=8000)
    temperature: float | None = Field(default=None, ge=0, le=2)
    conversation_id: int | None = None  # si présent, la question et la réponse sont enregistrées
    type_kv: TypeKV = "f16"  # réglage déclaré du serveur : sert à l'estimation enregistrée pour la calibration
    # outils cochés par l'utilisateur ; un nom hors de la liste d'edgefit est refusé (422)
    outils: list[NomOutil] = Field(default=[], max_length=10)


class DemandeModele(BaseModel):
    nom: NomModele


class DemandeEndurance(BaseModel):
    nom: NomModele
    duree_s: Literal[endurance.DUREES_S]


class DemandeConversation(BaseModel):
    modele: NomModele
    titre: str = Field(default="", max_length=200)
    systeme: str = Field(default="", max_length=8000)
    temperature: float | None = Field(default=None, ge=0, le=2)
    num_ctx: Contexte = 4096


class ModifConversation(BaseModel):
    titre: str | None = Field(default=None, max_length=200)
    modele: NomModele | None = None
    systeme: str | None = Field(default=None, max_length=8000)
    temperature: float | None = Field(default=None, ge=0, le=2)
    num_ctx: Contexte | None = None


class DemandeVoix(BaseModel):
    langue: Literal[LANGUES]


class DemandeEcoute(BaseModel):
    taille: Literal[TAILLES]


class DemandeInstallationImages(BaseModel):
    variante: Literal[tuple(MOTEURS)] = "cpu"


class DemandeImage(BaseModel):
    prompt: str = Field(min_length=1, max_length=PROMPT_MAX)
    negatif: str = Field(default="", max_length=PROMPT_MAX)
    largeur: Literal[TAILLES_IMAGES] = 512
    hauteur: Literal[TAILLES_IMAGES] = 512
    etapes: int = Field(default=1, ge=1, le=8)
    graine: int | None = Field(default=None, ge=0, le=2 ** 31 - 1)


class DemandeLecture(BaseModel):
    texte: str = Field(min_length=1, max_length=TEXTE_MAX)
    langue: Literal[LANGUES]


class DemandeCompatibilite(BaseModel):
    modele: NomModele
    num_ctx: Contexte = 4096
    # réglage global du serveur Ollama (OLLAMA_KV_CACHE_TYPE), que l'API ne permet pas de lire
    type_kv: TypeKV = "f16"
    parallele: int = Field(default=1, ge=1, le=16)  # OLLAMA_NUM_PARALLEL


def _message_indisponible(ollama):
    return f"Ollama ne répond pas sur {ollama.url} : est-il installé et lancé ?"


def _ndjson(ollama, evenements):
    # une ligne JSON par événement ; si Ollama tombe en cours de route, la réponse
    # HTTP est déjà partie : l'erreur est donc transmise comme un dernier événement
    async def lignes():
        try:
            async for evt in evenements:
                yield json.dumps(evt, ensure_ascii=False) + "\n"
        except OllamaIndisponible:
            yield json.dumps({"type": "erreur", "erreur": _message_indisponible(ollama)}, ensure_ascii=False) + "\n"
    return StreamingResponse(lignes(), media_type="application/x-ndjson")


async def _charges(ollama):
    try:
        return await ollama.charges()
    except OllamaIndisponible:
        return []


async def _avec_memoire_chargee(ollama, nom, evenements):
    # à la fin d'une réponse, le modèle est encore chargé : Ollama donne alors la mémoire
    # réellement occupée, à comparer avec l'estimation (base de la future calibration)
    async for evt in evenements:
        if evt["type"] == "fin":
            charge = next((m for m in await _charges(ollama) if m["nom"] == nom), None)
            if charge:
                evt["metriques"]["memoire_chargee_go"] = charge["taille"] / GO
                evt["metriques"]["memoire_gpu_go"] = charge["taille_vram"] / GO
        yield evt


class Occupation:
    """Qui utilise la carte : conversations en cours (par modèle) et banc d'essai.
    Un banc d'essai décharge et recharge les modèles : il ne doit pas couper une conversation."""

    def __init__(self):
        self.flux = {}
        self.banc = None

    def total(self):
        return sum(self.flux.values())

    def entrer(self, nom):
        self.flux[nom] = self.flux.get(nom, 0) + 1

    def sortir(self, nom):
        self.flux[nom] -= 1
        if not self.flux[nom]:
            del self.flux[nom]


def _avec_score(entree):
    s = banc.score(entree["mesures"])
    return {**entree, "score": s}


def creer_app(fournisseur, ollama, jeton, registre=None, catalogue=None, intervalle_s=1.0, voix=None, ecoute=None, images=None):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    registre = registre or Registre()
    catalogue = catalogue or Catalogue()
    voix = voix or Voix()
    ecoute = ecoute or Ecoute()
    images = images or Images()
    occupation = Occupation()
    carte = getattr(fournisseur, "carte", None) or fournisseur.machine

    async def observer(nom, num_ctx, type_kv, mesure_go):
        """Estimation détaillée de ce modèle à côté de la mémoire mesurée : un point de calibration."""
        try:
            infos = await ollama.infos(nom)
            if infos is None:
                return
            e = estimer(infos["architecture"], infos["taille_octets"], num_ctx, type_kv)
            await asyncio.to_thread(stockage.ajouter_observation, carte, nom, num_ctx, type_kv,
                                    e["poids"] / GO, e["cache_kv"] / GO, e["total"] / GO, mesure_go)
        except (OllamaIndisponible, ModeleInconnu, KeyError, TypeError):
            pass

    async def calibration_carte():
        return calibration.resume(await asyncio.to_thread(stockage.lister_observations, carte))

    async def budget_actuel():
        memoire = await asyncio.to_thread(fournisseur.memoire)
        charges = await _charges(ollama)
        b = budget(fournisseur.type_carte, memoire["totale"], memoire["disponible"], sum(m["taille"] for m in charges))
        return b, charges, memoire

    boite = outils.Boite(fournisseur, ollama, lambda: budget_actuel())

    async def sait_appeler_des_outils(nom):
        try:
            infos = await ollama.infos(nom)
        except (OllamaIndisponible, ModeleInconnu):
            return None
        capacites = (infos or {}).get("capacites")
        return None if capacites is None else "tools" in capacites  # None : Ollama trop ancien pour le dire

    async def installes():
        try:
            return await ollama.modeles()
        except OllamaIndisponible:
            return []

    @app.middleware("http")
    async def verifier_acces(request: Request, call_next):
        propose = request.query_params.get("token")
        if propose is not None:
            if not jeton_valide(propose, jeton):
                return HTMLResponse(PAGE_ACCES_REFUSE, status_code=403)
            reponse = RedirectResponse(str(request.url.remove_query_params("token")), status_code=303)
            reponse.set_cookie(NOM_COOKIE, jeton, httponly=True, samesite="strict", secure=request.url.scheme == "https")
            return reponse
        if not jeton_valide(request.cookies.get(NOM_COOKIE), jeton):
            if request.url.path.startswith("/api/"):
                return JSONResponse({"erreur": "accès refusé"}, status_code=401)
            return HTMLResponse(PAGE_ACCES_REFUSE, status_code=401)
        return await call_next(request)

    @app.exception_handler(OllamaIndisponible)
    async def ollama_indisponible(request, exc):
        return JSONResponse({"erreur": _message_indisponible(ollama)}, status_code=503)

    @app.get("/")
    async def accueil():
        return FileResponse(DOSSIER_STATIC / "index.html")

    @app.get("/api/metriques")
    async def metriques():
        return await asyncio.to_thread(fournisseur.lire)

    @app.get("/api/ollama")
    async def etat_ollama():
        # l'espace disque est un plus : une erreur ici ne doit jamais faire croire qu'Ollama est absent
        try:
            dossier = dossier_modeles()
            d = shutil.disk_usage(dossier)
            disque = {"libre_go": d.free / GO, "total_go": d.total / GO, "dossier": str(dossier)}
        except OSError:
            disque = None
        try:
            return {"disponible": True, "version": await ollama.version(), "url": ollama.url, "disque": disque}
        except OllamaIndisponible:
            return {"disponible": False, "version": None, "url": ollama.url, "disque": disque}

    cache_diag = {}

    @app.get("/api/diagnostic")
    async def diagnostic_carte():
        # quelques secondes (commandes système, accès internet) : gardé une minute
        import time
        if cache_diag.get("t", 0) > time.monotonic() - 60:
            return cache_diag["r"]
        from .diagnostic import etat_carte
        url = ollama.url if ollama.url.startswith("http") else "http://127.0.0.1:11434"
        r = await asyncio.to_thread(etat_carte, url)
        if not ollama.url.startswith("http"):  # mode --fake : Ollama simulé
            r["faits"]["ollama_version"] = "simulé"
            r["verifications"] = [v for v in r["verifications"] if not v["code"].startswith("ollama")]
            r["verifications"].insert(0, {"niveau": "OK", "code": "ollama", "params": {"version": "simulé"}})
        r["faits"]["base"] = str(stockage.chemin_db())
        cache_diag.update(t=time.monotonic(), r=r)
        return r

    @app.get("/api/calibration")
    async def voir_calibration():
        observations = await asyncio.to_thread(stockage.lister_observations, carte)
        return {"carte": carte, **calibration.resume(observations), "depart": calibration.DEPART,
                "min_points": calibration.MIN_POINTS, "observations": observations}

    @app.delete("/api/calibration")
    async def effacer_calibration():
        return {"effaces": await asyncio.to_thread(stockage.tout_effacer, "observations")}

    @app.get("/api/export")
    async def exporter():
        return await asyncio.to_thread(stockage.exporter)

    @app.delete("/api/conversations")
    async def effacer_conversations():
        return {"effaces": await asyncio.to_thread(stockage.tout_effacer, "conversations")}

    @app.delete("/api/bancs")
    async def effacer_bancs():
        return {"effaces": await asyncio.to_thread(stockage.tout_effacer, "bancs")}

    @app.get("/api/modeles")
    async def modeles():
        liste = await ollama.modeles()
        # nom exact de la version (« qwen:latest » -> « qwen:4b-chat-v1.5-q4_0 ») : 4 s au plus, puis en cache
        try:
            exacts = await asyncio.wait_for(asyncio.gather(*(registre.nom_exact(m["nom"], m.get("empreinte")) for m in liste)), 4)
        except (asyncio.TimeoutError, RegistreErreur):
            exacts = [None] * len(liste)
        return [{**m, "nom_exact": e} for m, e in zip(liste, exacts)]

    # nom en paramètre de requête : les noms Ollama contiennent "/" et ":"
    @app.get("/api/modeles/capacites")
    async def capacites(nom: str):
        """["completion", "tools", "vision", ...] d'après Ollama ; null si Ollama est trop ancien pour le dire."""
        if not nom_valide(nom):
            raise HTTPException(400, "nom de modèle invalide")
        try:
            infos = await ollama.infos(nom)
        except ModeleInconnu:
            infos = None
        if infos is None:
            raise HTTPException(404, "modèle non installé")
        return {"nom": nom, "capacites": infos.get("capacites")}

    @app.get("/api/modeles/architecture")
    async def architecture(nom: str):
        if not nom_valide(nom):
            raise HTTPException(400, "nom de modèle invalide")
        try:
            return await ollama.architecture(nom)
        except ModeleInconnu as e:
            raise HTTPException(404, str(e))

    @app.post("/api/modeles/telecharger")
    async def telecharger(demande: DemandeTelechargement):
        return _ndjson(ollama, ollama.telecharger(demande.nom))

    @app.post("/api/chat")
    async def chat(demande: DemandeChat):
        if occupation.banc:
            raise HTTPException(409, {"code": "banc_en_cours", "message": f"banc d'essai en cours ({occupation.banc})"})
        # « images » seulement quand il y en a : Ollama les envoie au modèle de vision
        messages = [m.model_dump(exclude={"images"} if not m.images else None) for m in demande.messages]
        envoyes = ([{"role": "system", "content": demande.systeme}] if demande.systeme.strip() else []) + messages
        options = {"num_ctx": demande.num_ctx}
        if demande.temperature is not None:
            options["temperature"] = demande.temperature
        if demande.conversation_id is not None:
            if await asyncio.to_thread(stockage.lire_conversation, demande.conversation_id) is None:
                raise HTTPException(404, {"code": "conversation", "message": "conversation introuvable"})
            # les images ne sont pas gardées (une carte SD n'est pas faite pour ça) : seulement leur nombre
            n_images = len(demande.messages[-1].images)
            await asyncio.to_thread(stockage.ajouter_message, demande.conversation_id, "user", messages[-1]["content"],
                                    {"images": n_images} if n_images else None)

        autorises = set(demande.outils)
        if autorises and await sait_appeler_des_outils(demande.modele) is False:
            autorises = set()
            sans_outils = True  # le modèle ne sait pas : on répond sans, et on le dit à la page
        else:
            sans_outils = False

        async def flux():
            occupation.entrer(demande.modele)
            texte, mesures, faits = [], None, []
            historique = list(envoyes)
            releveur = None
            try:
                releveur = await Releveur(fournisseur).demarrer()  # puissance de la carte pendant la réponse
                if sans_outils:
                    yield {"type": "outils_indisponibles"}
                # boucle : tant que le modèle demande des outils, edgefit les exécute et lui renvoie
                # les résultats ; au dernier tour, plus d'outils proposés, il doit répondre
                for tour in range(outils.MAX_TOURS + 1):
                    definitions = boite.definitions(autorises) if tour < outils.MAX_TOURS else None
                    appels, texte_tour = [], []
                    async for evt in _avec_memoire_chargee(ollama, demande.modele,
                                                           ollama.discuter(demande.modele, historique, options, definitions, GARDER_CHARGE)):
                        if evt["type"] == "appels":
                            appels += evt["appels"]
                            continue
                        if evt["type"] == "erreur" and definitions and "does not support tools" in evt["erreur"]:
                            # Ollama ancien qui ne disait pas que le modèle ne sait pas : on recommence sans
                            autorises.clear()
                            appels = [None]
                            yield {"type": "outils_indisponibles"}
                            break
                        if evt["type"] == "token":
                            texte.append(evt["texte"])
                            texte_tour.append(evt["texte"])
                            releveur.token()
                        elif evt["type"] == "fin":
                            mesures = evt["metriques"]
                            if appels:
                                continue  # fin d'un tour d'outils : la vraie fin viendra après
                            if mesures.get("memoire_chargee_go"):
                                await observer(demande.modele, demande.num_ctx, demande.type_kv, mesures["memoire_chargee_go"])
                            if faits:
                                mesures["outils"] = faits
                            mesures.update(releveur.resumer(mesures))
                        yield evt
                    if not appels:
                        break
                    if appels == [None]:
                        continue
                    historique.append({"role": "assistant", "content": "".join(texte_tour),
                                       "tool_calls": [{"function": {"name": a["nom"], "arguments": a["arguments"]}} for a in appels]})
                    for a in appels:
                        yield {"type": "outil", "nom": a["nom"], "arguments": a["arguments"]}
                        resultat = await boite.executer(a["nom"], a["arguments"], autorises)
                        faits.append({"nom": a["nom"], "arguments": a["arguments"], "resultat": resultat})
                        yield {"type": "outil_resultat", "nom": a["nom"], "arguments": a["arguments"], "resultat": resultat}
                        historique.append({"role": "tool", "content": outils.pour_le_modele(resultat), "tool_name": a["nom"]})
            finally:
                if releveur:
                    releveur.arreter()
                occupation.sortir(demande.modele)
                # une seule écriture par réponse, même si le navigateur coupe en cours de route
                if demande.conversation_id is not None and texte:
                    await asyncio.to_thread(stockage.ajouter_message, demande.conversation_id, "assistant",
                                            "".join(texte), mesures)
        return _ndjson(ollama, flux())

    @app.get("/api/recherche")
    async def recherche(q: str = ""):
        # suggestions pendant la frappe : aucune lecture de registre ici, seulement des noms
        return await catalogue.rechercher(q[:100], await installes())

    @app.get("/api/variantes")
    async def variantes(depot: str):
        if not depot_valide(depot):
            raise HTTPException(400, "nom de dépôt invalide")
        versions, erreur = await catalogue.variantes(depot)
        if erreur:
            raise HTTPException(502, {"code": erreur})
        return versions

    # --- catalogue : verdict de chaque modèle suggéré, en une requête, sans réseau ---
    async def modeles_carte(num_ctx, type_kv):
        """Le catalogue vu depuis cette carte : verdict mémoire, plafond de vitesse, dernière mesure."""
        b, charges, memoire = await budget_actuel()
        noms_installes = {m["nom"] for m in await installes()}
        plateforme = await asyncio.to_thread(plateforme_correspondante, getattr(fournisseur, "carte", None))
        bande = plateforme.get("bandwidth_gb_s") if plateforme else None
        derniers = {}
        for entree in await asyncio.to_thread(stockage.lister_bancs):
            derniers.setdefault(entree["modele"], entree)  # liste triée du plus récent au plus ancien
        modeles = []
        calib = await calibration_carte()
        for e in SUGGESTIONS:
            arch = {**e["architecture"], "projecteur_octets": e["projecteur_octets"]}
            v = verdict_rapide(arch, e["taille_octets"], b, min(num_ctx, arch.get("context_max") or num_ctx),
                               type_kv, bande_passante=bande)
            if calib["active"]:
                c = calib["coef"]
                total = calibration.predire((c["fixe_go"], c["a"], c["b"]), v["poids_go"], v["cache_kv_go"])
                v.update(total_brut_go=v["total_go"], total_go=total, reste_go=b["utilisable"] / GO - total,
                         verdict=_verdict(total * GO, b))
            mesure = derniers.get(e["nom"])
            modeles.append({**{k: e[k] for k in ("nom", "description", "famille", "parametres", "format", "vision", "tags")},
                            "taille_go": (e["taille_octets"] + e["projecteur_octets"]) / GO,
                            "installe": e["nom"] in noms_installes, **v,
                            "score": banc.score(mesure["mesures"]) if mesure else None,
                            "debit_mesure": mesure["mesures"].get("debit_tokens_s") if mesure else None})
        return b, charges, calib, plateforme, modeles

    @app.get("/api/catalogue")
    async def catalogue_carte(num_ctx: Contexte = 4096, type_kv: TypeKV = "f16"):
        b, charges, calib, plateforme, modeles = await modeles_carte(num_ctx, type_kv)
        return {"budget_go": {k: v / GO for k, v in b.items()}, "num_ctx": num_ctx, "type_kv": type_kv,
                "calibration": {k: calib[k] for k in ("n", "active")},
                "plateforme": plateforme["name"] if plateforme else None, "modeles": modeles,
                "charges": [{"nom": m["nom"], "taille_go": m["taille"] / GO} for m in charges]}

    @app.get("/api/objectif")
    async def choisir(usage: Literal[objectif.USAGES] | None = None, debit_min: Annotated[float, Field(ge=0, le=200)] = 0,
                      num_ctx: Contexte = 4096, type_kv: TypeKV = "f16"):
        b, _, _, plateforme, modeles = await modeles_carte(num_ctx, type_kv)
        rend, n = objectif.rendement((m["debit_mesure"], m["plafond_tokens_s"]) for m in modeles)
        return {"rendement": rend, "rendement_mesures": n, "plateforme": plateforme["name"] if plateforme else None,
                "budget_go": b["utilisable"] / GO, "num_ctx": num_ctx, "usage": usage, "debit_min": debit_min,
                **objectif.classer(modeles, usage, debit_min, rend)}

    # --- modèles installés ---
    @app.get("/api/charges")
    async def charges():
        return [{**m, "taille_go": m["taille"] / GO, "occupe": occupation.flux.get(m["nom"], 0) > 0}
                for m in await _charges(ollama)]

    @app.post("/api/modeles/decharger")
    async def decharger(demande: DemandeModele):
        if occupation.flux.get(demande.nom):
            raise HTTPException(409, {"code": "occupe", "message": "une conversation utilise ce modèle"})
        await ollama.decharger(demande.nom)
        return {"ok": True}

    @app.post("/api/modeles/supprimer")
    async def supprimer(demande: DemandeModele):
        if occupation.flux.get(demande.nom) or occupation.banc == demande.nom:
            raise HTTPException(409, {"code": "occupe", "message": "ce modèle est en cours d'utilisation"})
        try:
            await ollama.decharger(demande.nom)
            await ollama.supprimer(demande.nom)
        except ModeleInconnu as e:
            raise HTTPException(404, {"code": "introuvable", "message": str(e)})
        return {"ok": True}

    # --- conversations (SQLite sur la carte) ---
    @app.get("/api/conversations")
    async def conversations():
        return await asyncio.to_thread(stockage.lister_conversations)

    @app.post("/api/conversations")
    async def nouvelle_conversation(demande: DemandeConversation):
        return await asyncio.to_thread(stockage.creer_conversation, demande.modele, demande.titre,
                                       demande.systeme, demande.temperature, demande.num_ctx)

    @app.get("/api/conversations/{id_conv}")
    async def conversation(id_conv: int):
        conv = await asyncio.to_thread(stockage.lire_conversation, id_conv)
        if conv is None:
            raise HTTPException(404, {"code": "conversation", "message": "conversation introuvable"})
        return conv

    @app.patch("/api/conversations/{id_conv}")
    async def modifier_conversation(id_conv: int, demande: ModifConversation):
        champs = demande.model_dump(exclude_unset=True)
        conv = await asyncio.to_thread(lambda: stockage.modifier_conversation(id_conv, **champs))
        if conv is None:
            raise HTTPException(404, {"code": "conversation", "message": "conversation introuvable"})
        return conv

    @app.delete("/api/conversations/{id_conv}")
    async def supprimer_conversation(id_conv: int):
        return {"ok": await asyncio.to_thread(stockage.supprimer_conversation, id_conv)}

    # --- banc d'essai et score ---
    # --- endurance : le modèle écrit sans arrêt, on suit débit, température et puissance -------
    @app.get("/api/endurances")
    async def endurances():
        return await asyncio.to_thread(stockage.lister_endurances)

    @app.delete("/api/endurances")
    async def effacer_endurances():
        return {"effaces": await asyncio.to_thread(stockage.tout_effacer, "endurances")}

    @app.post("/api/endurance")
    async def lancer_endurance(demande: DemandeEndurance):
        if occupation.banc or occupation.total():
            raise HTTPException(409, {"code": "carte_occupee", "message": "la carte est déjà utilisée"})
        occupation.banc = demande.nom

        async def deroule():
            points, tokens = [], 0
            depart = endurance.horloge()
            try:
                yield {"type": "debut", "duree_s": demande.duree_s, "fenetre_s": endurance.FENETRE_S}
                fenetre = endurance.Fenetre(0.0)
                n = 0
                while endurance.horloge() - depart < demande.duree_s:
                    n += 1
                    options = {"num_ctx": endurance.NUM_CTX, "num_predict": endurance.NUM_PREDICT, "temperature": 0.7}
                    message = [{"role": "user", "content": endurance.CONSIGNE.format(n=n)}]
                    precedent = None  # instant du token précédent DANS cette requête
                    async with aclosing(ollama.discuter(demande.nom, message, options)) as flux:
                        async for evt in flux:
                            if evt["type"] == "erreur":
                                raise RuntimeError(evt["erreur"])
                            if evt["type"] != "token":
                                continue
                            maintenant = endurance.horloge()
                            tokens += 1
                            if precedent is not None:
                                fenetre.ajouter(maintenant - precedent)
                            precedent = maintenant
                            ecoule = maintenant - depart
                            if ecoule - fenetre.debut >= endurance.FENETRE_S:
                                mesure = await asyncio.to_thread(fournisseur.lire)
                                p = endurance.point(ecoule, fenetre.debit(), mesure)
                                points.append(p)
                                yield {"type": "point", **p, "tokens": tokens}
                                fenetre = endurance.Fenetre(ecoule)
                            if ecoule >= demande.duree_s:
                                break
                resume = endurance.resumer(points, demande.duree_s, tokens)
                await asyncio.to_thread(stockage.ajouter_endurance, demande.nom, demande.duree_s, endurance.NUM_CTX, resume, points)
                yield {"type": "fin", "resume": resume, "points": points}
            except (RuntimeError, ModeleInconnu) as e:
                yield {"type": "erreur", "erreur": str(e)}
            finally:
                occupation.banc = None
        return _ndjson(ollama, deroule())

    @app.get("/api/bancs")
    async def bancs():
        return [_avec_score(e) for e in await asyncio.to_thread(stockage.lister_bancs)]

    @app.post("/api/banc")
    async def lancer_banc(demande: DemandeModele):
        if occupation.banc or occupation.total():
            raise HTTPException(409, {"code": "carte_occupee", "message": "la carte est déjà utilisée"})
        occupation.banc = demande.nom

        async def phases():
            puissances, libres = [], []
            arret = asyncio.Event()

            async def relever():
                # puissance de la carte et mémoire libre, une fois par demi-seconde pendant les essais
                while not arret.is_set():
                    m = await asyncio.to_thread(fournisseur.lire)
                    if m.get("puissance_w"):
                        puissances.append(m["puissance_w"])
                    if m.get("ram_totale_go"):
                        libres.append((m["ram_totale_go"] - m["ram_utilisee_go"]) / m["ram_totale_go"])
                    await asyncio.sleep(0.5)

            async def essai(texte):
                options = {"num_ctx": banc.NUM_CTX, "num_predict": banc.NUM_PREDICT, "temperature": 0, "seed": 42}
                releveur = await Releveur(fournisseur).demarrer()
                try:
                    async for evt in ollama.discuter(demande.nom, [{"role": "user", "content": texte}], options):
                        if evt["type"] == "erreur":
                            raise RuntimeError(evt["erreur"])
                        if evt["type"] == "token":
                            releveur.token()
                        if evt["type"] == "fin":
                            return evt["metriques"] | releveur.resumer(evt["metriques"])
                finally:
                    releveur.arreter()

            releve = asyncio.create_task(relever())
            try:
                yield {"type": "phase", "phase": "dechargement"}
                await ollama.decharger(demande.nom)
                textes = banc.textes()
                yield {"type": "phase", "phase": "froid"}
                froid = await essai(textes[0])
                chauds = []
                for i, texte in enumerate(textes[1:], start=1):
                    yield {"type": "phase", "phase": "chaud", "numero": i}
                    chauds.append(await essai(texte))
                charge = next((m for m in await _charges(ollama) if m["nom"] == demande.nom), None)
                arret.set()
                mesures = banc.resumer(chauds, froid["chargement_s"], charge["taille"] / GO if charge else None,
                                       min(libres) if libres else None,
                                       sum(puissances) / len(puissances) if puissances else None)
                await asyncio.to_thread(stockage.ajouter_banc, demande.nom, banc.PROTOCOLE, banc.NUM_CTX, "f16", mesures)
                if mesures.get("memoire_go"):
                    await observer(demande.nom, banc.NUM_CTX, "f16", mesures["memoire_go"])
                yield {"type": "fin", "mesures": mesures, "score": banc.score(mesures)}
            except (RuntimeError, ModeleInconnu) as e:
                yield {"type": "erreur", "erreur": str(e)}
            finally:
                arret.set()
                releve.cancel()
                occupation.banc = None
        return _ndjson(ollama, phases())

    async def infos_modele(nom):
        """(infos, source) : installé (Ollama connaît la taille exacte) ou à télécharger (registre)."""
        try:
            infos = await ollama.infos(nom)
        except OllamaIndisponible:
            infos = None
        source = "installé"
        if infos is None:
            source = "registre"
            try:
                infos = await registre.infos(nom)
            except RegistreErreur as e:
                raise HTTPException(422, {"code": e.code, "message": str(e)})
        arch = infos["architecture"]
        if not all(arch.get(c) for c in ("num_layers", "num_kv_heads", "head_dim")):
            raise HTTPException(422, {"code": "gguf", "message": "architecture incomplète dans les métadonnées du modèle"})
        return infos, source

    # --- atelier : essayer des optimisations, puis fabriquer un modèle qui les garde ------------
    @app.post("/api/atelier")
    async def etudier(demande: DemandeCompatibilite):
        infos, source = await infos_modele(demande.modele)
        b, charges, _ = await budget_actuel()
        plateforme = await asyncio.to_thread(plateforme_correspondante, getattr(fournisseur, "carte", None))
        bande = plateforme.get("bandwidth_gb_s") if plateforme else None
        # rendement appris sur les bancs de cette carte (voir objectif.py)
        _, _, _, _, modeles = await modeles_carte(4096, "f16")
        rend, n = objectif.rendement((m["debit_mesure"], m["plafond_tokens_s"]) for m in modeles)
        etude = await asyncio.to_thread(atelier.etudier, demande.modele, infos["architecture"], infos["taille_octets"],
                                        infos.get("format"), b, demande.num_ctx, demande.type_kv, bande, rend,
                                        fournisseur.type_carte)
        # vrais noms des autres quantifications (bibliothèque Ollama) : sans nom, une option n'est pas cliquable
        if any(o["nom"] is None for o in etude["categories"]["poids"]):
            publies = await registre.variantes_ollama(demande.modele)
            for o in etude["categories"]["poids"]:
                o["nom"] = o["nom"] or publies.get(o["format"])
        # GPU utilisé ? Jetson : toujours ; PC : un GPU NVIDIA lu par nvidia-smi, ou un modèle chargé dont
        # Ollama a mis une partie en mémoire GPU (Vulkan, ROCm...). GPU intégré Intel : seulement si Ollama
        # tourne avec OLLAMA_VULKAN=1 et OLLAMA_IGPU_ENABLE=1.
        mesure = await asyncio.to_thread(fournisseur.lire)
        etude["gpu_detecte"] = (fournisseur.type_carte == "jetson" or mesure.get("gpu_pct") is not None
                                or any(m.get("taille_vram") for m in charges))
        etude.update(source=source, rendement_mesures=n, plateforme=plateforme["name"] if plateforme else None,
                     capacites=infos.get("capacites"),
                     charges=[{"nom": m["nom"], "taille_go": m["taille"] / GO} for m in charges],
                     telechargement_go=0 if source == "installé" else infos.get("telechargement_octets", infos["taille_octets"]) / GO)
        return etude

    @app.post("/api/modeles/creer")
    async def creer_modele(demande: DemandeCreation):
        try:
            parametres = atelier.valider_parametres(demande.parametres)
        except atelier.ErreurAtelier as e:
            raise HTTPException(422, {"code": "parametre", "message": str(e)})
        noms = {m["nom"] for m in await installes()}
        cible = demande.nom if ":" in demande.nom else f"{demande.nom}:latest"
        if demande.nom == demande.base or cible == demande.base:
            raise HTTPException(422, {"code": "meme_nom", "message": "le nouveau modèle doit porter un autre nom que sa base"})
        if (demande.nom in noms or cible in noms) and not demande.remplacer:
            raise HTTPException(409, {"code": "existe", "message": f"un modèle {demande.nom} existe déjà"})

        async def etapes():
            if demande.base not in noms:
                # la base n'est pas sur la carte : on la télécharge d'abord
                async for evt in ollama.telecharger(demande.base):
                    if evt["type"] == "fin":
                        break
                    yield {**evt, "phase": "telechargement"}
                    if evt["type"] == "erreur":
                        return
            async for evt in ollama.creer(demande.nom, demande.base, demande.systeme, parametres, demande.quantifier):
                yield {**evt, "phase": "creation"} if evt["type"] != "fin" else {"type": "fin", "nom": cible if cible in {
                    m["nom"] for m in await installes()} else demande.nom}
        return _ndjson(ollama, etapes())

    @app.post("/api/compatibilite")
    async def compatibilite(demande: DemandeCompatibilite):
        infos, source = await infos_modele(demande.modele)
        arch = infos["architecture"]

        # 2. la mémoire de la carte, mesurée maintenant
        memoire = await asyncio.to_thread(fournisseur.memoire)
        charges = await _charges(ollama)
        b = budget(fournisseur.type_carte, memoire["totale"], memoire["disponible"],
                   sum(m["taille"] for m in charges))

        # 3. le calcul (lit la liste des plateformes de llm_modelfit dans sa base SQLite)
        resultat = await asyncio.to_thread(
            analyser, demande.modele, arch, infos["taille_octets"], infos.get("format"), b,
            demande.num_ctx, demande.type_kv, demande.parallele, fournisseur.type_carte,
            memoire.get("bureau_actif", False), getattr(fournisseur, "carte", None),
        )
        calib = await calibration_carte()
        resultat["calibration"] = calib
        if calib["active"]:
            c = calib["coef"]
            e = resultat["estimation_go"]
            total = calibration.predire((c["fixe_go"], c["a"], c["b"]), e["poids"], e["cache_kv"])
            verdict = _verdict(total * GO, b)
            calib.update(total_go=total, verdict_brut=resultat["verdict"])
            resultat["verdict"] = verdict
            if verdict == "trop_gros" and resultat["solutions"] is None:
                resultat["solutions"] = await asyncio.to_thread(
                    solutions, demande.modele, arch, infos["taille_octets"], infos.get("format"), b, demande.num_ctx,
                    demande.type_kv, demande.parallele, fournisseur.type_carte, memoire.get("bureau_actif", False))
            if verdict == "trop_gros":
                resultat["solutions"]["manque_go"] = max(0.0, total - b["utilisable"] / GO)
        resultat["source"] = source
        resultat["taille_fichier_go"] = infos["taille_octets"] / GO
        resultat["projecteur_go"] = infos.get("projecteur_octets", 0) / GO
        # ce qu'il faut télécharger (installé : rien) et la place libre pour le faire
        resultat["telechargement_go"] = (0 if source == "installé"
                                         else infos.get("telechargement_octets", infos["taille_octets"]) / GO)
        resultat["disque_libre_go"] = shutil.disk_usage(dossier_modeles()).free / GO
        resultat["charges"] = [{"nom": m["nom"], "taille_go": m["taille"] / GO} for m in charges]
        return resultat

    # le middleware HTTP ne s'applique pas aux WebSockets : contrôle refait ici
    # --- voix (Piper) ---------------------------------------------------------------------------

    @app.get("/api/voix")
    async def etat_voix():
        return voix.etat()

    @app.post("/api/voix/telecharger")
    async def telecharger_voix(demande: DemandeVoix):
        return _ndjson(ollama, voix.telecharger(demande.langue))

    @app.post("/api/voix/lire")
    async def lire(demande: DemandeLecture):
        try:
            audio = await voix.lire(demande.texte, demande.langue)
        except VoixErreur as e:
            statut = {"moteur_absent": 501, "voix_absente": 409, "vide": 422}.get(e.code, 500)
            raise HTTPException(statut, {"code": e.code, "message": str(e)}) from e
        return Response(audio, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    # --- images (stable-diffusion.cpp) ---------------------------------------------------------
    @app.get("/api/images")
    async def etat_images():
        return images.etat()

    @app.post("/api/images/installer")
    async def installer_images(demande: DemandeInstallationImages):
        return _ndjson(ollama, images.installer(demande.variante))

    @app.post("/api/images/generer")
    async def generer_image(demande: DemandeImage):
        if occupation.banc:
            raise HTTPException(409, {"code": "carte_occupee", "message": "la carte est déjà utilisée (banc d'essai)"})

        async def etapes():
            # place pour le modèle d'images : si la mémoire libre ne suffit pas, on décharge les modèles
            # de langue (Ollama les rechargera à la prochaine question)
            b, charges, _ = await budget_actuel()
            besoin = MODELE_IMAGES["memoire_go"] * GO
            if b["disponible"] - b["marge"] < besoin and charges and not occupation.total():
                for m in charges:
                    await ollama.decharger(m["nom"])
                yield {"type": "decharge", "modeles": [m["nom"] for m in charges]}
            occupation.banc = "images"
            try:
                async for evt in images.generer(demande.prompt, demande.negatif, demande.largeur, demande.hauteur,
                                                demande.etapes, demande.graine):
                    yield evt
            finally:
                occupation.banc = None
        return _ndjson(ollama, etapes())

    @app.get("/api/images/galerie")
    async def galerie_images():
        return await asyncio.to_thread(images.galerie)

    @app.get("/api/images/fichier/{nom}")
    async def fichier_image(nom: str):
        chemin = images.chemin_image(nom)
        if chemin is None:
            raise HTTPException(404, "image introuvable")
        return FileResponse(chemin, media_type="image/png")

    @app.delete("/api/images/fichier/{nom}")
    async def supprimer_image(nom: str):
        if not await asyncio.to_thread(images.supprimer, nom):
            raise HTTPException(404, "image introuvable")
        return {"supprime": nom}

    # --- écoute (Whisper) : la parole devient la question --------------------------------------
    @app.get("/api/ecoute")
    async def etat_ecoute():
        return ecoute.etat()

    @app.post("/api/ecoute/telecharger")
    async def telecharger_ecoute(demande: DemandeEcoute):
        return _ndjson(ollama, ecoute.telecharger(demande.taille))

    @app.post("/api/ecoute/transcrire")
    async def transcrire(request: Request, taille: Literal[TAILLES] = "base", langue: Literal[LANGUES] | None = None):
        # l'audio arrive tel quel dans le corps de la requête (webm/opus du navigateur), sans formulaire
        if int(request.headers.get("content-length") or 0) > AUDIO_MAX_OCTETS:
            raise HTTPException(413, {"code": "trop_long", "message": "enregistrement trop long"})
        octets = await request.body()
        if len(octets) > AUDIO_MAX_OCTETS:
            raise HTTPException(413, {"code": "trop_long", "message": "enregistrement trop long"})
        try:
            return await ecoute.transcrire(octets, taille, langue)
        except EcouteErreur as e:
            statut = {"moteur_absent": 501, "modele_absent": 409, "vide": 422, "audio": 422, "trop_long": 413}.get(e.code, 500)
            raise HTTPException(statut, {"code": e.code, "message": str(e)}) from e

    @app.websocket("/ws/metriques")
    async def flux_metriques(ws: WebSocket):
        if not (origine_autorisee(ws.headers.get("origin"), ws.headers.get("host"))
                and jeton_valide(ws.cookies.get(NOM_COOKIE), jeton)):
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            while True:
                # lire() peut lancer une commande système (vcgencmd) : hors de la boucle
                # asynchrone, pour ne pas bloquer les autres connexions
                await ws.send_json(await asyncio.to_thread(fournisseur.lire))
                await asyncio.sleep(intervalle_s)
        except WebSocketDisconnect:
            pass

    app.mount("/static", StaticFiles(directory=DOSSIER_STATIC), name="static")
    return app
