#!/bin/sh
# Installation d'edgefit en une commande, sur une Jetson, un Raspberry Pi ou un PC Linux :
#
#   curl -fsSL https://raw.githubusercontent.com/yassine674/edgefit/main/install.sh | sh
#
# Ce que fait le script, dans l'ordre :
#   1. vérifie la machine, l'horloge (une date fausse fait refuser tous les certificats HTTPS)
#      et l'accès à internet ;
#   2. installe uv dans le dossier personnel (sans mot de passe) ;
#   3. si Ollama manque, DEMANDE avant de lancer son installateur officiel (qui, lui, demande
#      le mot de passe administrateur) ;
#   4. GPU : sur une Jetson JetPack 4 (CUDA 10.2, trop ancien pour Ollama), PROPOSE de compiler le
#      moteur GPU Vulkan officiel d'Ollama (recette « vulkan » de ses sources, même version) ;
#   5. installe edgefit avec uv (qui télécharge un Python récent si celui de la machine est trop vieux),
#      avec la voix (Piper) si on la demande ;
#   6. lance « edgefit diagnostic ».
#
# Options (après « sh -s -- » si le script est passé par un tuyau) :
#   --oui          répondre oui aux questions
#   --sans-ollama  ne pas proposer d'installer Ollama
#   --gpu          compiler le moteur GPU Vulkan d'Ollama sans poser la question (1 à 2 h sur une Jetson Nano)
#   --sans-gpu     ne pas le proposer
#   --voix         installer aussi la voix (Piper, ≈ 80 Mo) sans poser la question
#   --sans-voix    ne pas installer la voix
#   --service      démarrer edgefit automatiquement (service systemd de l'utilisateur)
#   --sans-jeton   avec --service : adresse sans jeton (réseau privé seulement, par exemple Tailscale)
#   --version REF  version d'edgefit à installer (tag ou branche ; défaut : main)
#   --essai        afficher ce qui serait fait, sans rien exécuter
#
# Désinstallation : uv tool uninstall edgefit
set -eu

DEPOT="https://github.com/yassine674/edgefit"
VERSION="main"
OUI=0
SANS_OLLAMA=0
VOIX=""
SERVICE=0
SANS_JETON=0
GPU=""
ESSAI=0
L4T=""

while [ $# -gt 0 ]; do
  case "$1" in
    --oui) OUI=1 ;;
    --sans-ollama) SANS_OLLAMA=1 ;;
    --gpu) GPU=1 ;;
    --sans-gpu) GPU=0 ;;
    --sans-jeton) SANS_JETON=1 ;;
    --voix) VOIX=1 ;;
    --sans-voix) VOIX=0 ;;
    --service) SERVICE=1 ;;
    --essai) ESSAI=1 ;;
    --version) shift; VERSION="${1:?--version demande une valeur}" ;;
    -h|--help) sed -n '2,30p' "$0" 2>/dev/null || true; exit 0 ;;
    *) echo "Option inconnue : $1" >&2; exit 2 ;;
  esac
  shift
done

info() { printf '  %s\n' "$*"; }
etape() { printf '\n== %s\n' "$*"; }
erreur() { printf '\nERREUR : %s\n' "$*" >&2; exit 1; }

# exécute une commande, ou l'affiche seulement en mode --essai
lancer() {
  if [ "$ESSAI" -eq 1 ]; then
    printf '  [essai] %s\n' "$*"
  else
    sh -c "$*"
  fi
}

# question oui/non lue sur le terminal (l'entrée standard est le script lui-même avec « curl | sh »)
demander() {
  if [ "$OUI" -eq 1 ]; then return 0; fi
  if [ ! -r /dev/tty ]; then return 1; fi
  printf '%s [O/n] ' "$1" > /dev/tty
  read -r reponse < /dev/tty || return 1
  case "$reponse" in [nN]*) return 1 ;; *) return 0 ;; esac
}

# --- 1. la machine ---------------------------------------------------------------------
etape "Machine"
SYSTEME=$(uname -s)
ARCH=$(uname -m)
[ "$SYSTEME" = "Linux" ] || [ "$SYSTEME" = "Darwin" ] || erreur "système non pris en charge : $SYSTEME"
CARTE=""
if [ -r /proc/device-tree/model ]; then
  CARTE=$(tr -d '\0' < /proc/device-tree/model)
