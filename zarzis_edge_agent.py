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
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

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

DR302_HOST = os.environ.get("DR302_HOST", "192.168.1.10")
DR302_PORT = env_int("DR302_PORT", 502)
POLL_SEC = max(2, env_int("EDGE_POLL_SEC", 5))
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

RAINBIRD_IP = os.environ.get("RAINBIRD_IP", "").strip()
RAINBIRD_STICK_ID = os.environ.get("RAINBIRD_STICK_ID", "").strip()
RAINBIRD_KEYCODE = os.environ.get("RAINBIRD_KEYCODE", "").strip()
RAINBIRD_MAX_DURATION_MIN = max(1, env_int("RAINBIRD_MAX_DURATION_MIN", 240))
rainbird_state = {"connected": False, "active_zones": [], "ip": RAINBIRD_IP, "last_cmd": ""}


client: ModbusTcpClient | None = None
last_status_snapshot: dict = {}
last_status_at = 0.0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    print(f"{now_iso()} {message}", flush=True)


def headers() -> dict[str, str]:
    result = {"Content-Type": "application/json", "User-Agent": AGENT_ID, "X-Agent-ID": AGENT_ID}
    if API_TOKEN:
        result["Authorization"] = f"Bearer {API_TOKEN}"
    return result


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
        client = ModbusTcpClient(DR302_HOST, port=DR302_PORT, timeout=5)
        ok = bool(client.connect())
        if not ok:
            log(f"Modbus indisponible sur {DR302_HOST}:{DR302_PORT}")
        return ok
    except Exception as exc:
        log(f"Erreur connexion Modbus: {exc}")
        return False


def read_regs(addr: int, reg: int, count: int = 1) -> list[int] | None:
    if reg < 0 or not ensure_modbus():
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


def rainbird_request(command: str, params: dict | None = None) -> tuple[bool, dict]:
    if not RAINBIRD_IP:
        return False, {"error": "RAINBIRD_IP non configure sur l'agent"}
    if not RAINBIRD_STICK_ID or not RAINBIRD_KEYCODE:
        return False, {"error": "RAINBIRD_STICK_ID/RAINBIRD_KEYCODE manquants"}
    payload = {"id": RAINBIRD_STICK_ID, "command": command}
    if params:
        payload.update(params)
    req = urllib.request.Request(
        f"http://{RAINBIRD_IP}/stick",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Basic {RAINBIRD_KEYCODE[:32]}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        rainbird_state["connected"] = True
        return True, {"raw": data}
    except Exception as exc:
        rainbird_state["connected"] = False
        return False, {"error": f"Rain Bird indisponible: {exc}"}


def execute_rainbird_command(cmd: dict) -> dict:
    action = str(cmd.get("action") or "").lower()
    if action == "start":
        zone = parse_int(cmd.get("zone"), 1)
        duration = max(1, min(parse_int(cmd.get("duration"), 10), RAINBIRD_MAX_DURATION_MIN))
        ok, info = rainbird_request("ZoneStartRequest", {"zone": zone, "duration": duration})
        if ok:
            rainbird_state["active_zones"] = [zone]
            rainbird_state["last_cmd"] = f"START Zone {zone} {duration}min"
        return {"ok": ok, "type": "rainbird", "action": action, "zone": zone, "duration": duration, **info}
    if action == "stop":
        zone = cmd.get("zone")
        ok, info = rainbird_request("StopIrrigationRequest")
        if ok:
            rainbird_state["active_zones"] = []
            rainbird_state["last_cmd"] = f"STOP {'zone ' + str(zone) if zone else 'tout'}"
        return {"ok": ok, "type": "rainbird", "action": action, "zone": zone, **info}
    return {"ok": False, "error": f"Action Rain Bird inconnue: {action}"}


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
    if time.time() - last_status_at > max(POLL_SEC * 3, 15):
        return ["Mesures locales trop anciennes"]
    devices = last_status_snapshot.get("devices") if isinstance(last_status_snapshot, dict) else {}
    blockers: list[str] = []
    for target in command_targets(device):
        blockers.extend(local_start_blockers(target, devices.get(target, {}) if isinstance(devices, dict) else {}))
    return blockers


def execute_command(cmd: dict) -> dict:
    if cmd.get("type") == "rainbird":
        result = execute_rainbird_command(cmd)
        log(f"Commande Rain Bird executee={result.get('ok')}: {result.get('action')}")
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


def push_status() -> None:
    global last_status_snapshot, last_status_at
    payload = {
        "agent_id": AGENT_ID,
        "site_time": now_iso(),
        "devices": {
            "invt": read_invt(),
            "salmson": read_salmson(),
            "wilo": read_wilo(),
            "coffret4": read_coffret4(),
        },
        "rainbird": rainbird_state,
    }
    last_status_snapshot = payload
    last_status_at = time.time()
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


def main() -> None:
    log(f"Agent local demarre: cloud={CLOUD_URL}, dr302={DR302_HOST}:{DR302_PORT}, allow_start={EDGE_ALLOW_START}")
    while True:
        try:
            push_status()
            fetch_and_execute_commands()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            log(f"Erreur HTTP {exc.code}: {body}")
        except Exception as exc:
            log(f"Erreur agent: {exc}")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
