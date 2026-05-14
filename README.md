# Zarzis Irrigation - Dashboard + API Cloud

Version corrigee `http_push` pour Render / GitHub.

Le projet contient :

- un dashboard PWA : `index.html`, `manifest.webmanifest`, `sw.js`, `icon.svg`
- une API Flask : `modbus_proxy_cloud.py`
- un agent local HTTP PUSH : `zarzis_edge_agent.py`
- la configuration Render : `render.yaml`
- les dépendances Python : `requirements.txt`
- les guides d'installation et d'achat matériel

## Architecture

```txt
Pompes / coffrets / capteurs
        -> RS485 Modbus RTU
USR-DR302
        -> Ethernet Modbus TCP
Routeur 4G/Wi-Fi existant du site
        -> Ethernet LAN
Agent local Zarzis sur Raspberry Pi
        -> HTTPS sortant
Render API + Dashboard
```

Decision mai 2026 : le USR-G781-E est annule. Le routeur 4G/Wi-Fi deja present sur site fournit Internet et le LAN. Le DR302 et le Raspberry Pi sont branches en Ethernet sur ce routeur. Si le routeur n'a pas deux ports LAN libres, ajouter un switch 5 ports non manage.

Important : le mode choisi reste `http_push`. Il fonctionne meme si la 4G est derriere NAT, car l'agent local sort vers Render en HTTPS. Tu n'as donc pas besoin de payer une IP publique/APN prive au depart.

Le mode `direct_tcp` reste une option seulement si le DR302 est joignable depuis Render via un reseau prive, par exemple APN prive ou VPN industriel.

## Agent local HTTP PUSH

Sur le PC/Raspberry du site Zarzis :

```txt
python -m pip install -r requirements.txt
set CLOUD_URL=https://zarzis-irrigation-1.onrender.com
set API_TOKEN=API_TOKEN_RENDER
set DR302_HOST=IP_LOCALE_DR302
set DR302_PORT=502
set EDGE_ALLOW_START=false
set RELAY_SIMULATION_ENABLED=false
python zarzis_edge_agent.py
```

`EDGE_ALLOW_START=false` bloque les demarrages mais laisse les arrets possibles. Passer `EDGE_ALLOW_START=true` seulement apres validation qModMaster et essai terrain.
`RELAY_SIMULATION_ENABLED=false` force les commandes relais a echouer si le GPIO Raspberry n'est pas disponible. Le passer a `true` seulement pour un test hors terrain.

## Variables Render

Dans Render > Environment :