fi
info "Système : $SYSTEME $ARCH${CARTE:+ · $CARTE}"
if [ -r /etc/nv_tegra_release ]; then
  L4T=$(sed -n 's/^# R\([0-9]*\) (release), REVISION: \([0-9.]*\).*/R\1.\2/p' /etc/nv_tegra_release)
  info "Jetson : L4T ${L4T:-inconnu}"
  case "$L4T" in
    R32*) info "JetPack 4 (L4T R32) : CUDA 10.2 est trop ancien pour Ollama ; le GPU sera utilisé par Vulkan (étape GPU)." ;;
  esac
fi

# une horloge en retard (carte sans pile, sans synchronisation) fait échouer tout le HTTPS
ANNEE=$(date +%Y)
if [ "$ANNEE" -lt 2025 ]; then
  erreur "l'horloge de la machine indique $(date '+%Y-%m-%d %H:%M'). Les certificats HTTPS seront refusés.
  Mettez-la à l'heure, puis relancez l'installation :
    sudo timedatectl set-ntp true          (synchronisation automatique)
  ou, à la main :
    sudo date -s \"AAAA-MM-JJ HH:MM:SS\""
fi
info "Horloge : $(date '+%Y-%m-%d %H:%M')"

command -v curl >/dev/null 2>&1 || erreur "curl est nécessaire : sudo apt install curl (après « sudo apt update »)"
if ! curl -fsS -m 15 -o /dev/null https://github.com; then
  erreur "pas d'accès à https://github.com. Vérifiez le réseau, le DNS et l'horloge."
fi
info "Accès internet : ok"

# --- 2. uv -----------------------------------------------------------------------------
etape "uv (gestionnaire de paquets Python)"
# ~/.local/bin est-il déjà dans le PATH du terminal de l'utilisateur ? (vérifié avant de l'ajouter ici)
case ":$PATH:" in *":$HOME/.local/bin:"*) DANS_PATH=1 ;; *) DANS_PATH=0 ;; esac
export PATH="$HOME/.local/bin:$PATH"
if command -v uv >/dev/null 2>&1; then
  info "déjà installé : $(uv --version)"
else
  info "installation dans $HOME/.local/bin (sans mot de passe)"
  lancer "curl -LsSf https://astral.sh/uv/install.sh | sh"
fi

# --- 3. Ollama ---------------------------------------------------------------------------
etape "Ollama (moteur des modèles)"
if command -v ollama >/dev/null 2>&1; then
  info "déjà installé : $(ollama --version 2>/dev/null | head -n 1)"
elif [ "$SANS_OLLAMA" -eq 1 ]; then
  info "non installé (--sans-ollama) : edgefit fonctionnera, mais sans modèles ni chat"
elif demander "Ollama n'est pas installé. Lancer son installateur officiel (il demandera votre mot de passe) ?"; then
  lancer "curl -fsSL https://ollama.com/install.sh | sh"
else
  info "non installé. Pour l'installer plus tard : curl -fsSL https://ollama.com/install.sh | sh"
fi

# --- 4. GPU par Vulkan -------------------------------------------------------------------
# Ollama ne livre son moteur GPU Vulkan que pour les PC x86. Sur une Jetson JetPack 4, son moteur
# CUDA ne peut pas servir (CUDA 10.2) alors que le GPU gère Vulkan 1.2 : on compile, depuis les
# sources officielles et à la version exacte d'Ollama, sa recette « vulkan » (llama/server), qui ne
# produit que lib/ollama/vulkan/libggml-vulkan.so. Ollama le charge tout seul ; rien d'autre ne change.
# Outils : en-têtes Vulkan et SPIR-V (Khronos), glslc (Google shaderc, absent d'Ubuntu 20.04),
# gcc >= 10, cmake >= 3.24. Tout est construit dans ~/.cache/edgefit/gpu (supprimable ensuite).
VULKAN_SDK_VERSION="1.4.321"   # même version que celle utilisée par Ollama pour ses propres builds
SHADERC_VERSION="v2026.4"

