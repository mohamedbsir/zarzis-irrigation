# Zarzis Irrigation - Dashboard + API Cloud

Version corrigée pour Render / GitHub.

Le projet contient :

- un dashboard PWA : `index.html`, `manifest.webmanifest`, `sw.js`, `icon.svg`
- une API Flask : `modbus_proxy_cloud.py`
- la configuration Render : `render.yaml`
- les dépendances Python : `requirements.txt`
- les guides d'installation et d'achat matériel

## Architecture

```txt
Pompes / coffrets / capteurs
        -> RS485 Modbus RTU
USR-DR302
        -> Ethernet Modbus TCP
USR-G781-E
        -> 4G
Render API + Dashboard
```

Important : Render expose surtout HTTP/HTTPS. Le mode `direct_tcp` doit etre utilise seulement si le G781-E est joignable via un reseau prive, par exemple APN prive ou VPN.

Si la SIM 4G n'a pas de reseau prive fiable, utiliser plutôt :

1. `G781_MODE=http_push` avec `/api/g781/push` et `/api/g781/commands`
2. ou une passerelle locale DR302/Raspberry qui synchronise avec le cloud

## Variables Render

Dans Render > Environment :

```txt
G781_MODE=direct_tcp
G781_HOST=IP privee VPN/APN du G781-E
G781_PORT=502
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
SALMSON_FLOAT_LOW_OK_VALUE=1
ADDR_INVT=1
ADDR_SALMSON=2
ADDR_WILO=3
ADDR_COFFRET4=4
```

Ne pas exposer le port Modbus TCP directement sur Internet. Utiliser `G781_PORT=502` seulement sur un reseau prive VPN/APN. Si un port public est ouvert vers le DR302/G781, Modbus reste non chiffre et non authentifie.

Tant que les registres réels ne sont pas validés avec le matériel, garder `MODBUS_REGISTERS_VALIDATED=false`. Les commandes `off` restent possibles, mais les démarrages réels sont bloqués. Après mapping et essai local, passer `MODBUS_REGISTERS_VALIDATED=true` dans Render.

Rain Bird est optionnel. Ne pas mettre de clé Rain Bird directement dans le code :

```txt
RAINBIRD_STICK_ID=
RAINBIRD_SERIAL=
RAINBIRD_KEYCODE=
RAINBIRD_WIFI=
RAINBIRD_ZONES=1,2,3,4,5,6
RAINBIRD_MAX_DURATION_MIN=240
RAINBIRD_MIN_INTERVAL_SEC=30
```

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
POST /api/g781/push
GET  /api/g781/commands
```

Les commandes `POST` demandent `API_TOKEN` si la variable existe sur Render. Dans le dashboard, renseigner ce token dans l'onglet Connexion. Le token doit passer par l'en-tete `Authorization: Bearer ...` ou `X-API-Token`, jamais dans l'URL.

## Commande start/stop

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

`forage` commande l'ensemble INVT + Salmson. `coffret4` est prévu pour lecture/supervision tant que son registre de commande réel n'est pas défini.

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
- synchronisation planning bloquee tant que `ENABLE_PLANNING=false`
- mode simulation backend via `G781_MODE=simulation` pour tester avant réception du matériel
- protection API sur tous les endpoints `/api/*` sauf `/api/ping` quand `API_TOKEN` existe
- verrouillage serveur minimal avant démarrage si défaut INVT/Salmson/Wilo déjà connu
- anti-spam serveur sur les commandes : 30 s entre deux demarrages et 60 s apres un arret avant redemarrage
- ecriture libre de registres Modbus desactivee par defaut avec `ALLOW_PARAM_WRITE=false`
- démarrages Modbus réels bloqués tant que `MODBUS_REGISTERS_VALIDATED=false`
- valeurs critiques configurables : `INVT_OFF_VALUE`, `SALMSON_FLOAT_LOW_OK_VALUE`, `*_CMD_REG`, `*_REG_*`
- icônes PWA PNG 192/512 pour installation mobile

Avant réception des modules, tu peux laisser le dashboard en simulation locale. Pour tester le serveur sans matériel, mettre temporairement `G781_MODE=simulation` sur Render. À la réception des modules, remettre `G781_MODE=direct_tcp` et renseigner `G781_HOST` + `G781_PORT`.

Le mot de passe local par défaut du dashboard est maintenant : `zarzis2026`. Le vrai verrouillage des commandes distantes reste `API_TOKEN` côté Render.

## Sécurité

Les sécurités pompes doivent rester locales dans les coffrets d'origine : thermique, manque d'eau, pression, défaut variateur, arrêt d'urgence.

Le dashboard sert à superviser, démarrer, arrêter et planifier. Il ne remplace pas une sécurité électrique ou hydraulique locale.

Synchronisation multi-appareils :

- Les données partagées sont centralisées sur Render via `/api/app-state`.
- iPhone, tablette et PC récupèrent les réglages toutes les 5 secondes après connexion au cloud.
- Sont synchronisés : zones, programmes, planning irrigation, exploitation, goutte-à-goutte, réservoirs, matériel, fertigation, localisation et compteurs.
- Ne sont pas synchronisés : `API_TOKEN`, mot de passe local, son et mode maintenance. Ces éléments restent propres à chaque appareil pour la sécurité.
- Après modification sur un appareil, attendre quelques secondes ou cliquer `SYNC APPAREILS` dans Connexion.

## Securite production

- Ne jamais exposer Modbus TCP directement sur Internet ouvert.
- Utiliser `direct_tcp` seulement via VPN, APN prive ou reseau prive equivalent.
- Un VPN grand public pour naviguer anonymement ne suffit pas. Il faut que Render/la passerelle et le site Zarzis soient dans le meme reseau prive.
- Garder `ALLOW_REMOTE_G781_CONNECT=false` en production : l'IP du G781 doit etre fixee dans `G781_HOST`, pas changee depuis le navigateur.
- Garder `ENABLE_PLANNING=false` si le dashboard doit rester en lecture/commande manuelle seulement.
- Garder `ALLOW_PARAM_WRITE=false` en production. L'activer seulement pour une maintenance courte et controlee.
- Le token API passe seulement par `Authorization: Bearer ...` ou `X-API-Token`. Ne jamais le mettre dans l'URL.
- Les commandes de demarrage sont limitees cote serveur par `COMMAND_MIN_INTERVAL_SEC` et `COMMAND_RESTART_DELAY_SEC`.

## Validation Modbus

Avant le premier demarrage reel :

1. Garder `MODBUS_REGISTERS_VALIDATED=false` dans Render.
2. Avec qModMaster ou Modbus Poll, lire un appareil a la fois en RS485 local.
3. Confirmer les registres de lecture : frequence, courant, tension, defaut, pression, debit.
4. Confirmer le registre de commande et les valeurs marche/arret.
5. Confirmer le sens du flotteur Salmson : si `0 = eau presente`, mettre `SALMSON_FLOAT_LOW_OK_VALUE=0`; si `1 = eau presente`, garder `1`.
6. Corriger les variables Render (`INVT_OFF_VALUE`, `SALMSON_CMD_REG`, `WILO_CMD_REG`, `*_REG_*`) si la notice ou le test donne d'autres valeurs.
7. Faire un essai manuel local on/off avec les securites locales actives.
8. Seulement apres validation, passer `MODBUS_REGISTERS_VALIDATED=true`.
