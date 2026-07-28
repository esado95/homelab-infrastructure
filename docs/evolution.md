# 🧭 Évolution du homelab — avant / après

Ce homelab n'est pas né dans sa forme actuelle. Il a commencé comme un simple serveur Jellyfin,
puis chaque limite rencontrée a amené une brique en plus. Cette page retrace **ce qui coinçait**,
**ce que j'ai changé** et **pourquoi** — c'est aussi le fil de ce que j'ai appris.

> Je ne prétends pas que ces choix soient les meilleurs : ce sont ceux que j'ai compris et su
> mettre en place, avec les moyens du bord.

---

## 📊 Vue d'ensemble

| Domaine | Avant | Aujourd'hui |
|---|---|---|
| **Accès depuis Internet** | IP partagée par l'opérateur : les ports 80/443 étaient inutilisables, le site tournait sur un port exotique | IP dédiée, ports 80/443 redirigés, certificats Let's Encrypt renouvelés tout seuls |
| **Administration** | Un mot de passe par outil, joignable seulement depuis le LAN ou le VPN | Un seul compte Keycloak pour tout, contrôle par rôle, chaque service sur son sous-domaine |
| **Point d'entrée** | Une collection de signets et de numéros de ports | Un portail interne unique, avec l'état des services en direct |
| **Gestion des conteneurs** | Portainer (interface lourde, fonctions clés réservées à l'édition payante) | Dockge, qui édite directement mes fichiers `compose` versionnés |
| **Supervision** | Tableaux de bord créés à la main dans l'interface, perdus si le conteneur est recréé | Provisionnée par fichiers (sources, tableaux de bord, alertes) — reproductible |
| **Alertes** | Aucune : je découvrais les problèmes en regardant | Alertes disque par e-mail **et** Telegram, plus un bot qui prévient si un conteneur disparaît |
| **Métriques GPU** | Invisibles (je ne savais pas si le transcodage matériel servait vraiment) | Exportées : température, charge, mémoire vidéo, nombre de transcodages en cours |
| **Sauvegardes** | Manuelles et irrégulières — la dernière image de VM datait de six semaines | Archive quotidienne automatique + image de VM hebdomadaire + copie sur un autre disque |
| **Versions d'images** | `:latest` partout | Versions figées sur les briques sensibles (passerelle SSO, portail, supervision) |

---

## 🌐 1. Sortir de l'IP partagée

**Le problème.** Mon opérateur attribuait une adresse IPv4 partagée entre plusieurs abonnés
(technique MAP-T). Conséquence : seuls quelques milliers de ports m'étaient réservés, et **ni le 80
ni le 443 n'en faisaient partie**. Le site n'était donc joignable que sur un port inhabituel, à
rallonger dans l'URL — et Let's Encrypt ne pouvait pas valider le domaine par le port 80.

**Ce que j'ai fait.** Activé l'option « adresse IP dédiée » chez l'opérateur, créé deux règles de
redirection (80 et 443) vers la VM, puis mis à jour l'enregistrement DNS. J'en ai profité pour
ajouter un enregistrement **générique** (`*.example.com`) : chaque nouveau service obtient son
sous-domaine sans retoucher au DNS.

**Ce que j'ai appris.** L'API du routeur accepte de créer une règle mais **ignore silencieusement**
la modification des ports (elle répond « OK » sans rien changer) — il a fallu terminer à la main
dans l'interface web. Leçon : toujours vérifier l'effet d'un appel d'API, jamais se fier au code de
retour.

---

## 🔐 2. Un seul compte pour toute l'administration

**Le problème.** Chaque outil avait son propre mot de passe. Multiplier les comptes, c'est
multiplier les occasions de mal faire — et rien n'était accessible proprement depuis l'extérieur.

**Ce que j'ai fait.** Ajouté une passerelle **oauth2-proxy** en mode *portier* (`forward_auth`) :
le reverse proxy demande à la passerelle, avant chaque requête, si la session Keycloak est valide
et si le **rôle** attendu est présent. Un rôle pour la famille, un autre pour l'administration.
Une fois connecté, tous les sous-domaines s'ouvrent sans redemander quoi que ce soit.

**Ce que j'ai appris.** En mode portier, le trafic de l'application ne traverse pas la passerelle —
c'est plus rapide et surtout ça ne casse pas les applications qui parlent en API. Une tentative
précédente, où la passerelle proxifiait *tout* le trafic, avait justement cassé les applis natives.

→ Détail : [`auth/sso-gateway/`](../auth/sso-gateway/)

---

## 🧭 3. Un portail plutôt qu'une liste de signets

**Le problème.** Une douzaine de services, autant d'adresses à retenir, et aucun endroit pour voir
d'un coup d'œil si tout allait bien.

**Ce que j'ai fait.** Monté un portail interne (derrière le SSO, rôle administration) qui regroupe
les raccourcis, l'état de chaque service, et des panneaux de supervision **intégrés depuis Grafana**.

**Ce que j'ai appris.** Deux pièges. D'abord, un service interne doit être appelé par son **nom de
conteneur** et pas par son adresse publique : sinon la requête repasse par la passerelle SSO et
reçoit une page HTML de connexion là où elle attend du JSON. Ensuite, intégrer un panneau dans une
page tierce demande d'autoriser explicitement l'affichage en cadre (`iframe`), sans quoi le
navigateur bloque tout, en silence.

→ Détail : [`dashboard/`](../dashboard/)

---

## 📊 4. Une supervision reproductible

**Le problème.** Mes tableaux de bord vivaient dans la base de l'outil : recréer le conteneur, et
tout était à refaire. Aucune alerte : je découvrais un disque plein en m'en apercevant.

**Ce que j'ai fait.** Tout est passé en **fichiers de provisionnement** — source de données,
tableaux de bord, contacts et règles d'alerte. Ajouté un exportateur de métriques GPU, et deux
règles simples : prévenir quand l'espace libre passe sous un seuil, sur le disque système comme sur
la médiathèque.

**Ce que j'ai appris.** Une VM ne voit pas les capteurs de température du processeur — le dossier
correspondant est vide côté invité. Seul le GPU, passé en direct, remonte sa température. Il faut
donc superviser l'hyperviseur lui-même pour avoir le reste (c'est dans la suite du programme).

