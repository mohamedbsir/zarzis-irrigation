#!/usr/bin/env python3
"""
ZARZIS IRRIGATION — API CLOUD V4
Objectif : dashboard = démarrer / arrêter / planifier / visualiser.
Les sécurités restent dans les coffrets d'origine.

Compatible Render Web Service.
Modes:
- direct_tcp : l'API se connecte à une passerelle Modbus TCP joignable.
- http_bridge : le terrain pousse l'état vers /api/g781/push et récupère les commandes.
"""

import os
import time
import logging
import threading
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from flask import Flask, jsonify, request
from flask_cors import CORS
from pymodbus.client import ModbusTcpClient

APP_VERSION = "zarzis-cloud-v4.0"
PORT = int(os.environ.get("PORT", "8080"))

# Si API_TOKEN est vide, l'API reste ouverte.
# Pour sécuriser plus tard : définir API_TOKEN dans Render et le renseigner dans le dashboard.
API_TOKEN = os.environ.get("API_TOKEN", "").strip()

G781_MODE = os.environ.get("G781_MODE", "direct_tcp").strip()  # direct_tcp | http_bridge
G781_HOST = os.environ.get("G781_HOST", "").strip()
G781_PORT = int(os.environ.get("G781_PORT", "502"))
MODBUS_TIMEOUT = float(os.environ.get("MODBUS_TIMEOUT", "5"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "5"))

ADDR_INVT = int(os.environ.get("ADDR_INVT", "1"))
ADDR_SALMSON = int(os.environ.get("ADDR_SALMSON", "2"))
ADDR_WILO = int(os.environ.get("ADDR_WILO", "3"))
ADDR_COFFRET4 = int(os.environ.get("ADDR_COFFRET4", "4"))

DEVICES: Dict[str, Dict[str, Any]] = {
    "invt": {
        "label": "Variateur INVT solaire",
        "slave": ADDR_INVT,
        "start_reg": int(os.environ.get("INVT_START_REG", str(0x2000))),
        "start_value": int(os.environ.get("INVT_START_VALUE", "1")),
        "stop_value": int(os.environ.get("INVT_STOP_VALUE", "5")),
        "read_regs": {
            "freq_hz": (0x1000, 0.01),
            "current_a": (0x1001, 0.1),
            "voltage_v": (0x1002, 1),
            "dc_bus_v": (0x1003, 1),
            "power_kw": (0x1004, 0.1),
            "fault_code": (0x8000, 1),
        },
    },
    "salmson": {
        "label": "Coffret Salmson forage",
        "slave": ADDR_SALMSON,
        "start_reg": int(os.environ.get("SALMSON_START_REG", str(0x0100))),
        "start_value": int(os.environ.get("SALMSON_START_VALUE", "1")),
        "stop_value": int(os.environ.get("SALMSON_STOP_VALUE", "0")),
        "read_regs": {
            "state": (0x0001, 1),
            "current_a": (0x0010, 0.1),
            "fault_code": (0x0020, 1),
            "float_low": (0x0030, 1),
            "float_high": (0x0031, 1),
        },
    },
    "wilo": {
        "label": "Coffret Wilo surpresseur",
        "slave": ADDR_WILO,
        "start_reg": int(os.environ.get("WILO_START_REG", str(0x0100))),
        "start_value": int(os.environ.get("WILO_START_VALUE", "1")),
        "stop_value": int(os.environ.get("WILO_STOP_VALUE", "0")),
        "read_regs": {
            "pressure": (0x0001, 0.01),
            "flow": (0x0002, 0.01),
            "pump1": (0x0010, 1),
            "pump2": (0x0011, 1),
            "fault_code": (0x0020, 1),
        },
    },
    "coffret4": {
        "label": "Coffret 4 / niveau",
        "slave": ADDR_COFFRET4,
        "start_reg": int(os.environ.get("COFFRET4_START_REG", str(0x0100))),
        "start_value": int(os.environ.get("COFFRET4_START_VALUE", "1")),
        "stop_value": int(os.environ.get("COFFRET4_STOP_VALUE", "0")),
        "read_regs": {
            "state": (0x0001, 1),
            "level_percent": (0x0002, 1),
            "fault_code": (0x0020, 1),
        },
    },
}

