#!/usr/bin/env python3
"""
Agent local Zarzis pour le mode http_push.

Il tourne sur le site (PC local, mini PC ou Raspberry Pi), lit le DR302/G781 en
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


CLOUD_URL = os.environ.get("CLOUD_URL", "https://zarzis-irrigation-1.onrender.com").rstrip("/")
API_TOKEN = os.environ.get("API_TOKEN", "").strip()
AGENT_ID = os.environ.get("AGENT_ID", "zarzis-edge-agent")

DR302_HOST = os.environ.get("DR302_HOST") or os.environ.get("G781_HOST", "192.168.8.10")
DR302_PORT = env_int("DR302_PORT", env_int("G781_PORT", 502))
POLL_SEC = max(2, env_int("EDGE_POLL_SEC", 5))
EDGE_ALLOW_START = env_bool("EDGE_ALLOW_START", False)
SALMSON_COMMAND_ENABLED = env_bool("SALMSON_COMMAND_ENABLED", False)
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

SALMSON_CMD = env_int("SALMSON_CMD_REG", 0x0100)
SALMSON_REGS = {
    "pump_state": env_int("SALMSON_REG_PUMP_STATE", 0x0001),
    "current_a": env_int("SALMSON_REG_CURRENT_A", 0x0010),
    "error_code": env_int("SALMSON_REG_ERROR_CODE", 0x0020),
    "float_low": env_int("SALMSON_REG_FLOAT_LOW", 0x0030),
    "float_high": env_int("SALMSON_REG_FLOAT_HIGH", 0x0031),
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


client: ModbusTcpClient | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    print(f"{now_iso()} {message}", flush=True)


def headers() -> dict[str, str]:
    result = {"Content-Type": "application/json", "User-Agent": AGENT_ID}
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


def read_salmson() -> dict:
    data: dict[str, float | int | bool | str | None] = {}
    for name, reg in SALMSON_REGS.items():
        values = read_regs(ADDR_SALMSON, reg)
        if not values:
            continue
        raw = values[0]
        data[name] = round(raw / 10, 1) if name == "current_a" else raw
    if not data:
        return unavailable("Salmson")
    error = int(data.get("error_code") or 0)
    running = int(data.get("pump_state") or 0) == 1
    data["running"] = running
    data["status"] = "ERREUR" if error else ("EN MARCHE" if running else "ARRET")
    data["error_text"] = f"Defaut Salmson {error}" if error else None
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


def execute_command(cmd: dict) -> None:
    if cmd.get("type") != "control":
        return
    device = str(cmd.get("device") or "").lower()
    action = str(cmd.get("action") or "").lower()
    if action == "start":
        action = "on"
    if action == "stop":
        action = "off"
    is_start = action in {"on", "forward", "reverse"}
    if is_start and not EDGE_ALLOW_START:
        log(f"Commande bloquee par EDGE_ALLOW_START=false: {device} {action}")
        return

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

    log(f"Commande executee={ok}: {device} {action}")


def push_status() -> None:
    payload = {
        "agent_id": AGENT_ID,
        "site_time": now_iso(),
        "devices": {
            "invt": read_invt(),
            "salmson": read_salmson(),
            "wilo": read_wilo(),
            "coffret4": read_coffret4(),
        },
    }
    response = post_json("/api/g781/push", payload)
    log(f"Push OK, commandes en attente: {response.get('commands_pending', 0)}")


def fetch_and_execute_commands() -> None:
    response = get_json("/api/g781/commands")
    for cmd in response.get("commands", []):
        execute_command(cmd)


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
