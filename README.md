# Zarzis Irrigation - Dashboard + API Cloud

Version **v9.1** — WebSocket + cerveau local — **Validé terrain 14/05/2026**

## Architecture

```
Oliveraie 90 arbres / 4.6 ha — Zarzis, Tunisie
14 panneaux Jinko 590W série → INVT GD100-5R5G-4-PV 5.5kW
        → Pompe forage Pedrollo 4PD/2 1.5kW (coffret Salmson EC-L)
        → 2x Pompes booster Wilo TEE (coffret Wilo W-CTRL-EC-B)
        → 6 zones irrigation + 2 zones libres (carte relais GPIO 8CH)

RS485 Modbus RTU — bus linéaire :
  DR302 [120Ω] → Wilo (addr 3) → INVT (addr 1) → Salmson (addr 2) [120Ω]
  Câble EIB-Y(St)Y : Rouge=A+, Noir=B-, Blindage=GND, Blanc/Jaune=non utilisés
  Vitesse : 9600 baud 8N1

USR-DR302 (192.168.1.250:502)
        → Ethernet Modbus TCP
Box Huawei B528s (192.168.1.1)
        → Ethernet LAN
Raspberry Pi 4 (192.168.1.31) — zarzis/zarzis2026
        → HTTPS sortant
Render API + Dashboard (https://zarzis-irrigation-1.onrender.com)
```

## Registres Modbus validés terrain (14/05/2026)

### INVT GD100-PV (adresse 1)

| Registre | Hex | Valeur | Description |
|----------|-----|--------|-------------|
| CMD | 0x1000 | 1=marche, 0=arrêt | Commande (FC06) |
| Fréquence sortie | 0x3000 | /100 Hz | ex: 5000 = 50.00 Hz |
| Fréquence consigne | 0x3001 | /100 Hz | |
| Tension DC bus | 0x3002 | /10 V | ex: 6964 = 696.4V |
| Tension sortie | 0x3003 | V | ex: 379V |
| Courant | 0x3004 | /10 A | ex: 38 = 3.8A |
| Puissance | 0x3005 | W | ex: 1500 = 1500W |
| Charge % | 0x3006 | /10 % | ex: 263 = 26.3% |
| Température | 0x3007 | /10 °C | ex: 225 = 22.5°C |
| Code défaut | 0x5000 | 0=OK | |

**Paramètres configurés :** P00.01=2 (Modbus), P14.00=1, P14.01=3 (9600), P14.02=0 (8N1)

### Salmson EC-L (adresse 2)

| Registre | Valeur | Description |
|----------|--------|-------------|
| 14 | 1=marche, 0=arrêt | Commande |
| 24 | cm | Niveau réservoir |
| 40 | mode | Pompe 1 (2=standby, EN MARCHE) |
| 61 | état | Switch state |
| 138 | 0=OK | Code défaut |
| 197 | bit0=dry_run | Float state (bit 0=1 → marche à sec) |

**Paramètres configurés :** menu 2.01=ON, 2.02=9600, 2.03=2, 2.04=None
**Note :** SALMSON_FLOAT_LOW_OK_VALUE=0 → check marche à sec désactivé (pas de flotteur forage)

### Wilo W-CTRL-EC-B (adresse 3)

| Registre | Valeur | Description |
|----------|--------|-------------|
| 14 | 1=marche, 0=arrêt | Commande |
| 25 | /10 bar | Pression actuelle (0 quand arrêté) |
| 26 | /10 bar | Pression consigne (fixe = 2.0 bar) |
| 40 | 2=actif | Mode pompe 1 |
| 41 | 2=standby | Mode pompe 2 |
| 61 | état | Switch state |
| 138 | 0=OK | Code défaut |

**Paramètres configurés :** menu 2.02=9600, 2.03=3, 2.04=None
**J2 retiré** (terminaison 120Ω désactivée — Wilo est au milieu du bus)

## Agent local (Raspberry Pi)

