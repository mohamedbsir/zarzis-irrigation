# Zarzis Irrigation — Terrain Express v8.8 FINAL

Pack préparé pour une intervention courte sur site : brancher, régler les appareils, tester, repartir. Aucun changement Python/HTML n’est prévu sur place.

## Profil livré

```txt
EDGE_MODE=http_push
MODBUS_REGISTERS_VALIDATED=true
EDGE_ALLOW_START=true
SALMSON_COMMAND_ENABLED=true
ENABLE_PLANNING=false
ALLOW_PARAM_WRITE=false
SERVER_SAFETY_ENABLED=true
```

Ce profil ouvre les commandes manuelles pour test terrain dès que l’agent local remonte des mesures fraîches. Il ne rend pas le planning automatique actif et ne permet pas l’écriture libre de paramètres Modbus.

## Architecture

```txt
Téléphone / PC
  -> Dashboard PWA HTTPS Render
  -> API Flask Render
  -> Agent local Zarzis sur Raspberry / mini PC
  -> HTTPS sortant vers Render
  -> DR302 en Ethernet LAN
  -> RS485 Modbus RTU
  -> INVT / Salmson / Wilo / coffret 4
```

Le mode retenu est `http_push`. Render ne contacte pas le DR302 directement. L’agent local lit le DR302 sur le LAN et pousse les mesures vers Render, ce qui évite le problème NAT/4G.

## Fichiers terrain importants

```txt
START_HERE_TERRAIN_EXPRESS.md     procédure courte
CHECKLIST_TERRAIN_15_MIN.md       checklist câblage + test
PARAMETRES_DR302_APPAREILS.md     valeurs Modbus à entrer
agent.env.terrain                 modèle environnement Raspberry
render.env.terrain                modèle variables Render
install_raspberry_agent.sh        installation service systemd
terrain_check.py                  diagnostic lecture seule
stop_all_cloud.py                 arrêt logiciel ALL via cloud
REBLOCAGE_RAPIDE.md               retour arrière sans modifier le code
```

## Avant de partir

1. Remplacer les fichiers du dépôt GitHub par ceux de ce ZIP.
2. Laisser Render redéployer.
3. Dans Render > Environment, mettre un `API_TOKEN` long.
4. Vérifier que Render a bien :

```txt
EDGE_MODE=http_push
MODBUS_REGISTERS_VALIDATED=true
SALMSON_COMMAND_ENABLED=true
ENABLE_PLANNING=false
ALLOW_PARAM_WRITE=false
```

5. Copier le ZIP sur clé USB ou sur le Raspberry.
6. Mettre le même `API_TOKEN` dans `agent.env.terrain`.
7. Mettre l’IP prévue du DR302 dans `DR302_HOST`, par exemple `192.168.1.10`.

## Sur place

Régler les appareils :

```txt
INVT    adresse 1, 9600 8N1
Salmson adresse 2, 9600 8N1
Wilo    adresse 3, 9600 8N1
Coffret adresse 4, 9600 8N1
```

Régler le DR302 :

```txt
Mode     : Modbus TCP vers Modbus RTU
Port TCP : 502 local LAN
RS485    : 9600 / 8 / None / 1
IP       : fixe ou réservation DHCP, conseillé 192.168.1.10
```

Installer l’agent :

```bash
sudo bash install_raspberry_agent.sh
sudo nano /etc/zarzis/agent.env
sudo systemctl restart zarzis-agent
/opt/zarzis/.venv/bin/python /opt/zarzis/terrain_check.py
journalctl -u zarzis-agent -f
```

## Test terrain conseillé

1. Envoyer d’abord `STOP ALL`.
2. Tester INVT/forage 5 à 10 secondes, puis OFF.
3. Tester Wilo 5 à 10 secondes, puis OFF.
4. Tester Salmson 5 à 10 secondes uniquement sous surveillance locale.
5. Vérifier les ACK et les valeurs dans le dashboard.

Les protections électriques et hydrauliques locales restent prioritaires : thermique, manque d’eau, pression, défaut variateur, arrêt d’urgence, disjoncteurs.

## Reblocage rapide

Sans modifier le code :

```txt
Render    : MODBUS_REGISTERS_VALIDATED=false, SALMSON_COMMAND_ENABLED=false
Raspberry : EDGE_ALLOW_START=false, SALMSON_COMMAND_ENABLED=false
```

Puis :

```bash
sudo systemctl restart zarzis-agent
```
