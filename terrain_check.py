#!/usr/bin/env python3
"""Diagnostic terrain Zarzis. Lecture seule: cloud, TCP DR302, registres Modbus principaux."""
from __future__ import annotations

import json
import os
import socket
import sys
import urllib.request
from pathlib import Path

try:
    from pymodbus.client import ModbusTcpClient
except Exception as exc:
    print(f"[ERREUR] pymodbus absent: {exc}")
    print("Lance: sudo bash install_raspberry_agent.sh")
    sys.exit(2)


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


for env_path in (Path("/etc/zarzis/agent.env"), Path("agent.env.terrain"), Path("agent.env.template")):
    load_env(env_path)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_int(name: str, default: int) -> int:
    try:
        return int(env(name, str(default)), 0)
    except Exception:
        return default


def cloud_ping() -> None:
    cloud = env("CLOUD_URL", "https://zarzis-irrigation-1.onrender.com").rstrip("/")
    print("\n=== CLOUD ===")
    print("URL:", cloud)
    try:
        req = urllib.request.Request(
            cloud + "/api/ping",
            headers={"User-Agent": "zarzis-terrain-check"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        print(f"[OK] ping version={data.get('version')} mode={data.get('mode')} connected={data.get('connected')}")
        start_auth = data.get("start_authorization") or {}
        if start_auth:
            print(f"[INFO] start_allowed={start_auth.get('start_commands_allowed')} reasons={start_auth.get('reasons')}")
    except Exception as exc:
        print("[KO] cloud:", exc)
    if not env("API_TOKEN") or "REMPLACER" in env("API_TOKEN"):
        print("[ATTENTION] API_TOKEN non renseigne dans agent.env")


def tcp_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except Exception as exc:
        print(f"[KO] TCP {host}:{port}: {exc}")
        return False


def read_one(client: ModbusTcpClient, addr: int, reg: int, label: str):
    if reg < 0:
        print(f"  - {label}: ignore")
        return None
    try:
        try:
            result = client.read_holding_registers(reg, count=1, slave=addr)
        except TypeError:
            result = client.read_holding_registers(reg, count=1, unit=addr)
        if result and not result.isError():
            value = int(result.registers[0])
            print(f"  [OK] addr={addr} reg={reg} {label}={value}")
            return value
        print(f"  [KO] addr={addr} reg={reg} {label}: {result}")
    except Exception as exc:
        print(f"  [KO] addr={addr} reg={reg} {label}: {exc}")
    return None


def modbus_check() -> None:
    host = env("DR302_HOST", "192.168.1.10")
    port = env_int("DR302_PORT", 502)
    timeout = float(env("MODBUS_TIMEOUT_SEC", "1.5").replace(",", "."))
    print("\n=== DR302 / MODBUS ===")
    print(f"DR302: {host}:{port}")
    if not tcp_open(host, port):
        return
    print("[OK] port TCP ouvert")
    client = ModbusTcpClient(host, port=port, timeout=timeout)
    if not client.connect():
        print("[KO] pymodbus connect")
        return
    print("[OK] pymodbus connect")

    addr_invt = env_int("ADDR_INVT", 1)
    addr_salmson = env_int("ADDR_SALMSON", 2)
    addr_wilo = env_int("ADDR_WILO", 3)
    addr_coffret4 = env_int("ADDR_COFFRET4", 4)

    print("\nINVT addr 1")
    read_one(client, addr_invt, env_int("INVT_REG_FREQ_HZ", 0x3000), "freq_hz")
    read_one(client, addr_invt, env_int("INVT_REG_DC_BUS_V", 0x3002), "dc_bus_v")
    read_one(client, addr_invt, env_int("INVT_REG_CURRENT_A", 0x3004), "current_a")
    read_one(client, addr_invt, env_int("INVT_REG_FAULT_CODE", 0x5000), "fault_code")

    print("\nSALMSON addr 2")
    read_one(client, addr_salmson, env_int("SALMSON_REG_LEVEL_CM", 25), "level_cm")
    read_one(client, addr_salmson, env_int("SALMSON_REG_SWITCH_STATE", 61), "switch_state")
    read_one(client, addr_salmson, env_int("SALMSON_REG_ERROR_CODE", 138), "error_code")
    read_one(client, addr_salmson, env_int("SALMSON_REG_FLOAT_STATE", 197), "float_state")

    print("\nWILO addr 3")
    read_one(client, addr_wilo, env_int("WILO_REG_PRESSURE", 25), "pressure")
    read_one(client, addr_wilo, env_int("WILO_REG_SWITCH_STATE", 61), "switch_state")
    read_one(client, addr_wilo, env_int("WILO_REG_ERROR_CODE", 138), "error_code")

    print("\nCOFFRET addr 4")
    read_one(client, addr_coffret4, 0x0001, "input_1")
    read_one(client, addr_coffret4, 0x0010, "analog_1")

    try:
        client.close()
    except Exception:
        pass


def main() -> None:
    print("ZARZIS TERRAIN CHECK - lecture seule")
    cloud_ping()
    modbus_check()
    print("\nFin. Si addr 1/2/3 repondent, relance agent et teste depuis dashboard.")


if __name__ == "__main__":
    main()
