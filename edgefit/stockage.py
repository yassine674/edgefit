# base SQLite de l'application : conversations, messages (avec leurs mesures) et bancs d'essai.
# Elle vit sur la carte, dans le dossier de données de l'utilisateur, pour que les conversations
# se retrouvent depuis n'importe quel navigateur (PC, téléphone). EDGEFIT_DB impose un autre fichier.
#
# Une connexion courte par appel (appelée via asyncio.to_thread depuis le serveur) et le mode WAL :
# lectures et écritures ne se bloquent pas. On écrit une fois par message, jamais à chaque token,
# pour ne pas user une carte SD.
import json
import os
import sqlite3
import time
from pathlib import Path

from platformdirs import user_data_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    titre TEXT NOT NULL,
    modele TEXT NOT NULL,
    systeme TEXT NOT NULL DEFAULT '',
    temperature REAL,
    num_ctx INTEGER NOT NULL DEFAULT 4096,
    cree REAL NOT NULL,
    modifie REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    contenu TEXT NOT NULL,
    mesures_json TEXT,
    cree REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    carte TEXT NOT NULL,
    modele TEXT NOT NULL,
    num_ctx INTEGER NOT NULL,
    type_kv TEXT NOT NULL,
    poids_go REAL NOT NULL,
    kv_go REAL NOT NULL,
    estime_go REAL NOT NULL,
    mesure_go REAL NOT NULL,
    cree REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS bancs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    modele TEXT NOT NULL,
    protocole INTEGER NOT NULL,
    num_ctx INTEGER NOT NULL,
    type_kv TEXT NOT NULL,
    mesures_json TEXT NOT NULL,
    cree REAL NOT NULL
);
-- identité d'un modèle : avatar, description, et la recette d'optimisation d'un modèle créé dans l'atelier
CREATE TABLE IF NOT EXISTS profils (
    nom TEXT PRIMARY KEY,
    avatar TEXT,
    couleur TEXT,
    description TEXT NOT NULL DEFAULT '',
    base TEXT,
    recette_json TEXT,
    modifie REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS endurances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    modele TEXT NOT NULL,
    duree_s INTEGER NOT NULL,
    num_ctx INTEGER NOT NULL,
    resume_json TEXT NOT NULL,
    points_json TEXT NOT NULL,
    cree REAL NOT NULL
);
"""


def chemin_db():
    chemin = os.environ.get("EDGEFIT_DB")
    if chemin:
        return Path(chemin)
    return Path(user_data_dir("edgefit", appauthor=False)) / "edgefit.db"


def connexion():
    chemin = chemin_db()
    chemin.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(chemin, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def _ligne(r):
    return dict(r) if r is not None else None


# --- conversations -------------------------------------------------------------------

def lister_conversations(limite=200):
    with connexion() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, titre, modele, modifie FROM conversations ORDER BY modifie DESC LIMIT ?", (limite,))]


def creer_conversation(modele, titre="", systeme="", temperature=None, num_ctx=4096):
    maintenant = time.time()
    with connexion() as c:
        cur = c.execute(
            "INSERT INTO conversations (titre, modele, systeme, temperature, num_ctx, cree, modifie) VALUES (?,?,?,?,?,?,?)",
            (titre, modele, systeme, temperature, num_ctx, maintenant, maintenant))
        return lire_conversation(cur.lastrowid, c)


def lire_conversation(id_conv, c=None):
    def lire(c):
        conv = _ligne(c.execute("SELECT * FROM conversations WHERE id = ?", (id_conv,)).fetchone())
        if conv is None:
            return None
        conv["messages"] = [
            {"id": r["id"], "role": r["role"], "contenu": r["contenu"],
             "mesures": json.loads(r["mesures_json"]) if r["mesures_json"] else None}
            for r in c.execute("SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", (id_conv,))]
        return conv
    if c is not None:
        return lire(c)
    with connexion() as c:
        return lire(c)


CHAMPS_MODIFIABLES = ("titre", "modele", "systeme", "temperature", "num_ctx")


def modifier_conversation(id_conv, **champs):
    champs = {k: v for k, v in champs.items() if k in CHAMPS_MODIFIABLES}
    with connexion() as c:
        if champs:
            affectations = ", ".join(f"{k} = ?" for k in champs)
            c.execute(f"UPDATE conversations SET {affectations}, modifie = ? WHERE id = ?",
                      (*champs.values(), time.time(), id_conv))
        return lire_conversation(id_conv, c)


def supprimer_conversation(id_conv):
    with connexion() as c:
        return c.execute("DELETE FROM conversations WHERE id = ?", (id_conv,)).rowcount > 0


def ajouter_message(id_conv, role, contenu, mesures=None):
    maintenant = time.time()
    with connexion() as c:
        c.execute("INSERT INTO messages (conversation_id, role, contenu, mesures_json, cree) VALUES (?,?,?,?,?)",
                  (id_conv, role, contenu, json.dumps(mesures) if mesures else None, maintenant))
        # titre automatique : le début du premier message
        c.execute("UPDATE conversations SET modifie = ?, titre = CASE WHEN titre = '' AND ? = 'user' "
                  "THEN substr(?, 1, 60) ELSE titre END WHERE id = ?",
                  (maintenant, role, " ".join(contenu.split()), id_conv))


# --- bancs d'essai -------------------------------------------------------------------

def ajouter_banc(modele, protocole, num_ctx, type_kv, mesures):
    with connexion() as c:
        cur = c.execute("INSERT INTO bancs (modele, protocole, num_ctx, type_kv, mesures_json, cree) VALUES (?,?,?,?,?,?)",
                        (modele, protocole, num_ctx, type_kv, json.dumps(mesures), time.time()))
        return cur.lastrowid


def lister_bancs():
    with connexion() as c:
        return [{**dict(r), "mesures": json.loads(r["mesures_json"])}
                for r in c.execute("SELECT * FROM bancs ORDER BY cree DESC")]


def ajouter_endurance(modele, duree_s, num_ctx, resume, points):
    with connexion() as c:
        cur = c.execute("INSERT INTO endurances (modele, duree_s, num_ctx, resume_json, points_json, cree) VALUES (?,?,?,?,?,?)",
                        (modele, duree_s, num_ctx, json.dumps(resume), json.dumps(points), time.time()))
        return cur.lastrowid


def lister_endurances(limite=50):
    with connexion() as c:
        return [{"id": r["id"], "modele": r["modele"], "duree_s": r["duree_s"], "num_ctx": r["num_ctx"], "cree": r["cree"],
                 "resume": json.loads(r["resume_json"]), "points": json.loads(r["points_json"])}
                for r in c.execute("SELECT * FROM endurances ORDER BY cree DESC LIMIT ?", (limite,))]


# --- profils : l'identité des modèles (avatar, description, recette de l'atelier) -------------

def ecrire_profil(nom, avatar=None, couleur=None, description="", base=None, recette=None):
    with connexion() as c:
        c.execute("INSERT INTO profils (nom, avatar, couleur, description, base, recette_json, modifie) VALUES (?, ?, ?, ?, ?, ?, ?) "
                  "ON CONFLICT(nom) DO UPDATE SET avatar=excluded.avatar, couleur=excluded.couleur, description=excluded.description, "
                  "base=COALESCE(excluded.base, profils.base), recette_json=COALESCE(excluded.recette_json, profils.recette_json), "
                  "modifie=excluded.modifie",
                  (nom, avatar, couleur, description, base, json.dumps(recette, ensure_ascii=False) if recette is not None else None, time.time()))


def lire_profils():
    with connexion() as c:
        return {r["nom"]: {"avatar": r["avatar"], "couleur": r["couleur"], "description": r["description"], "base": r["base"],
                           "recette": json.loads(r["recette_json"]) if r["recette_json"] else None}
                for r in c.execute("SELECT * FROM profils")}


def supprimer_profil(nom):
    with connexion() as c:
        c.execute("DELETE FROM profils WHERE nom = ?", (nom,))


def tout_effacer(table):
    """Efface toutes les conversations (et leurs messages), tous les bancs d'essai, etc."""
    assert table in ("conversations", "bancs", "observations", "endurances")
    with connexion() as c:
        return c.execute(f"DELETE FROM {table}").rowcount


