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
