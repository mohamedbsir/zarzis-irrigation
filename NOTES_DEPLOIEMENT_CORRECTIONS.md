# Corrections Zarzis - version terrain v8.4 offline

## Ce qui a ete corrige

### Dashboard `index.html`
- Connexion automatique au serveur Render a l'ouverture de l'application.
- Rechargement automatique du dernier etat connu si le serveur est hors-ligne.
- Historique local des derniers etats dans le navigateur.
- Affichage hors-ligne securise: les commandes pompes sont bloquees si le cloud n'est pas joignable.
- Reconnexion automatique quand Internet revient.

### Service worker `sw.js`
- Cache applicatif renomme v8.4.
- Cache des appels API GET pour permettre l'ouverture et la consultation du dernier etat hors-ligne.
- Les POST ne sont pas mis en file dans le navigateur pour eviter une commande retardee dangereuse.

### Agent Raspberry `zarzis_edge_agent.py`
- Ajout d'un buffer offline SQLite local.
- Si Internet/Render coupe, les mesures sont stockees localement puis renvoyees au retour reseau.
- Ajout de logs persistants dans `~/zarzis-data/logs/zarzis_edge_agent.log` par defaut.
- Aucune modification des adresses Modbus ni des registres.
- `EDGE_ALLOW_START=false` reste la securite par defaut.

### Backend Render `modbus_proxy_cloud.py`
- Version `2026.05.10-zarzis-http-push-v8.4-offline`.
- Creation automatique de `DATA_DIR`.
- Logs cloud persistants si `PERSISTENT_STORAGE_ENABLED=true`.
- Retour enrichi sur `/api/ping` et `/api/status` pour verifier le stockage.

### Render `render.yaml`
- Plan passe en `starter`.
- Persistent Disk active: `/var/data`, 1 GB.
- `PERSISTENT_STORAGE_ENABLED=true`.
- `DATA_DIR=/var/data`.

## A faire sur Render

1. Pousser ces fichiers sur GitHub.
2. Render redeploie automatiquement.
3. Verifier `/api/ping`.
4. Dans la reponse, verifier:

```txt
storage.persistent = true
storage.data_dir = /var/data
```

## A faire sur Raspberry

Si tu remplaces seulement `zarzis_edge_agent.py`, pas besoin de reparametrer le DR302 ni les equipements.

A ajouter dans `agent.env` si tu veux forcer le dossier data:

```txt
EDGE_DATA_DIR=/home/zarzis/zarzis-data
EDGE_OFFLINE_BUFFER_ENABLED=true
EDGE_OFFLINE_MAX_ITEMS=10000
```

Puis:

```bash
sudo systemctl restart zarzis-agent
journalctl -u zarzis-agent -f
```

## Important securite

Les commandes de demarrage restent bloquees tant que:

```txt
EDGE_ALLOW_START=false
MODBUS_REGISTERS_VALIDATED=false
```

Ne passe ces valeurs a `true` qu'apres validation Modbus reelle sur site.

---

# Correctif v9.0 — agent permanent + Raspberry cerveau local

## Objectif

Cette version ajoute les deux stratégies en même temps :

1. **Connexion permanente sortante Raspberry -> Render en WebSocket**.
2. **Raspberry conservé comme cerveau local** : lecture DR302 locale, commandes locales, sécurité locale, buffer offline et logs persistants.

L'ancien mode `HTTP PUSH` reste actif comme secours. Si le WebSocket ne se connecte pas, l'agent continue d'envoyer les mesures avec `/api/edge/push` et de récupérer les commandes avec `/api/edge/commands`.

## Fichiers modifiés

### `modbus_proxy_cloud.py`

- Ajout de `flask-sock` pour `/api/edge/ws`.
- Ajout d'une présence agent séparée des mesures Modbus :
  - `agent_online`
  - `data_fresh`
  - `ws_connected`
  - `last_heartbeat_age_sec`
  - `last_telemetry_age_sec`
- Le cloud ne confond plus :
  - agent Raspberry connecté
  - données Modbus fraîches
- Les commandes peuvent être envoyées par WebSocket si l'agent est connecté.
- L'ancien polling HTTP reste disponible.

### `zarzis_edge_agent.py`

- Ajout d'une boucle WebSocket permanente.
- Heartbeat indépendant des lectures Modbus toutes les `EDGE_HEARTBEAT_SEC` secondes.
- Si la lecture Modbus ralentit, l'agent reste visible comme connecté.
- Les commandes WebSocket sont exécutées localement puis acquittées.
- En cas d'échec WebSocket, l'ACK repasse en HTTP de secours.
- La boucle HTTP de commandes reste active uniquement si le WebSocket n'est pas connecté.

### `index.html`

- Auto-connexion dès ouverture de l'application.
- Affichage plus précis :
  - `WS CONNECTÉ ✅`
  - `AGENT OK / MODBUS...`
  - `AGENT ATTENTE`
- Le token est mémorisé par défaut sur l'appareil, sauf si la case est décochée.

### `render.yaml`

- Passage en plan `starter`.
- Ajout du disque persistant `/var/data`.
- `EDGE_PUSH_STALE_SEC=180`.
- `EDGE_AGENT_STALE_SEC=180`.
- `EDGE_WS_ENABLED=true`.
- `EDGE_WS_HEARTBEAT_SEC=10`.
- Gunicorn passe en `--timeout 0` pour éviter de couper les WebSockets.

### `requirements.txt`

Ajout :

```txt
flask-sock
simple-websocket
websocket-client
```

## Variables à mettre sur le Raspberry dans `agent.env`

```txt
CLOUD_URL=https://zarzis-irrigation-1.onrender.com
API_TOKEN=API_TOKEN_RENDER
AGENT_ID=zarzis-edge-agent
DR302_HOST=IP_LOCALE_DR302
DR302_PORT=502

EDGE_WS_ENABLED=true
EDGE_HEARTBEAT_SEC=10
EDGE_STATUS_PUSH_SEC=5
EDGE_COMMAND_POLL_SEC=1
MODBUS_TIMEOUT_SEC=0.8

EDGE_DATA_DIR=/home/zarzis/zarzis-data
EDGE_OFFLINE_BUFFER_ENABLED=true
EDGE_OFFLINE_MAX_ITEMS=10000

EDGE_ALLOW_START=false
SALMSON_COMMAND_ENABLED=false
```

## Test attendu

Après redéploiement Render et redémarrage Raspberry :

```bash
journalctl -u zarzis-agent -f
```

Tu dois voir :

```txt
Connexion WebSocket -> https://.../api/edge/ws
Push OK
```

Dans le dashboard :

```txt
WS CONNECTÉ ✅
```

Si les appareils Modbus ne répondent pas, tu peux voir :

```txt
AGENT OK / MODBUS...
```

Cela veut dire que le Raspberry est connecté, mais que le DR302 ou les registres Modbus ne répondent pas encore correctement.

## Sécurité

Les démarrages restent bloqués tant que :

```txt
EDGE_ALLOW_START=false
MODBUS_REGISTERS_VALIDATED=false
```

C'est volontaire. On ne passe ces deux valeurs à `true` qu'après validation terrain des registres et essais réels.
