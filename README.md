# Zarzis Irrigation — Mise à jour GitHub V3

Cette mise à jour prépare le projet pour :

- 4 appareils Modbus : INVT, Salmson, Wilo, coffret/capteur 4
- démarrage / arrêt depuis le dashboard
- planning simple de démarrage / arrêt
- lecture des états, consommations et alertes
- connexion USR-G781-E / DR302 selon architecture retenue

## Fichiers à mettre sur GitHub

Remplacer ou ajouter :

```txt
modbus_proxy_cloud.py
requirements.txt
render.yaml
README.md
GUIDE_INSTALLATION_ZARZIS.txt
MATERIEL_A_ACHETER.txt
```

## Variables Render importantes

Dans Render > Environment :

```txt
G781_MODE=direct_tcp
G781_HOST=
G781_PORT=502
API_TOKEN=mettre_un_mot_de_passe_long
MODBUS_BAUDRATE=9600
MODBUS_SERIAL=8N1
```

### Très important

Render expose surtout HTTP/HTTPS. Si le G781-E est derrière une SIM 4G sans IP publique, le mode `direct_tcp` ne pourra pas l'appeler directement.

Solutions possibles :

1. APN privé / IP publique / VPN : `direct_tcp` possible.
2. G781 en mode HTTPD Client : utiliser `/api/g781/push` et `/api/g781/commands`.
3. Architecture pro locale : DR302/Raspberry local puis synchro cloud.

## Endpoints API

```txt
GET  /api/ping
GET  /api/status
GET  /api/devices
POST /api/control
POST /api/param/read
POST /api/param/write
GET  /api/planning
POST /api/planning
GET  /api/events
```

## Commande start/stop

Exemple :

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
wilo
coffret4
all
```

## Planning

Exemple :

```json
POST /api/planning
{
  "planning": [
    {"device":"salmson", "action":"on",  "time":"08:00", "days":[0,1,2,3,4,5,6], "enabled":true},
    {"device":"salmson", "action":"off", "time":"09:00", "days":[0,1,2,3,4,5,6], "enabled":true}
  ]
}
```

Les jours sont :

```txt
0 = lundi
1 = mardi
2 = mercredi
3 = jeudi
4 = vendredi
5 = samedi
6 = dimanche
```

## Sécurité

Les sécurités pompes doivent rester locales dans les coffrets d'origine : thermique, manque d'eau, pression, défaut variateur, arrêt d'urgence.

Le dashboard sert seulement à superviser, démarrer, arrêter et planifier.
