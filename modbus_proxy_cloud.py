#!/usr/bin/env python3
"""
Proxy cloud Zarzis Irrigation.

Le serveur expose l'API HTTP du dashboard et, en mode direct_tcp, interroge la
passerelle USR-G781-E/DR302 en Modbus TCP.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from pymodbus.client import ModbusTcpClient


# ============ CONFIGURATION ============
APP_VERSION = "2026.05.04-zarzis-v4"

G781_MODE = os.environ.get("G781_MODE", "direct_tcp").strip().lower()
G781_HOST = os.environ.get("G781_HOST") or os.environ.get("USR_G781_IP", "")
G781_PORT = int(os.environ.get("G781_PORT") or os.environ.get("USR_G781_PORT", "502"))
SERVER_PORT = int(os.environ.get("PORT", "8080"))
UPDATE_SEC = int(os.environ.get("UPDATE_SEC", "5"))
API_TOKEN = os.environ.get("API_TOKEN", "").strip()

ADDR_INVT = int(os.environ.get("ADDR_INVT", "1"))
ADDR_SALMSON = int(os.environ.get("ADDR_SALMSON", "2"))
ADDR_WILO = int(os.environ.get("ADDR_WILO", "3"))
ADDR_COFFRET4 = int(os.environ.get("ADDR_COFFRET4", "4"))


# Registres INVT GD100-PV.
INVT_REGS = {
    "freq_hz": 0x1000,
    "current_a": 0x1001,
    "voltage_v": 0x1002,
    "dc_bus_v": 0x1003,
    "power_kw": 0x1004,
    "fault_code": 0x8000,
}
INVT_CMD = 0x2000
INVT_ACTIONS = {
    "on": 1,
    "start": 1,
    "forward": 1,
    "reverse": 2,
    "off": 5,
    "stop": 5,
}

# Registres Salmson.
SALMSON_REGS = {
    "pump_state": 0x0001,
    "current_a": 0x0010,
    "error_code": 0x0020,
    "float_low": 0x0030,
    "float_high": 0x0031,
}
SALMSON_CMD = 0x0100

# Registres Wilo.
WILO_REGS = {
    "pressure": 0x0001,
    "flow": 0x0002,
    "pump1": 0x0010,
    "pump2": 0x0011,
    "error_code": 0x0020,
}
WILO_CMD = 0x0100

# Coffret/capteur 4 : registres génériques à adapter si le matériel réel change.
COFFRET4_REGS = {
    "input_1": 0x0001,
    "input_2": 0x0002,
    "analog_1": 0x0010,
    "error_code": 0x0020,
}


# ============ APP FLASK ============
app = Flask(__name__)
CORS(app, origins="*", allow_headers=["Content-Type", "Authorization", "X-API-Token"])

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

cache = {
    "invt": {"status": "DÉCONNECTÉ", "running": False},
    "salmson": {"status": "DÉCONNECTÉ", "running": False},
    "wilo": {"status": "DÉCONNECTÉ", "running": False},
    "coffret4": {"status": "DÉCONNECTÉ"},
    "connected": False,
    "last_update": 0,
    "g781_ip": G781_HOST or "En attente...",
    "mode": G781_MODE,
}

client: ModbusTcpClient | None = None
current_host = G781_HOST
current_port = G781_PORT
lock = threading.Lock()
thread_started = False

events: deque[dict] = deque(maxlen=200)
planning: list[dict] = []
pending_commands: deque[dict] = deque(maxlen=100)


# ============ OUTILS ============
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_event(level: str, message: str, **data) -> None:
    entry = {"ts": now_iso(), "level": level, "message": message, **data}
    events.appendleft(entry)
    if level in {"error", "warning"}:
        log.warning(message)
    else:
        log.info(message)


def token_from_request() -> str:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return (
        request.headers.get("X-API-Token", "")
        or request.args.get("token", "")
        or (request.get_json(silent=True) or {}).get("token", "")
    ).strip()


def requires_auth() -> bool:
    if not API_TOKEN:
        return False
    if request.method == "OPTIONS":
        return False
    if request.path in {"/api/ping"}:
        return False
    if request.path == "/api/g781/commands":
        return True
    return request.method not in {"GET", "HEAD"}


@app.before_request
def check_api_token():
    if requires_auth() and token_from_request() != API_TOKEN:
        return jsonify({"success": False, "error": "API token manquant ou invalide"}), 401
    return None


def parse_int(value, default=0) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, int):
        return value
    text = str(value).strip()
    return int(text, 16) if text.lower().startswith("0x") else int(text)


def client_is_open() -> bool:
    if client is None:
        return False
    connected = getattr(client, "connected", None)
    if isinstance(connected, bool):
        return connected
    is_open = getattr(client, "is_socket_open", None)
    if callable(is_open):
        try:
            return bool(is_open())
        except Exception:
            return False
    return False


def set_disconnected(reason: str | None = None) -> None:
    cache["connected"] = False
    if reason:
        add_event("warning", reason)


def connect(host: str | None = None, port: int | None = None) -> bool:
    global client, current_host, current_port

    host = (host if host is not None else current_host or "").strip()
    port = int(port if port is not None else current_port)
    current_host = host
    current_port = port

    if not host:
        cache["connected"] = False
        cache["g781_ip"] = "En attente..."
        return False

    try:
        if client:
            try:
                client.close()
            except Exception:
                pass
        client = ModbusTcpClient(host, port=port, timeout=5)
        ok = bool(client.connect())
        cache["connected"] = ok
        cache["g781_ip"] = f"{host}:{port}"
        if ok:
            add_event("info", f"Connexion Modbus OK: {host}:{port}")
        else:
            add_event("warning", f"Connexion Modbus échouée: {host}:{port}")
        return ok
    except Exception as exc:
        cache["connected"] = False
        add_event("error", f"Erreur connexion Modbus: {exc}")
        return False


def read_reg(addr: int, reg: int, count: int = 1):
    if not client_is_open():
        return None
    try:
        try:
            result = client.read_holding_registers(reg, count=count, slave=addr)
        except TypeError:
            result = client.read_holding_registers(reg, count=count, unit=addr)
        if result and not result.isError():
            return result.registers
    except Exception as exc:
        add_event("warning", f"Lecture Modbus impossible addr={addr} reg={hex(reg)}: {exc}")
    return None


def write_reg(addr: int, reg: int, value: int) -> bool:
    if not client_is_open():
        return False
    try:
        try:
            result = client.write_register(reg, value, slave=addr)
        except TypeError:
            result = client.write_register(reg, value, unit=addr)
        return bool(result and not result.isError())
    except Exception as exc:
        add_event("warning", f"Écriture Modbus impossible addr={addr} reg={hex(reg)} val={value}: {exc}")
        return False


def queue_command(command: dict) -> dict:
    item = {"id": int(time.time() * 1000), "ts": now_iso(), **command}
    pending_commands.append(item)
    add_event("info", "Commande ajoutée à la file HTTPD Client", command=item)
    return item


# ============ LECTURE APPAREILS ============
def decode_invt(code: int) -> str:
    return {
        22: "A-LS - Tension DC insuffisante",
        7: "UV - Sous-tension bus DC",
        1: "OC1 - Surintensité",
        9: "OL2 - Surcharge moteur",
    }.get(code, f"Erreur INVT {code}")


def decode_salmson(code: int) -> str:
    return {
        40: "E040 - Manque eau / marche à sec",
        80: "E080 - Surcharge moteur",
        82: "E082 - Protection thermique",
        90: "E090 - Défaut thermique",
    }.get(code, f"Erreur Salmson {code}")


def get_invt() -> dict:
    data = {}
    for name, reg in INVT_REGS.items():
        value = read_reg(ADDR_INVT, reg)
        if not value:
            continue
        raw = value[0]
        if name == "freq_hz":
            data[name] = round(raw / 100, 2)
        elif name == "current_a":
            data[name] = round(raw / 10, 1)
        elif name == "power_kw":
            data[name] = round(raw / 10, 2)
        else:
            data[name] = raw
    data["running"] = data.get("freq_hz", 0) > 0.5
    fault = data.get("fault_code", 0)
    data["error_text"] = decode_invt(fault) if fault else None
    data["status"] = "ERREUR" if fault else ("EN MARCHE" if data["running"] else "ARRÊTÉ")
    return data


def get_salmson() -> dict:
    data = {}
    for name, reg in SALMSON_REGS.items():
        value = read_reg(ADDR_SALMSON, reg)
        if not value:
            continue
        raw = value[0]
        data[name] = round(raw / 10, 1) if name == "current_a" else raw
    data["running"] = data.get("pump_state", 0) == 1
    error = data.get("error_code", 0)
    data["error_text"] = decode_salmson(error) if error else None
    data["status"] = "ERREUR" if error else ("EN MARCHE" if data["running"] else "ARRÊTÉE")
    return data


def get_wilo() -> dict:
    data = {}
    for name, reg in WILO_REGS.items():
        value = read_reg(ADDR_WILO, reg)
        if not value:
            continue
        raw = value[0]
        data[name] = round(raw / 100, 2) if name in {"pressure", "flow"} else raw
    data["running"] = data.get("pump1", 0) == 1 or data.get("pump2", 0) == 1
    error = data.get("error_code", 0)
    data["error_text"] = f"Erreur Wilo {error}" if error else None
    data["status"] = "ERREUR" if error else ("EN MARCHE" if data["running"] else "ARRÊTÉ")
    return data


def get_coffret4() -> dict:
    data = {}
    for name, reg in COFFRET4_REGS.items():
        value = read_reg(ADDR_COFFRET4, reg)
        if value:
            data[name] = value[0]
    error = data.get("error_code", 0)
    data["error_text"] = f"Erreur coffret/capteur 4 {error}" if error else None
    data["status"] = "ERREUR" if error else ("OK" if data else "NON CONFIGURÉ")
    return data


def refresh_modbus_cache() -> None:
    with lock:
        cache["invt"] = get_invt()
        cache["salmson"] = get_salmson()
        cache["wilo"] = get_wilo()
        cache["coffret4"] = get_coffret4()
        cache["connected"] = client_is_open()
        cache["last_update"] = time.time()


def update_loop() -> None:
    while True:
        try:
            if G781_MODE not in {"direct_tcp", "tcp", "modbus_tcp"}:
                time.sleep(UPDATE_SEC)
                continue

            if not current_host:
                cache["connected"] = False
                cache["g781_ip"] = "En attente G781_HOST"
                time.sleep(UPDATE_SEC)
                continue

            if not client_is_open():
                connect(current_host, current_port)
                time.sleep(UPDATE_SEC)
                continue

            refresh_modbus_cache()
            time.sleep(UPDATE_SEC)
        except Exception as exc:
            set_disconnected(f"Erreur boucle Modbus: {exc}")
            time.sleep(max(UPDATE_SEC, 10))


def start_background_thread() -> None:
    global thread_started
    if thread_started:
        return
    thread_started = True
    thread = threading.Thread(target=update_loop, daemon=True, name="modbus-update-loop")
    thread.start()


# ============ COMMANDES ============
def apply_control(device: str, action: str) -> tuple[bool, str | None]:
    device = device.lower()
    action = action.lower()

    if action in {"start", "forward"}:
        action = "on"
    elif action in {"stop"}:
        action = "off"

    if action not in {"on", "off"}:
        return False, f"Action invalide: {action}"

    if G781_MODE in {"http_push", "httpd_client", "push"}:
        queue_command({"type": "control", "device": device, "action": action})
        return True, None

    if not client_is_open():
        return False, "USR-G781 non connecté"

    value = 1 if action == "on" else 0
    invt_value = 1 if action == "on" else 5

    ok = True
    if device == "forage":
        ok = write_reg(ADDR_INVT, INVT_CMD, invt_value) and write_reg(ADDR_SALMSON, SALMSON_CMD, value)
    elif device == "salmson":
        ok = write_reg(ADDR_SALMSON, SALMSON_CMD, value)
    elif device == "invt":
        ok = write_reg(ADDR_INVT, INVT_CMD, invt_value)
    elif device == "wilo":
        ok = write_reg(ADDR_WILO, WILO_CMD, value)
    elif device == "all":
        ok = (
            write_reg(ADDR_INVT, INVT_CMD, invt_value)
            and write_reg(ADDR_SALMSON, SALMSON_CMD, value)
            and write_reg(ADDR_WILO, WILO_CMD, value)
        )
    elif device == "coffret4":
        return False, "Aucun registre de commande défini pour coffret4"
    else:
        return False, f"Appareil inconnu: {device}"

    if ok:
        add_event("info", f"Commande Modbus OK: {device} {action}")
        return True, None
    return False, "Commande Modbus échouée"


# ============ API ============
@app.route("/api/ping")
def ping():
    return jsonify(
        {
            "status": "ok",
            "version": APP_VERSION,
            "mode": G781_MODE,
            "connected": cache["connected"],
            "g781_ip": cache["g781_ip"],
            "auth_required": bool(API_TOKEN),
        }
    )


@app.route("/api/status")
def status():
    with lock:
        return jsonify(
            {
                "connected": cache["connected"],
                "last_update": cache["last_update"],
                "g781_ip": cache["g781_ip"],
                "mode": cache["mode"],
                "invt": cache["invt"],
                "salmson": cache["salmson"],
                "wilo": cache["wilo"],
                "coffret4": cache["coffret4"],
            }
        )


@app.route("/api/devices")
def devices():
    return jsonify(
        {
            "devices": [
                {"id": "invt", "name": "INVT GD100-PV", "addr": ADDR_INVT, "status": cache["invt"]},
                {"id": "salmson", "name": "Salmson forage", "addr": ADDR_SALMSON, "status": cache["salmson"]},
                {"id": "wilo", "name": "Wilo surpresseur", "addr": ADDR_WILO, "status": cache["wilo"]},
                {"id": "coffret4", "name": "Coffret/capteur 4", "addr": ADDR_COFFRET4, "status": cache["coffret4"]},
            ]
        }
    )


@app.route("/api/connect", methods=["POST"])
def api_connect():
    body = request.get_json(silent=True) or {}
    host = str(body.get("host") or body.get("ip") or "").strip()
    port = parse_int(body.get("port"), G781_PORT)
    if not host:
        return jsonify({"success": False, "error": "IP/host G781 manquant"}), 400
    ok = connect(host, port)
    return jsonify({"success": ok, "host": host, "ip": host, "port": port})


@app.route("/api/control", methods=["POST"])
def control():
    body = request.get_json(silent=True) or {}
    device = str(body.get("device") or body.get("pump") or "").strip().lower()
    action = str(body.get("action") or "").strip().lower()
    if not device or not action:
        return jsonify({"success": False, "error": "device/pump et action requis"}), 400
    ok, error = apply_control(device, action)
    status_code = 200 if ok else (503 if error == "USR-G781 non connecté" else 400)
    return jsonify({"success": ok, "device": device, "pump": device, "action": action, "error": error}), status_code


@app.route("/api/inverter", methods=["POST"])
def inverter():
    body = request.get_json(silent=True) or {}
    action = str(body.get("action") or "stop").strip().lower()
    if action not in INVT_ACTIONS:
        return jsonify({"success": False, "error": f"Action INVT inconnue: {action}"}), 400
    if G781_MODE in {"http_push", "httpd_client", "push"}:
        item = queue_command({"type": "inverter", "device": "invt", "action": action})
        return jsonify({"success": True, "queued": True, "command": item})
    if not client_is_open():
        return jsonify({"success": False, "error": "USR-G781 non connecté"}), 503
    ok = write_reg(ADDR_INVT, INVT_CMD, INVT_ACTIONS[action])
    if ok:
        add_event("info", f"Commande INVT OK: {action}")
    return jsonify({"success": ok, "device": "invt", "action": action}), 200 if ok else 400


@app.route("/api/param/read", methods=["POST"])
def param_read():
    body = request.get_json(silent=True) or {}
    addr = parse_int(body.get("addr"), 1)
    reg = parse_int(body.get("reg"), 0)
    count = parse_int(body.get("count"), 1)
    if not client_is_open():
        return jsonify({"success": False, "error": "USR-G781 non connecté"}), 503
    with lock:
        values = read_reg(addr, reg, count)
    if values:
        return jsonify({"success": True, "value": values[0], "values": values, "reg": hex(reg), "addr": addr})
    return jsonify({"success": False, "error": "Registre illisible", "reg": hex(reg), "addr": addr}), 400


@app.route("/api/param/write", methods=["POST"])
def param_write():
    body = request.get_json(silent=True) or {}
    addr = parse_int(body.get("addr"), 1)
    reg = parse_int(body.get("reg"), 0)
    value = parse_int(body.get("value"), 0)
    if not client_is_open():
        return jsonify({"success": False, "error": "USR-G781 non connecté"}), 503
    with lock:
        ok = write_reg(addr, reg, value)
    if ok:
        add_event("info", f"Écriture registre OK addr={addr} reg={hex(reg)} val={value}")
        return jsonify({"success": True, "reg": hex(reg), "addr": addr, "value": value})
    return jsonify({"success": False, "error": "Écriture échouée", "reg": hex(reg), "addr": addr}), 400


@app.route("/api/planning", methods=["GET", "POST"])
def api_planning():
    global planning
    if request.method == "GET":
        return jsonify({"planning": planning})

    body = request.get_json(silent=True) or {}
    items = body.get("planning")
    if not isinstance(items, list):
        return jsonify({"success": False, "error": "planning doit être une liste"}), 400

    normalized = []
    for item in items:
        if not isinstance(item, dict):
            return jsonify({"success": False, "error": "Chaque entrée planning doit être un objet"}), 400
        device = str(item.get("device") or item.get("pump") or "").lower()
        action = str(item.get("action") or "").lower()
        time_text = str(item.get("time") or "")
        days = item.get("days", [])
        if device not in {"invt", "salmson", "wilo", "coffret4", "forage", "all"}:
            return jsonify({"success": False, "error": f"Appareil planning invalide: {device}"}), 400
        if action not in {"on", "off", "start", "stop"}:
            return jsonify({"success": False, "error": f"Action planning invalide: {action}"}), 400
        if len(time_text) != 5 or time_text[2] != ":":
            return jsonify({"success": False, "error": f"Heure invalide: {time_text}"}), 400
        normalized.append(
            {
                "device": device,
                "action": "on" if action == "start" else "off" if action == "stop" else action,
                "time": time_text,
                "days": [parse_int(day) for day in days],
                "enabled": bool(item.get("enabled", True)),
            }
        )

    planning = normalized
    add_event("info", f"Planning sauvegardé: {len(planning)} entrée(s)")
    return jsonify({"success": True, "planning": planning})


@app.route("/api/events")
def api_events():
    limit = min(parse_int(request.args.get("limit"), 50), 200)
    return jsonify({"events": list(events)[:limit]})


@app.route("/api/g781/push", methods=["POST"])
def g781_push():
    body = request.get_json(silent=True) or {}
    with lock:
        for key in ("invt", "salmson", "wilo", "coffret4"):
            if isinstance(body.get(key), dict):
                cache[key].update(body[key])
        cache["connected"] = True
        cache["g781_ip"] = request.headers.get("X-Forwarded-For", request.remote_addr or "G781 HTTPD Client")
        cache["last_update"] = time.time()
    add_event("info", "Push G781 reçu")
    return jsonify({"success": True, "commands_pending": len(pending_commands)})


@app.route("/api/g781/commands")
def g781_commands():
    commands = list(pending_commands)
    pending_commands.clear()
    return jsonify({"commands": commands})


# ============ RAIN BIRD API ============
RAINBIRD_STICK_ID = os.environ.get("RAINBIRD_STICK_ID", "")
RAINBIRD_SERIAL = os.environ.get("RAINBIRD_SERIAL", "")
RAINBIRD_KEYCODE = os.environ.get("RAINBIRD_KEYCODE", "")
RAINBIRD_WIFI = os.environ.get("RAINBIRD_WIFI", "")
RAINBIRD_ZONES = [parse_int(z) for z in os.environ.get("RAINBIRD_ZONES", "1,2,3,4,5,6").split(",") if z.strip()]
RAINBIRD_ENABLED = [parse_int(z) for z in os.environ.get("RAINBIRD_ENABLED", "1,3").split(",") if z.strip()]
RAINBIRD_PROGRAMS = {"1": "Arrosage", "2": "Fertilisation"}

rainbird_ip = os.environ.get("RAINBIRD_IP", "")
rainbird_state = {"connected": False, "active_zones": [], "ip": rainbird_ip, "last_cmd": ""}


def rainbird_request(ip: str, command: str, params: dict | None = None):
    if not RAINBIRD_STICK_ID or not RAINBIRD_KEYCODE:
        add_event("warning", "Rain Bird non configuré: RAINBIRD_STICK_ID/RAINBIRD_KEYCODE manquants")
        return None

    import json as jsonlib
    import urllib.request

    try:
        payload = {"id": RAINBIRD_STICK_ID, "command": command}
        if params:
            payload.update(params)
        req = urllib.request.Request(
            f"http://{ip}/stick",
            data=jsonlib.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Basic {RAINBIRD_KEYCODE[:32]}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return jsonlib.loads(resp.read())
    except Exception as exc:
        add_event("warning", f"Rain Bird erreur: {exc}")
        return None


@app.route("/api/rainbird/config")
def rainbird_config():
    return jsonify(
        {
            "configured": bool(RAINBIRD_STICK_ID and RAINBIRD_KEYCODE),
            "serial": RAINBIRD_SERIAL,
            "wifi": RAINBIRD_WIFI,
            "zones": RAINBIRD_ZONES,
            "enabled": RAINBIRD_ENABLED,
            "programs": RAINBIRD_PROGRAMS,
            "ip": rainbird_ip,
            "connected": rainbird_state["connected"],
            "active": rainbird_state["active_zones"],
        }
    )


@app.route("/api/rainbird/setip", methods=["POST"])
def rainbird_setip():
    global rainbird_ip
    body = request.get_json(silent=True) or {}
    ip = str(body.get("ip") or "").strip()
    if not ip:
        return jsonify({"success": False, "error": "IP manquante"}), 400
    rainbird_ip = ip
    rainbird_state["ip"] = ip
    add_event("info", f"Rain Bird IP définie: {ip}")
    return jsonify({"success": True, "ip": ip})


@app.route("/api/rainbird/start", methods=["POST"])
def rainbird_start():
    body = request.get_json(silent=True) or {}
    zone = parse_int(body.get("zone"), 1)
    duration = parse_int(body.get("duration"), 10)
    if zone not in RAINBIRD_ZONES:
        return jsonify({"success": False, "error": f"Zone {zone} invalide"}), 400

    rainbird_state["last_cmd"] = f"START Zone {zone} {duration}min"
    if not rainbird_ip:
        rainbird_state["active_zones"] = [zone]
        return jsonify({"success": True, "zone": zone, "duration": duration, "mode": "simulation"})

    result = rainbird_request(rainbird_ip, "ZoneStartRequest", {"zone": zone, "duration": duration})
    if result:
        rainbird_state["active_zones"] = [zone]
        rainbird_state["connected"] = True
        return jsonify({"success": True, "zone": zone, "duration": duration, "result": result})
    return jsonify({"success": False, "error": "Pas de réponse Rain Bird"}), 503


@app.route("/api/rainbird/stop", methods=["POST"])
def rainbird_stop():
    body = request.get_json(silent=True) or {}
    zone = body.get("zone")
    rainbird_state["last_cmd"] = f"STOP {'zone ' + str(zone) if zone else 'tout'}"
    rainbird_state["active_zones"] = []

    if not rainbird_ip:
        return jsonify({"success": True, "mode": "simulation"})

    result = rainbird_request(rainbird_ip, "StopIrrigationRequest")
    if result:
        rainbird_state["connected"] = True
        return jsonify({"success": True, "result": result})
    return jsonify({"success": False, "error": "Pas de réponse Rain Bird"}), 503


@app.route("/api/rainbird/status")
def rainbird_status():
    if not rainbird_ip:
        return jsonify(
            {
                "connected": False,
                "active_zones": rainbird_state["active_zones"],
                "last_cmd": rainbird_state["last_cmd"],
                "mode": "simulation",
            }
        )

    result = rainbird_request(rainbird_ip, "CurrentIrrigationStateRequest")
    if result:
        rainbird_state["connected"] = True
        return jsonify(
            {
                "connected": True,
                "active_zones": rainbird_state["active_zones"],
                "last_cmd": rainbird_state["last_cmd"],
                "raw": result,
            }
        )
    return jsonify({"connected": False, "active_zones": rainbird_state["active_zones"], "error": "Pas de réponse"})


@app.route("/")
def index():
    app_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.exists(os.path.join(app_dir, "index.html")):
        return send_from_directory(app_dir, "index.html")
    return f"""
    <h2>Zarzis Irrigation - Serveur Cloud</h2>
    <p>Version: <code>{APP_VERSION}</code></p>
    <p>API: <code>/api/ping</code>, <code>/api/status</code>, <code>/api/devices</code></p>
    <p>Mode G781: <strong>{G781_MODE}</strong></p>
    <p>Connecté: <strong>{"OUI" if cache["connected"] else "NON / en attente"}</strong></p>
    """


@app.route("/manifest.webmanifest")
def manifest():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "manifest.webmanifest")


@app.route("/sw.js")
def service_worker():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "sw.js")


@app.route("/icon.svg")
def icon():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "icon.svg")


start_background_thread()


if __name__ == "__main__":
    log.info("=" * 50)
    log.info("  ZARZIS CLOUD SERVER - MODBUS PROXY")
    log.info("=" * 50)
    app.run(host="0.0.0.0", port=SERVER_PORT, debug=False)
