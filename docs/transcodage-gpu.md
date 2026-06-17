# 🎮 Le transcodage GPU : mon plus gros défi

> La partie du projet qui m'a demandé le plus de recherche, de patience et de redémarrages.
> Je la documente honnêtement — y compris tous les murs sur lesquels je me suis cogné — parce que
> c'est là que j'ai le plus appris.

## Le but

Faire en sorte que Jellyfin transcode les vidéos **sur la carte graphique** (NVIDIA Quadro P1000)
au lieu du processeur : plus rapide, moins de chaleur, et plusieurs flux en parallèle.

## Pourquoi c'était (vraiment) difficile

Mon serveur tourne dans une **machine virtuelle**, et la carte graphique est une **carte de laptop**.
Contrairement à une carte de bureau, une carte de laptop ne range pas son « manuel de démarrage »
(le *vBIOS*) sur elle-même : il est stocké dans le firmware du portable et fourni au pilote via un
mécanisme **ACPI** (`_ROM`). Une machine virtuelle n'a pas ce mécanisme → le pilote NVIDIA échoue
avec l'erreur `Failed to copy vbios to system memory`.

C'est un problème connu et **réputé presque insoluble** sur les GPU de laptop. Beaucoup de guides
conseillent simplement d'acheter une carte de bureau. J'ai voulu aller au bout.

## Le parcours du combattant (les murs, dans l'ordre)

**1. La carte qui « disparaît » (D3cold).**
Au premier passthrough, la VM refusait de démarrer : `error getting device from group 2: No such
device`. La carte, en veille profonde (D3cold), était inaccessible — et une mauvaise manipulation
l'a même fait **disparaître complètement** du système.
→ *Solution :* ajouter `pcie_port_pm=off` au noyau de l'hyperviseur pour empêcher la carte de
s'endormir, puis redémarrer l'hôte.

**2. Conflit de pilotes.**
La carte visible dans la VM, mais le pilote NVIDIA se figeait : l'ancien pilote `nouveau` se
chargeait en parallèle.
→ *Solution :* le bloquer explicitement (`blacklist nouveau`) côté invité.

**3. `romfile` : la fausse bonne idée.**
La méthode classique (fournir le vBIOS via `romfile`) ne change **rien** — le pilote de laptop
cherche le vBIOS via ACPI, pas via le bus PCIe. J'ai confirmé ce que disaient certains articles :
pour un GPU mobile, `romfile` est inutile.

**4. Le mur des 64 Ko.**
J'ai voulu fournir le vBIOS via une table ACPI sur mesure (SSDT). Mais le vBIOS fait 172 Ko, et
l'option `-acpitable` de QEMU est **limitée à 64 Ko**. Impasse.

**5. Le firmware patché… qui ne suffisait pas.**
J'ai alors construit un **firmware OVMF patché** (un outil compile une UEFI qui embarque le vBIOS).
La VM démarrait, mais **même erreur**. En inspectant les tables ACPI de l'invité, j'ai compris : le
firmware plaçait bien sa méthode `_ROM`, mais **au mauvais endroit** (`PEG0.PEGP`), alors que ma
carte se trouvait à un autre chemin ACPI (`\_SB.PCI0.SE0.S00`).

## La solution qui a marché

La pièce manquante : le firmware patché **charge bien le vBIOS en mémoire**, il fallait juste que la
méthode `_ROM` soit au **bon chemin ACPI**. J'ai donc :

1. Récupéré le chemin exact de la carte dans l'invité
   (`/sys/bus/pci/devices/0000:01:00.0/firmware_node/path`).
2. Écrit ma **propre petite table ACPI (SSDT)** qui réimplémente `_ROM` à ce chemin, en lisant le
   vBIOS là où le firmware l'avait chargé en mémoire.
3. Chargé cette table (528 octets — ça tient largement dans la limite des 64 Ko) **en plus** du
   firmware patché.

Au redémarrage suivant : `nvidia-smi` affiche enfin la **Quadro P1000**. Plus d'erreur vBIOS. 🎉

## Le résultat

- ✅ Pilote NVIDIA fonctionnel dans la VM
- ✅ Conteneur Jellyfin avec accès GPU (NVENC + NVDEC)
- ✅ Transcodage matériel **vérifié** par un test d'encodage réel — et **persistant après
  redémarrage** (testé)

## Ce que j'en retiens

- La **virtualisation et le passthrough PCIe (VFIO)** ne pardonnent pas l'approximation — mais on
  peut tout annuler proprement quand on prépare ses retours en arrière à chaque étape.
- Lire **plusieurs sources** et **diagnostiquer pas à pas** (logs, inspection ACPI) vaut mieux que
  copier-coller une recette.
- La « bonne » solution documentée ne marche pas toujours telle quelle : il faut **comprendre
  pourquoi** elle fonctionne pour l'adapter à son cas.
- Et surtout : **la persévérance paie.** Ce que beaucoup déclaraient impossible, je l'ai fait
  tourner.

> ⚠️ *Honnêteté technique :* la solution repose sur une adresse mémoire fixe issue du firmware. Si
> je mets à jour l'UEFI ou QEMU un jour, il faudra peut-être réajuster la table ACPI. C'est noté.
