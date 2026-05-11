#!/usr/bin/env python3
"""
Agent local Zarzis pour le mode http_push.

Il tourne sur le site (PC local, mini PC ou Raspberry Pi), lit le DR302 en
Modbus TCP sur le reseau local, pousse les mesures vers Render en HTTPS, puis
recupere les commandes en attente.

Par defaut, les demarrages sont bloques cote agent avec EDGE_ALLOW_START=false.
Les arrets restent autorises. Passer EDGE_ALLOW_START=true seulement apres test
qModMaster et validation terrain.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse, urlunparse

from pymodbus.client import ModbusTcpClient

try:
    import websocket as websocket_client
except Exception:  # Optionnel: l'ancien HTTP PUSH reste disponible
    websocket_client = None


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


def parse_int(value, default=0) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, int):
        return value
    text = str(value).strip()
    return int(text, 16) if text.lower().startswith("0x") else int(float(text))


CLOUD_URL = os.environ.get("CLOUD_URL", "https://zarzis-irrigation-1.onrender.com").rstrip("/")
API_TOKEN = os.environ.get("API_TOKEN", "").strip()
AGENT_ID = os.environ.get("AGENT_ID", "zarzis-edge-agent")

EDGE_WS_ENABLED = env_bool("EDGE_WS_ENABLED", True)
EDGE_HEARTBEAT_SEC = max(5, env_int("EDGE_HEARTBEAT_SEC", 10))
EDGE_WS_RECONNECT_MIN_SEC = max(2, env_int("EDGE_WS_RECONNECT_MIN_SEC", 3))
EDGE_WS_RECONNECT_MAX_SEC = max(10, env_int("EDGE_WS_RECONNECT_MAX_SEC", 30))

DR302_HOST = os.environ.get("DR302_HOST", "192.168.1.10")
DR302_PORT = env_int("DR302_PORT", 502)
POLL_SEC = max(1, env_int("EDGE_POLL_SEC", 2))
COMMAND_POLL_SEC = max(1, env_int("EDGE_COMMAND_POLL_SEC", 1))
STATUS_PUSH_SEC = max(2, env_int("EDGE_STATUS_PUSH_SEC", 5))
MODBUS_TIMEOUT_SEC = max(0.5, env_float("MODBUS_TIMEOUT_SEC", 1.5))
DATA_DIR = Path(os.environ.get("EDGE_DATA_DIR", os.environ.get("DATA_DIR", str(Path.home() / "zarzis-data"))))
LOG_DIR = Path(os.environ.get("EDGE_LOG_DIR", str(DATA_DIR / "logs")))
OFFLINE_DB = Path(os.environ.get("EDGE_OFFLINE_DB", str(DATA_DIR / "edge_offline_queue.sqlite3")))
OFFLINE_BUFFER_ENABLED = env_bool("EDGE_OFFLINE_BUFFER_ENABLED", True)
OFFLINE_MAX_ITEMS = max(100, env_int("EDGE_OFFLINE_MAX_ITEMS", 10000))
EDGE_ALLOW_START = env_bool("EDGE_ALLOW_START", False)
SALMSON_COMMAND_ENABLED = env_bool("SALMSON_COMMAND_ENABLED", False)
SALMSON_FLOAT_LOW_OK_VALUE = env_int("SALMSON_FLOAT_LOW_OK_VALUE", 1)
INVT_NOMINAL_KW = env_float("INVT_NOMINAL_KW", 5.5)

ADDR_INVT = env_int("ADDR_INVT", 1)
ADDR_SALMSON = env_int("ADDR_SALMSON", 2)
ADDR_WILO = env_int("ADDR_WILO", 3)
ADDR_COFFRET4 = env_int("ADDR_COFFRET4", 4)

INVT_CMD = env_int("INVT_CMD_REG", 0x2000)
INVT_ACTIONS = {
    "on": env_int("INVT_ON_VALUE", 1),
    "forward": env_int("INVT_FORWARD_VALUE", 1),
    "reverse": env_int("INVT_REVERSE_VALUE", 2),
    "off": env_int("INVT_OFF_VALUE", 5),
}
INVT_REGS = {
    "freq_hz": env_int("INVT_REG_FREQ_HZ", 0x3000),
    "set_freq_hz": env_int("INVT_REG_SET_FREQ_HZ", 0x3001),
    "dc_bus_v": env_int("INVT_REG_DC_BUS_V", 0x3002),
    "voltage_v": env_int("INVT_REG_VOLTAGE_V", 0x3003),
    "current_a": env_int("INVT_REG_CURRENT_A", 0x3004),
    "power_pct": env_int("INVT_REG_POWER_PCT", 0x3006),
    "fault_code": env_int("INVT_REG_FAULT_CODE", 0x5000),
}

# Salmson EC-L / EC-Lift (profil Wilo-Control EC-L, Fieldbuslist Modbus EC).
# Adresses zero-based: 40015 => 14, 40026 => 25, 40198 => 197.
SALMSON_CMD = env_int("SALMSON_CMD_REG", 14)
SALMSON_REGS = {
    "level_cm": env_int("SALMSON_REG_LEVEL_CM", 25),
    "pump1_mode": env_int("SALMSON_REG_PUMP1_MODE", 40),
    "pump2_mode": env_int("SALMSON_REG_PUMP2_MODE", 41),
    "switch_state": env_int("SALMSON_REG_SWITCH_STATE", 61),
    "error_code": env_int("SALMSON_REG_ERROR_CODE", 138),
    "float_state": env_int("SALMSON_REG_FLOAT_STATE", 197),
}

WILO_CMD = env_int("WILO_CMD_REG", 14)
WILO_REGS = {
    "pressure": env_int("WILO_REG_PRESSURE", 25),
    "flow": env_int("WILO_REG_FLOW", -1),
    "pump1_mode": env_int("WILO_REG_PUMP1_MODE", 40),
    "pump2_mode": env_int("WILO_REG_PUMP2_MODE", 41),
    "switch_state": env_int("WILO_REG_SWITCH_STATE", 61),
    "error_code": env_int("WILO_REG_ERROR_CODE", 138),
}

RELAY_ENABLED = env_bool("RELAY_ENABLED", True)
RELAY_ACTIVE_LOW = env_bool("RELAY_ACTIVE_LOW", True)  # RUNCCI-YUN = actif bas par défaut
RELAY_MAX_DURATION_MIN = max(1, env_int("RELAY_MAX_DURATION_MIN", 120))
RELAY_ZONE_PINS = {
    1: env_int("RELAY_ZONE_1_PIN", 17),
    2: env_int("RELAY_ZONE_2_PIN", 27),
    3: env_int("RELAY_ZONE_3_PIN", 22),
    4: env_int("RELAY_ZONE_4_PIN", 23),
    5: env_int("RELAY_ZONE_5_PIN", 24),
    6: env_int("RELAY_ZONE_6_PIN", 25),
}
relay_state: dict = {"zones": {}, "active_zones": [], "last_cmd": ""}

# Initialisation GPIO — silencieuse si pas sur Raspberry
RELAY_GPIO_AVAILABLE = False
try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    for _zone, _pin in RELAY_ZONE_PINS.items():
        GPIO.setup(_pin, GPIO.OUT)
        GPIO.output(_pin, GPIO.HIGH if RELAY_ACTIVE_LOW else GPIO.LOW)
    RELAY_GPIO_AVAILABLE = True
    print(f"[RELAY] GPIO initialisé — {len(RELAY_ZONE_PINS)} zones, actif_bas={RELAY_ACTIVE_LOW}")
except Exception as _exc:
    print(f"[RELAY] GPIO non disponible (normal hors Raspberry): {_exc}")


client: ModbusTcpClient | None = None
modbus_lock = threading.RLock()
state_lock = threading.RLock()
last_status_snapshot: dict = {}
last_status_at = 0.0
ws_connected = False
ws_last_seen_at = 0.0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    line = f"{now_iso()} {message}"
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (LOG_DIR / "zarzis_edge_agent.log").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def headers() -> dict[str, str]:
    result = {"Content-Type": "application/json", "User-Agent": AGENT_ID, "X-Agent-ID": AGENT_ID}
    if API_TOKEN:
        result["Authorization"] = f"Bearer {API_TOKEN}"
    return result


def cloud_ws_url() -> str:
    parsed = urlparse(CLOUD_URL)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    query = {"agent_id": AGENT_ID}
    # Le header Authorization est aussi envoyé, mais le token en query sert de secours
    # pour les proxys/clients WebSocket qui ne conservent pas les headers custom.
    if API_TOKEN:
        query["token"] = API_TOKEN
    return urlunparse((scheme, parsed.netloc, "/api/edge/ws", "", urlencode(query, quote_via=quote), ""))


def post_json(path: str, payload: dict, timeout: int = 15) -> dict:
    req = urllib.request.Request(
        f"{CLOUD_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers(),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(path: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(f"{CLOUD_URL}{path}", headers=headers(), method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ensure_modbus() -> bool:
    global client
    try:
        if client is not None:
            connected = getattr(client, "connected", False)
            if connected:
                return True
            try:
                client.close()
            except Exception:
                pass
        client = ModbusTcpClient(DR302_HOST, port=DR302_PORT, timeout=MODBUS_TIMEOUT_SEC)
        ok = bool(client.connect())
        if not ok:
            log(f"Modbus indisponible sur {DR302_HOST}:{DR302_PORT}")
        return ok
    except Exception as exc:
        log(f"Erreur connexion Modbus: {exc}")
        return False


def read_regs(addr: int, reg: int, count: int = 1) -> list[int] | None:
    if reg < 0:
        return None
    with modbus_lock:
        if not ensure_modbus():
            return None
        try:
            try:
                result = client.read_holding_registers(reg, count=count, slave=addr)
            except TypeError:
                result = client.read_holding_registers(reg, count=count, unit=addr)
            if result and not result.isError():
                return list(result.registers)
        except Exception as exc:
            log(f"Lecture impossible addr={addr} reg={hex(reg)}: {exc}")
    return None


def write_reg(addr: int, reg: int, value: int) -> bool:
    with modbus_lock:
        if not ensure_modbus():
            return False
        try:
            try:
                result = client.write_register(reg, value, slave=addr)
            except TypeError:
                result = client.write_register(reg, value, unit=addr)
            return bool(result and not result.isError())
        except Exception as exc:
            log(f"Ecriture impossible addr={addr} reg={hex(reg)} val={value}: {exc}")
            return False


def unavailable(name: str) -> dict:
    return {"status": "DECONNECTE", "running": False, "error_text": f"{name} non lu par l'agent local", "last_seen": now_iso()}


def read_invt() -> dict:
    data: dict[str, float | int | bool | str | None] = {}
    for name, reg in INVT_REGS.items():
        values = read_regs(ADDR_INVT, reg)
        if not values:
            continue
        raw = values[0]
        if name in {"freq_hz", "set_freq_hz"}:
            data[name] = round(raw / 100, 2)
        elif name in {"current_a", "dc_bus_v", "power_pct"}:
            data[name] = round(raw / 10, 1)
        else:
            data[name] = raw
    if not data:
        return unavailable("INVT")
    if "power_pct" in data:
        data["power_kw"] = round((float(data["power_pct"]) / 100) * INVT_NOMINAL_KW, 2)
    fault = int(data.get("fault_code") or 0)
    running = float(data.get("freq_hz") or 0) > 0.5
    data["running"] = running
    data["status"] = "ERREUR" if fault else ("EN MARCHE" if running else "ARRET")
    data["error_text"] = f"Defaut INVT {fault}" if fault else None
    data["last_seen"] = now_iso()
    return data


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
    return " / ".join(active) if active else f"Defaut Salmson {bits}"


def enrich_salmson_data(data: dict) -> dict:
    switch_state = int(data.get("switch_state") or 0)
    running = bool(switch_state & ((1 << 0) | (1 << 8) | (1 << 9)))
    data["pump_state"] = 1 if running else 0
    data["running"] = running
    data["running_source"] = "Salmson EC-L switch_box_state"
    if "float_state" in data:
        float_state = int(data.get("float_state") or 0)
        dry_run = bool(float_state & (1 << 0))
        high_water = bool(float_state & (1 << 4))
        data["dry_run"] = dry_run
        data["high_water"] = high_water
        data["float_low"] = 0 if dry_run else SALMSON_FLOAT_LOW_OK_VALUE
        data["float_high"] = 1 if high_water else 0
    data["current_a"] = data.get("current_a", 0)
    return data


def read_salmson() -> dict:
    data: dict[str, float | int | bool | str | None] = {}
    for name, reg in SALMSON_REGS.items():
        if reg < 0:
            continue
        count = 2 if name == "error_code" else 1
        values = read_regs(ADDR_SALMSON, reg, count=count)
        if not values:
            continue
        raw = values[0] if count == 1 else (values[0] | (values[1] << 16))
        data[name] = raw
    if not data:
        return unavailable("Salmson")
    error = int(data.get("error_code") or 0)
    enrich_salmson_data(data)
    data["status"] = "ERREUR" if error else ("EN MARCHE" if data["running"] else "ARRET")
    data["error_text"] = decode_salmson_error_bitmap(error) if error else None
    data["last_seen"] = now_iso()
    return data


def read_wilo() -> dict:
    data: dict[str, float | int | bool | str | None] = {}
    for name, reg in WILO_REGS.items():
        count = 2 if name == "error_code" else 1
        values = read_regs(ADDR_WILO, reg, count=count)
        if not values:
            continue
        raw = values[0] if count == 1 else (values[0] | (values[1] << 16))
        data[name] = round(raw / 10, 1) if name in {"pressure", "flow"} else raw
    if not data:
        return unavailable("Wilo")
    switch_state = int(data.get("switch_state") or 0)
    error = int(data.get("error_code") or 0)
    data["running"] = bool(switch_state & 0x01)
    data["status"] = "ERREUR" if error else ("EN MARCHE" if data["running"] else "ARRET")
    data["error_text"] = f"Defaut Wilo {error}" if error else None
    data["last_seen"] = now_iso()
    return data


def read_coffret4() -> dict:
    return {"status": "NON CONFIGURE", "last_seen": now_iso()}


def relay_set_zone(zone: int, active: bool) -> bool:
    """Active ou désactive un relais GPIO pour une zone électrovanne."""
    if not RELAY_ENABLED or zone not in RELAY_ZONE_PINS:
        return False
    pin = RELAY_ZONE_PINS[zone]
    relay_state["zones"][str(zone)] = active
    if active and zone not in relay_state["active_zones"]:
        relay_state["active_zones"].append(zone)
    elif not active and zone in relay_state["active_zones"]:
        relay_state["active_zones"].remove(zone)
    if not RELAY_GPIO_AVAILABLE:
        log(f"[SIMULATION GPIO] Zone {zone} pin GPIO{pin} → {'ON' if active else 'OFF'}")
        return True
    try:
        level = GPIO.LOW if (active and RELAY_ACTIVE_LOW) or (not active and not RELAY_ACTIVE_LOW) else GPIO.HIGH
        GPIO.output(pin, level)
        log(f"Relais zone {zone} pin GPIO{pin} → {'ON' if active else 'OFF'}")
        return True
    except Exception as exc:
        log(f"Erreur GPIO zone {zone} pin {pin}: {exc}")
        return False


def relay_stop_all() -> bool:
    results = [relay_set_zone(z, False) for z in RELAY_ZONE_PINS]
    relay_state["active_zones"] = []
    relay_state["last_cmd"] = "STOP TOUTES ZONES"
    return all(results)


def execute_relay_command(cmd: dict) -> dict:
    action = str(cmd.get("action") or "").lower()
    zone_raw = cmd.get("zone")
    zone = int(zone_raw) if zone_raw not in (None, "") else None
    duration = max(1, min(int(cmd.get("duration") or 20), RELAY_MAX_DURATION_MIN))
    if action == "start" and zone:
        if zone not in RELAY_ZONE_PINS:
            return {"ok": False, "type": "relay", "error": f"Zone {zone} invalide — zones disponibles: {list(RELAY_ZONE_PINS.keys())}"}
        ok = relay_set_zone(zone, True)
        relay_state["last_cmd"] = f"START Zone {zone} {duration}min"
        if ok:
            import threading
            def _auto_stop(z: int, d: int) -> None:
                time.sleep(d * 60)
                relay_set_zone(z, False)
                log(f"Auto-arrêt zone {z} après {d}min")
            threading.Thread(target=_auto_stop, args=(zone, duration), daemon=True, name=f"relay-auto-stop-z{zone}").start()
        return {"ok": ok, "type": "relay", "action": "start", "zone": zone, "duration": duration}
    if action == "stop":
        if zone:
            ok = relay_set_zone(zone, False)
            relay_state["last_cmd"] = f"STOP Zone {zone}"
            return {"ok": ok, "type": "relay", "action": "stop", "zone": zone}
        ok = relay_stop_all()
        return {"ok": ok, "type": "relay", "action": "stop", "zone": None}
    return {"ok": False, "type": "relay", "error": f"Action relais inconnue: {action}"}


def command_targets(device: str) -> list[str]:
    if device == "forage":
        return ["invt", "salmson"]
    if device == "all":
        return ["invt", "salmson", "wilo"]
    if device in {"invt", "salmson", "wilo"}:
        return [device]
    return [device]


def safe_int(value, default: int | None = None) -> int | None:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def reading_unavailable(data: dict) -> bool:
    status = str(data.get("status") or "").lower()
    error_text = str(data.get("error_text") or "").lower()
    markers = ("deconnect", "non lu", "aucune lecture", "indisponible")
    return not data or any(marker in status or marker in error_text for marker in markers)


def local_start_blockers(target: str, data: dict) -> list[str]:
    blockers: list[str] = []
    if reading_unavailable(data):
        return [f"{target}: lecture critique absente"]

    if target == "invt":
        if "fault_code" not in data:
            blockers.append("INVT: registre defaut non lu")
        elif safe_int(data.get("fault_code"), 0):
            blockers.append(data.get("error_text") or f"Defaut INVT {data.get('fault_code')}")

    elif target == "salmson":
        if "error_code" not in data:
            blockers.append("Salmson: registre defaut non lu")
        elif safe_int(data.get("error_code"), 0):
            blockers.append(data.get("error_text") or f"Defaut Salmson {data.get('error_code')}")
        if "float_low" not in data:
            blockers.append("Salmson: flotteur manque d'eau non lu")
        elif safe_int(data.get("float_low")) != SALMSON_FLOAT_LOW_OK_VALUE:
            blockers.append("Manque d'eau Salmson")

    elif target == "wilo":
        missing = [name for name in ("error_code", "switch_state", "pressure") if name not in data]
        if missing:
            blockers.append("Wilo: registre critique non lu (" + ", ".join(missing) + ")")
        elif safe_int(data.get("error_code"), 0):
            blockers.append(data.get("error_text") or f"Defaut Wilo {data.get('error_code')}")

    return blockers


def local_command_blockers(device: str, action: str) -> list[str]:
    if action not in {"on", "forward", "reverse"}:
        return []
    with state_lock:
        snapshot = dict(last_status_snapshot) if isinstance(last_status_snapshot, dict) else {}
        snapshot_at = float(last_status_at or 0)
    if time.time() - snapshot_at > max(POLL_SEC * 3, 15):
        return ["Mesures locales trop anciennes"]
    devices = snapshot.get("devices") if isinstance(snapshot, dict) else {}
    blockers: list[str] = []
    for target in command_targets(device):
        blockers.extend(local_start_blockers(target, devices.get(target, {}) if isinstance(devices, dict) else {}))
    return blockers


def execute_command(cmd: dict) -> dict:
    if cmd.get("type") == "relay":
        result = execute_relay_command(cmd)
        log(f"Commande relais executee={result.get('ok')}: zone={result.get('zone')} action={result.get('action')}")
        return result
    if cmd.get("type") != "control":
        return {"ok": False, "error": f"Type commande inconnu: {cmd.get('type')}"}
    device = str(cmd.get("device") or "").lower()
    action = str(cmd.get("action") or "").lower()
    if action == "start":
        action = "on"
    if action == "stop":
        action = "off"
    is_start = action in {"on", "forward", "reverse"}
    if is_start and not EDGE_ALLOW_START:
        error = f"Commande bloquee par EDGE_ALLOW_START=false: {device} {action}"
        log(error)
        return {"ok": False, "type": "control", "device": device, "action": action, "error": error}
    blockers = local_command_blockers(device, action)
    if blockers:
        error = "Commande bloquee fail-closed: " + " / ".join(blockers)
        log(error)
        return {"ok": False, "type": "control", "device": device, "action": action, "error": error}

    relay_value = 1 if is_start else 0
    invt_value = INVT_ACTIONS.get(action, INVT_ACTIONS["on"] if is_start else INVT_ACTIONS["off"])
    ok = True

    if device in {"invt", "forage", "all"}:
        ok = write_reg(ADDR_INVT, INVT_CMD, invt_value) and ok
    if device in {"wilo", "all"}:
        ok = write_reg(ADDR_WILO, WILO_CMD, relay_value) and ok
    if device == "salmson" or (device in {"forage", "all"} and SALMSON_COMMAND_ENABLED):
        if SALMSON_COMMAND_ENABLED:
            ok = write_reg(ADDR_SALMSON, SALMSON_CMD, relay_value) and ok
        else:
            log("Commande Salmson ignoree: SALMSON_COMMAND_ENABLED=false")

    error = None if ok else "Ecriture Modbus echouee"
    log(f"Commande executee={ok}: {device} {action}")
    return {"ok": ok, "type": "control", "device": device, "action": action, "error": error}


def init_offline_db() -> None:
    if not OFFLINE_BUFFER_ENABLED:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(OFFLINE_DB) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS status_queue ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "created_at TEXT NOT NULL, "
            "payload TEXT NOT NULL, "
            "sent_at TEXT)"
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_status_queue_sent ON status_queue(sent_at, id)")
        db.commit()


def trim_offline_queue() -> None:
    if not OFFLINE_BUFFER_ENABLED:
        return
    try:
        with sqlite3.connect(OFFLINE_DB) as db:
            db.execute(
                "DELETE FROM status_queue WHERE id NOT IN "
                "(SELECT id FROM status_queue ORDER BY id DESC LIMIT ?)",
                (OFFLINE_MAX_ITEMS,),
            )
            db.commit()
    except Exception as exc:
        log(f"Buffer offline trim impossible: {exc}")


def enqueue_status_payload(payload: dict) -> None:
    if not OFFLINE_BUFFER_ENABLED:
        return
    try:
        init_offline_db()
        with sqlite3.connect(OFFLINE_DB) as db:
            db.execute(
                "INSERT INTO status_queue(created_at, payload, sent_at) VALUES (?, ?, NULL)",
                (now_iso(), json.dumps(payload, ensure_ascii=False)),
            )
            db.commit()
        trim_offline_queue()
    except Exception as exc:
        log(f"Buffer offline ecriture impossible: {exc}")


def mark_status_payload_sent(row_id: int) -> None:
    try:
        with sqlite3.connect(OFFLINE_DB) as db:
            db.execute("UPDATE status_queue SET sent_at=? WHERE id=?", (now_iso(), row_id))
            db.commit()
    except Exception as exc:
        log(f"Buffer offline ACK impossible: {exc}")


def flush_status_queue(limit: int = 20) -> dict | None:
    if not OFFLINE_BUFFER_ENABLED:
        return None
    init_offline_db()
    last_response = None
    with sqlite3.connect(OFFLINE_DB) as db:
        rows = db.execute(
            "SELECT id, payload FROM status_queue WHERE sent_at IS NULL ORDER BY id ASC LIMIT ?",
            (limit,),
        ).fetchall()
    for row_id, payload_text in rows:
        payload = json.loads(payload_text)
        response = post_json("/api/edge/push", payload)
        mark_status_payload_sent(int(row_id))
        last_response = response
    return last_response


def build_status_payload() -> dict:
    return {
        "agent_id": AGENT_ID,
        "site_time": now_iso(),
        "devices": {
            "invt": read_invt(),
            "salmson": read_salmson(),
            "wilo": read_wilo(),
            "coffret4": read_coffret4(),
        },
        "relay": relay_state,
    }


def push_status() -> None:
    global last_status_snapshot, last_status_at
    payload = build_status_payload()
    with state_lock:
        last_status_snapshot = payload
        last_status_at = time.time()
    if OFFLINE_BUFFER_ENABLED:
        enqueue_status_payload(payload)
        response = flush_status_queue() or {}
        log(f"Push OK + buffer offline vidange, commandes en attente: {response.get('commands_pending', 0)}")
    else:
        response = post_json("/api/edge/push", payload)
        log(f"Push OK, commandes en attente: {response.get('commands_pending', 0)}")


def acknowledge_command(cmd: dict, result: dict) -> None:
    command_id = cmd.get("id")
    if not command_id:
        return
    payload = {
        "id": command_id,
        "agent_id": AGENT_ID,
        "ok": bool(result.get("ok")),
        "error": result.get("error") or "",
        "result": result,
    }
    post_json("/api/edge/ack", payload, timeout=10)


def fetch_and_execute_commands() -> None:
    response = get_json("/api/edge/commands")
    for cmd in response.get("commands", []):
        try:
            result = execute_command(cmd)
        except Exception as exc:
            result = {"ok": False, "error": f"Erreur execution agent: {exc}"}
            log(result["error"])
        try:
            acknowledge_command(cmd, result)
            log(f"ACK commande {cmd.get('id')} envoye: {result.get('ok')}")
        except Exception as exc:
            log(f"ACK impossible commande {cmd.get('id')}: {exc}")


def ws_ack_command(ws, cmd: dict, result: dict) -> None:
    command_id = cmd.get("id")
    if not command_id:
        return
    payload = {
        "type": "ack",
        "id": command_id,
        "agent_id": AGENT_ID,
        "ok": bool(result.get("ok")),
        "error": result.get("error") or "",
        "result": result,
    }
    ws.send(json.dumps(payload, ensure_ascii=False))


def execute_ws_commands(ws, commands: list[dict]) -> None:
    for cmd in commands:
        try:
            result = execute_command(cmd)
        except Exception as exc:
            result = {"ok": False, "error": f"Erreur execution agent: {exc}"}
            log(result["error"])
        try:
            ws_ack_command(ws, cmd, result)
            log(f"ACK WS commande {cmd.get('id')} envoye: {result.get('ok')}")
        except Exception as exc:
            log(f"ACK WS impossible commande {cmd.get('id')}: {exc}")
            try:
                acknowledge_command(cmd, result)
                log(f"ACK HTTP secours commande {cmd.get('id')} envoye: {result.get('ok')}")
            except Exception as http_exc:
                log(f"ACK HTTP secours impossible commande {cmd.get('id')}: {http_exc}")


def websocket_loop() -> None:
    """Connexion permanente sortante Raspberry -> Render.

    Ce canal donne une vraie présence agent et pousse les commandes immédiatement.
    L'ancien HTTP PUSH reste actif pour la télémétrie et comme secours.
    """
    global ws_connected, ws_last_seen_at
    if not EDGE_WS_ENABLED:
        log("WebSocket desactive: EDGE_WS_ENABLED=false")
        return
    if websocket_client is None:
        log("WebSocket indisponible: installer websocket-client ou garder le mode HTTP PUSH")
        return

    backoff = EDGE_WS_RECONNECT_MIN_SEC
    while True:
        ws = None
        try:
            ws_url = cloud_ws_url()
            ws_headers = [f"X-Agent-ID: {AGENT_ID}", f"User-Agent: {AGENT_ID}"]
            if API_TOKEN:
                ws_headers.append(f"Authorization: Bearer {API_TOKEN}")
            log(f"Connexion WebSocket -> {ws_url.split('?')[0]}")
            ws = websocket_client.create_connection(ws_url, timeout=10, header=ws_headers)
            ws.settimeout(1)
            ws_connected = True
            ws_last_seen_at = time.time()
            backoff = EDGE_WS_RECONNECT_MIN_SEC
            ws.send(json.dumps({
                "type": "hello",
                "agent_id": AGENT_ID,
                "site_time": now_iso(),
                "dr302_host": DR302_HOST,
                "dr302_port": DR302_PORT,
                "mode": "local_brain_websocket",
            }, ensure_ascii=False))
            last_heartbeat = 0.0
            while True:
                now = time.time()
                if now - last_heartbeat >= EDGE_HEARTBEAT_SEC:
                    with state_lock:
                        telemetry_age = int(now - last_status_at) if last_status_at else None
                    ws.send(json.dumps({
                        "type": "heartbeat",
                        "agent_id": AGENT_ID,
                        "site_time": now_iso(),
                        "telemetry_age_sec": telemetry_age,
                        "modbus_connected": bool(client and getattr(client, "connected", False)),
                    }, ensure_ascii=False))
                    last_heartbeat = now

                try:
                    raw = ws.recv()
                except websocket_client.WebSocketTimeoutException:
                    raw = None
                if not raw:
                    continue
                ws_last_seen_at = time.time()
                try:
                    data = json.loads(raw)
                except Exception:
                    log(f"Message WS non JSON ignore: {str(raw)[:120]}")
                    continue
                msg_type = str(data.get("type") or "").lower()
                if msg_type in {"hello", "heartbeat_ack", "status_ack", "ack_result"}:
                    continue
                if msg_type == "commands":
                    execute_ws_commands(ws, data.get("commands") or [])
                elif msg_type == "error":
                    log(f"Erreur WS serveur: {data.get('error')}")
                else:
                    log(f"Message WS inconnu ignore: {msg_type}")
        except Exception as exc:
            log(f"WebSocket deconnecte: {exc}. Reconnexion dans {backoff}s")
        finally:
            ws_connected = False
            try:
                if ws is not None:
                    ws.close()
            except Exception:
                pass
        time.sleep(backoff)
        backoff = min(EDGE_WS_RECONNECT_MAX_SEC, max(EDGE_WS_RECONNECT_MIN_SEC, backoff * 2))


def command_loop() -> None:
    """Canal de secours HTTP: récupère les commandes si le WebSocket n'est pas actif."""
    consecutive_errors = 0
    while True:
        try:
            if EDGE_WS_ENABLED and ws_connected:
                time.sleep(COMMAND_POLL_SEC)
                continue
            fetch_and_execute_commands()
            consecutive_errors = 0
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            consecutive_errors += 1
            log(f"Erreur HTTP commandes {exc.code}: {body} (echec #{consecutive_errors})")
        except Exception as exc:
            consecutive_errors += 1
            log(f"Erreur boucle commandes: {exc} (echec #{consecutive_errors})")

        if consecutive_errors >= 10:
            time.sleep(min(COMMAND_POLL_SEC * 2, 10))
        else:
            time.sleep(COMMAND_POLL_SEC)


