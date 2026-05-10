# Plan complet installation Zarzis

Objectif : gérer 4 appareils avec le dashboard Zarzis, depuis téléphone, avec une architecture Modbus propre et des sécurités locales.

## Résultat final attendu

```txt
Téléphone / PC
  -> Dashboard PWA installé
  -> Render API HTTPS
  -> Agent local Zarzis en HTTPS sortant
  -> Routeur 4G/Wi-Fi existant / LAN local
  -> USR-DR302 Ethernet vers RS485
  -> Bus RS485 Modbus RTU
  -> 4 appareils
```

## Les 4 appareils prévus

```txt
Adresse 1 : INVT GD100-PV / variateur solaire
Adresse 2 : Salmson / pompe forage
Adresse 3 : Wilo / surpresseur
Adresse 4 : coffret ou capteur additionnel
```

Important : le quatrième appareil est prêt côté architecture, mais ses registres exacts devront être confirmés quand tu auras le matériel ou la notice. Pour l'instant il est prévu surtout en supervision.

## Règle de sécurité principale

Le cloud et le téléphone ne doivent jamais remplacer les sécurités électriques/hydrauliques locales.

À garder dans les coffrets :

- protection thermique
- manque d'eau
- pressostat / pression mini et maxi
- défaut variateur
- arrêt d'urgence
- disjoncteurs et protections moteur

Le dashboard sert à superviser, démarrer, arrêter et planifier. Il ne doit pas être la seule sécurité.

## Phase 1 - Avant de recevoir les modules

1. Mettre tous les fichiers du projet sur GitHub.
2. Déployer sur Render avec `render.yaml`.
3. Vérifier que l'URL Render répond :

```txt
https://zarzis-irrigation-1.onrender.com/api/ping
```

4. Dans Render > Environment, noter le `API_TOKEN` généré.
5. Ouvrir le dashboard :

```txt
https://zarzis-irrigation-1.onrender.com
```

6. Onglet Connexion :

```txt
URL serveur = https://zarzis-irrigation-1.onrender.com
Token API   = API_TOKEN de Render
Intervalle  = 5 secondes
```

7. Cliquer `CONNECTER`.
8. Sur téléphone, installer la PWA :

Android / Chrome :

```txt
Ouvrir l'URL Render -> menu ⋮ -> Installer l'application
```

iPhone / Safari :

```txt
Ouvrir l'URL Render -> Partager -> Ajouter à l'écran d'accueil
```

L'installation PWA fonctionne uniquement si l'application est servie en HTTPS. Render convient. Un simple fichier `index.html` ouvert en local ne suffit pas pour l'installation propre.

## Phase 2 - Préparer le câblage hors tension

Matériel minimum :

```txt
USR-DR302
Alimentation DIN 24V DC
Câble RS485 torsadé blindé 120 ohms
2 résistances 120 ohms
Borniers A+ / B- / GND / Shield
Raspberry Pi ou mini PC local
Switch Ethernet 5 ports si le routeur n'a pas deux ports LAN libres
```

Decision mai 2026 : le USR-G781-E est annule. Le routeur 4G/Wi-Fi deja present sur site fournit Internet. Brancher DR302 et Raspberry en Ethernet sur ce routeur, ou via un switch 5 ports non manage.

Ne pas alimenter le DR302 avec du 24V AC. Utiliser une alimentation DC stable.

Bus RS485 :

```txt
DR302 A+ -> appareil 1 A+ -> appareil 2 A+ -> appareil 3 A+ -> appareil 4 A+
DR302 B- -> appareil 1 B- -> appareil 2 B- -> appareil 3 B- -> appareil 4 B-
GND si disponible
Blindage à la terre d'un seul côté
```

À éviter :

- câblage en étoile
- inversion A/B
- câble RS485 collé aux câbles moteur/variateur
- plusieurs appareils avec la même adresse Modbus
- trop de résistances de terminaison

Terminaisons :

```txt
1 résistance 120 ohms au début du bus
1 résistance 120 ohms à la fin du bus
pas de résistance au milieu
```