def exporter():
    with connexion() as c:
        convs = [lire_conversation(r["id"], c) for r in c.execute("SELECT id FROM conversations ORDER BY id")]
    return {"conversations": convs, "bancs": lister_bancs(), "endurances": lister_endurances(1000)}


# --- calibration : mémoire estimée contre mémoire mesurée, par carte ------------------------

def ajouter_observation(carte, modele, num_ctx, type_kv, poids_go, kv_go, estime_go, mesure_go):
    """Une seule observation par (modèle, contexte, cache KV) : la dernière remplace les précédentes,
    pour qu'une longue conversation ne pèse pas plus qu'un essai."""
    with connexion() as c:
        c.execute("DELETE FROM observations WHERE carte = ? AND modele = ? AND num_ctx = ? AND type_kv = ?",
                  (carte, modele, num_ctx, type_kv))
        c.execute("INSERT INTO observations (carte, modele, num_ctx, type_kv, poids_go, kv_go, estime_go, mesure_go, cree) "
                  "VALUES (?,?,?,?,?,?,?,?,?)", (carte, modele, num_ctx, type_kv, poids_go, kv_go, estime_go, mesure_go, time.time()))


def lister_observations(carte):
    with connexion() as c:
        return [dict(r) for r in c.execute("SELECT * FROM observations WHERE carte = ? ORDER BY cree DESC", (carte,))]