compiler_vulkan() {
  SDK="$HOME/.local/share/edgefit/vulkan-sdk"
  CONSTR="$HOME/.cache/edgefit/gpu"
  MEM_KO=$(sed -n 's/^MemTotal: *\([0-9]*\).*/\1/p' /proc/meminfo)
  if [ "${MEM_KO:-0}" -lt 6000000 ]; then JOBS=2; else JOBS=$(nproc); fi   # 4 Go : 2 compilations à la fois au plus
  CC_V=gcc; CXX_V=g++
  if command -v apt-get >/dev/null 2>&1; then
    PAQUETS="git ninja-build libvulkan-dev python3"
    if [ "$(gcc -dumpversion 2>/dev/null | cut -d. -f1)" -lt 10 ] 2>/dev/null; then
      PAQUETS="$PAQUETS gcc-10 g++-10"; CC_V=gcc-10; CXX_V=g++-10
    fi
    lancer "sudo apt-get update && sudo apt-get install -y $PAQUETS"
  else
    info "Installez git, ninja, les fichiers de développement Vulkan et gcc >= 10, puis relancez avec --gpu."
    return 0
  fi
  CMAKE_V=$(cmake --version 2>/dev/null | sed -n 's/^cmake version \([0-9]*\)\.\([0-9]*\).*/\1 \2/p')
  if [ -z "$CMAKE_V" ] || [ "$(echo "$CMAKE_V" | awk '{print ($1 > 3 || ($1 == 3 && $2 >= 24))}')" != 1 ]; then
    lancer "uv tool install cmake"
  fi
  lancer "mkdir -p '$CONSTR' '$SDK'"
  for depot in "Vulkan-Headers https://github.com/KhronosGroup/Vulkan-Headers v$VULKAN_SDK_VERSION" \
               "SPIRV-Headers https://github.com/KhronosGroup/SPIRV-Headers vulkan-sdk-$VULKAN_SDK_VERSION.0"; do
    set -- $depot
    lancer "[ -d '$CONSTR/$1' ] || git clone --depth 1 --branch '$3' '$2' '$CONSTR/$1'"
    lancer "cmake -S '$CONSTR/$1' -B '$CONSTR/$1/build' -DCMAKE_INSTALL_PREFIX='$SDK' >/dev/null && cmake --install '$CONSTR/$1/build' >/dev/null"
  done
  if [ ! -x "$SDK/bin/glslc" ]; then
    info "Compilation de glslc (environ 45 min sur une Jetson Nano)"
    lancer "[ -d '$CONSTR/shaderc' ] || git clone --depth 1 --branch '$SHADERC_VERSION' https://github.com/google/shaderc '$CONSTR/shaderc'"
    lancer "cd '$CONSTR/shaderc' && python3 utils/git-sync-deps && CC=$CC_V CXX=$CXX_V cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DSHADERC_SKIP_TESTS=ON -DSHADERC_SKIP_EXAMPLES=ON -DSHADERC_SKIP_COPYRIGHT_CHECK=ON -DSPIRV_SKIP_TESTS=ON -DSPIRV_SKIP_EXECUTABLES=ON -DENABLE_GLSLANG_BINARIES=OFF -DCMAKE_POLICY_VERSION_MINIMUM=3.5 && ninja -C build -j $JOBS glslc_exe && mkdir -p '$SDK/bin' && cp build/glslc/glslc '$SDK/bin/glslc'"
  fi
  info "Compilation du moteur Vulkan d'Ollama $OLLAMA_VER (environ 1 h sur une Jetson Nano)"
  lancer "[ -d '$CONSTR/ollama-$OLLAMA_VER' ] || git clone --depth 1 --branch 'v$OLLAMA_VER' https://github.com/ollama/ollama '$CONSTR/ollama-$OLLAMA_VER'"
  lancer "cd '$CONSTR/ollama-$OLLAMA_VER' && export VULKAN_SDK='$SDK' PATH='$SDK/bin:$PATH' CC=$CC_V CXX=$CXX_V && cmake -S llama/server --preset vulkan -DCMAKE_BUILD_TYPE=Release && cmake --build build/llama-server-vulkan -j $JOBS && rm -rf '$CONSTR/dist' && cmake --install build/llama-server-vulkan --component llama-server --strip --prefix '$CONSTR/dist'"
  if [ "$ESSAI" -eq 1 ]; then
    lancer "sudo cp <moteur compilé> '$OLLAMA_LIB/vulkan/' et activer Vulkan dans le service ollama"
    return 0
  fi
  MOTEUR=$(find "$CONSTR/dist" -name 'libggml-vulkan*.so*' | head -n 1)
  [ -n "$MOTEUR" ] || erreur "la compilation n'a pas produit libggml-vulkan.so (voir les messages ci-dessus)"
  lancer "sudo mkdir -p '$OLLAMA_LIB/vulkan' && sudo cp -P '$(dirname "$MOTEUR")'/* '$OLLAMA_LIB/vulkan/' && echo '$OLLAMA_VER' | sudo tee '$MARQUE' >/dev/null"
  # Le GPU d'une Jetson est intégré : Ollama ne l'utilise par Vulkan qu'avec ces deux réglages
  lancer "sudo mkdir -p /etc/systemd/system/ollama.service.d && printf '%s\n' '[Service]' 'Environment=\"OLLAMA_VULKAN=1\"' 'Environment=\"OLLAMA_IGPU_ENABLE=1\"' | sudo tee /etc/systemd/system/ollama.service.d/edgefit-vulkan.conf >/dev/null && sudo systemctl daemon-reload && sudo systemctl restart ollama"
  sleep 8
  CALCUL=$(journalctl -u ollama -b --no-pager 2>/dev/null | grep 'inference compute' | tail -n 1)
  case "$CALCUL" in
    *[Vv]ulkan*) info "GPU détecté par Ollama : ${CALCUL##*msg=}" ;;
    *) info "ATTENTION : Ollama ne cite pas encore Vulkan (${CALCUL:-journal illisible})."
       info "Retour arrière : sudo rm -r '$OLLAMA_LIB/vulkan' /etc/systemd/system/ollama.service.d/edgefit-vulkan.conf && sudo systemctl daemon-reload && sudo systemctl restart ollama" ;;
  esac
  info "Dossier de compilation supprimable : rm -rf '$CONSTR'"
}

