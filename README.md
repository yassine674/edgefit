# edgefit

L'outil de mesure des LLM sur carte embarquée (NVIDIA Jetson, Raspberry Pi, ou PC) : savoir **avant** de télécharger si un modèle tient en mémoire, choisir le bon modèle pour un besoin, puis mesurer ce qu'il fait vraiment sur *cette* carte : vitesse, délai du premier mot, mémoire réelle, énergie par token, tenue dans la durée quand la carte chauffe. L'application tourne entièrement sur la carte ; depuis un PC ou un téléphone du même réseau, il suffit d'ouvrir un navigateur.

## Fonctionnalités

Le menu a trois groupes : **Modèles** (Catalogue, Pour mon besoin, Installés), **Mesurer** (Bancs d'essai, Comparer, Endurance, Fiche) et **Carte** (Matériel, Réglages). Le chat est toujours à portée, avec la liste des conversations.

- **Découvrir** : 26 modèles adaptés aux petites cartes, présentés en cartes avec leur verdict pour *cette* carte (tient, juste, trop gros), la mémoire nécessaire, la vitesse maximale théorique et le score mesuré. Filtres, recommandations, téléchargement en un clic. Au survol, la place du modèle apparaît dans la barre mémoire de la carte. La recherche interroge aussi Hugging Face (dépôts GGUF, avec le choix de la version).
- **Pour mon besoin** : on choisit un usage (chat, code, raisonnement, vision, multilingue), une vitesse minimale et un contexte ; edgefit garde les modèles qui tiennent en mémoire et atteignent la vitesse, et propose le plus gros. La vitesse d'un modèle pas encore mesuré est estimée à partir du plafond théorique (bande passante mémoire / taille des poids) multiplié par le **rendement appris sur la carte** : médiane « débit mesuré / plafond » des bancs d'essai déjà faits (60 % tant qu'il n'y en a aucun, et la page le dit).
- **Modèle** : verdict détaillé (poids, cache KV, buffers face à la mémoire libre mesurée), architecture, mémoire selon le contexte, et la liste complète des optimisations quand il ne tient pas. Le bouton Télécharger reste grisé tant que le modèle ne tient pas. La compatibilité est calculée **avant** de télécharger : seuls le manifeste et le premier mégaoctet du fichier GGUF sont lus.
- **Chat** : conversations enregistrées sur la carte (SQLite), instructions du système, température et contexte par conversation. Sous chaque réponse, une barre montre où est passé le temps (chargement, lecture du message, génération) et une phrase claire la résume (par exemple « 21 tok/s, environ 4 fois votre vitesse de lecture »). À droite : vitesse en direct, premier mot détaillé, mémoire estimée et mesurée, CPU, GPU, température, puissance, énergie, score. Les réponses sont mises en forme (titres, listes, tableaux, blocs de code avec bouton Copier), sans jamais injecter de HTML venu du modèle.
- **Énergie par token** : pendant chaque réponse, la puissance est relevée 4 fois par seconde. Sous la réponse : joules par token (et la part au-dessus du repos, mesuré juste avant la question), énergie totale, puissance moyenne. Capteurs : Jetson (VDD_IN, carte entière), Raspberry Pi 5 (lignes du contrôleur d'alimentation, `vcgencmd pmic_read_adc`), PC avec carte NVIDIA (`nvidia-smi`, GPU seul). Sans capteur, rien n'est affiché plutôt qu'une valeur inventée.
- **Images** : avec un modèle de vision (moondream, qwen2.5vl, llava…, reconnu grâce aux capacités annoncées par Ollama), un bouton permet de joindre jusqu'à 4 images ; on peut aussi les coller (Ctrl+V) ou les glisser. Elles sont réduites à 1024 px dans le navigateur avant l'envoi, et ne sont pas gardées sur la carte (seul leur nombre est noté).
- **Mes modèles** : modèles installés, chargés ou non, mémoire réelle, part sur le GPU, compte à rebours avant déchargement, espace disque ; décharger ou supprimer.
- **Outils** : le modèle peut demander à edgefit d'exécuter des fonctions pendant une réponse : état de la carte (mémoire, températures, puissance), modèles installés, vérifier si un modèle tient en mémoire, calculatrice exacte, date et heure. Aucun outil n'est autorisé au départ : on les coche un par un (Réglages, ou réglages du chat). Les outils ne font que lire (aucune commande, aucun fichier modifié, aucun accès à internet) ; le serveur refuse tout nom hors de sa liste et tout outil non coché, limite chaque outil à 5 s et une question à 4 allers-retours. Chaque appel s'affiche dans la réponse avec son résultat. Il faut un modèle qui sait appeler des outils (Qwen 2.5 et 3, Llama 3.1 et 3.2…) ; sinon edgefit le dit et le modèle répond sans.
- **Voix** (optionnelle) : les réponses lues à voix haute par Piper, sur la carte, sans internet. Bouton Écouter sous chaque réponse, ou lecture automatique pendant que la réponse s'écrit (phrase par phrase ; les blocs de code et le Markdown ne sont pas lus ; voix choisie selon la langue de la réponse). Voix française `fr_FR-siwis-medium` (CC-BY 4.0) et anglaise `en_US-lessac-medium`, ≈ 63 Mo chacune, téléchargées depuis la page Réglages et vérifiées par leur empreinte SHA-256. Piper s'installe avec l'extra `voix` (`edgefit[voix]`, ≈ 80 Mo) ; l'installateur pose la question.
- **Comparer** : la même question posée à deux modèles installés, l'un après l'autre (deux modèles chargés ensemble ne tiennent souvent pas en mémoire). Réponses côte à côte avec leurs mesures, puis un bilan en valeurs médianes : vitesse, premier mot, chargement, mémoire réelle, durée, score du banc d'essai. Options : température 0 (réponses reproductibles) et déchargement avant chaque réponse (pour comparer aussi le temps de chargement). La qualité ne se mesure pas : on donne son avis sous chaque question. Rien n'est enregistré.
- **Bancs d'essai et score edgefit** : le même test pour chaque modèle (contexte 4K, 128 tokens, trois textes de même longueur). Score sur 100 = 60 % vitesse (2 à 20 tokens/s) + 40 % délai du premier mot (10 s à 0,5 s), plafonné à 60 s'il reste moins de 10 % de mémoire libre ; pas de score si le modèle ne tient pas. L'énergie (joules par token, mesurée pendant l'écriture) est affichée à part.
- **Endurance** : le modèle écrit sans arrêt de 1 à 20 minutes ; toutes les 5 s, débit d'écriture, température et puissance sont tracés. Le résumé compare le début et la fin, donne la baisse en %, le moment où le débit décroche et le verdict (vitesse stable, léger ralentissement, bridage thermique). C'est ce que vit un assistant ou un robot qui tourne toute la journée, et qu'un banc d'une minute ne voit pas.
- **Fiche de la carte** : tout ce qui a été mesuré sur la carte sur une page (identité, bancs d'essai, endurance, calibration), à imprimer ou enregistrer en PDF, ou à exporter en JSON (mesures brutes).
- **Calibration** : après chaque réponse (et chaque banc d'essai), edgefit compare la mémoire réellement occupée selon Ollama à son estimation et ajuste la formule propre à la carte (réel ≈ fixe + a × poids + b × cache KV, en partant de la formule de llm_modelfit). À partir de 3 réglages mesurés, les verdicts utilisent l'estimation corrigée ; l'estimation brute reste affichée à côté. Détail, coefficients et erreur avant/après sur la page Matériel.
- **Matériel** : identité de la carte (JetPack, mode d'énergie, Ollama), répartition de la mémoire, courbes en direct, journal des alertes.
- **Réglages** : langue, contexte par défaut, cache KV déclaré du serveur Ollama (avec les commandes pour le changer), santé de la carte, export et effacement des données.

Interface en français et en anglais, utilisable sur PC et sur téléphone. Les conversations et les bancs d'essai sont dans `~/.local/share/edgefit/edgefit.db` (variable `EDGEFIT_DB` pour un autre fichier).

## Installation (Jetson, Raspberry Pi ou PC Linux)

Une seule commande, à lancer sur la machine qui fera tourner les modèles :

```bash
curl -fsSL https://raw.githubusercontent.com/yassine674/edgefit/main/install.sh | sh
```

Le script vérifie la machine, l'horloge (une date fausse fait refuser tous les certificats HTTPS) et l'accès à internet, installe `uv` dans le dossier personnel, **demande** avant d'installer Ollama (son installateur officiel demande le mot de passe administrateur), installe edgefit (avec un Python récent si celui de la machine est trop ancien, par exemple sur JetPack 5) puis lance `edgefit diagnostic`.

Options, à placer après `sh -s --` :

```bash
curl -fsSL https://raw.githubusercontent.com/yassine674/edgefit/main/install.sh | sh -s -- --service
```

| Option | Effet |
|---|---|
| `--service` | démarre edgefit automatiquement (service systemd de l'utilisateur) |
| `--sans-ollama` | ne propose pas d'installer Ollama |
| `--version v0.1.0` | installe une version précise (tag ou branche) |
| `--oui` | répond oui aux questions |
| `--essai` | affiche ce qui serait fait, sans rien exécuter |

Ensuite :

```bash
edgefit              # lance le serveur et affiche l'adresse à ouvrir depuis le PC
edgefit adresse      # réaffiche cette adresse (utile quand edgefit tourne en service)
edgefit diagnostic   # vérifie Python, JetPack, tegrastats, Ollama, GPU, disque, réseau
```

Mise à jour : relancer la commande d'installation (ou `uv tool upgrade edgefit`). Désinstallation : `uv tool uninstall edgefit`.

## Sécurité

- Un jeton aléatoire est généré à chaque démarrage. La première visite avec `?token=...` pose un cookie `HttpOnly` et `SameSite=Strict`, puis le jeton disparaît de l'URL.
- Sans ce cookie, la page, l'API et le WebSocket sont refusés. Une page web d'un autre site ouverte sur le PC ne peut donc pas piloter l'application (CSRF).
- Le WebSocket vérifie en plus que la page qui se connecte vient bien de ce serveur (en-tête `Origin`).
- Après un redémarrage du serveur, le jeton change : la page le signale et il faut rouvrir la nouvelle adresse.
- edgefit ne lance jamais de commande d'administration : les réglages d'Ollama ou du système sont proposés sous forme de commandes à copier.

## Développement

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/edgefit --fake        # métriques, Ollama et registre simulés : ni Jetson ni Ollama nécessaires
.venv/bin/python -m pytest
```

En mode `--fake`, la carte simulée consomme davantage et chauffe pendant l'écriture (et ralentit au-delà de 70 °C), pour voir l'énergie et l'endurance sans matériel. La vérification de compatibilité connaît les modèles installés simulés et deux modèles « à télécharger » : `gemma3:1b` et `llama3.1:8b`.

Organisation du code (`edgefit/`) :

| Fichier | Rôle |
|---|---|
| `cli.py` | commande `edgefit` : démarrage, adresse avec jeton, `diagnostic` |
| `serveur.py` | application FastAPI : routes, contrôle d'accès, WebSocket |
| `securite.py` | jeton, cookie, vérification de l'origine |
| `metriques/` | lecture du matériel : psutil, tegrastats (Jetson), vcgencmd (Raspberry Pi), simulé |
| `ollama.py` | client de l'API locale d'Ollama (modèles, téléchargement, chat, mesures) |
| `registre.py` | infos d'un modèle avant téléchargement : manifeste et en-tête GGUF |
| `compatibilite.py` | calcul mémoire, réglages possibles et pistes (sans réseau, testable) |
| `diagnostic.py` | vérifications de `edgefit diagnostic` |
| `energie.py` | puissance relevée pendant une réponse : joules par token, brut et au-dessus du repos |
| `endurance.py` | test d'endurance : débit par fenêtre de 5 s, résumé début / fin, détection du bridage |
| `objectif.py` | choix d'un modèle par usage et vitesse, rendement appris sur les bancs de la carte |
| `static/` | la page (HTML, CSS, JavaScript sans bibliothèque externe) |
