# edgefit

Tableau de bord local pour faire tourner des LLM sur une NVIDIA Jetson ou un Raspberry Pi : savoir avant de télécharger si un modèle rentre en mémoire, quoi faire sinon, puis discuter avec lui en mesurant ses performances. L'application tourne entièrement sur la carte ; depuis un PC du même réseau, il suffit d'ouvrir un navigateur.

## Fonctionnalités

- **Compatibilité avant téléchargement.** Pour un modèle Ollama (`llama3.2:3b`) ou un GGUF de Hugging Face (`hf.co/<dépôt>:Q4_K_M`), edgefit lit seulement le manifeste et le premier mégaoctet du fichier (taille exacte, couches, têtes KV, contexte), puis compare à la mémoire libre mesurée sur la carte, marge de sécurité comprise.
  - Si le modèle rentre : les réglages possibles (tel quel, meilleure qualité, plus long contexte, plus rapide), avec un plafond théorique de tokens/s.
  - Sinon : des pistes classées de la plus simple à la plus lourde (réduire le contexte, quantifier le cache KV d'Ollama, autre quantification, libérer de la mémoire, modèle plus petit, autre matériel), avec le gain de chaque piste et les commandes à lancer.
  - Les formules de mémoire (cache KV, buffers) viennent de [llm-modelfit](https://github.com/yassine674/llm-modelfit).
- **Matériel en direct** (WebSocket, chaque seconde) : CPU, GPU, mémoire, températures, puissance, alertes d'alimentation du Raspberry Pi.
- **Modèles** : liste des modèles installés dans Ollama, téléchargement avec progression. Pour un modèle protégé sur Hugging Face, Ollama s'authentifie avec sa propre clé SSH (`~/.ollama/id_ed25519.pub`, à ajouter dans les clés SSH du compte Hugging Face) : aucun token n'est stocké par l'application.
- **Chat** en streaming, avec le contexte (`num_ctx`) choisi. Sous chaque réponse : TTFT, débit de génération et de prefill, temps de chargement, et mémoire réellement occupée par le modèle selon Ollama (à comparer à l'estimation).

## Installation sur la Jetson (ou le Raspberry Pi)

Les commandes se lancent sur la carte elle-même (clavier et écran, ou terminal déjà ouvert dessus).

1. **Ollama**, le moteur qui fait tourner les modèles. Son script d'installation détecte la Jetson et installe la version qui utilise le GPU :
   ```bash
   curl -fsSL https://ollama.com/install.sh | sh
   ```
2. **uv**, l'outil qui installe edgefit dans son propre environnement. Il télécharge aussi une version récente de Python si celle de la carte est trop ancienne (JetPack 5 n'a que Python 3.8) :
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   source ~/.bashrc
   ```
3. **edgefit** :
   ```bash
   uv tool install git+https://github.com/yassine674/edgefit.git
   ```
4. **Vérifier la carte**, puis lancer :
   ```bash
   edgefit diagnostic
   edgefit
   ```
   Le terminal affiche l'adresse à ouvrir depuis le PC, par exemple `http://192.168.1.42:8000/?token=...`.

Mise à jour : `uv tool upgrade edgefit`. Désinstallation : `uv tool uninstall edgefit`.

`edgefit diagnostic` vérifie Python, la version de JetPack, tegrastats, le mode d'énergie, la mémoire, qu'Ollama répond, qu'il utilise bien le GPU et que son port n'est pas ouvert au réseau, l'espace disque et l'accès au registre. Il ne modifie rien : il indique quoi corriger.

Options de `edgefit` : `--port` (défaut 8000), `--hote` (défaut 0.0.0.0, joignable depuis le réseau local), `--ollama` (défaut `http://127.0.0.1:11434`). Le port d'Ollama n'a pas besoin d'être ouvert sur le réseau : seul edgefit lui parle.

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