etape "GPU"
OLLAMA_EXE=$(command -v ollama 2>/dev/null || true)
if [ "$SYSTEME" != "Linux" ] || [ -z "$OLLAMA_EXE" ]; then
  info "sans objet (Ollama absent ou système autre que Linux)"
else
  OLLAMA_LIB="$(dirname "$(dirname "$(readlink -f "$OLLAMA_EXE")")")/lib/ollama"
  OLLAMA_VER=$(ollama --version 2>/dev/null | sed -n 's/.*version is \([0-9][0-9.]*\).*/\1/p' | head -n 1)
  MARQUE="$OLLAMA_LIB/vulkan/edgefit-version"
  PROPOSER=0
  case "$L4T" in R32*) PROPOSER=1 ;; esac
  [ "$GPU" = "1" ] && PROPOSER=1
  if [ -r "$MARQUE" ] && [ "$(cat "$MARQUE")" = "$OLLAMA_VER" ]; then
    info "moteur Vulkan déjà compilé pour Ollama $OLLAMA_VER"
  elif [ ! -r "$MARQUE" ] && ls "$OLLAMA_LIB"/vulkan/libggml-vulkan* >/dev/null 2>&1; then
    info "moteur Vulkan fourni par Ollama lui-même : rien à faire"
  elif [ "$GPU" = "0" ] || [ "$PROPOSER" -eq 0 ]; then
    info "Ollama gère lui-même le GPU de cette machine (pour forcer le moteur Vulkan : --gpu)"
  elif [ -z "$OLLAMA_VER" ]; then
    info "version d'Ollama illisible : moteur Vulkan non compilé"
  else
    [ -r "$MARQUE" ] && info "Ollama a changé de version ($(cat "$MARQUE") -> $OLLAMA_VER) : le moteur Vulkan doit être recompilé"
    info "Ollama ne livre pas de moteur GPU utilisable sur cette carte, mais son GPU gère Vulkan."
    info "edgefit peut compiler le moteur Vulkan officiel d'Ollama $OLLAMA_VER : 1 à 2 h, environ 2 Go de disque."
    if [ "$GPU" = "1" ] || demander "Compiler le moteur GPU maintenant (mot de passe administrateur demandé) ?"; then
      compiler_vulkan
    else
      info "Plus tard : relancez l'installateur avec --gpu"
    fi
  fi