```txt
EDGE_MODE=http_push
EDGE_HOST=
EDGE_PORT=502
EDGE_PUSH_STALE_SEC=10
EDGE_COMMAND_TTL_SEC=300
EDGE_ACK_TIMEOUT_SEC=10
HISTORY_MAX_ITEMS=2000
HISTORY_SAVE_MIN_INTERVAL_SEC=30
API_TOKEN=mot_de_passe_long
APP_LOGIN_ENABLED=true
APP_LOGIN_EMAIL=mohamedbsir@live.fr
APP_LOGIN_PASSWORD_HASH=hash_pbkdf2_du_mot_de_passe
APP_LOGIN_SESSION_SECRET=secret_long_aleatoire
APP_LOGIN_REMEMBER_TTL_DAYS=3650
UPDATE_SEC=2
CORS_ORIGINS=https://zarzis-irrigation-1.onrender.com
ENABLE_PLANNING=false
PERSISTENT_STORAGE_ENABLED=false
ALLOW_REMOTE_CONNECT=false
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

Les noms `EDGE_*` sont les variables officielles. Les anciens noms `G781_*` restent acceptes uniquement comme alias de compatibilite historique; aucun USR-G781-E n'est necessaire.

Ne pas exposer le port Modbus TCP directement sur Internet. En `http_push`, Render ne contacte jamais le DR302 en direct : seul l'agent local parle au Modbus sur le LAN du routeur 4G existant.

Tant que les registres réels ne sont pas validés avec le matériel, garder `MODBUS_REGISTERS_VALIDATED=false`. Les commandes `off` restent possibles, mais les démarrages réels sont bloqués. Après mapping et essai local, passer `MODBUS_REGISTERS_VALIDATED=true` dans Render.

Rain Bird est optionnel. Ne pas mettre de clé Rain Bird directement dans le code :

```txt
RAINBIRD_STICK_ID=
RAINBIRD_SERIAL=
RAINBIRD_KEYCODE=
RAINBIRD_IP=
RAINBIRD_WIFI=
RAINBIRD_ZONES=1,2,3,4,5,6
RAINBIRD_MAX_DURATION_MIN=240
RAINBIRD_MIN_INTERVAL_SEC=30
```

## Stockage Render

Le code utilise `DATA_DIR` pour stocker `planning_zarzis.json` et `app_state_zarzis.json`.
En plan gratuit Render, ce stockage reste ephemere. C'est suffisant pour tester le dashboard, mais pas pour compter durablement sur la synchro multi-appareils.

Quand tu passes au plan payant avec Persistent Disk :

```txt
DATA_DIR=/var/data
PERSISTENT_STORAGE_ENABLED=true
```

Et ajouter le disque Render indique en commentaire dans `render.yaml`.

## Endpoints API

```txt
GET  /api/ping
GET  /api/status
GET  /api/devices
POST /api/connect
POST /api/control
POST /api/inverter
POST /api/param/read
POST /api/param/write  (maintenance seulement, desactive par defaut)
GET  /api/planning
POST /api/planning
GET  /api/events
POST /api/edge/push
GET  /api/edge/commands
POST /api/edge/ack
GET  /api/history
POST /api/ai/diagnose
```

Les anciennes routes `/api/g781/*` restent disponibles comme alias de compatibilite, mais l'agent local utilise maintenant `/api/edge/*`.

Les commandes `POST` demandent `API_TOKEN` si la variable existe sur Render. Dans le dashboard, renseigner ce token dans l'onglet Connexion. Le token doit passer par l'en-tete `Authorization: Bearer ...` ou `X-API-Token`, jamais dans l'URL.

L'assistant IA est volontairement en lecture seule. Il analyse `/api/status`, `/api/events`, `/api/history` et prépare des recommandations ou brouillons de planning, mais il ne doit jamais appeler `/api/control`, `/api/inverter`, `/api/param/write` ni une commande Rain Bird.

## Commande start/stop

En mode `http_push`, `success=true` signifie que la commande est acceptee par le serveur. Si la reponse contient `queued=true`, la commande est seulement mise en file et reste en attente de l'ACK de l'agent local. L'execution reelle est confirmee ensuite via `/api/edge/ack`, puis remontee dans `/api/status` avec `last_command_ack` et `recent_command_acks`.

Les demarrages sont en mode fail-closed : si l'agent est absent, si les mesures sont trop anciennes, ou si un registre critique manque (`fault_code` INVT, defaut/flotteur Salmson, defaut/etat/pression Wilo), le demarrage est refuse. Les arrets restent autorises autant que possible.

```json
POST /api/control
{
  "device": "wilo",
  "action": "on"
}
```

Appareils disponibles :

```txt
invt
salmson
forage
wilo
coffret4
all
```

`forage` commande l'INVT. La commande Salmson est desactivee par defaut (`SALMSON_COMMAND_ENABLED=false`) tant que la table Modbus EC-L exacte n'est pas validee. `coffret4` est prevu pour lecture/supervision tant que son registre de commande reel n'est pas defini.

## Planning

Dans la configuration actuelle, le planning cloud est desactive par defaut avec `ENABLE_PLANNING=false`. Le dashboard sert donc a lire les informations et a envoyer des commandes manuelles. Reactiver le planning seulement si tu acceptes que le serveur cloud envoie des ordres automatiques.

```json
POST /api/planning
{
  "planning": [
    {"device":"salmson", "action":"on",  "time":"08:00", "days":[0,1,2,3,4,5,6], "enabled":true},
    {"device":"salmson", "action":"off", "time":"09:00", "days":[0,1,2,3,4,5,6], "enabled":true}
  ]
}
```

Jours :

```txt
0 = lundi
1 = mardi
2 = mercredi
3 = jeudi
4 = vendredi
5 = samedi
6 = dimanche
```

## Version prête installation

Cette version ajoute :

- planning cloud present mais desactive par defaut avec `ENABLE_PLANNING=false`
- mode `http_push` actif par defaut pour eviter le blocage NAT operateur
- agent local `zarzis_edge_agent.py` pour pousser mesures et recuperer commandes
- accuse de reception agent via `/api/edge/ack` pour tracer commandes executees ou refusees
- historique persistant `history_zarzis.json` pour mesures, commandes, ACK et diagnostics IA
- Rain Bird route via l'agent local en mode `http_push`
- assistant IA lecture seule pour diagnostic, surveillance et propositions sans commande directe
- synchronisation planning bloquee tant que `ENABLE_PLANNING=false`
- mode simulation backend via `EDGE_MODE=simulation` pour tester avant réception du matériel
- protection API sur tous les endpoints `/api/*` sauf `/api/ping` quand `API_TOKEN` existe
- verrouillage serveur minimal avant démarrage si défaut INVT/Salmson/Wilo déjà connu
- anti-spam serveur sur les commandes : 30 s entre deux demarrages et 60 s apres un arret avant redemarrage
- ecriture libre de registres Modbus desactivee par defaut avec `ALLOW_PARAM_WRITE=false`
- démarrages Modbus réels bloqués tant que `MODBUS_REGISTERS_VALIDATED=false`
- valeurs critiques configurables : `INVT_OFF_VALUE`, `SALMSON_FLOAT_LOW_OK_VALUE`, `*_CMD_REG`, `*_REG_*`
- icônes PWA PNG 192/512 pour installation mobile

Avant reception des modules, tu peux laisser le dashboard en simulation locale. Pour tester le serveur sans materiel, mettre temporairement `EDGE_MODE=simulation` sur Render. Pour la mise en service reelle, garder `EDGE_MODE=http_push` et lancer l'agent local sur site.

Le dashboard utilise maintenant une vraie connexion serveur via `/api/auth/login`. Le mot de passe n'est pas stocke en clair dans le depot: Render doit recevoir `APP_LOGIN_EMAIL`, `APP_LOGIN_PASSWORD_HASH` et `APP_LOGIN_SESSION_SECRET`. L'option `Rester connecte automatiquement` garde seulement un jeton de session longue duree.

Principe connexion v9.2 : apres une premiere connexion, l'application se rouvre directement connectee au cloud. Elle ne relance plus une boucle de recherche de l'agent toutes les quelques secondes. L'etat de l'agent local est lu via `/api/status`, alimente par WebSocket/HTTP push depuis le Raspberry.

Version v9.3 : les blocs dashboard `Securite exploitation` et `Actions rapides` sont retires. Les commandes manuelles restent disponibles dans l'onglet Controle; les securites reelles restent gerees par le serveur, l'agent local et les coffrets.

## Sécurité

Les sécurités pompes doivent rester locales dans les coffrets d'origine : thermique, manque d'eau, pression, défaut variateur, arrêt d'urgence.

Le dashboard sert à superviser, démarrer, arrêter et planifier. Il ne remplace pas une sécurité électrique ou hydraulique locale.

Synchronisation multi-appareils :

- Les données partagées sont centralisées sur Render via `/api/app-state`.
- iPhone, tablette et PC récupèrent les réglages toutes les 5 secondes après connexion au cloud.
- Sont synchronisés : zones, programmes, planning irrigation, exploitation, goutte-à-goutte, réservoirs, matériel, fertigation, localisation et compteurs.
- Ne sont pas synchronisés : session de connexion, `API_TOKEN`, son et mode maintenance. Ces éléments restent propres à chaque appareil pour la sécurité.
- Après modification sur un appareil, attendre quelques secondes ou cliquer `SYNC APPAREILS` dans Connexion.

## Securite production

- Ne jamais exposer Modbus TCP directement sur Internet ouvert.
- Utiliser `http_push` par defaut avec agent local.
- Utiliser `direct_tcp` seulement via VPN, APN prive ou reseau prive equivalent.
- Un VPN grand public pour naviguer anonymement ne suffit pas. Il faut que Render/la passerelle et le site Zarzis soient dans le meme reseau prive.
- Garder `ALLOW_REMOTE_CONNECT=false` en production. En `http_push`, `EDGE_HOST` reste vide cote Render; en `direct_tcp`, il serait fixe cote serveur, jamais depuis le navigateur.
- Garder `ENABLE_PLANNING=false` si le dashboard doit rester en lecture/commande manuelle seulement.
- Garder `ALLOW_PARAM_WRITE=false` en production. L'activer seulement pour une maintenance courte et controlee.
- Le token API ou la session dashboard passent seulement par `Authorization: Bearer ...` ou `X-API-Token`. Ne jamais les mettre dans l'URL.
- Les commandes de demarrage sont limitees cote serveur par `COMMAND_MIN_INTERVAL_SEC` et `COMMAND_RESTART_DELAY_SEC`.

## Validation Modbus

Avant le premier demarrage reel :

1. Garder `MODBUS_REGISTERS_VALIDATED=false` dans Render.
2. Avec qModMaster ou Modbus Poll, lire un appareil a la fois en RS485 local.
3. Confirmer INVT : commande `0x2000`, marche `1`, arret `5`, lectures `0x3000` a `0x3006`.
4. Confirmer Wilo EC-B : 40015/40026/40041/40042/40062/40139-40140. D'apres la notice 43587401, les menus Wilo d'usine sont 2.02=19200, 2.03=10, 2.04=even, 2.05=1 : regler le coffret sur 9600 / adresse 3 / parite none si le bus DR302 reste en 9600 8N1.
5. Confirmer Salmson EC-L / EC-Lift : 40015 commande, 40026 niveau, 40041/40042 modes pompes, 40062 etat coffret, 40139-40140 defauts, 40198 flotteurs. La fiche EC-Lift fournie confirme l'interface Modbus mais pas la table complete; la table utilisee vient de la Fieldbuslist Modbus EC Wilo.
6. Confirmer le sens terrain du manque d'eau : le code mappe le bit dry-run de 40198 vers `float_low`; si le test reel indique une logique differente, ajuster `SALMSON_FLOAT_LOW_OK_VALUE`.
7. Laisser `SALMSON_COMMAND_ENABLED=false` jusqu'au test qModMaster, meme si `SALMSON_CMD_REG=14` est maintenant renseigne.
8. Corriger les variables Render (`INVT_OFF_VALUE`, `SALMSON_CMD_REG`, `WILO_CMD_REG`, `*_REG_*`) si le test terrain donne d'autres valeurs.
9. Faire un essai manuel local on/off avec les securites locales actives.
10. Seulement apres validation, passer `MODBUS_REGISTERS_VALIDATED=true` et `EDGE_ALLOW_START=true`.

---

## Version v9.0 — WebSocket + cerveau local

Cette version garde l'architecture sûre avec Raspberry local, DR302 local et aucun port Modbus ouvert sur Internet.

```txt
Dashboard Render HTTPS
  -> WebSocket sortant permanent
  -> Raspberry / agent local
  -> DR302 Ethernet local
  -> RS485 Modbus
  -> INVT / Salmson / Wilo / capteurs
```

Le Raspberry reste le cerveau local : il lit le Modbus, applique les sécurités locales, exécute les commandes et garde un buffer offline. Render sert à l'accès distant et à l'interface.

Le WebSocket sert à garder l'agent visible comme connecté, même si une lecture Modbus prend du temps. Les mesures continuent aussi d'être envoyées via HTTP push pour garder une compatibilité de secours.

Variables principales côté Render :

```txt
EDGE_MODE=http_push
EDGE_WS_ENABLED=true
EDGE_WS_HEARTBEAT_SEC=5
EDGE_WS_COMMAND_PUSH_SEC=0.25
EDGE_WS_RECEIVE_TIMEOUT_SEC=0.25
EDGE_AGENT_STALE_SEC=20
EDGE_PUSH_STALE_SEC=10
```

Variables principales côté Raspberry :

```txt
EDGE_WS_ENABLED=true
EDGE_HEARTBEAT_SEC=5
EDGE_STATUS_PUSH_SEC=2
EDGE_COMMAND_POLL_SEC=1
MODBUS_TIMEOUT_SEC=0.8
```

Mode reactif recommande : l'interface relit l'etat toutes les 1 s, l'agent pousse la telemetrie toutes les 2 s, et les commandes WebSocket sont poussees par le serveur environ toutes les 0,25 s. Garder `COMMAND_RESTART_DELAY_SEC=60` pour proteger les pompes contre les redemarrages rapides.

Ne pas ouvrir le port Modbus 502 sur Internet. Le WebSocket sort du Raspberry vers Render, donc il reste compatible avec box 4G, fibre ou ADSL derrière NAT.