## Phase 3 - Paramètres Modbus des 4 appareils

Mettre tous les appareils sur les mêmes paramètres série :

```txt
Protocole : Modbus RTU
Vitesse   : 9600 bauds
Données   : 8 bits
Parité    : None
Stop      : 1 bit
Format    : 8N1
```

Adresses :

```txt
INVT    = 1
Salmson = 2
Wilo    = 3
Coffret/capteur 4 = 4
```

Ordre conseillé :

1. Configurer l'adresse INVT seule, sans les autres appareils branchés.
2. Configurer l'adresse Salmson seule.
3. Configurer l'adresse Wilo seule.
4. Configurer le coffret/capteur 4 seul.
5. Brancher ensuite les 4 sur le même bus.

## Phase 3 bis - Validation des registres Modbus

Avant toute commande cloud, garder dans Render :

```txt
MODBUS_REGISTERS_VALIDATED=false
```

Procedure de mapping :

1. Brancher un seul appareil RS485 a la fois.
2. Tester avec qModMaster ou Modbus Poll en local, pas depuis le cloud.
3. Lire les registres de mesure et verifier que les valeurs changent correctement.
4. Lire les registres de defaut et provoquer seulement des etats sans danger si possible.
5. INVT : verifier `0x2000`, marche `1`, arret `5`, lectures `0x3000` a `0x3006`.
6. Wilo EC-B : verifier 40015, 40026, 40041, 40042, 40062, 40139-40140. La notice 43587401 donne les valeurs usine Modbus 19200 / adresse 10 / parite even / stop 1; regler les menus 2.01 a 2.05 pour obtenir 9600 / adresse 3 / parite none / stop 1 si le bus reste en 9600 8N1.
7. Salmson EC-L / EC-Lift : verifier 40015, 40026, 40041, 40042, 40062, 40139-40140, 40198. La commande reste bloquee par `SALMSON_COMMAND_ENABLED=false` jusqu'au test terrain.
8. Verifier le manque d'eau : le bit dry-run de 40198 est mappe vers `float_low`; ajuster `SALMSON_FLOAT_LOW_OK_VALUE` seulement si le test reel indique une logique differente.
9. Pour Coffret4, remplacer les registres generiques par les adresses reelles via les variables `COFFRET4_REG_*`.
10. Faire un cycle local on/off avec les securites locales actives.
11. Seulement apres ces tests, mettre :

```txt
MODBUS_REGISTERS_VALIDATED=true
EDGE_ALLOW_START=true sur l'agent local
```

## Phase 4 - Configurer le USR-DR302

Le DR302 fait la conversion :

```txt
Modbus TCP côté Ethernet
Modbus RTU côté RS485
```

Configuration recommandée :

```txt
Mode réseau      : IP fixe ou DHCP réservé
Mode de travail  : Modbus TCP vers Modbus RTU
Port local TCP   : 502 sur LAN local uniquement
RS485 vitesse    : 9600
RS485 format     : None / 8 / 1
Modbus gateway   : activé
```

Étapes :

1. Brancher le DR302 au PC ou au routeur.
2. Trouver son IP avec la page du routeur, l'outil USR ou l'adresse indiquée sur la notice.
3. Ouvrir son interface web.
4. Se connecter avec les identifiants de la notice.
5. Mettre une IP fixe, par exemple :

```txt
DR302 IP      = IP libre dans le LAN du routeur existant
Masque        = 255.255.255.0
Passerelle    = IP LAN du routeur existant
DNS           = passerelle ou 8.8.8.8
```

6. Régler le port série :

```txt
Baudrate = 9600
Data     = 8
Parity   = None
Stop     = 1
```

7. Activer la fonction Modbus TCP vers RTU.
8. Régler le port local TCP :

```txt
502 uniquement sur LAN local ou reseau prive/VPN/APN
Ne pas exposer 502/1502 sur Internet ouvert
```

9. Sauvegarder.
10. Redémarrer le DR302.