```bash
# Démarrage service
sudo systemctl start zarzis-agent
sudo systemctl status zarzis-agent

# Logs
journalctl -u zarzis-agent -f

# Test Modbus direct
source ~/zarzis/.venv/bin/activate
python3 -c "
from pymodbus.client import ModbusTcpClient
c = ModbusTcpClient('192.168.1.250', port=502, timeout=3)
c.connect()
print('INVT freq:', c.read_holding_registers(0x3000, 1, slave=1).registers)
print('Wilo pression:', c.read_holding_registers(25, 1, slave=3).registers)
print('Salmson float:', c.read_holding_registers(197, 1, slave=2).registers)
c.close()
"
```

## Variables Render (état 14/05/2026)

Variables critiques modifiées par rapport à la config initiale :

```
INVT_CMD_REG=0x1000          # était 0x2000 — corrigé terrain
INVT_OFF_VALUE=0             # était 5 — corrigé terrain
INVT_ON_VALUE=1              # validé
SALMSON_COMMAND_ENABLED=true # était false
SALMSON_FLOAT_LOW_OK_VALUE=0 # était 1 — pas de flotteur forage
SALMSON_REG_LEVEL_CM=24      # était 25 — corrigé terrain
WILO_COMMAND_ENABLED=true    # ajouté
INVT_COMMAND_ENABLED=true    # ajouté
SERVER_SAFETY_ENABLED=false  # désactivé pour exploitation
COMMAND_MIN_INTERVAL_SEC=0   # désactivé
COMMAND_RESTART_DELAY_SEC=0  # désactivé
MODBUS_REGISTERS_VALIDATED=true  # validé terrain
EDGE_ALLOW_START=true
```

## Modifications code validées terrain

### zarzis_edge_agent.py

1. **Fire-and-forget Wilo** (ligne 561) — écriture sans blocage sur ACK
2. **Fire-and-forget Salmson** (ligne 564) — écriture sans blocage sur ACK
3. **Wilo fail-closed** (ligne 506) — "pressure" retiré du check obligatoire
4. **Salmson float check** (ligne 502) — SALMSON_FLOAT_LOW_OK_VALUE=0 bypass

### index.html (dashboard)

1. Sécurité exploitation — **supprimée**
2. Actions rapides — **supprimées**
3. Mode simulation — **supprimé**
4. Mode AUTO/MANUEL — **supprimé**
5. canExecutePumpCommand — toujours autorisé
6. Connexion automatique au démarrage
7. Reconnexion permanente en arrière-plan
8. Synchronisation données inter-modules (Tableau de bord ↔ Contrôle ↔ État)
9. Wilo pression cible : 3.5 bar → **2 bar**

## Endpoints API

```
GET  /api/ping           — pas d'auth requise
GET  /api/status         — état complet INVT + Salmson + Wilo + GPIO
POST /api/control        — démarrage/arrêt équipements
POST /api/inverter       — commande INVT spécifique
POST /api/relay/zone     — électrovanne zone N
GET  /api/history        — historique mesures et commandes
POST /api/edge/push      — push agent local → cloud
GET  /api/edge/commands  — commandes en attente pour l'agent
POST /api/edge/ack       — accusé réception agent
```

## Commandes disponibles

```json
POST /api/control
{ "device": "forage", "action": "on" }   // Pompe forage via INVT
{ "device": "wilo",   "action": "on" }   // Surpresseur Wilo
{ "device": "salmson","action": "on" }   // Coffret Salmson
{ "device": "all",    "action": "off" }  // Tout arrêter
```

```json
POST /api/relay/zone
{ "zone": 1, "action": "on", "duration_min": 20 }  // Zone irrigation
```

## Sécurités physiques (ne pas modifier)

Les sécurités pompes restent locales dans les coffrets d'origine :
- Salmson EC-L : protection thermique, manque d'eau (bornes 25/26 pontées = bypass), flotteur haut (bornes 26/27)
- Wilo W-CTRL-EC-B : protection thermique, pression min/max
- INVT GD100-PV : protection DC bus, surintensité, surchauffe

Le dashboard supervise et commande mais ne remplace pas les sécurités électriques locales.
