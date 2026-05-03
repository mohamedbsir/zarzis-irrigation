#!/usr/bin/env python3
"""
=================================================
PROXY MODBUS CLOUD — ZARZIS IRRIGATION SOLAIRE
=================================================
Serveur cloud pour USR-G781 4G
→ Deploy sur Render.com (gratuit)
→ USR-G781 se connecte en mode TCP Client
→ Notre dashboard s'y connecte depuis la France

Prérequis : pip install flask flask-cors pymodbus gunicorn
Lancement local  : python3 modbus_proxy.py
Lancement cloud  : gunicorn modbus_proxy:app
=================================================
"""

import os
import threading
import time
import logging
from flask import Flask, jsonify, request
from flask_cors import CORS
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

# ============ CONFIGURATION ============
# En mode cloud : USR-G781 se connecte en TCP Client
# Le serveur écoute les connexions entrantes du G781
USR_G781_IP   = os.environ.get("USR_G781_IP", "0.0.0.0")
USR_G781_PORT = int(os.environ.get("USR_G781_PORT", 502))
SERVER_PORT   = int(os.environ.get("PORT", 8080))
UPDATE_SEC    = 5

# Adresses Modbus
ADDR_INVT    = 1
ADDR_SALMSON = 2
ADDR_WILO    = 3

# Registres INVT GD100-PV
INVT_REGS = {
    "freq_hz"   : 0x1000,
    "current_a" : 0x1001,
    "voltage_v" : 0x1002,
    "dc_bus_v"  : 0x1003,
    "power_kw"  : 0x1004,
    "fault_code": 0x8000,
}
INVT_CMD = 0x2000  # 1=marche, 5=arrêt

# Registres Salmson
SALMSON_REGS = {
    "pump_state": 0x0001,
    "current_a" : 0x0010,
    "error_code": 0x0020,
    "float_low" : 0x0030,
    "float_high": 0x0031,
}
SALMSON_CMD = 0x0100

# Registres Wilo
WILO_REGS = {
    "pressure"  : 0x0001,
    "flow"      : 0x0002,
    "pump1"     : 0x0010,
    "pump2"     : 0x0011,
    "error_code": 0x0020,
}
WILO_CMD = 0x0100

# ============ APP FLASK ============
app = Flask(__name__)
CORS(app, origins="*")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
log = logging.getLogger(__name__)

# Cache données
cache = {
    "invt"       : {"status": "DÉCONNECTÉ"},
    "salmson"    : {"status": "DÉCONNECTÉ"},
    "wilo"       : {"status": "DÉCONNECTÉ"},
    "connected"  : False,
    "last_update": 0,
    "g781_ip"    : "En attente...",
}

client = None
lock = threading.Lock()

# ============ MODBUS ============
def connect(ip, port):
    global client
    try:
        if client and client.is_socket_open():
            client.close()
        client = ModbusTcpClient(ip, port=port, timeout=5)
        ok = client.connect()
        if ok:
            log.info(f"✅ Connecté au USR-G781 ({ip}:{port})")
            cache["connected"] = True
            cache["g781_ip"] = ip
            return True
        log.warning(f"⚠️ Connexion échouée ({ip}:{port})")
        cache["connected"] = False
        return False
    except Exception as e:
        log.error(f"❌ Erreur connexion: {e}")
        cache["connected"] = False
        return False

def read_reg(addr, reg, count=1):
    try:
        r = client.read_holding_registers(reg, count, slave=addr)
        if r and not r.isError():
            return r.registers
        return None
    except:
        return None

def write_reg(addr, reg, val):
    try:
        r = client.write_register(reg, val, slave=addr)
        return not r.isError()
    except Exception as e:
        log.error(f"Erreur écriture: {e}")
        return False

def get_invt():
    d = {}
    for name, addr in INVT_REGS.items():
        v = read_reg(ADDR_INVT, addr)
        if v:
            raw = v[0]
            if name == "freq_hz":    d[name] = round(raw/100, 2)
            elif name == "current_a": d[name] = round(raw/10, 1)
            elif name == "power_kw":  d[name] = round(raw/10, 2)
            else: d[name] = raw
    d["running"]    = d.get("freq_hz", 0) > 0.5
    fc = d.get("fault_code", 0)
    d["error_text"] = decode_invt(fc) if fc else None
    d["status"]     = "ERREUR" if fc else ("EN MARCHE" if d["running"] else "ARRÊTÉ")
    return d

