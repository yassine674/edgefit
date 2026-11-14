# energie : combien d'énergie coûte une réponse, mesurée sur la carte.
#
# Pendant une génération, on relève la puissance électrique quatre fois par seconde. Ensuite :
#   énergie de la réponse (J)  = puissance moyenne (W) × durée totale (s)
#   énergie par token (J/tok)  = puissance moyenne pendant l'écriture × durée d'écriture / tokens écrits
#   part due au modèle         = la même chose, moins la puissance de la carte au repos (mesurée
#                                juste avant la question) : ce que coûte vraiment l'inférence.
#
# Ce que mesure le capteur dépend de la carte (champ « source ») :
#   Jetson        : VDD_IN, la carte entière (CPU, GPU, mémoire) ;
#   Raspberry Pi 5: somme des lignes du contrôleur d'alimentation (PMIC), sans les ports USB ;
#   PC + NVIDIA   : le GPU seul (nvidia-smi), sans le processeur ni la RAM.
# Sans capteur (PC sans NVIDIA : les compteurs Intel RAPL sont réservés à root), rien n'est inventé.
import asyncio
import time

PERIODE_S = 0.25


class Releveur:
    def __init__(self, fournisseur, periode_s=PERIODE_S):
        self.fournisseur = fournisseur
        self.periode_s = periode_s
        self.repos_w = None
        self.source = None
        self.releves = []          # (instant, watts)
        self.premier_token = None
        self._tache = None

    async def demarrer(self):
        m = await asyncio.to_thread(self.fournisseur.lire)
        self.repos_w = m.get("puissance_w")
        self.source = m.get("source_puissance")
        if self.repos_w is not None:  # pas de capteur : inutile de relever
            self._tache = asyncio.create_task(self._relever())
        return self

    async def _relever(self):
        while True:
            m = await asyncio.to_thread(self.fournisseur.lire)
            if m.get("puissance_w") is not None:
                self.releves.append((time.perf_counter(), m["puissance_w"]))
            await asyncio.sleep(self.periode_s)

    def token(self):
        if self.premier_token is None:
            self.premier_token = time.perf_counter()

    def arreter(self):
        if self._tache:
            self._tache.cancel()
            self._tache = None

    def resumer(self, mesures):
        """Champs à ajouter aux mesures d'une réponse (vide sans capteur)."""
        self.arreter()
        if self.repos_w is None or not self.releves:
            return {}
        tous = [w for _, w in self.releves]
        # pendant l'écriture seulement : le chargement et la lecture du message consomment autrement
        ecriture = [w for t, w in self.releves if self.premier_token and t >= self.premier_token] or tous
        moyenne, moyenne_ecriture = sum(tous) / len(tous), sum(ecriture) / len(ecriture)
        r = {"puissance_moy_w": moyenne, "puissance_repos_w": self.repos_w, "source_puissance": self.source,
             "energie_j": moyenne * (mesures.get("duree_totale_s") or 0)}
        tokens, debit = mesures.get("tokens_generes"), mesures.get("debit_tokens_s")
        if tokens and debit:
            duree_ecriture = tokens / debit
            r["joules_par_token"] = moyenne_ecriture * duree_ecriture / tokens
            r["joules_par_token_net"] = max(0.0, moyenne_ecriture - self.repos_w) * duree_ecriture / tokens
            r["tokens_par_joule"] = debit / moyenne_ecriture if moyenne_ecriture else None
        r["releves"] = len(self.releves)
        return r
