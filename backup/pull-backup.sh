#!/bin/bash
# ---------------------------------------------------------------------------
# L'hyperviseur VA CHERCHER (pull) la dernière archive du serveur,
# pour en garder une copie sur un AUTRE disque physique.
#
# Pourquoi « pull » et non « push » ?
#   Le serveur est exposé sur Internet. S'il était compromis, une sauvegarde en
#   « push » lui donnerait un accès SSH vers l'hyperviseur — donc vers TOUTES
#   les sauvegardes, y compris les images de VM. Ici, c'est l'hyperviseur (qui
#   n'est joignable que depuis le réseau local) qui initie la connexion :
#   le serveur, lui, n'a aucun accès vers l'hyperviseur.
#
# À exécuter SUR L'HYPERVISEUR.
# Cron : 15 4 * * * $HOME/bin/pull-backup.sh >> $HOME/offsite/pull.log 2>&1
#        (45 min après la sauvegarde du serveur, qui tourne à 3 h 30)
# ---------------------------------------------------------------------------
set -e

SRC_HOST=192.168.1.10        # la VM Docker (le serveur)
SRC_USER=homelab             # compte de service, authentification par clé SSH dédiée
SRC_DIR=/srv/backup/homelab
DST=$HOME/offsite            # sur un autre disque physique que la VM
KEEP=7

SSH_OPTS="-o BatchMode=yes -o StrictHostKeyChecking=accept-new"

mkdir -p "$DST"

# Nom de l'archive la plus récente côté serveur
LATEST=$(ssh $SSH_OPTS "$SRC_USER@$SRC_HOST" "ls -1t $SRC_DIR/homelab-*.tar.gz 2>/dev/null | head -1")
[ -n "$LATEST" ] || { echo "$(date '+%F %T') aucune archive trouvée sur $SRC_HOST"; exit 1; }

NAME=$(basename "$LATEST")

# Rien à faire si la copie du jour est déjà là (le script peut être relancé sans risque)
[ -f "$DST/$NAME" ] || scp -q $SSH_OPTS "$SRC_USER@$SRC_HOST:$LATEST" "$DST/$NAME"
chmod 600 "$DST/$NAME"

# Rotation : on ne garde que les KEEP copies les plus récentes
ls -1t "$DST"/homelab-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm --

echo "$(date '+%F %T') ok $NAME $(du -h "$DST/$NAME" | cut -f1)"
