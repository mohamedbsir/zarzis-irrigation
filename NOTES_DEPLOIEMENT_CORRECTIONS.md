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