→ Détail : [`monitoring/`](../monitoring/)

---

## 💾 5. Des sauvegardes qui existent vraiment

**Le problème.** C'est le point qui m'a le plus gêné en faisant l'inventaire : les sauvegardes
étaient manuelles, donc oubliées. La dernière image complète de la VM datait de **six semaines**,
alors que la configuration avait beaucoup changé entre-temps.

**Ce que j'ai fait.** Deux niveaux. Chaque nuit, une petite archive (~90 Mo) : bases de données,
fichiers `compose`, configurations des services, et un inventaire du système (versions d'images,
tâches planifiées, disques). Chaque semaine, une image complète de la VM. Enfin, l'hyperviseur
**vient chercher** l'archive et la garde sur un autre disque physique.

**Ce que j'ai appris.** Ma première image de VM a échoué : elle embarquait aussi le disque de la
médiathèque, soit plus de 500 Gio — le disque de destination s'est rempli avant la fin. En excluant
les disques dont le contenu est re-téléchargeable, l'image tombe à 16 Go et prend quatre minutes.
Une sauvegarde utile, c'est une sauvegarde **choisie**.

Et un choix volontaire : la copie est **tirée** par l'hyperviseur, pas poussée par le serveur.
Le serveur exposé sur Internet n'a ainsi aucun accès à l'hyperviseur ni aux sauvegardes.

→ Détail : [`backup/`](../backup/)

---

## 🧹 6. Le ménage, aussi

Moins spectaculaire, mais ça compte : suppression des conteneurs et fichiers d'anciens essais,
retrait d'un client de test resté déclaré dans Keycloak, mise à jour du système et redémarrage sur
un noyau récent, et remplacement de `:latest` par des versions figées sur les briques dont une
mise à jour surprise ferait mal.

Un détail appris à cette occasion : mettre à jour les bibliothèques du pilote graphique **sans
redémarrer** casse le conteneur qui utilise le GPU (les versions ne correspondent plus entre l'hôte
et le conteneur). Il repart après le redémarrage.

---

## 🎯 Ce qu'il reste honnêtement à faire

- **Tester une restauration complète** : une sauvegarde jamais restaurée n'est qu'une hypothèse.
- **Une copie hors du boîtier** : tout est encore sur la même machine physique.
- **Un pare-feu sur l'hôte**, avec un garde-fou pour ne pas me verrouiller dehors.
- **Alerter si une sauvegarde échoue** — aujourd'hui, l'échec ne se voit que dans un fichier journal.

---

<div align="center">
<sub>Chaque ligne de ce tableau est partie d'un problème concret 🌱</sub>
</div>
