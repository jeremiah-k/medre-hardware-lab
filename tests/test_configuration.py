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


def test_ownership_blocks_overlap_and_releases():
    with own(["test-owned-endpoint"]):
        with pytest.raises(RuntimeError, match="owns"):
            with own(["test-owned-endpoint"]):
                pass
        with own(["different-endpoint"]):
            pass
    with own(["test-owned-endpoint"]):
        pass
