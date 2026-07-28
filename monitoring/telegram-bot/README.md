# 🤖 Bot Telegram de supervision

Un petit **pupitre de contrôle dans Telegram** pour surveiller le homelab depuis le
téléphone, sans ouvrir de session SSH. Code maison (Python + `python-telegram-bot`).

> Je débute : ce bot fait volontairement peu de choses, mais il les fait en
> **lecture seule**. Il ne redémarre rien et ne supprime rien — c'était la contrainte
> que je me suis fixée avant d'écrire la première ligne.

---

## 🎛 Ce qu'il affiche

Le menu tient en huit boutons (plus « 🔎 Tout vérifier », qui regroupe les quatre premiers) :

| Bouton | Contenu | Source |
|---|---|---|
| 📊 **Serveur** | Temps de fonctionnement, processeur, charge, mémoire, swap | Prometheus / node-exporter |
| 💾 **Disques** | Occupation et espace libre par point de montage (⚠️ au-delà de 88 %) | Prometheus / node-exporter |
| 🐳 **Conteneurs** | Nombre de conteneurs actifs + ceux qui manquent à l'appel | Prometheus / cAdvisor |
| 🎮 **Carte graphique** | Utilisation, mémoire vidéo, température, consommation, transcodages | Exportateur NVIDIA |
| 🎬 **En lecture** | Qui regarde quoi, sur quel appareil, en direct ou en transcodage | API Jellyfin |
| 🎯 **Demandes** | Dernières demandes de contenus et leur état | API Jellyseerr |
| 💽 **Sauvegardes** | Âge et taille de la dernière archive, volume total | Dossier monté en lecture seule |
| 🌐 **Services** | Code HTTP et temps de réponse des adresses publiques | Requêtes HTTP |

Chaque rubrique se termine par l'heure de mise à jour et deux boutons : **⬅️ Menu** et
**🔄 Actualiser** (le message est modifié sur place, pas de conversation qui s'allonge).

Deux commandes existent aussi : `/menu` (ouvrir le pupitre) et `/etat` (raccourci vers
la rubrique Serveur).

**Exemple de rendu** — rubrique Serveur :

```
📊 Serveur homelab

⏱ En ligne : 12 j 4 h 37 min
🧠 Processeur : 8 %  ▰▱▱▱▱▱▱▱▱▱
⚙️ Charge : 0.42 / 0.51  (7 % de 6 cœurs)
💭 Mémoire : 6.8 Gio / 11.7 Gio  ▰▰▰▰▰▰▱▱▱▱
♻️ Swap : 0 %

màj 21:14:03
```

---

## 🔔 La veille (watchdog)

Une tâche de fond compare **chaque minute** la liste des conteneurs actifs :

- un conteneur absent sur **deux relevés consécutifs** (~2 min) → notification `🔴` ;
  s'il fait partie des services essentiels, c'est signalé,
- son retour → notification `🟢`.

Les deux relevés évitent le bruit : un simple redémarrage ou une mise à jour d'image
ne déclenche pas d'alerte.

---

## 🔒 Pourquoi pas de `docker.sock`

C'est le point sur lequel j'ai le plus réfléchi avant d'écrire le bot.

Pour lister des conteneurs, le réflexe est de monter `/var/run/docker.sock` dans le
conteneur. Mais ce socket donne le **contrôle total du démon Docker** — donc, en pratique,
un accès équivalent à **root sur l'hôte** : créer un conteneur privilégié, monter `/`,
lire n'importe quel fichier. Un bot exposé à Internet via l'API Telegram est exactement
le genre de service à qui il ne faut pas confier cela.

L'état des conteneurs vient donc de la métrique `container_last_seen` de **cAdvisor**,
lue à travers **Prometheus** : un conteneur vu il y a moins de deux minutes est
considéré actif. C'est un peu moins précis qu'un `docker ps`, et c'est suffisant.

Le reste suit le même principe de **moindre privilège** :

| | |
|---|---|
| 🚫 Pas de socket Docker | l'état vient des métriques |
| 👁 Lecture seule | aucune commande d'action n'existe dans le code |
| 📁 Dossier de sauvegardes en `:ro` | seuls la date et le poids des archives sont lus |
| 🔑 Clés API en lecture | Jellyfin / Jellyseerr, dans le `.env` |
| 🙊 Un seul chat autorisé | tout message venant d'ailleurs est ignoré, sans réponse |
| 🌐 Aucun port ouvert | le bot appelle Telegram (long polling), rien n'entre |

---

## 🆔 Trouver son `chat_id`

Le bot ne répond qu'à un seul chat, dont l'identifiant numérique va dans `ALLOWED_CHAT`.

1. Créer le bot auprès de **@BotFather** (`/newbot`) et garder le jeton.
2. Envoyer un message quelconque au nouveau bot depuis son propre compte.
3. Lire l'identifiant renvoyé par l'API :

```bash
curl -s "https://api.telegram.org/bot<JETON>/getUpdates" \
  | grep -o '"chat":{"id":[-0-9]*'
```

Le nombre obtenu est le `chat_id` (négatif s'il s'agit d'un groupe).

---

## 🚀 Déploiement

```bash
# 1. Copier le dossier sur l'hôte Docker
scp -r telegram-bot/ <utilisateur>@<IP_SERVEUR>:/opt/docker/compose/homelab-bot/

# 2. Renseigner les secrets
cd /opt/docker/compose/homelab-bot
cp .env.example .env && nano .env      # BOT_TOKEN, ALLOWED_CHAT, JF_TOKEN, JS_TOKEN

# 3. Démarrer
docker compose up -d --build
docker logs -f homelab-bot
```

**Prérequis :** un réseau Docker externe `proxy` sur lequel vivent déjà Prometheus,
Jellyfin et Jellyseerr (le bot les joint par leur nom de conteneur).

**À adapter dans `app.py`** avant de s'en servir ailleurs :

- `DISKS` — les points de montage à suivre,
- `CORE` — les conteneurs considérés comme essentiels,
- `SITES` — les adresses publiques à tester (`example.com` ici).

---

## 🌱 Ce qu'il me reste à améliorer

- [ ] Le fuseau horaire est un décalage figé (`TZ_OFFSET`) : passer à `zoneinfo`.
- [ ] La rubrique « Services » ignore la vérification du certificat TLS (`verify=False`),
      parce que le test part de l'intérieur du réseau Docker — le tester depuis l'extérieur serait plus honnête.
- [ ] Les alertes de la veille ne sont pas regroupées : un redémarrage complet de la
      pile envoie beaucoup de messages d'un coup.
- [ ] Ajouter un `healthcheck` et une rotation des journaux dans le `compose.yaml`.

---

> 🔐 Toutes les valeurs sensibles (jeton du bot, `chat_id`, clés API) vivent dans un
> `.env` **non versionné** — voir [`.env.example`](.env.example).