ALIASES = {
    "forage": "salmson",
    "salmson": "salmson",
    "wilo": "wilo",
    "surpresseur": "wilo",
    "invt": "invt",
    "coffret4": "coffret4",
    "niveau": "coffret4",
}

app = Flask(__name__)
CORS(app, origins="*")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("zarzis")

lock = threading.Lock()
client: Optional[ModbusTcpClient] = None

state: Dict[str, Any] = {
    "version": APP_VERSION,
    "mode": G781_MODE,
    "connected": False,
    "g781_ip": G781_HOST or "En attente...",
    "g781_port": G781_PORT,
    "last_update": None,
    "last_push": None,
    "invt": {"status": "DÉCONNECTÉ"},
    "salmson": {"status": "DÉCONNECTÉ"},
    "wilo": {"status": "DÉCONNECTÉ"},
    "coffret4": {"status": "DÉCONNECTÉ"},
    "events": [],
    "pending_commands": [],
    "planning": [],
    "rainbird": {"connected": False, "ip": None, "active_zones": []},
}

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def add_event(level: str, msg: str, **extra) -> None:
    item = {"ts": now_iso(), "level": level, "msg": msg}
    item.update(extra)
    with lock:
        state["events"].insert(0, item)
        state["events"] = state["events"][:200]
    log.info("%s — %s", level, msg)

def authorized() -> bool:
    if not API_TOKEN:
        return True
    token = request.headers.get("X-API-Token") or request.args.get("token") or ""
    return token == API_TOKEN

def auth_error():
    if not authorized():
        return jsonify({"success": False, "error": "Token API invalide"}), 401
    return None

def normalize_device(name: str) -> Optional[str]:
    return ALIASES.get((name or "").lower().strip())

def is_client_open() -> bool:
    global client
    try:
        return bool(client and client.is_socket_open())
    except Exception:
        return False

def connect_modbus(host: str, port: int = 502) -> bool:
    global client
    try:
        if client:
            try:
                client.close()
            except Exception:
                pass
        client = ModbusTcpClient(host=host, port=port, timeout=MODBUS_TIMEOUT)
        ok = client.connect()
        with lock:
            state["connected"] = bool(ok)
            state["g781_ip"] = host if ok else (host or "En attente...")
            state["g781_port"] = port
        add_event("ok" if ok else "warn", f"Connexion Modbus {host}:{port} = {ok}")
        return bool(ok)
    except Exception as exc:
        with lock:
            state["connected"] = False
        add_event("err", f"Erreur connexion Modbus: {exc}")
        return False

def read_holding(slave: int, reg: int, count: int = 1) -> Optional[list]:
    if not is_client_open():
        return None
    try:
        try:
            res = client.read_holding_registers(reg, count=count, slave=slave)
        except TypeError:
            res = client.read_holding_registers(reg, count=count, unit=slave)
        if res is None or getattr(res, "isError", lambda: True)():
            return None
        return list(res.registers)
    except Exception as exc:
        add_event("err", f"Lecture Modbus impossible slave={slave} reg={reg}: {exc}")
        return None

def write_single(slave: int, reg: int, value: int) -> bool:
    if not is_client_open():
        return False
    try:
        try:
            res = client.write_register(reg, int(value), slave=slave)
        except TypeError:
            res = client.write_register(reg, int(value), unit=slave)
        return bool(res is not None and not getattr(res, "isError", lambda: True)())
    except Exception as exc:
        add_event("err", f"Écriture Modbus impossible slave={slave} reg={reg}: {exc}")
        return False

def decode_fault(device: str, code: int) -> Optional[str]:
    if not code:
        return None
    return f"Défaut {device.upper()} code {code}"