fi

# --- 5. edgefit --------------------------------------------------------------------------
etape "edgefit ($VERSION)"
if [ -z "$VOIX" ]; then
  if demander "Installer aussi la voix (lecture des réponses par Piper, environ 80 Mo) ?"; then VOIX=1; else VOIX=0; fi
fi
SANS="git+$DEPOT@$VERSION"
AVEC="edgefit[voix] @ git+$DEPOT@$VERSION"
if [ "$VOIX" -eq 1 ]; then
  if [ "$ESSAI" -eq 1 ]; then
    lancer "uv tool install --force --python 3.12 '$AVEC'"
  elif ! uv tool install --force --python 3.12 "$AVEC"; then
    # onnxruntime (utilisé par Piper) n'existe pas pour tous les systèmes anciens
    info "La voix n'a pas pu être installée sur ce système : edgefit est installé sans elle."
    lancer "uv tool install --force --python 3.12 '$SANS'"
  fi
else
  lancer "uv tool install --force --python 3.12 '$SANS'"
fi
# Sur Ubuntu, ~/.profile n'ajoute ~/.local/bin au PATH que si le dossier existait à l'ouverture
# de la session : juste après une première installation, « edgefit » serait introuvable, même
# dans un nouveau terminal. On l'ajoute donc aussi au fichier lu par chaque nouveau terminal.
if [ "$DANS_PATH" -eq 0 ]; then
  case "${SHELL:-}" in
    */zsh) RC="$HOME/.zshrc" ;;
    *) RC="$HOME/.bashrc" ;;
  esac
  if ! grep -qs 'edgefit : commandes installées par uv' "$RC"; then
    lancer "printf '\n# edgefit : commandes installées par uv\nexport PATH=\"\$HOME/.local/bin:\$PATH\"\n' >> '$RC'"
    info "$HOME/.local/bin ajouté au PATH dans $RC"
  fi
fi

if [ "$SERVICE" -eq 1 ]; then
  etape "Démarrage automatique"
  if command -v systemctl >/dev/null 2>&1; then
    UNITE="$HOME/.config/systemd/user/edgefit.service"
    EXEC="%h/.local/bin/edgefit"
    [ "$SANS_JETON" -eq 1 ] && EXEC="$EXEC --sans-jeton"
    lancer "mkdir -p '$HOME/.config/systemd/user' && printf '%s\n' '[Unit]' 'Description=edgefit' 'After=network-online.target' '' '[Service]' 'ExecStart=$EXEC' 'Restart=on-failure' '' '[Install]' 'WantedBy=default.target' > '$UNITE'"
    lancer "systemctl --user daemon-reload && systemctl --user enable --now edgefit"
    info "Pour qu'il démarre aussi sans session ouverte (au démarrage de la carte) :"
    info "  sudo loginctl enable-linger $(id -un)"
  else
    info "systemd introuvable : démarrage automatique non configuré"
  fi
fi

# --- 6. vérification ----------------------------------------------------------------------
etape "Diagnostic"
if [ "$ESSAI" -eq 1 ]; then
  info "[essai] edgefit diagnostic"
else
  "$HOME/.local/bin/edgefit" diagnostic || true
fi

etape "Terminé"
if [ "$SERVICE" -eq 1 ]; then
  info "edgefit tourne en arrière-plan. Adresse à ouvrir depuis le PC : edgefit adresse"
else
  info "Lancer edgefit :   edgefit"
  info "Puis ouvrir depuis le PC l'adresse affichée (elle se réaffiche avec : edgefit adresse)"
fi
if [ "$DANS_PATH" -eq 0 ]; then
  info ""
  info "IMPORTANT : ce terminal ne connaît pas encore la commande edgefit. Au choix :"
  info "  - ouvrir un nouveau terminal ;"
  info "  - ou taper ici : export PATH=\"\$HOME/.local/bin:\$PATH\""
fi
