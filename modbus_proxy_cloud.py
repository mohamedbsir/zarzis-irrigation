#!/usr/bin/env python3
"""
Proxy cloud Zarzis Irrigation.

Le serveur expose l'API HTTP du dashboard.
En production 4G/NAT, il fonctionne en mode http_push avec un agent local.
Le mode direct_tcp reste disponible seulement via VPN/APN prive.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import unicodedata
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from pymodbus.client import ModbusTcpClient


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or str(value).strip() == "":
        return default
    return int(str(value).strip(), 0)


def env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or str(value).strip() == "":
        return default
    return float(str(value).strip().replace(",", "."))


# ============ CONFIGURATION ============
APP_VERSION = "2026.05.10-zarzis-http-push-v8.4-offline"
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(APP_DIR)))
PLANNING_FILE = Path(os.environ.get("PLANNING_FILE", str(DATA_DIR / "planning_zarzis.json")))
APP_STATE_FILE = Path(os.environ.get("APP_STATE_FILE", str(DATA_DIR / "app_state_zarzis.json")))
HISTORY_FILE = Path(os.environ.get("HISTORY_FILE", str(DATA_DIR / "history_zarzis.json")))
PERSISTENT_STORAGE_ENABLED = env_bool("PERSISTENT_STORAGE_ENABLED", False)
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

G781_MODE = os.environ.get("G781_MODE", "http_push").strip().lower()
G781_HOST = os.environ.get("G781_HOST") or os.environ.get("USR_G781_IP", "")
G781_PORT = int(os.environ.get("G781_PORT") or os.environ.get("USR_G781_PORT", "502"))
SERVER_PORT = int(os.environ.get("PORT", "8080"))
UPDATE_SEC = max(2, int(os.environ.get("UPDATE_SEC", "5")))
PLANNING_POLL_SEC = max(5, int(os.environ.get("PLANNING_POLL_SEC", "10")))
API_TOKEN = os.environ.get("API_TOKEN", "").strip()
CORS_ORIGINS = [origin.strip() for origin in os.environ.get("CORS_ORIGINS", "*").split(",") if origin.strip()] or ["*"]
LOCAL_TZ_NAME = os.environ.get("LOCAL_TZ", "Africa/Tunis").strip() or "Africa/Tunis"
try:
    LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)
except Exception:
    LOCAL_TZ = timezone.utc
    LOCAL_TZ_NAME = "UTC"

SERVER_SAFETY_ENABLED = os.environ.get("SERVER_SAFETY_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
ALLOW_PARAM_WRITE = os.environ.get("ALLOW_PARAM_WRITE", "false").strip().lower() not in {"0", "false", "no", "off"}
MODBUS_REGISTERS_VALIDATED = os.environ.get("MODBUS_REGISTERS_VALIDATED", "false").strip().lower() in {"1", "true", "yes", "on"}
ENABLE_PLANNING = env_bool("ENABLE_PLANNING", False)
ALLOW_REMOTE_G781_CONNECT = env_bool("ALLOW_REMOTE_G781_CONNECT", False)
COMMAND_MIN_INTERVAL_SEC = max(0, int(os.environ.get("COMMAND_MIN_INTERVAL_SEC", "30")))
COMMAND_RESTART_DELAY_SEC = max(0, int(os.environ.get("COMMAND_RESTART_DELAY_SEC", "60")))
G781_HTTP_PUSH_STALE_SEC = max(10, env_int("G781_HTTP_PUSH_STALE_SEC", 45))
G781_COMMAND_TTL_SEC = max(30, env_int("G781_COMMAND_TTL_SEC", 300))
G781_COMMAND_ACK_TIMEOUT_SEC = max(10, env_int("G781_COMMAND_ACK_TIMEOUT_SEC", 45))
HISTORY_MAX_ITEMS = max(100, env_int("HISTORY_MAX_ITEMS", 2000))
HISTORY_SAVE_MIN_INTERVAL_SEC = max(5, env_int("HISTORY_SAVE_MIN_INTERVAL_SEC", 30))
SALMSON_FLOAT_LOW_OK_VALUE = env_int("SALMSON_FLOAT_LOW_OK_VALUE", 1)
SALMSON_COMMAND_ENABLED = env_bool("SALMSON_COMMAND_ENABLED", False)
INVT_NOMINAL_KW = env_float("INVT_NOMINAL_KW", 5.5)

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
    "freq_hz": env_int("INVT_REG_FREQ_HZ", 0x3000),
    "set_freq_hz": env_int("INVT_REG_SET_FREQ_HZ", 0x3001),
    "dc_bus_v": env_int("INVT_REG_DC_BUS_V", 0x3002),
    "voltage_v": env_int("INVT_REG_VOLTAGE_V", 0x3003),
    "current_a": env_int("INVT_REG_CURRENT_A", 0x3004),
    "power_pct": env_int("INVT_REG_POWER_PCT", 0x3006),
    "fault_code": env_int("INVT_REG_FAULT_CODE", 0x5000),
}
INVT_CMD = env_int("INVT_CMD_REG", 0x2000)
INVT_ACTIONS = {
    "on": env_int("INVT_ON_VALUE", 1),
    "start": env_int("INVT_ON_VALUE", 1),
    "forward": env_int("INVT_FORWARD_VALUE", 1),
    "reverse": env_int("INVT_REVERSE_VALUE", 2),
    "off": env_int("INVT_OFF_VALUE", 5),
    "stop": env_int("INVT_OFF_VALUE", 5),
}

# Registres Salmson EC-L / EC-Lift (profil Wilo-Control EC-L, Fieldbuslist Modbus EC).
# Les adresses sont zero-based: 40015 => 14, 40026 => 25, 40198 => 197.
SALMSON_REGS = {
    "level_cm": env_int("SALMSON_REG_LEVEL_CM", 25),
    "pump1_mode": env_int("SALMSON_REG_PUMP1_MODE", 40),
    "pump2_mode": env_int("SALMSON_REG_PUMP2_MODE", 41),
    "switch_state": env_int("SALMSON_REG_SWITCH_STATE", 61),
    "error_code": env_int("SALMSON_REG_ERROR_CODE", 138),
    "float_state": env_int("SALMSON_REG_FLOAT_STATE", 197),
}
SALMSON_CMD = env_int("SALMSON_CMD_REG", 14)

# Registres Wilo Control EC-B Booster (notice 43587401).
# Les adresses sont zero-based: 40015 => 14, 40026 => 25.
WILO_REGS = {
    "pressure": env_int("WILO_REG_PRESSURE", 25),
    "flow": env_int("WILO_REG_FLOW", -1),
    "pump1_mode": env_int("WILO_REG_PUMP1_MODE", 40),
    "pump2_mode": env_int("WILO_REG_PUMP2_MODE", 41),
    "switch_state": env_int("WILO_REG_SWITCH_STATE", 61),
    "error_code": env_int("WILO_REG_ERROR_CODE", 138),
}
WILO_CMD = env_int("WILO_CMD_REG", 14)
WILO_ACK_REG = env_int("WILO_ACK_REG", 140)

# Coffret/capteur 4 : registres génériques à adapter si le matériel réel change.
COFFRET4_REGS = {
    "input_1": env_int("COFFRET4_REG_INPUT_1", 0x0001),
    "input_2": env_int("COFFRET4_REG_INPUT_2", 0x0002),
    "analog_1": env_int("COFFRET4_REG_ANALOG_1", 0x0010),
    "error_code": env_int("COFFRET4_REG_ERROR_CODE", 0x0020),
}


# ============ APP FLASK ============
app = Flask(__name__)
CORS(app, origins=CORS_ORIGINS, allow_headers=["Content-Type", "Authorization", "X-API-Token"])

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)
try:
    if PERSISTENT_STORAGE_ENABLED:
        file_handler = logging.FileHandler(DATA_DIR / "zarzis_cloud.log", encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        log.addHandler(file_handler)
except Exception:
    pass


def unavailable_state(status: str = "DÉCONNECTÉ") -> dict:
    return {"status": status, "running": False, "error_text": "Aucune lecture Modbus valide"}


cache = {
    "invt": unavailable_state(),
    "salmson": unavailable_state(),
    "wilo": unavailable_state(),
    "coffret4": {"status": "DÉCONNECTÉ", "error_text": "Aucune lecture Modbus valide"},
    "connected": False,
    "last_update": 0,
    "g781_ip": G781_HOST or "En attente agent local...",
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
history: deque[dict] = deque(maxlen=HISTORY_MAX_ITEMS)
recent_command_acks: deque[dict] = deque(maxlen=100)
planning: list[dict] = []
app_state: dict[str, str] = {}
app_state_revision = 0
app_state_updated_at = ""
last_history_save_at = 0.0
last_event_at: dict[str, float] = {}
last_plan_runs: dict[str, float] = {}
running_plan_ids: set[str] = set()
last_command_at: dict[str, float] = {}
last_off_at: dict[str, float] = {}
last_rainbird_command_at = 0.0


# ============ RAIN BIRD CONFIG ============
RAINBIRD_STICK_ID = os.environ.get("RAINBIRD_STICK_ID", "")
RAINBIRD_SERIAL = os.environ.get("RAINBIRD_SERIAL", "")
RAINBIRD_KEYCODE = os.environ.get("RAINBIRD_KEYCODE", "")
RAINBIRD_WIFI = os.environ.get("RAINBIRD_WIFI", "")
RAINBIRD_ZONES = []
RAINBIRD_ENABLED = []
RAINBIRD_PROGRAMS = {"1": "Arrosage", "2": "Fertilisation"}
RAINBIRD_MAX_DURATION_MIN = max(1, env_int("RAINBIRD_MAX_DURATION_MIN", 240))
RAINBIRD_MIN_INTERVAL_SEC = max(0, env_int("RAINBIRD_MIN_INTERVAL_SEC", 30))
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
    return request.headers.get("X-API-Token", "").strip()


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


def http_push_is_fresh() -> bool:
    if G781_MODE not in HTTP_PUSH_MODES:
        return False
    last_update = float(cache.get("last_update") or 0)
    return last_update > 0 and (time.time() - last_update) <= G781_HTTP_PUSH_STALE_SEC


def client_is_open() -> bool:
    if G781_MODE in SIMULATION_MODES:
        return True
    if G781_MODE in HTTP_PUSH_MODES:
        return http_push_is_fresh()
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

        if G781_MODE in HTTP_PUSH_MODES:
            cache["connected"] = http_push_is_fresh()
            cache["g781_ip"] = "HTTP PUSH - en attente agent local"
            cache["mode"] = G781_MODE
            add_event("info", "Mode HTTP PUSH actif: aucune IP publique entrante necessaire")
            return True

        if not host:
            cache["connected"] = False
            cache["g781_ip"] = "En attente cible Modbus TCP"
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
    if G781_MODE in HTTP_PUSH_MODES:
        return None
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
    if G781_MODE in HTTP_PUSH_MODES:
        return False
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


def command_snapshot(command: dict) -> dict:
    public = dict(command)
    public.pop("expires_at", None)
    return public


def save_history_to_disk(force: bool = False) -> None:
    global last_history_save_at
    current = time.time()
    if not force and current - last_history_save_at < HISTORY_SAVE_MIN_INTERVAL_SEC:
        return
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": APP_VERSION,
            "saved_at": now_iso(),
            "history": list(history),
        }
        tmp = HISTORY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(HISTORY_FILE)
        last_history_save_at = current
    except Exception as exc:
        add_event("warning", f"Historique non sauvegardé: {exc}")


def add_history(kind: str, force_save: bool = False, **data) -> dict:
    entry = {"ts": now_iso(), "kind": kind, **data}
    history.appendleft(entry)
    save_history_to_disk(force=force_save or kind.startswith("command"))
    return entry


def queue_command(command: dict) -> dict:
    item = {
        "id": f"cmd-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}",
        "ts": now_iso(),
        "created_at": time.time(),
        "expires_at": time.time() + G781_COMMAND_TTL_SEC,
        "status": "queued",
        "attempts": 0,
        "last_fetch_at": 0,
        **command,
    }
    pending_commands.append(item)
    add_history("command_queued", force_save=True, command=command_snapshot(item))
    add_event("info", "Commande ajoutee a la file HTTP PUSH", command=item)
    return item


def cleanup_pending_commands() -> None:
    current = time.time()
    kept = []
    expired = []
    for cmd in pending_commands:
        if float(cmd.get("expires_at", current)) >= current:
            kept.append(cmd)
        else:
            expired.append(cmd)
    for cmd in expired:
        cmd["status"] = "expired"
        add_history("command_expired", force_save=True, command=command_snapshot(cmd))
    pending_commands.clear()
    pending_commands.extend(kept)


def command_can_retry(command: dict) -> bool:
    if str(command.get("type") or "") == "rainbird":
        return str(command.get("action") or "").lower() == "stop"
    return str(command.get("action") or "").lower() in {"off", "stop"}


def command_status_counts() -> dict[str, int]:
    cleanup_pending_commands()
    counts = {"queued": 0, "sent": 0}
    for cmd in pending_commands:
        status = str(cmd.get("status") or "queued")
        counts[status] = counts.get(status, 0) + 1
    return counts


def is_start_action(action: str) -> bool:
    return normalize_action(action) in {"on", "forward", "reverse"}


def safe_int(value, default: int | None = None) -> int | None:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def device_reading_unavailable(data: dict) -> bool:
    status = str(data.get("status") or "").lower()
    error_text = str(data.get("error_text") or "").lower()
    if not data:
        return True
    text = f"{status} {error_text}"
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    if any(marker in normalized for marker in ("deconnect", "non lu", "aucune lecture", "indisponible")):
        return True
    return False


def start_freshness_blocker() -> str | None:
    if G781_MODE in SIMULATION_MODES:
        return None
    if G781_MODE in HTTP_PUSH_MODES:
        if not http_push_is_fresh():
            return "Agent local absent ou mesures trop anciennes"
        return None
    last_update = float(cache.get("last_update") or 0)
    max_age = max(15, UPDATE_SEC * 3 + 5)
    if last_update <= 0 or time.time() - last_update > max_age:
        return "Mesures Modbus trop anciennes"
    if not client_is_open():
        return "Cible Modbus TCP non connectee"
    return None


def device_start_blockers(target: str, data: dict) -> list[str]:
    blockers: list[str] = []
    if device_reading_unavailable(data):
        blockers.append(f"{target}: lecture critique absente")
        return blockers

    if target == "invt":
        if "fault_code" not in data:
            blockers.append("INVT: registre defaut non lu")
            return blockers
        fault = safe_int(data.get("fault_code"), 0) or 0
        if fault:
            blockers.append(data.get("error_text") or f"Defaut variateur INVT {fault}")

    elif target == "salmson":
        if "error_code" not in data:
            blockers.append("Salmson: registre defaut non lu")
        else:
            error = safe_int(data.get("error_code"), 0) or 0
            if error:
                blockers.append(data.get("error_text") or f"Defaut Salmson {error}")
        if "float_low" not in data:
            blockers.append("Salmson: flotteur manque d'eau non lu")
        elif safe_int(data.get("float_low")) != SALMSON_FLOAT_LOW_OK_VALUE:
            blockers.append("Manque d'eau Salmson")

    elif target == "wilo":
        required = ["error_code", "switch_state"]
        if WILO_REGS.get("pressure", -1) >= 0:
            required.append("pressure")
        missing = [name for name in required if name not in data]
        if missing:
            blockers.append("Wilo: registre critique non lu (" + ", ".join(missing) + ")")
        error = safe_int(data.get("error_code"), 0) or 0
        if error:
            blockers.append(data.get("error_text") or f"Defaut Wilo {error}")

    return blockers


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


def decode_salmson_error_bitmap(bits: int) -> str:
    names = {
        0: "Defaut capteur",
        3: "Protection thermique pompe 1",
        4: "Protection thermique pompe 2",
        5: "Alarme pompe 1",
        6: "Alarme pompe 2",
        11: "Marche a sec",
        12: "Niveau haut",
        16: "Priorite off",
        17: "Redondance",
        19: "Communication esclave",
        20: "Defaut alimentation",
        21: "Fuite detectee",
        22: "Extern off",
        23: "Plausibilite",
    }
    active = [label for bit, label in names.items() if bits & (1 << bit)]
    return " / ".join(active) if active else f"Erreur Salmson {bits}"


def enrich_salmson_data(data: dict) -> dict:
    switch_state = int(data.get("switch_state", 0) or 0)
    running = bool(switch_state & ((1 << 0) | (1 << 8) | (1 << 9)))
    data["pump_state"] = 1 if running else 0
    data["running"] = running
    data["running_source"] = "Salmson EC-L switch_box_state"
    if "float_state" in data:
        float_state = int(data.get("float_state", 0) or 0)
        dry_run = bool(float_state & (1 << 0))
        high_water = bool(float_state & (1 << 4))
        data["dry_run"] = dry_run
        data["high_water"] = high_water
        data["float_low"] = 0 if dry_run else SALMSON_FLOAT_LOW_OK_VALUE
        data["float_high"] = 1 if high_water else 0
    data["current_a"] = data.get("current_a", 0)
    return data


def decode_wilo_error_bitmap(bits: int) -> str:
    names = {
        0: "Defaut capteur",
        1: "Pression trop haute",
        2: "Pression trop basse",
        5: "Alarme pompe 1",
        6: "Alarme pompe 2",
        7: "Alarme pompe 3",
        15: "Niveau haut",
        20: "Defaut alimentation",
        21: "Fuite detectee",
    }
    active = [label for bit, label in names.items() if bits & (1 << bit)]
    return " / ".join(active) if active else f"Erreur Wilo {bits}"


def get_invt() -> dict:
    data = {}
    for name, reg in INVT_REGS.items():
        if reg < 0:
            continue
        value = read_reg(ADDR_INVT, reg)
        if value is None:
            continue
        raw = value[0]
        if name in {"freq_hz", "set_freq_hz"}:
            data[name] = round(raw / 100, 2)
        elif name in {"current_a", "dc_bus_v", "power_pct"}:
            data[name] = round(raw / 10, 1)
        else:
            data[name] = raw
    if not data:
        return unavailable_state()
    if "power_pct" in data:
        data["power_kw"] = round((data["power_pct"] / 100) * INVT_NOMINAL_KW, 2)
    data["running"] = data.get("freq_hz", 0) > 0.5
    fault = data.get("fault_code", 0)
    data["error_text"] = decode_invt(fault) if fault else None
    data["status"] = "ERREUR" if fault else ("EN MARCHE" if data["running"] else "ARRÊTÉ")
    data["last_seen"] = now_iso()
    return data


def get_salmson() -> dict:
    data = {}
    for name, reg in SALMSON_REGS.items():
        if reg < 0:
            continue
        count = 2 if name == "error_code" else 1
        value = read_reg(ADDR_SALMSON, reg, count=count)
        if value is None:
            continue
        raw = value[0] if count == 1 else (value[0] | (value[1] << 16))
        data[name] = raw
    if not data:
        return unavailable_state()
    enrich_salmson_data(data)
    error = int(data.get("error_code", 0) or 0)
    data["error_text"] = decode_salmson_error_bitmap(error) if error else None
    data["status"] = "ERREUR" if error else ("EN MARCHE" if data["running"] else "ARRÊTÉE")
    data["last_seen"] = now_iso()
    return data


def get_wilo() -> dict:
    data = {}
    for name, reg in WILO_REGS.items():
        if reg < 0:
            continue
        count = 2 if name == "error_code" else 1
        value = read_reg(ADDR_WILO, reg, count=count)
        if value is None:
            continue
        raw = value[0] if count == 1 else (value[0] | (value[1] << 16))
        data[name] = round(raw / 10, 1) if name in {"pressure", "flow"} else raw
    if not data:
        return unavailable_state()
    switch_state = int(data.get("switch_state", 0) or 0)
    data["running"] = bool(switch_state & 0x01)
    data["running_source"] = "Wilo switch_box_state.SBM"
    error = data.get("error_code", 0)
    data["error_text"] = decode_wilo_error_bitmap(error) if error else None
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
                set_disconnected("En attente de la cible Modbus TCP")
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


def command_targets(device: str) -> list[str]:
    if device == "forage":
        return ["invt", "salmson"]
    if device == "all":
        return ["invt", "salmson", "wilo"]
    if device in {"invt", "salmson", "wilo"}:
        return [device]
    return [device]


def command_rate_limit_error(device: str, action: str) -> str | None:
    if action not in {"on", "forward", "reverse"}:
        return None

    current = time.time()
    with lock:
        for target in command_targets(device):
            since_command = current - last_command_at.get(target, 0)
            if since_command < COMMAND_MIN_INTERVAL_SEC:
                remaining = int(COMMAND_MIN_INTERVAL_SEC - since_command) + 1
                return f"Commande trop rapprochée pour {target}: attendre {remaining}s"

            since_stop = current - last_off_at.get(target, 0)
            if since_stop < COMMAND_RESTART_DELAY_SEC:
                remaining = int(COMMAND_RESTART_DELAY_SEC - since_stop) + 1
                return f"Redémarrage trop rapide pour {target}: attendre {remaining}s"

    return None


def registers_validation_error(action: str) -> str | None:
    if MODBUS_REGISTERS_VALIDATED or action not in {"on", "forward", "reverse"}:
        return None
    if G781_MODE in SIMULATION_MODES:
        return None
    return "Registres Modbus non validés: définir MODBUS_REGISTERS_VALIDATED=true après mapping matériel"


def record_control_success(device: str, action: str) -> None:
    current = time.time()
    with lock:
        for target in command_targets(device):
            last_command_at[target] = current
            if action == "off":
                last_off_at[target] = current


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
        if "float_low" in salmson and salmson.get("float_low") != SALMSON_FLOAT_LOW_OK_VALUE:
            blockers.append("Manque d'eau Salmson")
    if device in {"wilo", "all"} and wilo.get("error_code"):
        blockers.append(wilo.get("error_text") or "Défaut Wilo")
    return blockers


def command_blockers_strict(device: str, action: str) -> list[str]:
    if not SERVER_SAFETY_ENABLED or not is_start_action(action):
        return []
    blockers: list[str] = []
    freshness = start_freshness_blocker()
    if freshness:
        blockers.append(freshness)
        return blockers

    for target in command_targets(device):
        if target == "coffret4":
            blockers.append("coffret4: aucun registre de commande valide")
            continue
        blockers.extend(device_start_blockers(target, cache.get(target, {})))
    return blockers


def apply_control(device: str, action: str, source: str = "manual") -> tuple[bool, str | None]:
    device = str(device or "").strip().lower()
    action = normalize_action(action)
    source = str(source or "manual")

    if device not in VALID_DEVICES:
        return False, f"Appareil inconnu: {device}"
    if action not in {"on", "off", "forward", "reverse"}:
        return False, f"Action invalide: {action}"
    if source.lower().startswith("ai"):
        add_event("warning", "Commande refusée: IA en lecture seule", device=device, action=action, source=source)
        add_history("command_rejected", force_save=True, device=device, action=action, source=source, error="IA lecture seule")
        return False, "IA en lecture seule: diagnostic et proposition uniquement"
    if device == "coffret4":
        return False, "Aucun registre de commande défini pour coffret4"
    if device == "salmson" and not SALMSON_COMMAND_ENABLED:
        return False, "Commande Salmson desactivee: table Modbus EC-L non validee"

    validation_error = registers_validation_error(action)
    if validation_error:
        add_event("warning", validation_error, device=device, action=action, source=source)
        add_history("command_rejected", force_save=True, device=device, action=action, source=source, error=validation_error)
        return False, validation_error

    blockers = command_blockers_strict(device, action)
    if blockers:
        error = "Commande bloquée: " + " / ".join(blockers)
        add_history("command_rejected", force_save=True, device=device, action=action, source=source, error=error)
        return False, error

    rate_error = command_rate_limit_error(device, action)
    if rate_error:
        add_event("warning", f"Commande refusée: {rate_error}", device=device, action=action, source=source)
        add_history("command_rejected", force_save=True, device=device, action=action, source=source, error=rate_error)
        return False, rate_error

    if G781_MODE in SIMULATION_MODES:
        set_simulated_device_state(device, action)
        record_control_success(device, action)
        add_history("command_executed", force_save=True, device=device, action=action, source=source, mode="simulation")
        add_event("info", f"[SIMULATION] Commande OK: {device} {action}", source=source)
        return True, None

    if G781_MODE in HTTP_PUSH_MODES:
        if action in {"on", "forward", "reverse"} and not http_push_is_fresh():
            error = "Agent local absent ou trop ancien: démarrage refusé"
            add_history("command_rejected", force_save=True, device=device, action=action, source=source, error=error)
            return False, error
        queue_command({
            "type": "control",
            "device": device,
            "action": action,
            "source": source,
            "salmson_command_enabled": SALMSON_COMMAND_ENABLED,
        })
        record_control_success(device, action)
        return True, None

    with lock:
        if not client_is_open():
            return False, "Cible Modbus TCP non connectee"

        value = 1 if action in {"on", "forward", "reverse"} else 0
        invt_value = INVT_ACTIONS.get(action, INVT_ACTIONS["on"] if value else INVT_ACTIONS["off"])

        ok = True
        if device == "forage":
            ok = write_reg(ADDR_INVT, INVT_CMD, invt_value)
            if SALMSON_COMMAND_ENABLED:
                ok = ok and write_reg(ADDR_SALMSON, SALMSON_CMD, value)
        elif device == "salmson":
            ok = write_reg(ADDR_SALMSON, SALMSON_CMD, value)
        elif device == "invt":
            ok = write_reg(ADDR_INVT, INVT_CMD, invt_value)
        elif device == "wilo":
            ok = write_reg(ADDR_WILO, WILO_CMD, value)
        elif device == "all":
            ok = write_reg(ADDR_INVT, INVT_CMD, invt_value) and write_reg(ADDR_WILO, WILO_CMD, value)
            if SALMSON_COMMAND_ENABLED:
                ok = ok and write_reg(ADDR_SALMSON, SALMSON_CMD, value)

    if ok:
        record_control_success(device, action)
        add_history("command_executed", force_save=True, device=device, action=action, source=source, mode="direct_tcp")
        add_event("info", f"Commande Modbus OK: {device} {action}", source=source)
        return True, None
    add_history("command_failed", force_save=True, device=device, action=action, source=source, mode="direct_tcp", error="Commande Modbus échouée")
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
    global last_rainbird_command_at
    if zone not in RAINBIRD_ZONES:
        return False, {"error": f"Zone {zone} invalide"}
    current = time.time()
    if current - last_rainbird_command_at < RAINBIRD_MIN_INTERVAL_SEC:
        remaining = int(RAINBIRD_MIN_INTERVAL_SEC - (current - last_rainbird_command_at)) + 1
        return False, {"error": f"Commande Rain Bird trop rapprochee: attendre {remaining}s"}
    duration = max(1, min(int(duration), RAINBIRD_MAX_DURATION_MIN))
    rainbird_state["last_cmd"] = f"START Zone {zone} {duration}min"
    if G781_MODE in HTTP_PUSH_MODES:
        if not http_push_is_fresh():
            return False, {"error": "Agent local absent ou trop ancien: Rain Bird refusé"}
        queue_command({"type": "rainbird", "action": "start", "zone": zone, "duration": duration})
        rainbird_state["last_cmd"] = f"QUEUED START Zone {zone} {duration}min"
        last_rainbird_command_at = current
        return True, {"mode": "http_push", "queued": True, "zone": zone, "duration": duration}
    if not rainbird_ip:
        if G781_MODE in SIMULATION_MODES:
            rainbird_state["active_zones"] = [zone]
            last_rainbird_command_at = current
            add_history("command_executed", force_save=True, type="rainbird", action="start", zone=zone, duration=duration, mode="simulation")
            return True, {"mode": "simulation", "zone": zone, "duration": duration}
        return False, {"error": "Rain Bird IP manquante: simulation refusée en mode réel"}
    result = rainbird_request(rainbird_ip, "ZoneStartRequest", {"zone": zone, "duration": duration})
    if result:
        rainbird_state["active_zones"] = [zone]
        rainbird_state["connected"] = True
        last_rainbird_command_at = current
        add_history("command_executed", force_save=True, type="rainbird", action="start", zone=zone, duration=duration, mode="direct")
        return True, {"result": result, "zone": zone, "duration": duration}
    add_history("command_failed", force_save=True, type="rainbird", action="start", zone=zone, duration=duration, error="Pas de réponse Rain Bird")
    return False, {"error": "Pas de réponse Rain Bird"}


def rainbird_stop_zone(zone: int | None = None) -> tuple[bool, dict]:
    rainbird_state["last_cmd"] = f"STOP {'zone ' + str(zone) if zone else 'tout'}"
    rainbird_state["active_zones"] = []
    if G781_MODE in HTTP_PUSH_MODES:
        queue_command({"type": "rainbird", "action": "stop", "zone": zone})
        return True, {"mode": "http_push", "queued": True, "zone": zone}
    if not rainbird_ip:
        return True, {"mode": "simulation"}
    result = rainbird_request(rainbird_ip, "StopIrrigationRequest")
    if result:
        rainbird_state["connected"] = True
        add_history("command_executed", force_save=True, type="rainbird", action="stop", zone=zone, mode="direct")
        return True, {"result": result}
    add_history("command_failed", force_save=True, type="rainbird", action="stop", zone=zone, error="Pas de réponse Rain Bird")
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


def load_history_from_disk() -> None:
    try:
        if not HISTORY_FILE.exists():
            return
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        items = data.get("history", data if isinstance(data, list) else [])
        history.clear()
        for item in items[:HISTORY_MAX_ITEMS]:
            if isinstance(item, dict):
                history.append(item)
        add_event("info", f"Historique chargé: {len(history)} entrée(s)")
    except Exception as exc:
        history.clear()
        add_event("warning", f"Historique non chargé: {exc}")



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
    if ENABLE_PLANNING and not scheduler_started:
        scheduler_started = True
        threading.Thread(target=planning_loop, daemon=True, name="planning-loop").start()


# ============ ASSISTANT IA LECTURE SEULE ============
def device_problem(label: str, data: dict) -> list[str]:
    findings = []
    status = str(data.get("status") or "")
    error_text = data.get("error_text")
    if "DÉCONNECT" in status or "DECONNECT" in status:
        findings.append(f"{label}: aucune lecture récente valide.")
    if error_text:
        findings.append(f"{label}: {error_text}.")
    return findings


def build_planning_suggestion(question: str) -> list[str]:
    text = question.lower()
    if not any(word in text for word in ("planning", "programme", "arros", "irrig", "demain", "olivier")):
        return []
    return [
        "Brouillon planning: privilégier une fenêtre tôt le matin, par exemple 06:00-08:00, avant forte chaleur.",
        "Séparer grands et petits oliviers si les débits par ligne sont différents.",
        "Garder le planning cloud désactivé tant que les démarrages, arrêts et sécurités locales ne sont pas validés terrain.",
        "Transformer ce brouillon en programme seulement après validation humaine dans l'onglet Planning.",
    ]


def build_ai_diagnostic(question: str) -> dict:
    q = str(question or "").strip()
    findings: list[str] = []
    recommendations: list[str] = []
    blocked = []

    with lock:
        connected = client_is_open()
        status_snapshot = {
            "connected": connected,
            "mode": cache.get("mode"),
            "last_update": cache.get("last_update"),
            "invt": dict(cache.get("invt", {})),
            "salmson": dict(cache.get("salmson", {})),
            "wilo": dict(cache.get("wilo", {})),
            "coffret4": dict(cache.get("coffret4", {})),
            "commands": command_status_counts(),
            "events": list(events)[:8],
        }

    if G781_MODE in HTTP_PUSH_MODES and not http_push_is_fresh():
        findings.append("Agent local absent ou données trop anciennes.")
        blocked.append("Aucun démarrage ne doit être proposé tant que l'agent n'est pas frais.")
    if not connected:
        findings.append("Serveur non connecté à une source terrain fraîche.")

    findings.extend(device_problem("INVT", status_snapshot["invt"]))
    findings.extend(device_problem("Salmson", status_snapshot["salmson"]))
    findings.extend(device_problem("Wilo", status_snapshot["wilo"]))

    salmson = status_snapshot["salmson"]
    if "float_low" in salmson and salmson.get("float_low") != SALMSON_FLOAT_LOW_OK_VALUE:
        blocked.append("Manque d'eau Salmson détecté par flotteur.")
    if status_snapshot["commands"].get("sent", 0):
        findings.append(f"{status_snapshot['commands']['sent']} commande(s) envoyée(s) en attente d'ACK agent.")
    if not MODBUS_REGISTERS_VALIDATED:
        blocked.append("Registres Modbus non validés: démarrages réels bloqués.")
    if not SALMSON_COMMAND_ENABLED:
        recommendations.append("Laisser Salmson en lecture/supervision tant que la table EC-L n'est pas confirmée.")

    if not findings:
        findings.append("Aucune anomalie évidente dans les données disponibles.")
    if blocked:
        recommendations.extend([f"Ne pas démarrer: {item}" for item in blocked])
    else:
        recommendations.append("Si un test est nécessaire, le préparer en mode maintenance, durée courte, avec validation humaine.")

    if any(word in q.lower() for word in ("démarre", "demarre", "start", "lance", "ouvre", "arrête", "arrete", "stop")):
        recommendations.insert(0, "Je ne peux pas exécuter de commande. Je peux seulement préparer une action à valider manuellement.")

    planning = build_planning_suggestion(q)
    risk_level = "danger" if blocked else ("warning" if findings else "ok")
    return {
        "mode": "read_only",
        "can_execute_commands": False,
        "risk_level": risk_level,
        "summary": "Assistant IA Zarzis: diagnostic lecture seule, aucune écriture Modbus.",
        "question": q,
        "findings": findings,
        "recommendations": recommendations,
        "planning_suggestion": planning,
        "status": status_snapshot,
    }


# ============ API ============
@app.route("/api/ping")
def ping():
    connected = client_is_open()
    return jsonify(
        {
            "status": "ok",
            "version": APP_VERSION,
            "mode": G781_MODE,
            "connected": connected,
            "g781_ip": cache["g781_ip"],
            "edge_agent": cache["g781_ip"],
            "auth_required": bool(API_TOKEN),
            "server_time": local_now().isoformat(),
            "timezone": LOCAL_TZ_NAME,
            "planning_enabled": ENABLE_PLANNING,
            "planning_count": len(planning),
            "http_push_stale_sec": G781_HTTP_PUSH_STALE_SEC if G781_MODE in HTTP_PUSH_MODES else None,
            "storage": {"data_dir": str(DATA_DIR), "persistent": PERSISTENT_STORAGE_ENABLED, "history_file": str(HISTORY_FILE), "app_state_file": str(APP_STATE_FILE)},
            "simulation": G781_MODE in SIMULATION_MODES,
        }
    )


@app.route("/api/status")
def status():
    with lock:
        connected = client_is_open()
        if G781_MODE in HTTP_PUSH_MODES:
            cache["connected"] = connected
        command_counts = command_status_counts()
        return jsonify(
            {
                "connected": connected,
                "last_update": cache["last_update"],
                "g781_ip": cache["g781_ip"],
                "edge_agent": cache["g781_ip"],
                "mode": cache["mode"],
                "server_time": local_now().isoformat(),
                "timezone": LOCAL_TZ_NAME,
                "planning_enabled": ENABLE_PLANNING,
                "planning_count": len(planning),
                "http_push_stale_sec": G781_HTTP_PUSH_STALE_SEC if G781_MODE in HTTP_PUSH_MODES else None,
                "commands_pending": len(pending_commands),
                "commands_status": command_counts,
                "last_command_ack": recent_command_acks[0] if recent_command_acks else None,
                "recent_command_acks": list(recent_command_acks)[:20],
                "storage": {"data_dir": str(DATA_DIR), "persistent": PERSISTENT_STORAGE_ENABLED, "history_file": str(HISTORY_FILE), "app_state_file": str(APP_STATE_FILE)},
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
    if ALLOW_REMOTE_G781_CONNECT:
        host = str(body.get("host") or body.get("ip") or "").strip()
        port = parse_int(body.get("port"), G781_PORT)
    else:
        host = G781_HOST
        port = G781_PORT
    if not host and G781_MODE not in SIMULATION_MODES and G781_MODE not in HTTP_PUSH_MODES:
        return jsonify({"success": False, "error": "Cible Modbus TCP non configuree cote serveur"}), 400
    ok = connect(host, port)
    return jsonify(
        {
            "success": ok,
            "error": None if ok else "Connexion Modbus TCP echouee",
            "host": host,
            "ip": host,
            "port": port,
            "mode": G781_MODE,
            "remote_target_allowed": ALLOW_REMOTE_G781_CONNECT,
        }
    ), 200 if ok else 503


@app.route("/api/control", methods=["POST"])
def control():
    body = request.get_json(silent=True) or {}
    device = str(body.get("device") or body.get("pump") or "").strip().lower()
    action = str(body.get("action") or "").strip().lower()
    source = str(body.get("source") or "api").strip().lower()
    if source.startswith("ai"):
        return jsonify({"success": False, "error": "IA en lecture seule: commande directe interdite"}), 403
    if not device or not action:
        return jsonify({"success": False, "error": "device/pump et action requis"}), 400
    ok, error = apply_control(device, action, source=source)
    status_code = 200 if ok else (429 if error and ("attendre" in error or "trop" in error) else (503 if error == "Cible Modbus TCP non connectee" else 400))
    queued = bool(ok and G781_MODE in HTTP_PUSH_MODES)
    executed = bool(ok and not queued)
    return jsonify({
        "success": ok,
        "device": device,
        "pump": device,
        "action": action,
        "error": error,
        "queued": queued,
        "executed": executed,
        "pending_ack": queued,
        "command_status": "queued" if queued else ("executed" if executed else "rejected"),
        "message": "Commande mise en file, attente ACK agent" if queued else ("Commande executee" if executed else error),
    }), status_code


@app.route("/api/inverter", methods=["POST"])
def inverter():
    body = request.get_json(silent=True) or {}
    action = str(body.get("action") or "stop").strip().lower()
    source = str(body.get("source") or "api").strip().lower()
    if source.startswith("ai"):
        return jsonify({"success": False, "error": "IA en lecture seule: commande directe interdite"}), 403
    if action not in INVT_ACTIONS:
        return jsonify({"success": False, "error": f"Action INVT inconnue: {action}"}), 400
    ok, error = apply_control("invt", action, source=source)
    status_code = 200 if ok else (429 if error and ("attendre" in error or "trop" in error) else (503 if error == "Cible Modbus TCP non connectee" else 400))
    queued = bool(ok and G781_MODE in HTTP_PUSH_MODES)
    executed = bool(ok and not queued)
    return jsonify({
        "success": ok,
        "device": "invt",
        "action": action,
        "error": error,
        "queued": queued,
        "executed": executed,
        "pending_ack": queued,
        "command_status": "queued" if queued else ("executed" if executed else "rejected"),
        "message": "Commande mise en file, attente ACK agent" if queued else ("Commande executee" if executed else error),
    }), status_code


@app.route("/api/param/read", methods=["POST"])
def param_read():
    body = request.get_json(silent=True) or {}
    addr = parse_int(body.get("addr"), 1)
    reg = parse_int(body.get("reg"), 0)
    count = min(max(parse_int(body.get("count"), 1), 1), 64)
    if G781_MODE in HTTP_PUSH_MODES:
        return jsonify({"success": False, "error": "Lecture registre brute indisponible en HTTP PUSH: utiliser l'agent local ou qModMaster sur site"}), 409
    if not client_is_open():
        return jsonify({"success": False, "error": "Cible Modbus TCP non connectee"}), 503
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
    if str(body.get("source") or "").strip().lower().startswith("ai"):
        return jsonify({"success": False, "error": "IA en lecture seule: écriture paramètre interdite"}), 403
    addr = parse_int(body.get("addr"), 1)
    reg = parse_int(body.get("reg"), 0)
    value = parse_int(body.get("value"), 0)
    if G781_MODE in HTTP_PUSH_MODES:
        return jsonify({"success": False, "error": "Ecriture registre brute indisponible en HTTP PUSH depuis le cloud"}), 409
    if not client_is_open():
        return jsonify({"success": False, "error": "Cible Modbus TCP non connectee"}), 503
    with lock:
        ok = write_reg(addr, reg, value)
    if ok:
        add_event("info", f"Écriture registre OK addr={addr} reg={hex(reg)} val={value}")
        return jsonify({"success": True, "reg": hex(reg), "addr": addr, "value": value})
    return jsonify({"success": False, "error": "Écriture échouée", "reg": hex(reg), "addr": addr}), 400


@app.route("/api/planning", methods=["GET", "POST"])
def api_planning():
    global planning
    if not ENABLE_PLANNING:
        if request.method == "GET":
            return jsonify(
                {
                    "planning": [],
                    "enabled": False,
                    "timezone": LOCAL_TZ_NAME,
                    "server_time": local_now().isoformat(),
                }
            )
        return jsonify({"success": False, "enabled": False, "error": "Planning desactive cote serveur"}), 403

    if request.method == "GET":
        return jsonify(
            {
                "planning": planning,
                "enabled": True,
                "timezone": LOCAL_TZ_NAME,
                "server_time": local_now().isoformat(),
            }
        )

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
    return jsonify({"success": True, "enabled": True, "planning": planning, "timezone": LOCAL_TZ_NAME})



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
        app_state_updated_at = now_iso()
        save_app_state_to_disk()
    add_event("info", f"État partagé synchronisé: revision {app_state_revision}")
    return jsonify({"success": True, "revision": app_state_revision, "updated_at": app_state_updated_at, "state": app_state})


@app.route("/api/events")
def api_events():
    limit = min(parse_int(request.args.get("limit"), 50), 300)
    return jsonify({"events": list(events)[:limit]})


@app.route("/api/history")
def api_history():
    limit = min(parse_int(request.args.get("limit"), 100), HISTORY_MAX_ITEMS)
    kind = str(request.args.get("kind") or "").strip()
    items = list(history)
    if kind:
        items = [item for item in items if item.get("kind") == kind]
    return jsonify({"history": items[:limit], "count": len(items), "max": HISTORY_MAX_ITEMS})


@app.route("/api/ai/diagnose", methods=["POST"])
def ai_diagnose():
    body = request.get_json(silent=True) or {}
    question = str(body.get("question") or body.get("message") or "").strip()
    diagnostic = build_ai_diagnostic(question)
    add_history(
        "ai_diagnostic",
        force_save=True,
        question=question,
        risk_level=diagnostic["risk_level"],
        findings=diagnostic["findings"][:5],
        recommendations=diagnostic["recommendations"][:5],
    )
    return jsonify({"success": True, **diagnostic})


@app.route("/api/edge/push", methods=["POST"])
@app.route("/api/g781/push", methods=["POST"])
def g781_push():
    body = request.get_json(silent=True) or {}
    with lock:
        payload = body.get("devices") if isinstance(body.get("devices"), dict) else body
        for key in ("invt", "salmson", "wilo", "coffret4"):
            item = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(item, dict):
                cache[key] = dict(item)
                if "last_seen" not in item:
                    cache[key]["last_seen"] = now_iso()
        if isinstance(body.get("rainbird"), dict):
            rainbird_state.update(body["rainbird"])
        cache["connected"] = True
        cache["mode"] = G781_MODE
        cache["g781_ip"] = str(body.get("agent_id") or request.headers.get("X-Forwarded-For") or request.remote_addr or "agent local")
        cache["last_update"] = time.time()
        cleanup_pending_commands()
        queued = len(pending_commands)
        snapshot = {
            "agent_id": cache["g781_ip"],
            "devices": {key: dict(cache[key]) for key in ("invt", "salmson", "wilo", "coffret4")},
            "rainbird": dict(rainbird_state),
        }
    add_history("measurement", **snapshot)
    add_event("info", "Push agent local reçu")
    return jsonify({"success": True, "commands_pending": queued, "server_time": local_now().isoformat()})


@app.route("/api/edge/commands")
@app.route("/api/g781/commands")
def g781_commands():
    agent_id = request.headers.get("X-Agent-ID") or request.args.get("agent_id") or request.headers.get("User-Agent") or "agent local"
    with lock:
        cleanup_pending_commands()
        current = time.time()
        commands = []
        for cmd in pending_commands:
            status = str(cmd.get("status") or "queued")
            last_fetch_at = float(cmd.get("last_fetch_at") or 0)
            can_send = status == "queued"
            if status == "sent" and command_can_retry(cmd) and current - last_fetch_at >= G781_COMMAND_ACK_TIMEOUT_SEC:
                can_send = True
            if not can_send:
                continue
            cmd["status"] = "sent"
            cmd["last_fetch_at"] = current
            cmd["fetched_at"] = now_iso()
            cmd["agent_id"] = agent_id
            cmd["attempts"] = int(cmd.get("attempts", 0)) + 1
            commands.append(command_snapshot(cmd))
        if commands:
            add_history("command_sent", agent_id=agent_id, commands=commands, force_save=True)
    return jsonify({"success": True, "commands": commands, "count": len(commands), "server_time": local_now().isoformat()})


@app.route("/api/edge/ack", methods=["POST"])
@app.route("/api/g781/ack", methods=["POST"])
def g781_ack():
    body = request.get_json(silent=True) or {}
    command_id = str(body.get("id") or body.get("command_id") or "").strip()
    agent_id = str(body.get("agent_id") or request.headers.get("X-Agent-ID") or request.headers.get("User-Agent") or "agent local")
    if not command_id:
        return jsonify({"success": False, "error": "id commande manquant"}), 400
    with lock:
        matched = None
        kept = []
        for cmd in pending_commands:
            if str(cmd.get("id")) == command_id:
                matched = cmd
                continue
            kept.append(cmd)
        pending_commands.clear()
        pending_commands.extend(kept)
    ack = {
        "id": command_id,
        "ts": now_iso(),
        "agent_id": agent_id,
        "ok": bool(body.get("ok", body.get("success", False))),
        "error": str(body.get("error") or ""),
        "result": body.get("result") if isinstance(body.get("result"), dict) else {},
        "command": command_snapshot(matched) if matched else None,
    }
    if ack["command"]:
        ack["type"] = ack["command"].get("type")
        ack["device"] = ack["command"].get("device")
        ack["action"] = ack["command"].get("action")
        if ack["ok"] and ack["type"] == "rainbird":
            if ack["action"] == "start":
                rainbird_state["active_zones"] = [ack["command"].get("zone")]
            elif ack["action"] == "stop":
                rainbird_state["active_zones"] = []
            rainbird_state["last_cmd"] = f"ACK {ack['action']} OK"
    recent_command_acks.appendleft(ack)
    add_history("command_ack", force_save=True, **ack)
    if ack["command"]:
        history_kind = "command_executed" if ack["ok"] else "command_failed"
        add_history(
            history_kind,
            force_save=True,
            mode="http_push",
            agent_id=agent_id,
            device=ack.get("device"),
            action=ack.get("action"),
            error=ack["error"],
            command_id=command_id,
            result=ack["result"],
        )
    add_event("info" if ack["ok"] else "warning", f"ACK agent commande {command_id}: {'OK' if ack['ok'] else ack['error'] or 'ECHEC'}")
    return jsonify({"success": True, "known": matched is not None, "ack": ack})


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
    if str(body.get("source") or "").strip().lower().startswith("ai"):
        return jsonify({"success": False, "error": "IA en lecture seule: configuration Rain Bird interdite"}), 403
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
    if str(body.get("source") or "").strip().lower().startswith("ai"):
        return jsonify({"success": False, "error": "IA en lecture seule: commande Rain Bird interdite"}), 403
    zone = parse_int(body.get("zone"), 1)
    duration = parse_int(body.get("duration"), 10)
    ok, info = rainbird_start_zone(zone, duration)
    if ok:
        return jsonify({"success": True, **info})
    return jsonify({"success": False, **info}), 503 if info.get("error") == "Pas de réponse Rain Bird" else 400


@app.route("/api/rainbird/stop", methods=["POST"])
def rainbird_stop():
    body = request.get_json(silent=True) or {}
    if str(body.get("source") or "").strip().lower().startswith("ai"):
        return jsonify({"success": False, "error": "IA en lecture seule: commande Rain Bird interdite"}), 403
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
    <p>Mode terrain: <strong>{G781_MODE}</strong></p>
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


load_history_from_disk()
load_app_state_from_disk()
load_planning_from_disk()
start_background_threads()


if __name__ == "__main__":
    log.info("=" * 50)
    log.info("  ZARZIS CLOUD SERVER - MODBUS PROXY")
    log.info("=" * 50)
    app.run(host="0.0.0.0", port=SERVER_PORT, debug=False)
