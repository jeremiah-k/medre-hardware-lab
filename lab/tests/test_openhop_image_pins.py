"""A tag alone cannot attest the sources baked into an openHop image."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import openhop_image_pins as image_pins  # noqa: E402


def test_baked_meshcore_pin_is_required_even_if_other_pins_match(monkeypatch):
    refs = {key: {"ref": str(index) * 40} for index, key in enumerate(image_pins.MEDRE)}
    labels = {f"io.medre.lab.pin.{key}": entry["ref"] for key, entry in refs.items()}

    def inspect(*command):
        assert command[-1] == "sha256:selected"
        return json.dumps(labels)

    monkeypatch.setattr(image_pins, "check_output", inspect)
    image_pins.check("sha256:selected", image_pins.MEDRE, refs)
    labels["io.medre.lab.pin.medre-adapter-meshcore"] = "f" * 40
    with pytest.raises(ValueError, match="baked medre-adapter-meshcore pin"):
        image_pins.check("sha256:selected", image_pins.MEDRE, refs)
