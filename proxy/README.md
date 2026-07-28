# 🌐 Reverse proxy — Caddy

Caddy est la **porte d'entrée unique** du serveur. Il gère :

- le **TLS** (HTTPS automatique avec renouvellement des certificats),
- les **en-têtes de sécurité** (HSTS, nosniff, etc.),
- le **routage** vers chaque service.

Aperçu du routage :
- Domaine public → **Jellyfin** (avec page d'accueil Homelab et connexion TV par QR code),
- `auth.` → **Keycloak**,
- un sous-domaine par service d'administration → **derrière la passerelle SSO**
  (`forward_auth` vers oauth2-proxy, contrôle par rôle Keycloak) — voir
  [`auth/sso-gateway/`](../auth/sso-gateway/) pour le snippet à importer et les explications,
- hôtes `*.local` → l'ancien chemin, **filtré par réseau** (LAN + Tailscale), gardé comme repli
  si la passerelle SSO est indisponible.

> Le `Caddyfile` est inclus. Le vrai domaine a été remplacé par `example.com`.