Test local recommandé avant la 4G :

```txt
PC -> DR302 IP -> port 502 prive -> lecture Modbus adresse 1
```

Tant que ce test local ne marche pas, inutile de passer à Render.

## Phase 5 - Utiliser le routeur 4G existant

Decision mai 2026 : le USR-G781-E est annule. Le routeur 4G/Wi-Fi deja present sur site donne l'acces Internet et le LAN local.

Étapes générales :

1. Verifier que le routeur 4G existant a Internet.
2. Verifier qu'il a deux ports LAN libres.
3. Si besoin, ajouter un switch Ethernet 5 ports non manage.
4. Brancher le DR302 en Ethernet sur le routeur ou le switch.
5. Brancher le Raspberry Pi en Ethernet sur le routeur ou le switch.
6. Noter l'IP locale du DR302 et la mettre dans `DR302_HOST`.

Configuration LAN exemple :

```txt
Routeur 4G existant = passerelle LAN
DR302 IP            = IP locale reservee ou fixe
Raspberry Pi        = DHCP ou IP reservee
```

Le choix retenu est HTTP PUSH : Render ne doit pas appeler le DR302 directement.

Solution A - retenue maintenant :

```txt
Agent local Zarzis sur PC/Raspberry
Il lit le DR302 en local
Il pousse vers Render en HTTPS
Il recupere les commandes via /api/edge/commands
```

Ne pas faire en production :

```txt
Port externe routeur : 1502 ou 502 ouvert sur Internet
Destination LAN   : DR302 / Modbus TCP
Risque            : Modbus non chiffre et non authentifie
```

Dans Render :

```txt
G781_MODE=http_push
G781_HOST=
G781_PORT=502
```

Solution B - option future seulement si APN/VPN :

```txt
G781_MODE=direct_tcp
G781_HOST=IP privee VPN/APN du DR302 ou du reseau site
G781_PORT=502
```

Ne jamais exposer Modbus TCP directement sur Internet.

Solution C - evolution autonome a long terme :

```txt
Raspberry Pi ou mini PC local
Il lit le DR302 en local
Il synchronise avec Render en HTTPS
Le planning peut continuer même si le cloud tombe
```

## Phase 6 - Configurer Render

Variables à mettre dans Render :

```txt
G781_MODE=http_push
G781_HOST=
G781_PORT=502
G781_HTTP_PUSH_STALE_SEC=45
G781_COMMAND_TTL_SEC=300
G781_COMMAND_ACK_TIMEOUT_SEC=45
HISTORY_MAX_ITEMS=2000
API_TOKEN=mot_de_passe_long
UPDATE_SEC=5
CORS_ORIGINS=https://zarzis-irrigation-1.onrender.com
ENABLE_PLANNING=false
ALLOW_REMOTE_G781_CONNECT=false
ALLOW_PARAM_WRITE=false
MODBUS_REGISTERS_VALIDATED=false
COMMAND_MIN_INTERVAL_SEC=30
COMMAND_RESTART_DELAY_SEC=60
INVT_CMD_REG=0x2000
INVT_ON_VALUE=1
INVT_OFF_VALUE=5
INVT_NOMINAL_KW=5.5
INVT_REG_FREQ_HZ=0x3000
INVT_REG_SET_FREQ_HZ=0x3001
INVT_REG_DC_BUS_V=0x3002
INVT_REG_VOLTAGE_V=0x3003
INVT_REG_CURRENT_A=0x3004
INVT_REG_POWER_PCT=0x3006
INVT_REG_FAULT_CODE=0x5000
SALMSON_FLOAT_LOW_OK_VALUE=1
SALMSON_COMMAND_ENABLED=false
SALMSON_CMD_REG=14
SALMSON_REG_LEVEL_CM=25
SALMSON_REG_PUMP1_MODE=40
SALMSON_REG_PUMP2_MODE=41
SALMSON_REG_SWITCH_STATE=61
SALMSON_REG_ERROR_CODE=138
SALMSON_REG_FLOAT_STATE=197
WILO_CMD_REG=14
WILO_REG_PRESSURE=25
WILO_REG_FLOW=-1
WILO_REG_PUMP1_MODE=40
WILO_REG_PUMP2_MODE=41
WILO_REG_SWITCH_STATE=61
WILO_REG_ERROR_CODE=138
ADDR_INVT=1
ADDR_SALMSON=2
ADDR_WILO=3
ADDR_COFFRET4=4
```

