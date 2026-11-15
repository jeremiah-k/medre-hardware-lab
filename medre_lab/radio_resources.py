"""Resolve actual RNode ports before asserting hardware-only qualification."""

from pathlib import Path


def rnode_ports(profile: str) -> list[str]:
    from RNS.vendor.configobj import ConfigObj

    config = ConfigObj(str(Path(profile) / "config"), file_error=True)
    reticulum = config.get("reticulum", {})
    if str(reticulum.get("share_instance", "yes")).lower() not in ("no", "false", "0"):
        raise ValueError("hardware RNS profiles must disable share_instance")
    ports = []
    for interface in config.get("interfaces", {}).values():
        enabled = str(
            interface.get("interface_enabled", interface.get("enabled", "no"))
        ).lower()
        if enabled not in ("yes", "true", "1"):
            continue
        if interface.get("type") != "RNodeInterface":
            raise ValueError(
                "RF-only qualification requires only enabled RNodeInterface interfaces"
            )
        port = interface.get("port")
        if not isinstance(port, str) or not Path(port).exists():
            raise ValueError("enabled RNode interface needs an existing serial port")
        ports.append(str(Path(port).resolve()))
    if not ports:
        raise ValueError("hardware RNS profile has no enabled RNode serial interface")
    return ports