def get_salmson():
    d = {}
    for name, addr in SALMSON_REGS.items():
        v = read_reg(ADDR_SALMSON, addr)
        if v:
            raw = v[0]
            d[name] = round(raw/10, 1) if name == "current_a" else raw
    d["running"]    = d.get("pump_state", 0) == 1
    ec = d.get("error_code", 0)
    d["error_text"] = decode_salmson(ec) if ec else None
    d["status"]     = "ERREUR" if ec else ("EN MARCHE" if d["running"] else "ARRÊTÉE")
    return d

def get_wilo():
    d = {}
    for name, addr in WILO_REGS.items():
        v = read_reg(ADDR_WILO, addr)
        if v:
            raw = v[0]
            if name in ("pressure","flow"): d[name] = round(raw/100, 2)
            else: d[name] = raw
    d["running"]    = d.get("pump1",0)==1 or d.get("pump2",0)==1
    ec = d.get("error_code", 0)
    d["error_text"] = f"Erreur Wilo {ec}" if ec else None
    d["status"]     = "ERREUR" if ec else ("EN MARCHE" if d["running"] else "ARRÊTÉ")
    return d

# ============ DÉCODEURS ERREURS ============
def decode_invt(code):
    return {
        22: "A-LS — Tension DC insuffisante",
        7 : "UV — Sous-tension bus DC",
        1 : "OC1 — Surintensité",
        9 : "OL2 — Surcharge moteur",
    }.get(code, f"Erreur INVT {code}")

def decode_salmson(code):
    return {
        40: "E040 — Manque eau / Marche à sec",
        80: "E080 — Surcharge moteur",
        82: "E082 — Protection thermique",
        90: "E090 — Défaut thermique",
    }.get(code, f"Erreur Salmson {code}")

# ============ BOUCLE ARRIÈRE-PLAN ============
def update_loop():
    global client
    ip   = os.environ.get("USR_G781_IP", "")
    port = int(os.environ.get("USR_G781_PORT", 502))

    while True:
        try:
            if not ip or ip == "0.0.0.0":
                # Attente connexion entrante du G781
                time.sleep(5)
                continue

            if not cache["connected"] or not client or not client.is_socket_open():
                connect(ip, port)
                time.sleep(5)
                continue

            with lock:
                cache["invt"]    = get_invt()
                cache["salmson"] = get_salmson()
                cache["wilo"]    = get_wilo()
                cache["last_update"] = time.time()

            time.sleep(UPDATE_SEC)

        except Exception as e:
            log.error(f"Erreur boucle: {e}")
            cache["connected"] = False
            time.sleep(10)

# ============ API ============
@app.route('/api/status')
def status():
    with lock:
        return jsonify({
            "connected"  : cache["connected"],
            "last_update": cache["last_update"],
            "g781_ip"    : cache["g781_ip"],
            "invt"       : cache["invt"],
            "salmson"    : cache["salmson"],
            "wilo"       : cache["wilo"],
        })

@app.route('/api/connect', methods=['POST'])
def api_connect():
    """Mettre à jour l'IP du USR-G781 depuis le dashboard"""
    body = request.json or {}
    ip   = body.get("ip", "")
    port = int(body.get("port", 502))
    if not ip:
        return jsonify({"success": False, "error": "IP manquante"}), 400

    os.environ["USR_G781_IP"]   = ip
    os.environ["USR_G781_PORT"] = str(port)

    ok = connect(ip, port)
    return jsonify({"success": ok, "ip": ip, "port": port})

