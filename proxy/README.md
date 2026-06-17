# 🌐 Reverse proxy — Caddy

Caddy est la **porte d'entrée unique** du serveur. Il gère :

- le **TLS** (HTTPS automatique avec renouvellement des certificats),
- les **en-têtes de sécurité** (HSTS, nosniff, etc.),
- le **routage** vers chaque service.

Aperçu du routage :
- Domaine public → **Jellyfin** (avec page d'accueil Homelab et connexion TV par QR code),
- `auth.` → **Keycloak**,
- services d'administration (`*.local`) → **filtrés par réseau** (LAN + Tailscale uniquement).

> Le `Caddyfile` est inclus. Le vrai domaine a été remplacé par `example.com`.
