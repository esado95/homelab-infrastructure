# 🔐 Accès & SSO

L'authentification unique (SSO) et la validation des inscriptions — la partie du projet
qui m'a demandé le plus de réglages.

| Dossier | Rôle |
|---|---|
| [`keycloak/`](keycloak/) | Fournisseur d'identité (SSO / OpenID Connect) — un seul compte pour tous les services |
| [`sso-gateway/`](sso-gateway/) | **Passerelle SSO** (oauth2-proxy) : place tous les services d'administration derrière une seule connexion, avec contrôle par rôle |
| [`access-approve/`](access-approve/) | Service **maison** : valide les nouvelles inscriptions en un clic depuis un lien sécurisé |

L'accès à Jellyfin est protégé par un **rôle `media`** : un nouvel inscrit ne peut entrer
**qu'après validation de l'admin** (sinon, pas d'accès).

> 📂 Le **code source** de `access-approve` est inclus (`app.py`, `Dockerfile`, `compose`) — c'est un
> petit service Flask que j'ai écrit moi-même.
> 🔐 Les secrets restent dans des `.env` non versionnés (voir `access-approve/.env.example`).
