#!/bin/bash
# ---------------------------------------------------------------------------
# Sauvegarde quotidienne « ciblée » du serveur (VM Docker).
#
# Idée : ne sauvegarder QUE ce qui permet de reconstruire le serveur — pas les
# fichiers volumineux et re-téléchargeables. Résultat : ~90 Mo par jour.
#
#   1. les bases de données (Keycloak, appli maison, tableau de bord SQLite)
#   2. un inventaire du système (conteneurs, images, cron, disques, ports)
#   3. les fichiers Compose, les configurations et les données des services
#
# Exclus volontairement : médiathèque, métriques Prometheus, cache et
# métadonnées Jellyfin (voir README.md).
#
# Sortie : /srv/backup/homelab/homelab-AAAAMMJJ_HHMM.tar.gz
# Cron   : 30 3 * * * $HOME/bin/backup-configs.sh >> $HOME/logs/backup.log 2>&1
# ---------------------------------------------------------------------------
set -e

SRC=/opt/docker              # racine des services (compose / config / data)
DST=/srv/backup/homelab      # disque dédié aux sauvegardes, monté dans la VM
KEEP=14                      # nombre d'archives conservées
TS=$(date +%Y%m%d_%H%M)
STAGE=/tmp/homelab-backup-$TS

mkdir -p "$STAGE"
trap 'rm -rf "$STAGE"' EXIT

# --- 1. Bases de données ---------------------------------------------------
# Aucun mot de passe n'est écrit ici : on réutilise les variables d'environnement
# déjà présentes DANS le conteneur (d'où les quotes simples).
docker exec keycloak-postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > "$STAGE/keycloak.sql"

# Base d'une autre application hébergée sur le même serveur (hors périmètre de ce dépôt) :
# on ne la sauvegarde que si le conteneur existe, sinon le script s'arrêterait ici.
if docker inspect app-postgres >/dev/null 2>&1; then
  docker exec app-postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > "$STAGE/app.sql"
fi

# Portail (Homarr) : base SQLite copiée à chaud avec « VACUUM INTO »,
# qui produit une copie cohérente sans arrêter le service.
docker run --rm -v "$SRC/data/homarr":/d -v "$STAGE":/s alpine sh -c \
  "apk add -q sqlite && sqlite3 /d/db/db.sqlite \"VACUUM INTO '/s/homarr.sqlite'\"" 2>/dev/null

# --- 2. Inventaire du système ---------------------------------------------
# Pour savoir, six mois plus tard, ce qui tournait et dans quelle version.
{
  echo "# Instantané du serveur — $(date)"
  echo; echo "## Conteneurs et versions des images"
  docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
  echo; echo "## Cron de l'utilisateur"
  crontab -l 2>/dev/null
  echo; echo "## Disques"
  df -h | grep -Ev 'tmpfs|udev'
  echo; echo "## Ports en écoute"
  ss -tlnp 2>/dev/null | awk '{print $1, $4}'
} > "$STAGE/manifest.txt" 2>&1

# --- 3. Configurations et données des services -----------------------------
# Une partie des fichiers appartient à root et je n'ai pas sudo sur ce compte :
# un conteneur alpine jetable (root dans son namespace) lit le dossier en
# lecture seule et écrit l'archive dans le dossier de travail.
docker run --rm \
  -v "$SRC":/src/services:ro \
  -v "$STAGE":/stage \
  alpine tar czf /stage/configs.tar.gz \
    --exclude='*/prometheus/*' \
    --exclude='*/jellyfin/config/data/metadata/*' \
    --exclude='*/jellyfin/config/cache/*' \
    --exclude='*/jellyfin/config/log/*' \
    --exclude='*/jellyfin/config/data/transcodes/*' \
    --exclude='*.bak-*' \
    -C /src \
    services/compose services/config services/admin services/scripts services/data

# --- 4. Archive finale -----------------------------------------------------
mkdir -p "$DST"
tar czf "$DST/homelab-$TS.tar.gz" -C "$STAGE" .
# 600 : l'archive contient les fichiers .env, donc des secrets en clair.
chmod 600 "$DST/homelab-$TS.tar.gz"

# --- 5. Rotation -----------------------------------------------------------
ls -1t "$DST"/homelab-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm --

echo "$(date '+%F %T') ok $(du -h "$DST/homelab-$TS.tar.gz" | cut -f1)"
