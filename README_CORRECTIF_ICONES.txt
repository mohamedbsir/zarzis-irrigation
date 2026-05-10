# Correctif icones Zarzis

Ce pack corrige les 404 visibles dans Chrome DevTools :

- `/icon.svg`
- `/icons/icon-192.png`
- `/icons/icon-512.png`

Il est adapte a ton depot GitHub actuel, ou les fichiers image sont a la racine avec accents :

- `icône.svg`
- `icône-192.png`
- `icône-512.png`

## Fichiers a remplacer

Remplace a la racine du depot GitHub :

- `index.html`
- `manifest.webmanifest`
- `sw.js`
- `modbus_proxy_cloud.py`

Garde les fichiers image actuels :

- `icône.svg`
- `icône-192.png`
- `icône-512.png`

## Commit GitHub

```bash
git add .
git commit -m "fix pwa icon paths"
git push
```

## Verification apres deploy Render

Ouvre ces liens :

```txt
https://zarzis-irrigation-1.onrender.com/icône.svg
https://zarzis-irrigation-1.onrender.com/icône-192.png
https://zarzis-irrigation-1.onrender.com/icône-512.png
```

Et aussi les anciens chemins, qui sont maintenant compatibles :

```txt
https://zarzis-irrigation-1.onrender.com/icon.svg
https://zarzis-irrigation-1.onrender.com/icons/icon-192.png
https://zarzis-irrigation-1.onrender.com/icons/icon-512.png
```

Si les images s'ouvrent, c'est bon.

## Telephone

Supprime l'ancienne PWA puis reinstalle-la.
Sinon le telephone peut garder l'ancienne icone cassee en cache.
