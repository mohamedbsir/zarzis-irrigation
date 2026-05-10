# REBLOCAGE RAPIDE

Ce pack est livré sans verrou logiciel pré-installation. Pour rebloquer sans modifier le code :

Render :

```txt
MODBUS_REGISTERS_VALIDATED=false
SALMSON_COMMAND_ENABLED=false
```

Raspberry `/etc/zarzis/agent.env` :

```txt
EDGE_ALLOW_START=false
SALMSON_COMMAND_ENABLED=false
```

Puis :

```bash
sudo systemctl restart zarzis-agent
```
