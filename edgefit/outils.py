# outils : des fonctions que le modèle peut demander à edgefit d'exécuter pendant une réponse.
#
# Le déroulé (« tool calling » d'Ollama) :
#   1. edgefit envoie la question ET la liste des outils autorisés (nom, description, paramètres) ;
#   2. au lieu de répondre, le modèle peut demander « appelle board_status » ;
#   3. edgefit (et non le modèle) exécute la fonction, renvoie le résultat au modèle,
#      qui écrit alors sa réponse.
#
# Règles de sécurité :
#   - chaque outil est une fonction écrite ici, en lecture seule : pas de commande shell,
#     pas d'écriture de fichier, pas d'accès réseau ;
#   - seuls les outils de cette liste existent, et seuls ceux que l'utilisateur a cochés
#     sont proposés au modèle ; un appel à un autre nom est refusé sans rien exécuter ;
#   - arguments vérifiés, 5 s au plus par outil, résultat tronqué, 4 allers-retours au plus.
#
# Les noms et descriptions sont en anglais : c'est le modèle qui les lit, et les petits
# modèles appellent mieux les outils décrits en anglais. L'interface les traduit.
import ast
import asyncio
import json
import math
import operator
import time

from .catalogue import SUGGESTIONS
from .compatibilite import GIO, verdict_rapide
from .ollama import ModeleInconnu, OllamaIndisponible

MAX_TOURS = 4           # allers-retours modèle -> outil -> modèle, par question
DELAI_S = 5             # temps maximal d'un outil
TAILLE_MAX = 4000       # caractères renvoyés au modèle


class ErreurOutil(Exception):
    pass


# --- calculatrice sûre : seule l'arithmétique est évaluée, jamais du code Python -------------

_OPERATIONS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
               ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_UNAIRES = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FONCTIONS = {"sqrt": math.sqrt, "log": math.log, "log2": math.log2, "log10": math.log10, "exp": math.exp,
              "abs": abs, "round": round, "min": min, "max": max, "ceil": math.ceil, "floor": math.floor}
_CONSTANTES = {"pi": math.pi, "e": math.e}


def calculer(expression):
    """Évalue une expression arithmétique (« 2 * 32 * 8 * 128 * 4096 * 2 / 1024**3 ») en parcourant
    son arbre : tout ce qui n'est pas un nombre, une opération ou une fonction de la liste est refusé."""
    if not isinstance(expression, str) or len(expression) > 200:
        raise ErreurOutil("expression must be a string of at most 200 characters")
    try:
        arbre = ast.parse(expression.replace("^", "**").replace("×", "*"), mode="eval")
    except SyntaxError as e:
        raise ErreurOutil(f"invalid expression: {e.msg}") from e

    def val(n):
        if isinstance(n, ast.Expression):
            return val(n.body)
        if isinstance(n, ast.Constant) and type(n.value) in (int, float):
            return n.value
        if isinstance(n, ast.Name) and n.id in _CONSTANTES:
            return _CONSTANTES[n.id]
        if isinstance(n, ast.UnaryOp) and type(n.op) in _UNAIRES:
            return _UNAIRES[type(n.op)](val(n.operand))
        if isinstance(n, ast.BinOp) and type(n.op) in _OPERATIONS:
            gauche, droite = val(n.left), val(n.right)
            # 9**9**9 bloquerait le serveur : on borne les puissances
            if isinstance(n.op, ast.Pow) and (abs(droite) > 64 or abs(gauche) > 1e6):
                raise ErreurOutil("power too large")
            return _OPERATIONS[type(n.op)](gauche, droite)
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FONCTIONS
                and not n.keywords and len(n.args) <= 3):
            return _FONCTIONS[n.func.id](*[val(a) for a in n.args])
        raise ErreurOutil("only numbers, + - * / // % **, parentheses and " + ", ".join(_FONCTIONS) + " are allowed")

    try:
        resultat = val(arbre)
    except (ZeroDivisionError, OverflowError, ValueError) as e:
        raise ErreurOutil(str(e)) from e
    return {"expression": expression, "result": round(resultat, 10) if isinstance(resultat, float) else resultat}


# --- les outils --------------------------------------------------------------------------------

def _definition(nom, description, proprietes=None, requis=()):
    return {"type": "function", "function": {
        "name": nom, "description": description,
        "parameters": {"type": "object", "properties": proprietes or {}, "required": list(requis)}}}


DEFINITIONS = {
    "board_status": _definition(
        "board_status",
        "Live measurements of the board this assistant runs on: memory used and free, CPU and GPU load, "
        "temperatures, power draw, and which models are loaded in memory right now."),
    "installed_models": _definition(
        "installed_models",
        "List the language models installed on this board, with file size, parameters and quantization."),
    "check_model_fit": _definition(
        "check_model_fit",
        "Check whether a model fits in this board's free memory for a given context length. "
        "Returns the estimated memory (weights + KV cache + buffers) and the verdict.",
        {"model": {"type": "string", "description": "Ollama model name, for example llama3.2:3b or qwen3:8b"},
         "context": {"type": "integer", "description": "context length in tokens (default 4096)"}},
        ["model"]),
    "calculator": _definition(
        "calculator",
        "Evaluate an arithmetic expression exactly. Use it for any computation instead of guessing.",
        {"expression": {"type": "string", "description": "for example (2 * 28 * 8 * 128 * 4096 * 2) / 1024**3"}},
        ["expression"]),
    "current_time": _definition("current_time", "Current date and time on the board."),
}
NOMS = tuple(DEFINITIONS)