def poll_device(device: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    data = {
        "label": cfg["label"],
        "slave": cfg["slave"],
        "status": "CONNECTÉ" if state["connected"] else "DÉCONNECTÉ",
    }
    for key, (reg, factor) in cfg["read_regs"].items():
        regs = read_holding(cfg["slave"], int(reg), 1)
        if regs is None:
            continue
        raw = regs[0]
        data[key] = raw * factor
    if "fault_code" in data:
        data["error_text"] = decode_fault(device, int(data["fault_code"]))
    if device in ("salmson", "wilo", "coffret4"):
        data["running"] = bool(data.get("state") or data.get("pump1") or data.get("pump2"))
    return data

def poll_all_once() -> None:
    if G781_MODE != "direct_tcp":
        return
    if not is_client_open():
        if G781_HOST:
            connect_modbus(G781_HOST, G781_PORT)
        return
    snapshot = {}
    for dev, cfg in DEVICES.items():
        snapshot[dev] = poll_device(dev, cfg)
    with lock:
        for k, v in snapshot.items():
            state[k].update(v)
        state["connected"] = is_client_open()
        state["last_update"] = now_iso()

def poll_loop() -> None:
    while True:
        try:
            poll_all_once()
        except Exception as exc:
            add_event("err", f"Erreur boucle polling: {exc}")
        time.sleep(max(2, POLL_SECONDS))

def queue_command(device: str, action: str) -> None:
    with lock:
        state["pending_commands"].append({
            "id": int(time.time() * 1000),
            "ts": now_iso(),
            "device": device,
            "action": action,
        })
        state["pending_commands"] = state["pending_commands"][-100:]

@app.route("/")
def index():
    return jsonify({
        "name": "Zarzis Irrigation API",
        "version": APP_VERSION,
        "status": "ok",
        "connected": state["connected"],
        "dashboard": "Utiliser index.html / GitHub Pages",
    })

@app.route("/api/ping")
def api_ping():
    return jsonify({
        "status": "ok",
        "version": APP_VERSION,
        "mode": G781_MODE,
        "connected": state["connected"],
        "g781_ip": state["g781_ip"],
        "last_update": state["last_update"],
    })

@app.route("/api/status")
def api_status():
    with lock:
        return jsonify(dict(state))

@app.route("/api/devices")
def api_devices():
    return jsonify({
        "success": True,
        "devices": {
            k: {"label": v["label"], "slave": v["slave"]}
            for k, v in DEVICES.items()
        }
    })

@app.route("/api/connect", methods=["POST"])
def api_connect():
    err = auth_error()
    if err: return err
    body = request.get_json(silent=True) or {}
    host = body.get("ip") or body.get("host") or G781_HOST
    port = int(body.get("port") or G781_PORT)
    if not host:
        return jsonify({"success": False, "error": "IP/host G781 manquant"})
    ok = connect_modbus(host, port)
    return jsonify({"success": ok, "connected": ok, "host": host, "port": port})

@app.route("/api/control", methods=["POST"])
def api_control():
    err = auth_error()
    if err: return err
    body = request.get_json(silent=True) or {}
    action = (body.get("action") or "").lower().strip()
    requested = body.get("device") or body.get("pump")
    if requested == "all":
        targets = ["salmson", "wilo", "invt", "coffret4"]
    else:
        dev = normalize_device(requested)
        if not dev:
            return jsonify({"success": False, "error": f"Appareil inconnu: {requested}"})
        targets = [dev]
    if action not in ("on", "off", "start", "stop"):
        return jsonify({"success": False, "error": "Action invalide. Utiliser on/off."})
    action_norm = "on" if action in ("on", "start") else "off"

    results = {}
    if G781_MODE == "http_bridge":
        for dev in targets:
            queue_command(dev, action_norm)
            results[dev] = "queued"
        add_event("cmd", f"Commande mise en file: {targets} {action_norm}")
        return jsonify({"success": True, "mode": "http_bridge", "results": results})

    if not is_client_open():
        return jsonify({"success": False, "error": "Modbus non connecté"})

    for dev in targets:
        cfg = DEVICES[dev]
        value = cfg["start_value"] if action_norm == "on" else cfg["stop_value"]
        ok = write_single(cfg["slave"], cfg["start_reg"], value)
        results[dev] = ok
        add_event("cmd" if ok else "err", f"Commande {dev} {action_norm} = {ok}")
    return jsonify({"success": all(results.values()), "results": results})

@app.route("/api/param/read", methods=["POST"])
def api_param_read():
    err = auth_error()
    if err: return err
    body = request.get_json(silent=True) or {}
    dev = normalize_device(body.get("device"))
    slave = int(body.get("addr") or (DEVICES.get(dev, {}).get("slave") if dev else 1))
    reg = int(body.get("reg", 0))
    regs = read_holding(slave, reg, 1)
    if regs is None:
        return jsonify({"success": False, "error": "Lecture impossible ou Modbus non connecté"})
    return jsonify({"success": True, "device": dev, "addr": slave, "reg": reg, "value": regs[0]})

@app.route("/api/param/write", methods=["POST"])
def api_param_write():
    err = auth_error()
    if err: return err
    body = request.get_json(silent=True) or {}
    dev = normalize_device(body.get("device"))
    slave = int(body.get("addr") or (DEVICES.get(dev, {}).get("slave") if dev else 1))
    reg = int(body.get("reg", 0))
    value = int(body.get("value", 0))
    ok = write_single(slave, reg, value)
    add_event("cmd" if ok else "err", f"Param write slave={slave} reg={reg} value={value} ok={ok}")
    return jsonify({"success": ok, "device": dev, "addr": slave, "reg": reg, "value": value})

@app.route("/api/planning", methods=["GET", "POST"])
def api_planning():
    err = auth_error() if request.method == "POST" else None
    if err: return err
    if request.method == "GET":
        return jsonify({"success": True, "planning": state["planning"]})
    body = request.get_json(silent=True) or {}
    planning = body.get("planning", [])
    if not isinstance(planning, list):
        return jsonify({"success": False, "error": "planning doit être une liste"})
    with lock:
        state["planning"] = planning
    add_event("ok", f"Planning mis à jour ({len(planning)} lignes)")
    return jsonify({"success": True, "planning": planning})

@app.route("/api/events")
def api_events():
    with lock:
        return jsonify({"success": True, "events": state["events"]})

# Mode HTTPD Client / bridge : le terrain pousse son état et vient chercher les commandes.
@app.route("/api/g781/push", methods=["POST"])
def api_g781_push():
    err = auth_error()
    if err: return err
    body = request.get_json(silent=True) or {}
    with lock:
        for dev in ("invt", "salmson", "wilo", "coffret4"):
            if isinstance(body.get(dev), dict):
                state[dev].update(body[dev])
        state["connected"] = True
        state["last_push"] = now_iso()
        state["last_update"] = state["last_push"]
        state["g781_ip"] = request.remote_addr or state["g781_ip"]
    return jsonify({"success": True, "status": "received"})

@app.route("/api/g781/commands", methods=["GET", "POST"])
def api_g781_commands():
    err = auth_error()
    if err: return err
    with lock:
        cmds = list(state["pending_commands"])
        if request.method == "POST" or request.args.get("clear") == "1":
            state["pending_commands"] = []
    return jsonify({"success": True, "commands": cmds})

# Endpoints Rain Bird conservés pour ne pas casser le dashboard actuel.
@app.route("/api/rainbird/setip", methods=["POST"])
def api_rainbird_setip():
    body = request.get_json(silent=True) or {}
    with lock:
        state["rainbird"]["ip"] = body.get("ip")
        state["rainbird"]["connected"] = bool(body.get("ip"))
    return jsonify({"success": True, "connected": state["rainbird"]["connected"], "ip": state["rainbird"]["ip"]})

@app.route("/api/rainbird/status")
def api_rainbird_status():
    return jsonify(state["rainbird"])

@app.route("/api/rainbird/start", methods=["POST"])
def api_rainbird_start():
    body = request.get_json(silent=True) or {}
    zone = body.get("zone")
    with lock:
        if zone and zone not in state["rainbird"]["active_zones"]:
            state["rainbird"]["active_zones"].append(zone)
    return jsonify({"success": True, "zone": zone})

@app.route("/api/rainbird/stop", methods=["POST"])
def api_rainbird_stop():
    body = request.get_json(silent=True) or {}
    zone = body.get("zone")
    with lock:
        if zone:
            state["rainbird"]["active_zones"] = [z for z in state["rainbird"]["active_zones"] if z != zone]
        else:
            state["rainbird"]["active_zones"] = []
    return jsonify({"success": True})

if __name__ == "__main__":
    add_event("ok", f"Démarrage {APP_VERSION}")
    threading.Thread(target=poll_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, debug=False)
else:
    add_event("ok", f"Démarrage {APP_VERSION} via gunicorn")
    threading.Thread(target=poll_loop, daemon=True).start()
