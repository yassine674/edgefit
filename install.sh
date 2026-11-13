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
#   4. installe edgefit avec uv (qui télécharge un Python récent si celui de la machine est trop vieux) ;
#   5. lance « edgefit diagnostic ».
#
# Options (après « sh -s -- » si le script est passé par un tuyau) :
#   --oui          répondre oui aux questions
#   --sans-ollama  ne pas proposer d'installer Ollama
#   --service      démarrer edgefit automatiquement (service systemd de l'utilisateur)
#   --version REF  version d'edgefit à installer (tag ou branche ; défaut : main)
#   --essai        afficher ce qui serait fait, sans rien exécuter
#
# Désinstallation : uv tool uninstall edgefit
set -eu

DEPOT="https://github.com/yassine674/edgefit"
VERSION="main"
OUI=0
SANS_OLLAMA=0
SERVICE=0
ESSAI=0

while [ $# -gt 0 ]; do
  case "$1" in
    --oui) OUI=1 ;;
    --sans-ollama) SANS_OLLAMA=1 ;;
    --service) SERVICE=1 ;;
    --essai) ESSAI=1 ;;
    --version) shift; VERSION="${1:?--version demande une valeur}" ;;
    -h|--help) sed -n '2,26p' "$0" 2>/dev/null || true; exit 0 ;;
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
    R32*) info "ATTENTION : JetPack 4 (L4T R32) est trop ancien pour qu'Ollama utilise le GPU : les modèles tourneront sur le CPU." ;;
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

# --- 4. edgefit --------------------------------------------------------------------------
etape "edgefit ($VERSION)"
lancer "uv tool install --force --python 3.12 'git+$DEPOT@$VERSION'"
lancer "uv tool update-shell >/dev/null 2>&1 || true"

if [ "$SERVICE" -eq 1 ]; then
  etape "Démarrage automatique"
  if command -v systemctl >/dev/null 2>&1; then
    UNITE="$HOME/.config/systemd/user/edgefit.service"
    lancer "mkdir -p '$HOME/.config/systemd/user' && printf '%s\n' '[Unit]' 'Description=edgefit' 'After=network-online.target' '' '[Service]' 'ExecStart=%h/.local/bin/edgefit' 'Restart=on-failure' '' '[Install]' 'WantedBy=default.target' > '$UNITE'"
    lancer "systemctl --user daemon-reload && systemctl --user enable --now edgefit"
    info "Pour qu'il démarre aussi sans session ouverte (au démarrage de la carte) :"
    info "  sudo loginctl enable-linger $(id -un)"
  else
    info "systemd introuvable : démarrage automatique non configuré"
  fi
fi

# --- 5. vérification ----------------------------------------------------------------------
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
info "Ouvrez un nouveau terminal si la commande edgefit n'est pas trouvée."
