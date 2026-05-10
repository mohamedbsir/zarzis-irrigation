#!/usr/bin/env python3
"""Commande arret ALL via le cloud. Ne remplace pas l'arret d'urgence materiel."""
from __future__ import annotations
import json, os, sys, urllib.request
from pathlib import Path
for path in (Path('/etc/zarzis/agent.env'), Path('agent.env.terrain'), Path('agent.env.template')):
    if path.exists():
        for raw in path.read_text(encoding='utf-8', errors='ignore').splitlines():
            line=raw.strip()
            if line and not line.startswith('#') and '=' in line:
                k,v=line.split('=',1); os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
cloud=os.environ.get('CLOUD_URL','https://zarzis-irrigation-1.onrender.com').rstrip('/')
token=os.environ.get('API_TOKEN','').strip()
if not token or 'REMPLACER' in token:
    print('[ERREUR] API_TOKEN non renseigne'); sys.exit(2)
req=urllib.request.Request(cloud+'/api/control', data=json.dumps({'device':'all','action':'off'}).encode('utf-8'), headers={'Content-Type':'application/json','Authorization':'Bearer '+token}, method='POST')
with urllib.request.urlopen(req, timeout=15) as resp: print(resp.read().decode('utf-8'))
