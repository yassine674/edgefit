# accès au tableau de bord : un jeton aléatoire, nouveau à chaque démarrage (comme Jupyter)
# 1re visite avec http://<ip>:<port>/?token=<jeton> : le serveur pose un cookie et
# redirige vers la même page sans le jeton dans l'URL ; ensuite, le cookie suffit.
# Le cookie est SameSite=Strict : le navigateur ne l'envoie jamais depuis une page
# d'un autre site, donc une page web malveillante ouverte sur le PC ne peut pas
# piloter l'appli à distance (attaque CSRF / DNS rebinding).
import secrets
from urllib.parse import urlsplit

NOM_COOKIE = "edgefit_jeton"


def nouveau_jeton():
    return secrets.token_urlsafe(24)


def jeton_valide(propose, attendu):
    # compare_digest : durée de comparaison constante, ne laisse pas deviner le jeton caractère par caractère
    return propose is not None and secrets.compare_digest(propose.encode(), attendu.encode())


def origine_autorisee(origin, host):
    # WebSocket : le navigateur envoie toujours l'en-tête Origin (la page qui ouvre la
    # connexion) ; elle doit être servie par ce serveur lui-même
    if not origin or not host:
        return False
    return urlsplit(origin).netloc == host
