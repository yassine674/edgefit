# edgefit

Tableau de bord local pour faire tourner des LLM sur une NVIDIA Jetson ou un Raspberry Pi : savoir avant de télécharger si un modèle rentre en mémoire, quoi faire sinon, puis discuter avec lui en mesurant ses performances. L'application tourne entièrement sur la carte ; depuis un PC du même réseau, il suffit d'ouvrir un navigateur.

## Fonctionnalités

Une appli d'hébergement d'IA locale, à la manière d'Open WebUI, pensée pour une petite carte :

- **Découvrir** : 26 modèles adaptés aux petites cartes, présentés en cartes avec leur verdict pour *cette* carte (tient, juste, trop gros), la mémoire nécessaire, la vitesse maximale théorique et le score mesuré. Filtres, recommandations, téléchargement en un clic. Au survol, la place du modèle apparaît dans la barre mémoire de la carte. La recherche interroge aussi Hugging Face (dépôts GGUF, avec le choix de la version).
- **Modèle** : verdict détaillé (poids, cache KV, buffers face à la mémoire libre mesurée), architecture, mémoire selon le contexte, et la liste complète des optimisations quand il ne tient pas. Le bouton Télécharger reste grisé tant que le modèle ne tient pas. La compatibilité est calculée **avant** de télécharger : seuls le manifeste et le premier mégaoctet du fichier GGUF sont lus.
- **Chat** : conversations enregistrées sur la carte (SQLite), instructions du système, température et contexte par conversation. Sous chaque réponse, une barre montre où est passé le temps (chargement, lecture du message, génération) et une phrase claire la résume (par exemple « 21 tok/s, environ 4 fois votre vitesse de lecture »). À droite : vitesse en direct, premier mot détaillé, mémoire estimée et mesurée, CPU, GPU, température, puissance, score.
- **Mes modèles** : modèles installés, chargés ou non, mémoire réelle, part sur le GPU, compte à rebours avant déchargement, espace disque ; décharger ou supprimer.
- **Bancs d'essai et score edgefit** : le même test pour chaque modèle (contexte 4K, 128 tokens, trois textes de même longueur). Score sur 100 = 60 % vitesse (2 à 20 tokens/s) + 40 % délai du premier mot (10 s à 0,5 s), plafonné à 60 s'il reste moins de 10 % de mémoire libre ; pas de score si le modèle ne tient pas. L'énergie (tokens par joule) est affichée à part.
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

En mode `--fake`, la vérification de compatibilité connaît les modèles installés simulés et deux modèles « à télécharger » : `gemma3:1b` et `llama3.1:8b`.

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
| `static/` | la page (HTML, CSS, JavaScript sans bibliothèque externe) |
