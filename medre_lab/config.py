"""Validate portable operator configuration without touching hardware."""

from __future__ import annotations

import json
import math
import re
import tomllib
from pathlib import Path

TRANSPORTS = ("meshtastic", "meshcore", "lxmf")


def path_from(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def load(path: Path) -> dict:
    path = path.resolve()
    with path.open("rb") as stream:
        config = tomllib.load(stream)
    if set(config) - {"hardware"}:
        raise ValueError("unknown configuration section")
    hw = config.get("hardware", {})
    if not isinstance(hw, dict):
        raise ValueError("hardware must be a table")
    allowed = {
        *TRANSPORTS,
        "ble_adapter",
        "startup_timeout",
        "send_timeout",
        "observation_timeout",
    }
    if set(hw) - allowed:
        raise ValueError("unknown hardware option")
    for key, default in (
        ("startup_timeout", 60),
        ("send_timeout", 100),
        ("observation_timeout", 50),
    ):
        value = hw.setdefault(key, default)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{key} must be finite and positive")
    if "ble_adapter" in hw and not re.fullmatch(r"hci\d+", str(hw["ble_adapter"])):
        raise ValueError("ble_adapter must be an HCI name such as hci1")
    for kind in TRANSPORTS:
        if kind not in hw:
            continue
        table = hw[kind]
        keys = (
            {"profiles", "identities"}
            if kind == "lxmf"
            else (
                {"ports", "channel"}
                if kind == "meshtastic"
                else {"addresses", "channel"}
            )
        )
        if not isinstance(table, dict) or set(table) != keys:
            raise ValueError(f"{kind} requires exactly {sorted(keys)}")
        for key in keys - {"channel"}:
            values = table[key]
            if (
                not isinstance(values, list)
                or len(values) != 2
                or any(not isinstance(v, str) or not v.strip() for v in values)
            ):
                raise ValueError(f"{kind}.{key} requires two distinct endpoints")
            if kind == "meshcore":
                if any(
                    not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", v)
                    for v in values
                ):
                    raise ValueError("invalid MeshCore BLE address")
                values = [v.upper() for v in values]
            else:
                values = [str(path_from(path.parent, v)) for v in values]
            if values[0] == values[1]:
                raise ValueError(f"{kind}.{key} endpoints must be distinct")
            table[key] = values
        if kind != "lxmf":
            channel = table["channel"]
            if (
                isinstance(channel, bool)
                or not isinstance(channel, int)
                or not 0 <= channel <= (7 if kind == "meshtastic" else 255)
            ):
                raise ValueError(f"invalid {kind} channel")
    config["hardware"] = hw
    return config


def pair(value: str) -> tuple[str, str]:
    parts = value.split(":")
    if (
        len(parts) != 2
        or any(p not in TRANSPORTS for p in parts)
        or parts[0] == parts[1]
    ):
        raise ValueError(
            "pair must name two different transports, e.g. meshtastic:lxmf"
        )
    # Keep ordering stable across CLI spelling; roles still select endpoint indices.
    return tuple(sorted(parts, key=TRANSPORTS.index))


def write_effective_config(path: Path, config: dict) -> None:
    """Write the validated, resolved settings in the private campaign directory."""
    hw = config["hardware"]
    lines = ["[hardware]"]
    for key, value in sorted(hw.items()):
        if not isinstance(value, dict):
            lines.append(f"{key} = {json.dumps(value, ensure_ascii=False)}")
    for kind in TRANSPORTS:
        if kind in hw:
            lines.extend(("", f"[hardware.{kind}]"))
            for key, value in sorted(hw[kind].items()):
                lines.append(f"{key} = {json.dumps(value, ensure_ascii=False)}")
    with path.open("x", encoding="utf-8") as stream:
        path.chmod(0o600)
        stream.write("\n".join(lines) + "\n")
    if load(path) != config:
        raise ValueError(
            "effective configuration snapshot differs from validated settings"
        )
