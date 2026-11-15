import sys
from types import SimpleNamespace

import pytest

from medre_lab.radio_resources import rnode_ports


def install_parser(monkeypatch, data):
    monkeypatch.setitem(
        sys.modules,
        "RNS.vendor.configobj",
        SimpleNamespace(ConfigObj=lambda *a, **kw: data),
    )


def test_rf_profile_resolves_actual_serial_endpoint(tmp_path, monkeypatch):
    port = tmp_path / "radio"
    port.touch()
    install_parser(
        monkeypatch,
        {
            "reticulum": {"share_instance": "No"},
            "interfaces": {
                "radio": {
                    "type": "RNodeInterface",
                    "interface_enabled": "Yes",
                    "port": str(port),
                }
            },
        },
    )
    assert rnode_ports(str(tmp_path)) == [str(port)]


@pytest.mark.parametrize(
    "data",
    [
        {"reticulum": {"share_instance": "Yes"}, "interfaces": {}},
        {
            "reticulum": {"share_instance": "No"},
            "interfaces": {"network": {"type": "UDPInterface", "enabled": "Yes"}},
        },
        {"reticulum": {"share_instance": "No"}, "interfaces": {}},
        {
            "reticulum": {"share_instance": "No"},
            "interfaces": {
                "radio": {
                    "type": "RNodeInterface",
                    "enabled": "Yes",
                    "port": "/missing-radio",
                }
            },
        },
    ],
)
def test_nonphysical_or_missing_interface_fails_before_ownership(monkeypatch, data):
    install_parser(monkeypatch, data)
    with pytest.raises(ValueError):
        rnode_ports("unused")
