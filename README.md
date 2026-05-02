# Zarzis Irrigation — Mise à jour GitHub Dashboard + API

Mise à jour centrée sur GitHub/Render et le dashboard.

## Fichiers

```txt
index.html
manifest.webmanifest
service-worker.js
icons/icon-192.png
icons/icon-512.png
modbus_proxy_cloud.py
requirements.txt
render.yaml
README.md
```

## Ce qui a été corrigé

- Dashboard vérifié : syntaxe JavaScript OK.
- URL Render par défaut corrigée : `https://zarzis-irrigation-1.onrender.com`.
- Ajout d'un champ Token API optionnel dans l'onglet Connexion.
- API Flask vérifiée : syntaxe Python OK.
- Endpoints compatibles avec le dashboard :
  - `GET /api/ping`
  - `GET /api/status`
  - `POST /api/connect`
  - `POST /api/control`
  - `POST /api/param/read`
  - `POST /api/param/write`
  - `GET/POST /api/planning`
  - `GET /api/events`

## Render

Le service Render démarre avec :

```txt
gunicorn modbus_proxy_cloud:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120
```

## Sécurité

Pour l'instant `API_TOKEN` est vide dans `render.yaml` pour éviter de bloquer le dashboard.
Plus tard, tu peux mettre un token dans Render, puis le renseigner dans le dashboard : onglet Connexion > Token API Render.

## Commande start/stop

```json
{
  "pump": "wilo",
  "action": "on"
}
```

ou

```json
{
  "pump": "forage",
  "action": "off"
}
```

Le dashboard garde les sécurités dans les coffrets d'origine. Il sert à démarrer, arrêter, planifier et visualiser.