Le port 502 reste local entre l'agent et le DR302. Ne pas exposer directement 502 ou 1502 sur Internet.

Stockage Render :

```txt
Plan gratuit = stockage ephemere, OK pour tests.
Avant de compter durablement sur /api/app-state, passer au disque Render payant :
DATA_DIR=/var/data
PERSISTENT_STORAGE_ENABLED=true
```

## Phase 7 - Connecter l'application

Avant le dashboard, lancer l'agent local sur le PC/Raspberry du site :

```txt
python -m pip install -r requirements.txt
set CLOUD_URL=https://zarzis-irrigation-1.onrender.com
set API_TOKEN=API_TOKEN_RENDER
set DR302_HOST=IP_LOCALE_DR302_DANS_LE_ROUTEUR
set DR302_PORT=502
set EDGE_ALLOW_START=false
set RAINBIRD_IP=
set RAINBIRD_STICK_ID=
set RAINBIRD_KEYCODE=
python zarzis_edge_agent.py
```

Dans le dashboard :

1. Ouvrir l'onglet Connexion.
2. Renseigner :

```txt
URL serveur = https://zarzis-irrigation-1.onrender.com
Token API = API_TOKEN Render
```

3. Cliquer `CONNECTER`.
4. Cliquer `Verifier HTTP PUSH`.
5. Attendre que le champ dernier agent affiche `zarzis-edge-agent` ou l'adresse source.
6. Vérifier le badge :

```txt
Serveur cloud connecté
Agent local connecté
```

## Phase 8 - Mise en service progressive

Ne jamais brancher les 4 appareils d'un coup pour le premier test.

Étape 1 : DR302 seul

```txt
Objectif : vérifier réseau + port TCP
Résultat attendu : l'agent local lit le DR302 puis Render voit un push
```

Étape 2 : INVT seul

```txt
Adresse = 1
Lire les registres
Vérifier tension DC, fréquence, courant, défaut
Ne pas envoyer marche avant tant que le câblage moteur n'est pas validé
```

Étape 3 : Salmson seul

```txt
Adresse = 2
Lire état pompe, courant, flotteurs, défaut
Tester commande OFF d'abord
Tester ON seulement si eau et protections OK
```

Étape 4 : Wilo seul

```txt
Adresse = 3
Lire pression, débit, pompe 1, pompe 2, défaut
Registres confirmes a tester : 40015, 40026, 40041, 40042, 40062, 40139-40140
Tester arrêt
Tester marche avec pression/débit surveillés
```

Étape 5 : Coffret/capteur 4

```txt
Adresse = 4
Lire les registres disponibles
Adapter ensuite le fichier modbus_proxy_cloud.py avec les bons registres
```

Étape 6 : Bus complet

```txt
Brancher les 4 appareils
Lire /api/status
Lire /api/devices
Tester chaque commande séparément
Tester arrêt général
Garder planning desactive sauf decision explicite
```

## Phase 9 - Contrôles avant autoriser les commandes

Checklist :

```txt
[ ] Les 4 adresses Modbus sont uniques
[ ] Tous les appareils sont en 9600 8N1 None
[ ] A/B non inversés
[ ] 120 ohms uniquement aux deux extrémités
[ ] Le blindage est à la terre d'un seul côté
[ ] Les sécurités locales arrêtent bien les pompes
[ ] L'arrêt d'urgence coupe localement
[ ] Le dashboard lit les états avant d'écrire
[ ] API_TOKEN renseigné dans l'application
[ ] Agent local lance et visible dans le dashboard
[ ] EDGE_ALLOW_START=true seulement apres essais terrain
[ ] MODBUS_REGISTERS_VALIDATED=true seulement apres mapping reel
[ ] Mode maintenance activé seulement pour tester les commandes
```

