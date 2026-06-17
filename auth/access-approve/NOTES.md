# Homelab Approve — déploiement

Service d'approbation des inscriptions. L'accès à Jellyfin est protégé par le rôle `media` ;
ce service permet à l'administrateur d'approuver/refuser les nouveaux utilisateurs (page + e-mails).

## Déjà en place sur le serveur
- Rôle `media` dans le realm `homelab`, attribué à plusieurs utilisateurs.
- Le plugin SSO de Jellyfin exige le rôle `media` (`<Roles><string>media</string></Roles>`).
- Client Keycloak `access-approve` (confidential, standard flow + service account),
  redirect : `https://example.com/_access-approve/oauth2/callback`,
  le compte de service dispose de `manage-users`.

## Étapes de déploiement
1. Copier le dossier dans `/opt/docker/compose/access-approve/` sur l'hôte Docker.
2. Créer `.env` à partir de `.env.example` :
   - `OIDC_CLIENT_SECRET` = secret du client `access-approve`
     (`kcadm get clients/<id>/client-secret -r homelab`).
   - `SIGN_KEY`, `FLASK_SECRET` = chaînes aléatoires (`openssl rand -hex 32`).
   - SMTP_* = paramètres de la boîte No Reply (si les e-mails sont nécessaires d'emblée).
3. `docker compose up -d --build`
4. Ajouter dans le Caddyfile (à l'intérieur du bloc `example.com { ... }`, AVANT `handle { reverse_proxy jellyfin:8096 }`) :

   ```
   @approve path /_access-approve /_access-approve/*
   handle @approve {
       reverse_proxy access-approve:8090
   }
   ```
   puis `docker exec caddy caddy reload --config /etc/caddy/Caddyfile` (ou redémarrer caddy).

## Vérification
- Ouvrir `https://example.com/_access-approve/` → redirection vers la connexion Keycloak → après connexion admin, la liste des utilisateurs en attente s'affiche.
- Inscrire un utilisateur de test → il doit apparaître dans la liste (et un e-mail arrive si SMTP est configuré).
- « Approuver » → l'utilisateur reçoit le rôle `media` → il peut se connecter à Jellyfin.

## Retour arrière (rollback)
- Caddy : supprimer le bloc `@approve`, reload.
- `docker compose down` dans le dossier du service.
- Role-gating : remettre `<Roles />` dans SSO-Auth.xml (sauvegarde `.bak-rolegate-*` disponible) et redémarrer jellyfin.
