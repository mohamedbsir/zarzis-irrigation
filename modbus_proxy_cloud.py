#!/usr/bin/env python3
"""
Proxy cloud Zarzis Irrigation.

Le serveur expose l'API HTTP du dashboard et, en mode direct_tcp, interroge la
passerelle USR-G781-E/DR302 en Modbus TCP.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from pymodbus.client import ModbusTcpClient


# ============ CONFIGURATION ============
APP_VERSION = "2026.05.04-zarzis-ready-v5"
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(APP_DIR)))
PLANNING_FILE = Path(os.environ.get("PLANNING_FILE", str(DATA_DIR / "planning_zarzis.json")))
APP_STATE_FILE = Path(os.environ.get("APP_STATE_FILE", str(DATA_DIR / "app_state_zarzis.json")))

G781_MODE = os.environ.get("G781_MODE", "direct_tcp").strip().lower()
G781_HOST = os.environ.get("G781_HOST") or os.environ.get("USR_G781_IP", "")
G781_PORT = int(os.environ.get("G781_PORT") or os.environ.get("USR_G781_PORT", "502"))
SERVER_PORT = int(os.environ.get("PORT", "8080"))
UPDATE_SEC = max(2, int(os.environ.get("UPDATE_SEC", "5")))
PLANNING_POLL_SEC = max(5, int(os.environ.get("PLANNING_POLL_SEC", "10")))
API_TOKEN = os.environ.get("API_TOKEN", "").strip()
LOCAL_TZ_NAME = os.environ.get("LOCAL_TZ", "Africa/Tunis").strip() or "Africa/Tunis"
try:
    LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)
except Exception:
    LOCAL_TZ = timezone.utc
    LOCAL_TZ_NAME = "UTC"

SERVER_SAFETY_ENABLED = os.environ.get("SERVER_SAFETY_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
ALLOW_PARAM_WRITE = os.environ.get("ALLOW_PARAM_WRITE", "true").strip().lower() not in {"0", "false", "no", "off"}

ADDR_INVT = int(os.environ.get("ADDR_INVT", "1"))
ADDR_SALMSON = int(os.environ.get("ADDR_SALMSON", "2"))
ADDR_WILO = int(os.environ.get("ADDR_WILO", "3"))
ADDR_COFFRET4 = int(os.environ.get("ADDR_COFFRET4", "4"))

TCP_MODES = {"direct_tcp", "tcp", "modbus_tcp"}
HTTP_PUSH_MODES = {"http_push", "httpd_client", "push"}
SIMULATION_MODES = {"simulation", "demo", "offline"}
VALID_DEVICES = {"invt", "salmson", "wilo", "coffret4", "forage", "all"}
VALID_ACTIONS = {"on", "off", "start", "stop", "forward", "reverse"}
SYNC_STATE_KEYS = {
    "zoneConfig",
    "rbPrograms",
    "irrigPrograms",
    "fertConfig",
    "fertHistory",
    "runHours",
    "systemMode",
    "zarzis_exploitation_config",
    "zarzis_exploitation_zones",
    "zarzis_drip_lines",
    "zarzis_location",
    "zarzis_equipment_config",
    "zarzis_reservoirs_config",
}


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


def unavailable_state(status: str = "DÉCONNECTÉ") -> dict:
    return {"status": status, "running": False, "error_text": "Aucune lecture Modbus valide"}


cache = {
    "invt": unavailable_state(),
    "salmson": unavailable_state(),
    "wilo": unavailable_state(),
    "coffret4": {"status": "DÉCONNECTÉ", "error_text": "Aucune lecture Modbus valide"},
    "connected": False,
    "last_update": 0,
    "g781_ip": G781_HOST or "En attente...",
    "mode": G781_MODE,
}

client: ModbusTcpClient | None = None
current_host = G781_HOST
current_port = G781_PORT
lock = threading.RLock()
thread_started = False
scheduler_started = False

events: deque[dict] = deque(maxlen=300)
pending_commands: deque[dict] = deque(maxlen=200)
planning: list[dict] = []
app_state: dict[str, str] = {}
app_state_revision = 0
app_state_updated_at = ""
last_event_at: dict[str, float] = {}
last_plan_runs: dict[str, float] = {}
running_plan_ids: set[str] = set()


# ============ RAIN BIRD CONFIG ============
RAINBIRD_STICK_ID = os.environ.get("RAINBIRD_STICK_ID", "")
RAINBIRD_SERIAL = os.environ.get("RAINBIRD_SERIAL", "")
RAINBIRD_KEYCODE = os.environ.get("RAINBIRD_KEYCODE", "")
RAINBIRD_WIFI = os.environ.get("RAINBIRD_WIFI", "")
RAINBIRD_ZONES = []
RAINBIRD_ENABLED = []
RAINBIRD_PROGRAMS = {"1": "Arrosage", "2": "Fertilisation"}
rainbird_ip = os.environ.get("RAINBIRD_IP", "")
rainbird_state = {"connected": False, "active_zones": [], "ip": rainbird_ip, "last_cmd": ""}


# ============ OUTILS ============
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def local_now() -> datetime:
    return datetime.now(LOCAL_TZ)


def add_event(level: str, message: str, **data) -> None:
    signature = f"{level}:{message}"
    current = time.time()
    if current - last_event_at.get(signature, 0) < 30:
        return
    last_event_at[signature] = current
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
    if not request.path.startswith("/api/"):
        return False
    return request.path not in {"/api/ping"}


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
    return int(text, 16) if text.lower().startswith("0x") else int(float(text))


def parse_int_list(value, default=None) -> list[int]:
    if value is None:
        return list(default or [])
    if isinstance(value, list):
        items = value
    else:
        items = str(value).split(",")
    parsed = []
    for item in items:
        if str(item).strip() == "":
            continue
        parsed.append(parse_int(item))
    return parsed


RAINBIRD_ZONES = parse_int_list(os.environ.get("RAINBIRD_ZONES", "1,2,3,4,5,6"))
RAINBIRD_ENABLED = parse_int_list(os.environ.get("RAINBIRD_ENABLED", "1,3"))


def normalize_action(action: str) -> str:
    action = str(action or "").strip().lower()
    if action == "start":
        return "on"
    if action == "stop":
        return "off"
    return action


def valid_time_text(value: str) -> bool:
    if not re.match(r"^\d{2}:\d{2}$", value or ""):
        return False
    hour, minute = map(int, value.split(":"))
    return 0 <= hour <= 23 and 0 <= minute <= 59


def add_minutes(time_text: str, minutes: int) -> str:
    hour, minute = map(int, time_text.split(":"))
    total = (hour * 60 + minute + int(minutes)) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def client_is_open() -> bool:
    if G781_MODE in SIMULATION_MODES:
        return True
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
    with lock:
        cache["connected"] = False
        for key in ("invt", "salmson", "wilo"):
            cache[key] = unavailable_state()
        cache["coffret4"] = {"status": "DÉCONNECTÉ", "error_text": "Aucune lecture Modbus valide"}
    if reason:
        add_event("warning", reason)


def connect(host: str | None = None, port: int | None = None) -> bool:
    global client, current_host, current_port

    host = (host if host is not None else current_host or "").strip()
    port = int(port if port is not None else current_port)
    current_host = host
    current_port = port

    with lock:
        if G781_MODE in SIMULATION_MODES:
            cache["connected"] = True
            cache["g781_ip"] = f"SIMULATION {host or 'locale'}"
            add_event("info", "Mode simulation backend actif")
            return True

        if not host:
            cache["connected"] = False
            cache["g781_ip"] = "En attente G781_HOST"
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
    if G781_MODE in SIMULATION_MODES:
        return [0] * count
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
    if G781_MODE in SIMULATION_MODES:
        add_event("info", f"[SIMULATION] Écriture addr={addr} reg={hex(reg)} val={value}")
        return True
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
        if value is None:
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
    if not data:
        return unavailable_state()
    data["running"] = data.get("freq_hz", 0) > 0.5
    fault = data.get("fault_code", 0)
    data["error_text"] = decode_invt(fault) if fault else None
    data["status"] = "ERREUR" if fault else ("EN MARCHE" if data["running"] else "ARRÊTÉ")
    data["last_seen"] = now_iso()
    return data


def get_salmson() -> dict:
    data = {}
    for name, reg in SALMSON_REGS.items():
        value = read_reg(ADDR_SALMSON, reg)
        if value is None:
            continue
        raw = value[0]
        data[name] = round(raw / 10, 1) if name == "current_a" else raw
    if not data:
        return unavailable_state()
    data["running"] = data.get("pump_state", 0) == 1
    error = data.get("error_code", 0)
    data["error_text"] = decode_salmson(error) if error else None
    data["status"] = "ERREUR" if error else ("EN MARCHE" if data["running"] else "ARRÊTÉE")
    data["last_seen"] = now_iso()
    return data


def get_wilo() -> dict:
    data = {}
    for name, reg in WILO_REGS.items():
        value = read_reg(ADDR_WILO, reg)
        if value is None:
            continue
        raw = value[0]
        data[name] = round(raw / 100, 2) if name in {"pressure", "flow"} else raw
    if not data:
        return unavailable_state()
    data["running"] = data.get("pump1", 0) == 1 or data.get("pump2", 0) == 1
    error = data.get("error_code", 0)
    data["error_text"] = f"Erreur Wilo {error}" if error else None
    data["status"] = "ERREUR" if error else ("EN MARCHE" if data["running"] else "ARRÊTÉ")
    data["last_seen"] = now_iso()
    return data


def get_coffret4() -> dict:
    data = {}
    for name, reg in COFFRET4_REGS.items():
        value = read_reg(ADDR_COFFRET4, reg)
        if value is not None:
            data[name] = value[0]
    if not data:
        return {"status": "DÉCONNECTÉ", "error_text": "Aucune lecture Modbus valide"}
    error = data.get("error_code", 0)
    data["error_text"] = f"Erreur coffret/capteur 4 {error}" if error else None
    data["status"] = "ERREUR" if error else "OK"
    data["last_seen"] = now_iso()
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
            if G781_MODE in SIMULATION_MODES:
                refresh_modbus_cache()
                time.sleep(UPDATE_SEC)
                continue

            if G781_MODE not in TCP_MODES:
                time.sleep(UPDATE_SEC)
                continue

            if not current_host:
                set_disconnected("En attente de G781_HOST")
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


# ============ COMMANDES ============
def set_simulated_device_state(device: str, action: str) -> None:
    running = action in {"on", "forward", "reverse"}
    targets = []
    if device == "forage":
        targets = ["invt", "salmson"]
    elif device == "all":
        targets = ["invt", "salmson", "wilo"]
    elif device in {"invt", "salmson", "wilo"}:
        targets = [device]
    with lock:
        for target in targets:
            cache[target].update({
                "running": running,
                "status": "EN MARCHE" if running else ("ARRÊTÉE" if target == "salmson" else "ARRÊTÉ"),
                "error_text": None,
                "last_seen": now_iso(),
            })
        cache["connected"] = True
        cache["last_update"] = time.time()


def command_blockers(device: str, action: str) -> list[str]:
    if not SERVER_SAFETY_ENABLED or action in {"off", "stop"}:
        return []
    blockers = []
    invt = cache.get("invt", {})
    salmson = cache.get("salmson", {})
    wilo = cache.get("wilo", {})

    if device in {"invt", "forage", "all"} and invt.get("fault_code"):
        blockers.append(invt.get("error_text") or "Défaut variateur INVT")
    if device in {"salmson", "forage", "all"}:
        if salmson.get("error_code"):
            blockers.append(salmson.get("error_text") or "Défaut Salmson")
        if "float_low" in salmson and not bool(salmson.get("float_low")):
            blockers.append("Manque d'eau Salmson")
    if device in {"wilo", "all"} and wilo.get("error_code"):
        blockers.append(wilo.get("error_text") or "Défaut Wilo")
    return blockers


def apply_control(device: str, action: str, source: str = "manual") -> tuple[bool, str | None]:
    device = str(device or "").strip().lower()
    action = normalize_action(action)

    if device not in VALID_DEVICES:
        return False, f"Appareil inconnu: {device}"
    if action not in {"on", "off", "forward", "reverse"}:
        return False, f"Action invalide: {action}"
    if device == "coffret4":
        return False, "Aucun registre de commande défini pour coffret4"

    blockers = command_blockers(device, action)
    if blockers:
        return False, "Commande bloquée: " + " / ".join(blockers)

    if G781_MODE in SIMULATION_MODES:
        set_simulated_device_state(device, action)
        add_event("info", f"[SIMULATION] Commande OK: {device} {action}", source=source)
        return True, None

    if G781_MODE in HTTP_PUSH_MODES:
        queue_command({"type": "control", "device": device, "action": action, "source": source})
        return True, None

    with lock:
        if not client_is_open():
            return False, "USR-G781 non connecté"

        value = 1 if action in {"on", "forward", "reverse"} else 0
        invt_value = INVT_ACTIONS.get(action, 1 if value else 5)

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

    if ok:
        add_event("info", f"Commande Modbus OK: {device} {action}", source=source)
        return True, None
    return False, "Commande Modbus échouée"


# ============ RAIN BIRD ============
def rainbird_request(ip: str, command: str, params: dict | None = None):
    if not RAINBIRD_STICK_ID or not RAINBIRD_KEYCODE:
        add_event("warning", "Rain Bird non configuré: RAINBIRD_STICK_ID/RAINBIRD_KEYCODE manquants")
        return None

    import urllib.request

    try:
        payload = {"id": RAINBIRD_STICK_ID, "command": command}
        if params:
            payload.update(params)
        req = urllib.request.Request(
            f"http://{ip}/stick",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Basic {RAINBIRD_KEYCODE[:32]}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        add_event("warning", f"Rain Bird erreur: {exc}")
        return None


def rainbird_start_zone(zone: int, duration: int) -> tuple[bool, dict]:
    if zone not in RAINBIRD_ZONES:
        return False, {"error": f"Zone {zone} invalide"}
    duration = max(1, min(int(duration), 360))
    rainbird_state["last_cmd"] = f"START Zone {zone} {duration}min"
    if not rainbird_ip:
        if G781_MODE in SIMULATION_MODES:
            rainbird_state["active_zones"] = [zone]
            return True, {"mode": "simulation", "zone": zone, "duration": duration}
        return False, {"error": "Rain Bird IP manquante: simulation refusée en mode réel"}
    result = rainbird_request(rainbird_ip, "ZoneStartRequest", {"zone": zone, "duration": duration})
    if result:
        rainbird_state["active_zones"] = [zone]
        rainbird_state["connected"] = True
        return True, {"result": result, "zone": zone, "duration": duration}
    return False, {"error": "Pas de réponse Rain Bird"}


def rainbird_stop_zone(zone: int | None = None) -> tuple[bool, dict]:
    rainbird_state["last_cmd"] = f"STOP {'zone ' + str(zone) if zone else 'tout'}"
    rainbird_state["active_zones"] = []
    if not rainbird_ip:
        return True, {"mode": "simulation"}
    result = rainbird_request(rainbird_ip, "StopIrrigationRequest")
    if result:
        rainbird_state["connected"] = True
        return True, {"result": result}
    return False, {"error": "Pas de réponse Rain Bird"}


# ============ PLANNING ============
def normalize_plan_item(item: dict, index: int = 0) -> dict:
    if not isinstance(item, dict):
        raise ValueError("Chaque entrée planning doit être un objet")
    item_type = str(item.get("type") or "control").strip().lower()
    time_text = str(item.get("time") or "").strip()
    if not valid_time_text(time_text):
        raise ValueError(f"Heure invalide: {time_text}")
    days = parse_int_list(item.get("days", []))
    if not days or any(day < 0 or day > 6 for day in days):
        raise ValueError("days doit contenir des jours 0-6")

    base = {
        "id": str(item.get("id") or f"plan-{index}-{int(time.time() * 1000)}"),
        "type": item_type,
        "name": str(item.get("name") or item.get("label") or "").strip(),
        "time": time_text,
        "days": sorted(set(days)),
        "enabled": bool(item.get("enabled", True)),
        "source": str(item.get("source") or "api"),
    }

    if item_type == "control":
        device = str(item.get("device") or item.get("pump") or "").strip().lower()
        action = normalize_action(item.get("action"))
        if device not in VALID_DEVICES:
            raise ValueError(f"Appareil planning invalide: {device}")
        if action not in {"on", "off", "forward", "reverse"}:
            raise ValueError(f"Action planning invalide: {action}")
        base.update({"device": device, "action": action})
        return base

    if item_type == "rainbird_sequence":
        zones = parse_int_list(item.get("zones", []))
        if not zones:
            raise ValueError("rainbird_sequence exige au moins une zone")
        invalid = [z for z in zones if z not in RAINBIRD_ZONES]
        if invalid:
            raise ValueError(f"Zone Rain Bird invalide: {invalid[0]}")
        duration = max(1, min(parse_int(item.get("duration"), 20), 360))
        pause_min = max(0, min(parse_int(item.get("pause_min"), 2), 120))
        start_pumps = bool(item.get("start_pumps", True))
        pumps = [str(p).lower() for p in item.get("pumps", ["forage", "wilo"])]
        pumps = [p for p in pumps if p in {"forage", "wilo", "salmson", "invt", "all"}]
        base.update({
            "zones": zones,
            "duration": duration,
            "pause_min": pause_min,
            "start_pumps": start_pumps,
            "pumps": pumps,
        })
        return base

    raise ValueError(f"Type planning invalide: {item_type}")


def load_planning_from_disk() -> None:
    global planning
    try:
        if not PLANNING_FILE.exists():
            planning = []
            return
        data = json.loads(PLANNING_FILE.read_text(encoding="utf-8"))
        items = data.get("planning", data if isinstance(data, list) else [])
        planning = [normalize_plan_item(item, i) for i, item in enumerate(items)]
        add_event("info", f"Planning chargé: {len(planning)} entrée(s)")
    except Exception as exc:
        planning = []
        add_event("warning", f"Planning non chargé: {exc}")


def save_planning_to_disk() -> None:
    try:
        PLANNING_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": APP_VERSION,
            "timezone": LOCAL_TZ_NAME,
            "saved_at": now_iso(),
            "planning": planning,
        }
        tmp = PLANNING_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(PLANNING_FILE)
    except Exception as exc:
        add_event("warning", f"Planning non sauvegardé: {exc}")



def load_app_state_from_disk() -> None:
    global app_state, app_state_revision, app_state_updated_at
    try:
        if not APP_STATE_FILE.exists():
            app_state = {}
            app_state_revision = 0
            app_state_updated_at = ""
            return
        data = json.loads(APP_STATE_FILE.read_text(encoding="utf-8"))
        raw_state = data.get("state", {})
        app_state = {
            str(key): value
            for key, value in raw_state.items()
            if key in SYNC_STATE_KEYS and isinstance(value, str)
        }
        app_state_revision = int(data.get("revision", 0))
        app_state_updated_at = str(data.get("updated_at") or now_iso())
        add_event("info", f"État partagé chargé: revision {app_state_revision}")
    except Exception as exc:
        app_state = {}
        app_state_revision = 0
        app_state_updated_at = ""
        add_event("warning", f"État partagé non chargé: {exc}")


def save_app_state_to_disk() -> None:
    try:
        APP_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": APP_VERSION,
            "revision": app_state_revision,
            "updated_at": app_state_updated_at,
            "state": app_state,
        }
        tmp = APP_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(APP_STATE_FILE)
    except Exception as exc:
        add_event("warning", f"État partagé non sauvegardé: {exc}")


def clean_shared_state(raw_state: dict) -> dict[str, str | None]:
    cleaned: dict[str, str | None] = {}
    for key, value in raw_state.items():
        key = str(key)
        if key not in SYNC_STATE_KEYS:
            continue
        if value is None:
            cleaned[key] = None
            continue
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        if len(value) > 250_000:
            raise ValueError(f"Valeur trop grande pour {key}")
        cleaned[key] = value
    return cleaned


def is_plan_due(item: dict, now: datetime) -> bool:
    if not item.get("enabled", True):
        return False
    if now.weekday() not in item.get("days", []):
        return False
    return now.strftime("%H:%M") == item.get("time")


def execute_rainbird_sequence(item: dict) -> None:
    plan_id = item.get("id", "sequence")
    if plan_id in running_plan_ids:
        add_event("warning", f"Planning déjà en cours: {item.get('name') or plan_id}")
        return
    running_plan_ids.add(plan_id)
    try:
        add_event("info", f"Début séquence planning: {item.get('name') or plan_id}")
        pumps = item.get("pumps", [])
        if item.get("start_pumps", True):
            for pump in pumps:
                ok, err = apply_control(pump, "on", source=f"planning:{plan_id}")
                if not ok:
                    add_event("error", f"Pompe non démarrée pour planning {plan_id}: {err}")
                    return
                time.sleep(1)

        zones = item.get("zones", [])
        duration = int(item.get("duration", 20))
        pause_min = int(item.get("pause_min", 2))
        for idx, zone in enumerate(zones):
            ok, info = rainbird_start_zone(int(zone), duration)
            if not ok:
                add_event("error", f"Zone Rain Bird non lancée: {info.get('error')}")
                break
            add_event("info", f"Zone {zone} lancée {duration} min", planning=plan_id)
            time.sleep(duration * 60)
            rainbird_stop_zone(int(zone))
            if idx < len(zones) - 1 and pause_min > 0:
                time.sleep(pause_min * 60)
    finally:
        if item.get("start_pumps", True):
            for pump in reversed(item.get("pumps", [])):
                apply_control(pump, "off", source=f"planning:{plan_id}")
                time.sleep(1)
        running_plan_ids.discard(plan_id)
        add_event("info", f"Fin séquence planning: {item.get('name') or plan_id}")


def execute_plan_item(item: dict) -> None:
    if item.get("type") == "rainbird_sequence":
        execute_rainbird_sequence(item)
        return
    ok, error = apply_control(item["device"], item["action"], source=f"planning:{item.get('id')}")
    if ok:
        add_event("info", f"Planning exécuté: {item.get('name') or item.get('id')}")
    else:
        add_event("error", f"Planning échoué: {item.get('name') or item.get('id')} - {error}")


def planning_loop() -> None:
    while True:
        try:
            now = local_now()
            with lock:
                items = list(planning)
            for item in items:
                if not is_plan_due(item, now):
                    continue
                run_key = f"{item.get('id')}:{now.date()}:{now.strftime('%H:%M')}"
                if run_key in last_plan_runs:
                    continue
                last_plan_runs[run_key] = time.time()
                thread = threading.Thread(target=execute_plan_item, args=(item,), daemon=True, name=f"planning-{item.get('id')}")
                thread.start()
            cutoff = time.time() - 86400
            for key, ts in list(last_plan_runs.items()):
                if ts < cutoff:
                    last_plan_runs.pop(key, None)
            time.sleep(PLANNING_POLL_SEC)
        except Exception as exc:
            add_event("error", f"Erreur boucle planning: {exc}")
            time.sleep(max(PLANNING_POLL_SEC, 20))


def start_background_threads() -> None:
    global thread_started, scheduler_started
    if not thread_started:
        thread_started = True
        threading.Thread(target=update_loop, daemon=True, name="modbus-update-loop").start()
    if not scheduler_started:
        scheduler_started = True
        threading.Thread(target=planning_loop, daemon=True, name="planning-loop").start()


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
            "server_time": local_now().isoformat(),
            "timezone": LOCAL_TZ_NAME,
            "planning_count": len(planning),
            "simulation": G781_MODE in SIMULATION_MODES,
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
                "server_time": local_now().isoformat(),
                "timezone": LOCAL_TZ_NAME,
                "planning_count": len(planning),
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
    if not host and G781_MODE not in SIMULATION_MODES:
        return jsonify({"success": False, "error": "IP/host G781 manquant"}), 400
    ok = connect(host, port)
    return jsonify({"success": ok, "host": host, "ip": host, "port": port, "mode": G781_MODE})


@app.route("/api/control", methods=["POST"])
def control():
    body = request.get_json(silent=True) or {}
    device = str(body.get("device") or body.get("pump") or "").strip().lower()
    action = str(body.get("action") or "").strip().lower()
    if not device or not action:
        return jsonify({"success": False, "error": "device/pump et action requis"}), 400
    ok, error = apply_control(device, action, source="api")
    status_code = 200 if ok else (503 if error == "USR-G781 non connecté" else 400)
    return jsonify({"success": ok, "device": device, "pump": device, "action": action, "error": error}), status_code


@app.route("/api/inverter", methods=["POST"])
def inverter():
    body = request.get_json(silent=True) or {}
    action = str(body.get("action") or "stop").strip().lower()
    if action not in INVT_ACTIONS:
        return jsonify({"success": False, "error": f"Action INVT inconnue: {action}"}), 400
    if G781_MODE in HTTP_PUSH_MODES:
        item = queue_command({"type": "inverter", "device": "invt", "action": action, "source": "api"})
        return jsonify({"success": True, "queued": True, "command": item})
    ok, error = apply_control("invt", action, source="api")
    return jsonify({"success": ok, "device": "invt", "action": action, "error": error}), 200 if ok else 400


@app.route("/api/param/read", methods=["POST"])
def param_read():
    body = request.get_json(silent=True) or {}
    addr = parse_int(body.get("addr"), 1)
    reg = parse_int(body.get("reg"), 0)
    count = min(max(parse_int(body.get("count"), 1), 1), 64)
    if not client_is_open():
        return jsonify({"success": False, "error": "USR-G781 non connecté"}), 503
    with lock:
        values = read_reg(addr, reg, count)
    if values is not None:
        return jsonify({"success": True, "value": values[0], "values": values, "reg": hex(reg), "addr": addr})
    return jsonify({"success": False, "error": "Registre illisible", "reg": hex(reg), "addr": addr}), 400


@app.route("/api/param/write", methods=["POST"])
def param_write():
    if not ALLOW_PARAM_WRITE:
        return jsonify({"success": False, "error": "Écriture paramètres désactivée côté serveur"}), 403
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
        return jsonify({"planning": planning, "timezone": LOCAL_TZ_NAME, "server_time": local_now().isoformat()})

    body = request.get_json(silent=True) or {}
    items = body.get("planning")
    if not isinstance(items, list):
        return jsonify({"success": False, "error": "planning doit être une liste"}), 400

    try:
        normalized = [normalize_plan_item(item, idx) for idx, item in enumerate(items)]
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    with lock:
        planning = normalized
        save_planning_to_disk()
    add_event("info", f"Planning sauvegardé: {len(planning)} entrée(s)")
    return jsonify({"success": True, "planning": planning, "timezone": LOCAL_TZ_NAME})



@app.route("/api/app-state", methods=["GET", "POST"])
def api_app_state():
    global app_state, app_state_revision, app_state_updated_at
    if request.method == "GET":
        with lock:
            return jsonify(
                {
                    "success": True,
                    "revision": app_state_revision,
                    "updated_at": app_state_updated_at,
                    "state": app_state,
                    "keys": sorted(SYNC_STATE_KEYS),
                }
            )

    body = request.get_json(silent=True) or {}
    raw_state = body.get("state")
    if not isinstance(raw_state, dict):
        return jsonify({"success": False, "error": "state doit être un objet"}), 400
    try:
        cleaned = clean_shared_state(raw_state)
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    with lock:
        for key, value in cleaned.items():
            if value is None:
                app_state.pop(key, None)
            else:
                app_state[key] = value
        app_state_revision += 1
        app_state_updated_at = ""
        save_app_state_to_disk()
    add_event("info", f"État partagé synchronisé: revision {app_state_revision}")
    return jsonify({"success": True, "revision": app_state_revision, "updated_at": app_state_updated_at, "state": app_state})


@app.route("/api/events")
def api_events():
    limit = min(parse_int(request.args.get("limit"), 50), 300)
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
    ok, info = rainbird_start_zone(zone, duration)
    if ok:
        return jsonify({"success": True, **info})
    return jsonify({"success": False, **info}), 503 if info.get("error") == "Pas de réponse Rain Bird" else 400


@app.route("/api/rainbird/stop", methods=["POST"])
def rainbird_stop():
    body = request.get_json(silent=True) or {}
    zone = body.get("zone")
    zone = parse_int(zone, 0) if zone not in {None, ""} else None
    ok, info = rainbird_stop_zone(zone)
    if ok:
        return jsonify({"success": True, **info})
    return jsonify({"success": False, **info}), 503


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
    if (APP_DIR / "index.html").exists():
        return send_from_directory(APP_DIR, "index.html")
    return f"""
    <h2>Zarzis Irrigation - Serveur Cloud</h2>
    <p>Version: <code>{APP_VERSION}</code></p>
    <p>API: <code>/api/ping</code>, <code>/api/status</code>, <code>/api/devices</code></p>
    <p>Mode G781: <strong>{G781_MODE}</strong></p>
    <p>Connecté: <strong>{"OUI" if cache["connected"] else "NON / en attente"}</strong></p>
    """


@app.route("/manifest.webmanifest")
def manifest():
    return send_from_directory(APP_DIR, "manifest.webmanifest")


@app.route("/sw.js")
def service_worker():
    return send_from_directory(APP_DIR, "sw.js")


@app.route("/icon.svg")
def icon():
    return send_from_directory(APP_DIR, "icon.svg")


@app.route("/icons/<path:filename>")
def icons(filename):
    return send_from_directory(APP_DIR / "icons", filename)


load_app_state_from_disk()
load_planning_from_disk()
start_background_threads()


if __name__ == "__main__":
    log.info("=" * 50)
    log.info("  ZARZIS CLOUD SERVER - MODBUS PROXY")
    log.info("=" * 50)
    app.run(host="0.0.0.0", port=SERVER_PORT, debug=False)