## Pannes fréquentes

Serveur Render OK mais agent non connecté :

```txt
Verifier que zarzis_edge_agent.py tourne, API_TOKEN, CLOUD_URL, Internet local et DR302_HOST.
```

Lecture impossible d'un appareil :

```txt
Vérifier adresse Modbus, A/B, vitesse série, terminaison, registre demandé.
```

Un seul appareil répond :

```txt
Deux appareils ont peut-être la même adresse, ou le bus RS485 est mal câblé.
```

Ça marche en local mais pas depuis Render :

```txt
En http_push, le NAT operateur n'est pas le probleme principal.
Verifier plutot que l'agent local peut joindre Render en HTTPS et que le token est bon.
```

L'appli ne s'installe pas sur téléphone :

```txt
Ouvrir l'URL HTTPS Render, pas le fichier local.
Vérifier manifest.webmanifest et sw.js présents.
Sur iPhone utiliser Safari -> Partager -> Ajouter à l'écran d'accueil.
```

## Ce qu'il faudra vérifier quand les modules arrivent

1. IP LAN du routeur 4G existant et nombre de ports LAN libres.
2. IP locale attribuee au DR302, a reporter dans `DR302_HOST`.
3. Si l'operateur propose un APN prive ou une option VPN machine-to-machine, utile seulement pour direct_tcp futur.
4. Registres Modbus exacts du coffret/capteur 4.
5. Validation terrain de la commande Salmson avant d'activer `SALMSON_COMMAND_ENABLED`.
6. Confirmation terrain INVT arret (`INVT_OFF_VALUE=5`) et sens du manque d'eau Salmson (`SALMSON_FLOAT_LOW_OK_VALUE`).

## Décision importante

Si tu veux que le système soit fiable même sans Internet, la meilleure évolution sera :

```txt
DR302 + mini PC/Raspberry local + Render pour supervision distante
```

Le cloud sera alors une interface distante, mais le planning et les sécurités resteront locaux.

AJOUT VERSION PRÊTE INSTALLATION
--------------------------------
- Mot de passe local dashboard par défaut : zarzis2026
- API_TOKEN Render obligatoire pour les commandes et la lecture API.
- Le planning cloud est desactive par defaut (ENABLE_PLANNING=false) pour rester en lecture/commande manuelle.
- Pour tester avant réception du matériel : G781_MODE=simulation.
- Pour mise en service réelle : G781_MODE=http_push et lancement de zarzis_edge_agent.py sur site.
- ALLOW_REMOTE_G781_CONNECT=false : le navigateur ne choisit pas l'IP Modbus en production. Le nom est historique.
- API_TOKEN accepte seulement les headers Authorization Bearer ou X-API-Token, pas le token dans l'URL.
- /api/param/write reste desactive par defaut avec ALLOW_PARAM_WRITE=false.
- Les demarrages reels restent bloques tant que MODBUS_REGISTERS_VALIDATED=false.
- Anti-redemarrage serveur : COMMAND_MIN_INTERVAL_SEC=30 et COMMAND_RESTART_DELAY_SEC=60.
- Activer les programmes seulement après validation manuelle des arrêts, sécurités locales et registres Modbus réels.

Synchronisation multi-appareils :

- Les données partagées sont centralisées sur Render via `/api/app-state`.
- iPhone, tablette et PC récupèrent les réglages toutes les 5 secondes après connexion au cloud.
- Sont synchronisés : zones, programmes, planning irrigation, exploitation, goutte-à-goutte, réservoirs, matériel, fertigation, localisation et compteurs.
- Ne sont pas synchronisés : `API_TOKEN`, mot de passe local, son et mode maintenance. Ces éléments restent propres à chaque appareil pour la sécurité.
- Après modification sur un appareil, attendre quelques secondes ou cliquer `SYNC APPAREILS` dans Connexion.