class Boite:
    """Exécute les outils pour une carte : accès en lecture aux mesures, à Ollama et au budget mémoire."""

    def __init__(self, fournisseur, ollama, budget_actuel):
        self.fournisseur = fournisseur
        self.ollama = ollama
        self.budget_actuel = budget_actuel  # async () -> (budget, modèles chargés, mémoire)

    def definitions(self, autorises):
        return [DEFINITIONS[n] for n in NOMS if n in autorises]

    async def executer(self, nom, arguments, autorises):
        """Résultat (dict) prêt à renvoyer au modèle ; une erreur devient {"error": ...} pour qu'il puisse
        se corriger, au lieu de couper la réponse."""
        if nom not in DEFINITIONS:
            return {"error": f"unknown tool '{nom}'. Available tools: {', '.join(sorted(autorises))}"}
        if nom not in autorises:
            return {"error": f"tool '{nom}' is not allowed by the user"}
        if not isinstance(arguments, dict):
            arguments = {}
        try:
            return await asyncio.wait_for(getattr(self, f"_{nom}")(**_arguments(nom, arguments)), DELAI_S)
        except ErreurOutil as e:
            return {"error": str(e)}
        except asyncio.TimeoutError:
            return {"error": f"tool '{nom}' took more than {DELAI_S} s"}
        except OllamaIndisponible:
            return {"error": "Ollama is not responding"}

    async def _board_status(self):
        m = await asyncio.to_thread(self.fournisseur.lire)
        b, charges, _ = await self.budget_actuel()
        garder = ("cpu_pct", "gpu_pct", "temp_cpu_c", "temp_gpu_c", "puissance_w")
        noms = {"cpu_pct": "cpu_load_percent", "gpu_pct": "gpu_load_percent", "temp_cpu_c": "cpu_temperature_c",
                "temp_gpu_c": "gpu_temperature_c", "puissance_w": "power_w"}
        etat = {"board": m.get("machine"), "memory_total_gb": m.get("ram_totale_go"),
                "memory_used_gb": m.get("ram_utilisee_go"),
                "memory_available_for_a_new_model_gb": round(b["utilisable"] / GIO, 2)}
        etat.update({noms[k]: m[k] for k in garder if m.get(k) is not None})
        etat["loaded_models"] = [{"name": c["nom"], "memory_gb": round(c["taille"] / GIO, 2)} for c in charges]
        return etat

    async def _installed_models(self):
        return {"models": [{"name": m["nom"], "file_gb": round(m["taille_go"], 2), "parameters": m.get("parametres"),
                            "quantization": m.get("quantification")} for m in await self.ollama.modeles()]}

    async def _check_model_fit(self, model, context=4096):
        try:
            infos = await self.ollama.infos(model)
        except ModeleInconnu:
            infos = None
        source = "installed"
        if infos is None:
            entree = next((e for e in SUGGESTIONS if e["nom"] == model), None)
            if entree is None:
                proches = [e["nom"] for e in SUGGESTIONS if model.split(":")[0] in e["nom"]][:5]
                raise ErreurOutil(f"model '{model}' is neither installed nor in the edgefit catalogue"
                                  + (f"; similar: {', '.join(proches)}" if proches else ""))
            infos = {"taille_octets": entree["taille_octets"],
                     "architecture": {**entree["architecture"], "projecteur_octets": entree.get("projecteur_octets", 0)}}
            source = "catalogue (not installed)"
        b, _, _ = await self.budget_actuel()
        v = verdict_rapide(infos["architecture"], infos["taille_octets"], b, context)
        verdicts = {"ok": "fits", "ok_apres_dechargement": "fits after unloading the models currently loaded",
                    "trop_gros": "does not fit"}
        return {"model": model, "source": source, "context": context, "verdict": verdicts[v["verdict"]],
                "estimated_total_gb": round(v["total_go"], 2), "weights_gb": round(v["poids_go"], 2),
                "kv_cache_gb": round(v["cache_kv_go"], 2), "memory_left_after_loading_gb": round(v["reste_go"], 2)}

    async def _calculator(self, expression):
        return calculer(expression)

    async def _current_time(self):
        return {"local_time": time.strftime("%Y-%m-%d %H:%M:%S"), "timezone": time.strftime("%Z")}


def _arguments(nom, arguments):
    """Ne garde que les paramètres déclarés, avec le bon type (les petits modèles envoient parfois
    « "4096" » au lieu de 4096, ou des paramètres inventés)."""
    proprietes = DEFINITIONS[nom]["function"]["parameters"]["properties"]
    requis = DEFINITIONS[nom]["function"]["parameters"]["required"]
    propres = {}
    for cle, schema in proprietes.items():
        if cle not in arguments or arguments[cle] in (None, ""):
            continue
        v = arguments[cle]
        if schema["type"] == "integer":
            try:
                v = int(float(v))
            except (TypeError, ValueError) as e:
                raise ErreurOutil(f"'{cle}' must be an integer") from e
            if not 256 <= v <= 1_048_576:
                raise ErreurOutil(f"'{cle}' must be between 256 and 1048576")
        elif not isinstance(v, str) or len(v) > 200:
            raise ErreurOutil(f"'{cle}' must be a short string")
        propres[cle] = v
    manquants = [c for c in requis if c not in propres]
    if manquants:
        raise ErreurOutil(f"missing argument: {', '.join(manquants)}")
    return propres


def pour_le_modele(resultat):
    """Texte renvoyé au modèle (message de rôle « tool »)."""
    texte = json.dumps(resultat, ensure_ascii=False)
    return texte if len(texte) <= TAILLE_MAX else texte[:TAILLE_MAX] + "…"