def status_loop() -> None:
    """Canal télémétrie: lit le Modbus et pousse l'état vers Render."""
    consecutive_errors = 0
    last_heartbeat_log = 0.0
    while True:
        cycle_start = time.time()
        try:
            push_status()
            consecutive_errors = 0
            if cycle_start - last_heartbeat_log > 300:
                log("♥ Heartbeat OK — agent actif")
                last_heartbeat_log = cycle_start
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            consecutive_errors += 1
            log(f"Erreur HTTP télémétrie {exc.code}: {body} (echec #{consecutive_errors})")
        except Exception as exc:
            consecutive_errors += 1
            log(f"Erreur boucle télémétrie: {exc} (echec #{consecutive_errors})")

        if consecutive_errors >= 10:
            time.sleep(min(STATUS_PUSH_SEC * 2, 10))
        else:
            time.sleep(STATUS_PUSH_SEC)


def main() -> None:
    init_offline_db()
    log(
        f"Agent local demarre: cloud={CLOUD_URL}, dr302={DR302_HOST}:{DR302_PORT}, "
        f"allow_start={EDGE_ALLOW_START}, buffer_offline={OFFLINE_BUFFER_ENABLED}, "
        f"data_dir={DATA_DIR}, command_poll={COMMAND_POLL_SEC}s, "
        f"status_push={STATUS_PUSH_SEC}s, modbus_timeout={MODBUS_TIMEOUT_SEC}s, "
        f"ws_enabled={EDGE_WS_ENABLED}, heartbeat={EDGE_HEARTBEAT_SEC}s"
    )

    threading.Thread(target=websocket_loop, daemon=True, name="zarzis-websocket-loop").start()
    threading.Thread(target=command_loop, daemon=True, name="zarzis-command-loop").start()
    status_loop()


if __name__ == "__main__":
    main()
