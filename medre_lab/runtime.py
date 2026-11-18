"""Assemble two real MEDRE adapters without transport policy in the lab."""

from pathlib import Path


def build(
    config: dict,
    transports: tuple[str, str],
    role: int,
    run: Path,
    destinations: dict,
    message_delay_seconds: float = 2.5,
):
    from medre.config.model import (
        AdapterConfigSet,
        LoggingConfig,
        RuntimeConfig,
        RuntimeOptions,
        StorageConfig,
    )
    from medre.config.paths import MedrePaths
    from medre.config.routes import RouteConfig, RouteConfigSet
    from medre.runtime.builder import RuntimeBuilder

    hw = config["hardware"]
    adapters = {}
    for kind in transports:
        entry = hw[kind]
        common = dict(
            adapter_id=kind,
            message_delay_seconds=message_delay_seconds if kind == "meshcore" else 2.5,
            relay_prefix="{sender}: ",
        )
        if kind == "meshtastic":
            from medre.config.adapters.meshtastic import MeshtasticConfig
            from medre.config.adapters.meshtastic_runtime import MeshtasticRuntimeConfig

            settings = MeshtasticConfig(
                **common,
                connection_type="serial",
                serial_port=entry["ports"][role],
                default_channel=entry["channel"],
                max_text_bytes=200,
            )
            runtime = MeshtasticRuntimeConfig
        elif kind == "meshcore":
            from medre.config.adapters.meshcore import MeshCoreConfig
            from medre.config.adapters.meshcore_runtime import MeshCoreRuntimeConfig

            settings = MeshCoreConfig(
                **common,
                connection_type="ble",
                ble_address=entry["addresses"][role],
                default_channel=entry["channel"],
                max_text_bytes=160,
                identity=f"MEDRE-LAB-{role}",
            )
            runtime = MeshCoreRuntimeConfig
        else:
            from medre.config.adapters.lxmf import LxmfConfig
            from medre.config.adapters.lxmf_runtime import LxmfRuntimeConfig

            settings = LxmfConfig(
                **common,
                connection_type="reticulum",
                identity_path=entry["identities"][role],
                reticulum_config_dir=entry["profiles"][role],
                storage_path=str(run / "lxmf-storage"),
                display_name="MEDRE lab",
                announce_interval_seconds=8,
                stamp_cost=0,
                default_delivery_method="direct",
            )
            runtime = LxmfRuntimeConfig
        adapters[kind] = {
            kind: runtime(
                adapter_id=kind,
                enabled=True,
                adapter_kind="real",
                config=settings.validate(),
            )
        }
    routes = []
    for source, dest in (transports, transports[::-1]):
        routes.append(
            RouteConfig(
                route_id=f"{source}_to_{dest}",
                source_adapters=(source,),
                dest_adapters=(dest,),
                source_channel=str(hw[source]["channel"]) if source != "lxmf" else None,
                dest_channel=destinations[dest],
            )
        )
    routing = RouteConfigSet(routes=tuple(routes))
    routing.validate()
    db = run / "state.sqlite"
    cfg = RuntimeConfig(
        runtime=RuntimeOptions(name="hardware-lab"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="sqlite", path=str(db)),
        adapters=AdapterConfigSet(**adapters),
        routes=routing,
    )
    paths = MedrePaths(
        config_dir=run / "config",
        config_file=run / "config/config.yaml",
        state_dir=run / "state",
        data_dir=run / "data",
        cache_dir=run / "cache",
        log_dir=run / "logs",
        database_path=db,
    )
    return RuntimeBuilder(cfg, paths).build()