@app.route('/api/control', methods=['POST'])
def control():
    body   = request.json or {}
    pump   = body.get("pump")
    action = body.get("action")

    if not cache["connected"]:
        return jsonify({"success": False, "error": "USR-G781 non connecté"}), 503

    try:
        with lock:
            if pump == "forage":
                write_reg(ADDR_INVT,    INVT_CMD,    1 if action=="on" else 5)
                write_reg(ADDR_SALMSON, SALMSON_CMD, 1 if action=="on" else 0)

            elif pump == "wilo":
                write_reg(ADDR_WILO, WILO_CMD, 1 if action=="on" else 0)

            elif pump == "all":
                if action == "on":
                    write_reg(ADDR_INVT,    INVT_CMD,    1)
                    write_reg(ADDR_SALMSON, SALMSON_CMD, 1)
                    write_reg(ADDR_WILO,    WILO_CMD,    1)
                else:
                    write_reg(ADDR_INVT,    INVT_CMD,    5)
                    write_reg(ADDR_SALMSON, SALMSON_CMD, 0)
                    write_reg(ADDR_WILO,    WILO_CMD,    0)

        log.info(f"✓ Commande: {pump} → {action}")
        return jsonify({"success": True, "pump": pump, "action": action})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/param/read', methods=['POST'])
