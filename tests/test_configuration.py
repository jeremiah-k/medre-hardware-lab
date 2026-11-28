import pytest

from medre_lab.config import load, pair
from medre_lab.ownership import own


def test_pair_order_and_invalid_shapes():
    assert pair("lxmf:meshcore") == ("meshcore", "lxmf")
    for value in ("lxmf:lxmf", "bad:meshcore", "meshcore", "meshcore:lxmf:meshtastic"):
        with pytest.raises(ValueError):
            pair(value)


def test_paths_follow_configuration_location(tmp_path):
    config = tmp_path / "lab.toml"
    config.write_text('[hardware.lxmf]\nprofiles=["a", "b"]\nidentities=["ia", "ib"]\n')
    result = load(config)
    assert result["hardware"]["lxmf"]["profiles"] == [
        str(tmp_path / "a"),
        str(tmp_path / "b"),
    ]


@pytest.mark.parametrize(
    "body",
    [
        "[hardware]\nsend_timeout=0",
        "[hardware]\nsend_timeout=true",
        "[hardware]\nsend_timeout=nan",
        '[hardware]\nble_adapter="default"',
        '[hardware.meshtastic]\nchannel=0\nports=["a","./a"]',
        '[hardware.meshcore]\nchannel=1\naddresses=["aa:bb:cc:dd:ee:ff","AA:BB:CC:DD:EE:FF"]',
        '[hardware.meshcore]\nchannel=1\naddresses=["wrong","also wrong"]',
        '[hardware.meshtastic]\nchannel=true\nports=["a","b"]',
        '[hardware]\nidentity_key="no inline secrets"',
    ],
)
def test_invalid_configuration_fails_before_hardware(tmp_path, body):
    config = tmp_path / "lab.toml"
    config.write_text(body)
    with pytest.raises(ValueError):
        load(config)


def _write_hardware(tmp_path, body: str):
    config = tmp_path / "lab.toml"
    config.write_text(body)
    return config


def test_ble_adapter_env_overrides_configured_controller(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDRE_LAB_BLE_ADAPTER", "hci0")
    assert (
        load(_write_hardware(tmp_path, '[hardware]\nble_adapter="hci1"\n'))["hardware"][
            "ble_adapter"
        ]
        == "hci0"
    )


def test_ble_adapter_is_not_injected_without_meshcore(tmp_path, monkeypatch):
    monkeypatch.delenv("MEDRE_LAB_BLE_ADAPTER", raising=False)
    assert "ble_adapter" not in load(
        _write_hardware(tmp_path, "[hardware]\nsend_timeout=5\n")
    )["hardware"]


def test_ble_adapter_keeps_configured_controller_without_override(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("MEDRE_LAB_BLE_ADAPTER", raising=False)
    assert (
        load(_write_hardware(tmp_path, '[hardware]\nble_adapter="hci1"\n'))["hardware"][
            "ble_adapter"
        ]
        == "hci1"
    )


def test_ble_adapter_rejects_invalid_override(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDRE_LAB_BLE_ADAPTER", "wlan0")
    with pytest.raises(ValueError, match="MEDRE_LAB_BLE_ADAPTER"):
        load(_write_hardware(tmp_path, '[hardware]\nble_adapter="hci1"\n'))


def test_ownership_blocks_overlap_and_releases():
    with own(["test-owned-endpoint"]):
        with pytest.raises(RuntimeError, match="owns"):
            with own(["test-owned-endpoint"]):
                pass
        with own(["different-endpoint"]):
            pass
    with own(["test-owned-endpoint"]):
        pass


def test_effective_controller_rejects_a_conflicting_environment(tmp_path, monkeypatch):
    from medre_lab.config import BLE_ADAPTER_ENV, load, write_effective_config

    source = tmp_path / "hardware.toml"
    source.write_text('[hardware]\nble_adapter = "hci2"\n')
    monkeypatch.setenv(BLE_ADAPTER_ENV, "hci1")
    snapshot = tmp_path / "effective-config.toml"
    write_effective_config(snapshot, load(source))
    assert load(snapshot, effective=True)["hardware"]["ble_adapter"] == "hci1"

    monkeypatch.setenv(BLE_ADAPTER_ENV, "hci0")
    with pytest.raises(ValueError, match="conflicts with the effective configuration"):
        load(snapshot, effective=True)
