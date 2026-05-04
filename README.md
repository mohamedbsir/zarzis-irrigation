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

Important : Render expose surtout HTTP/HTTPS. Le mode `direct_tcp` fonctionne seulement si le G781-E est joignable depuis Internet, par exemple via IP publique, APN privé ou VPN.

Si la SIM 4G n'a pas d'IP publique, utiliser plutôt :

1. `G781_MODE=http_push` avec `/api/g781/push` et `/api/g781/commands`
2. ou une passerelle locale DR302/Raspberry qui synchronise avec le cloud

## Variables Render

Dans Render > Environment :

```txt
G781_MODE=direct_tcp
G781_HOST=
G781_PORT=502
API_TOKEN=mot_de_passe_long
UPDATE_SEC=5
ADDR_INVT=1
ADDR_SALMSON=2
ADDR_WILO=3
ADDR_COFFRET4=4
```

Rain Bird est optionnel. Ne pas mettre de clé Rain Bird directement dans le code :

```txt
RAINBIRD_STICK_ID=
RAINBIRD_SERIAL=
RAINBIRD_KEYCODE=
RAINBIRD_WIFI=
RAINBIRD_ZONES=1,2,3,4,5,6
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
POST /api/param/write
GET  /api/planning
POST /api/planning
GET  /api/events
POST /api/g781/push
GET  /api/g781/commands
```

Les commandes `POST` demandent `API_TOKEN` si la variable existe sur Render. Dans le dashboard, renseigner ce token dans l'onglet Connexion.

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

- planning exécuté côté serveur, sauvegardé dans `planning_zarzis.json`
- synchronisation des programmes du dashboard vers `/api/planning`
- mode simulation backend via `G781_MODE=simulation` pour tester avant réception du matériel
- protection API sur tous les endpoints `/api/*` sauf `/api/ping` quand `API_TOKEN` existe
- verrouillage serveur minimal avant démarrage si défaut INVT/Salmson/Wilo déjà connu
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
