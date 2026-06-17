# 🎬 Média

Le cœur du divertissement : streaming et acquisition automatisée des contenus.

| Service | Rôle |
|---|---|
| **Jellyfin** | Serveur de streaming (films, séries, musique) — accès protégé par le rôle SSO `media` |
| **Jellyseerr** | Portail de demandes de contenus |
| **Prowlarr** | Agrégateur d'indexeurs pour la suite *arr |
| **Sonarr / Radarr** | Gestion automatisée des séries / films |
| **qBittorrent** | Client de téléchargement |
| **FlareSolverr** | Contournement des protections anti-bot des indexeurs |
| **jellyfin-automation** | Petits automatismes autour de Jellyfin |

> 🔐 Aucun secret ici : les mots de passe et clés vivent dans des fichiers `.env` **non versionnés**.