def param_read():
    """Lit un registre Modbus"""
    body = request.json or {}
    addr = int(body.get('addr', 1))
    reg  = int(body.get('reg', 0))
    try:
        with lock:
            vals = read_reg(addr, reg)
        if vals:
            return jsonify({"success": True, "value": vals[0], "reg": hex(reg)})
        return jsonify({"success": False, "error": "Registre illisible"}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/param/write', methods=['POST'])
def param_write():
    """Écrit une valeur dans un registre Modbus"""
    body  = request.json or {}
    addr  = int(body.get('addr', 1))
    reg   = int(body.get('reg', 0))
    value = int(body.get('value', 0))
    try:
        with lock:
            ok = write_reg(addr, reg, value)
        if ok:
            log.info(f"✏️ Écriture addr={addr} reg={hex(reg)} val={value}")
            return jsonify({"success": True, "reg": hex(reg), "value": value})
        return jsonify({"success": False, "error": "Écriture échouée"}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/ping')
def ping():
    return jsonify({
        "status"   : "ok",
        "connected": cache["connected"],
        "g781_ip"  : cache["g781_ip"],
    })

# ============ RAIN BIRD API ============
# Infos extraites du fichier ESP-TM2.rbcf
RAINBIRD_STICK_ID    = "10061C5496B8"
RAINBIRD_SERIAL      = "30C22470FE790000"
RAINBIRD_KEYCODE     = "03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f44f0d27b39a217a2468cdf80e5e4938a83cd0b77c49eee11a9dd267df61c466ba580fccb4b4b25348190a6ed91c5dd9c9"
RAINBIRD_WIFI        = "BABOUCHE 2"
RAINBIRD_ZONES       = [1, 2, 3, 4, 5, 6]
RAINBIRD_ENABLED     = [1, 3]
RAINBIRD_PROGRAMS    = {"1": "Arrosage", "2": "Fertilisation"}

# IP locale du Rain Bird sur le WiFi BABOUCHE 2
# À mettre à jour avec l'IP réelle une fois connecté
rainbird_ip = os.environ.get("RAINBIRD_IP", "")
rainbird_state = {
    "connected"   : False,
    "active_zones": [],
    "ip"          : "",
    "last_cmd"    : "",
}

def rainbird_request(ip, command, params=None):
    """Envoie une commande HTTP à l'API locale Rain Bird LNK"""
    import hashlib, json as jsonlib
    try:
        import urllib.request, urllib.error
        url = f"http://{ip}/stick"
        data = {
            "id"     : RAINBIRD_STICK_ID,
            "command": command,
        }
        if params:
            data.update(params)
        payload = jsonlib.dumps(data).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Basic {RAINBIRD_KEYCODE[:32]}"
            }
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return jsonlib.loads(resp.read())
    except Exception as e:
        log.error(f"Rain Bird erreur: {e}")
        return None

@app.route('/api/rainbird/config')
def rainbird_config():
    """Retourne la configuration Rain Bird"""
    return jsonify({
        "stick_id"  : RAINBIRD_STICK_ID,
        "serial"    : RAINBIRD_SERIAL,
        "wifi"      : RAINBIRD_WIFI,
        "zones"     : RAINBIRD_ZONES,
        "enabled"   : RAINBIRD_ENABLED,
        "programs"  : RAINBIRD_PROGRAMS,
        "ip"        : rainbird_ip,
        "connected" : rainbird_state["connected"],
        "active"    : rainbird_state["active_zones"],
    })

@app.route('/api/rainbird/setip', methods=['POST'])
def rainbird_setip():
    """Définir l'IP locale du Rain Bird"""
    global rainbird_ip
    body = request.json or {}
    ip   = body.get("ip", "")
    if not ip:
        return jsonify({"success": False, "error": "IP manquante"}), 400
    rainbird_ip = ip
    rainbird_state["ip"] = ip
    os.environ["RAINBIRD_IP"] = ip
    log.info(f"🌧️ Rain Bird IP définie : {ip}")
    return jsonify({"success": True, "ip": ip})

@app.route('/api/rainbird/start', methods=['POST'])
def rainbird_start():
    """Démarrer une zone Rain Bird"""
    body     = request.json or {}
    zone     = int(body.get("zone", 1))
    duration = int(body.get("duration", 10))  # minutes

    if zone not in RAINBIRD_ZONES:
        return jsonify({"success": False, "error": f"Zone {zone} invalide"}), 400

    log.info(f"🌧️ Rain Bird: Démarrer zone {zone} pendant {duration} min")
    rainbird_state["last_cmd"] = f"START Zone {zone} {duration}min"

    if not rainbird_ip:
        # Mode simulation
        rainbird_state["active_zones"] = [zone]
        return jsonify({
            "success": True, "zone": zone,
            "duration": duration, "mode": "simulation"
        })

    result = rainbird_request(rainbird_ip, "ZoneStartRequest", {
        "zone"    : zone,
        "duration": duration
    })

    if result:
        rainbird_state["active_zones"] = [zone]
        rainbird_state["connected"]    = True
        return jsonify({"success": True, "zone": zone, "duration": duration, "result": result})
    else:
        return jsonify({"success": False, "error": "Pas de réponse Rain Bird"}), 503

@app.route('/api/rainbird/stop', methods=['POST'])
def rainbird_stop():
    """Arrêter toutes les zones Rain Bird"""
    body = request.json or {}
    zone = body.get("zone", None)

    log.info(f"🌧️ Rain Bird: Arrêt {'zone '+str(zone) if zone else 'toutes zones'}")
    rainbird_state["last_cmd"] = f"STOP {'zone '+str(zone) if zone else 'tout'}"
    rainbird_state["active_zones"] = []

    if not rainbird_ip:
        return jsonify({"success": True, "mode": "simulation"})

    result = rainbird_request(rainbird_ip, "StopIrrigationRequest")
    if result:
        rainbird_state["connected"] = True
        return jsonify({"success": True, "result": result})
    else:
        return jsonify({"success": False, "error": "Pas de réponse Rain Bird"}), 503

@app.route('/api/rainbird/status')
def rainbird_status():
    """État actuel du Rain Bird"""
    if not rainbird_ip:
        return jsonify({
            "connected"   : False,
            "active_zones": rainbird_state["active_zones"],
            "last_cmd"    : rainbird_state["last_cmd"],
            "mode"        : "simulation",
        })

    result = rainbird_request(rainbird_ip, "CurrentIrrigationStateRequest")
    if result:
        rainbird_state["connected"] = True
        return jsonify({
            "connected"   : True,
            "active_zones": rainbird_state["active_zones"],
            "last_cmd"    : rainbird_state["last_cmd"],
            "raw"         : result,
        })
    return jsonify({
        "connected"   : False,
        "active_zones": rainbird_state["active_zones"],
        "error"       : "Pas de réponse",
    })

@app.route('/')
def index():
    return """
    <h2>🌱 Zarzis Irrigation — Serveur Cloud</h2>
    <p>API disponible sur <code>/api/status</code></p>
    <p>Connecté: {}</p>
    """.format("✅ OUI" if cache["connected"] else "⏳ En attente USR-G781")

# ============ DÉMARRAGE ============
if __name__ == '__main__':
    log.info("=" * 50)
    log.info("  ZARZIS CLOUD SERVER — MODBUS PROXY")
    log.info("=" * 50)
    t = threading.Thread(target=update_loop, daemon=True)
    t.start()
    app.run(host='0.0.0.0', port=SERVER_PORT, debug=False)
