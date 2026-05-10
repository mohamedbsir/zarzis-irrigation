# ZARZIS TERRAIN EXPRESS v8.8 FINAL

Objectif : arriver sur place, brancher, régler les appareils, tester, repartir. Pas de reprogrammation Python/HTML sur site.

## État livré

```txt
MODBUS_REGISTERS_VALIDATED=true
EDGE_ALLOW_START=true
SALMSON_COMMAND_ENABLED=true
ENABLE_PLANNING=false
ALLOW_PARAM_WRITE=false
SERVER_SAFETY_ENABLED=true
EDGE_MODE=http_push
```

Les commandes manuelles sont prêtes dès que l'agent local remonte des mesures fraîches. Les sécurités runtime restent actives : agent absent, mesures anciennes, défaut INVT/Wilo/Salmson, manque d'eau, défaut Modbus, anti-redémarrage rapide.

## Avant de partir

1. Remplacer le dépôt GitHub par ce ZIP.
2. Déployer Render.
3. Dans Render > Environment, vérifier `API_TOKEN`, `EDGE_MODE=http_push`, `MODBUS_REGISTERS_VALIDATED=true`, `SALMSON_COMMAND_ENABLED=true`.
4. Copier ce ZIP sur clé USB ou Raspberry.
5. Dans `agent.env.terrain`, mettre le même `API_TOKEN` que Render et l'IP du DR302.

## Sur place

```bash
sudo bash install_raspberry_agent.sh
sudo nano /etc/zarzis/agent.env
sudo systemctl restart zarzis-agent
/opt/zarzis/.venv/bin/python /opt/zarzis/terrain_check.py
journalctl -u zarzis-agent -f
```

## Premier essai conseillé

1. Envoyer d'abord un `STOP ALL`.
2. Tester INVT/forage 5 à 10 secondes, puis OFF.
3. Tester Wilo 5 à 10 secondes, puis OFF.
4. Tester Salmson 5 à 10 secondes uniquement sous surveillance locale.
5. Vérifier les ACK dans le dashboard.

L'arrêt d'urgence et les protections coffret restent prioritaires sur le logiciel.
