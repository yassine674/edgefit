# l'application web : une seule appli FastAPI qui sert à la fois l'API et la page
# du tableau de bord. La page ne passe que par l'API et le WebSocket, jamais par le
# code interne : elle pourrait être servie ailleurs sans rien changer côté serveur.
import asyncio
import json
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AfterValidator, BaseModel, Field

from .compatibilite import OCTETS_KV, analyser, budget
from .ollama import GO, ModeleInconnu, OllamaIndisponible, nom_valide
from .registre import Registre, RegistreErreur
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


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=20_000)


# bornes larges : Ollama accepte jusqu'au contexte maximal du modèle (128K pour Llama 3.1...)
Contexte = Annotated[int, Field(ge=256, le=1_048_576)]
TypeKV = Literal[tuple(OCTETS_KV)]


class DemandeChat(BaseModel):
    modele: NomModele
    messages: list[Message] = Field(min_length=1, max_length=200)
    # toujours envoyé à Ollama : sa valeur par défaut change d'une version à l'autre,
    # et c'est elle qui fixe la taille du cache KV réservé
    num_ctx: Contexte = 4096


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


def creer_app(fournisseur, ollama, jeton, registre=None, intervalle_s=1.0):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    registre = registre or Registre()

    @app.middleware("http")
    async def verifier_acces(request: Request, call_next):
        propose = request.query_params.get("token")
        if propose is not None:
            if not jeton_valide(propose, jeton):
                return HTMLResponse(PAGE_ACCES_REFUSE, status_code=403)
            reponse = RedirectResponse(str(request.url.remove_query_params("token")), status_code=303)
            reponse.set_cookie(NOM_COOKIE, jeton, httponly=True, samesite="strict")
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
        try:
            return {"disponible": True, "version": await ollama.version(), "url": ollama.url}
        except OllamaIndisponible:
            return {"disponible": False, "version": None, "url": ollama.url}

    @app.get("/api/modeles")
    async def modeles():
        return await ollama.modeles()

    # nom en paramètre de requête : les noms Ollama contiennent "/" et ":"
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
        messages = [m.model_dump() for m in demande.messages]
        flux = ollama.discuter(demande.modele, messages, {"num_ctx": demande.num_ctx})
        return _ndjson(ollama, _avec_memoire_chargee(ollama, demande.modele, flux))

    @app.post("/api/compatibilite")
    async def compatibilite(demande: DemandeCompatibilite):
        # 1. le fichier : installé (Ollama connaît sa taille exacte) ou à télécharger (registre)
        try:
            infos = await ollama.infos(demande.modele)
        except OllamaIndisponible:
            infos = None
        source = "installé"
        if infos is None:
            source = "registre"
            try:
                infos = await registre.infos(demande.modele)
            except RegistreErreur as e:
                raise HTTPException(422, str(e))
        arch = infos["architecture"]
        if not all(arch.get(c) for c in ("num_layers", "num_kv_heads", "head_dim")):
            raise HTTPException(422, "architecture incomplète dans les métadonnées du modèle")

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
        resultat["source"] = source
        resultat["taille_fichier_go"] = infos["taille_octets"] / GO
        resultat["charges"] = [{"nom": m["nom"], "taille_go": m["taille"] / GO} for m in charges]
        return resultat

    # le middleware HTTP ne s'applique pas aux WebSockets : contrôle refait ici
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
