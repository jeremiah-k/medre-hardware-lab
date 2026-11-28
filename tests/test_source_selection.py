import json
from pathlib import Path

import pytest

from medre_lab import presets
from medre_lab.docker import (
    DEFAULT_MANIFEST,
    DEFAULT_SOURCE_GROUP,
    OPENHOP_MANIFEST,
    OPENHOP_SOURCE_GROUP,
    SOURCES,
    lock,
)

ROOT = Path(__file__).resolve().parents[1]


def test_committed_sources_are_complete_and_exact():
    """The lock names every Docker dependency, and no more.

    The five original pins (the default rig) are unchanged; the openHop
    profile adds five MORE keys, so the lock has ten entries.
    """
    assert len(lock(ROOT)) == 10
    assert len(SOURCES) == 10


def test_default_pins_are_untouched():
    """The openHop work must not move the default rig's five pins."""
    data = lock(ROOT)
    assert data["medre"]["ref"] == "fd2927d1a518c792f85bf8c31c297dbe6e7e7cb8"
    assert data["medre-adapter-matrix"]["ref"] == (
        "d39212bc0c3c2e021c9427bc0e958870baaa1afa"
    )
    assert data["medre-adapter-meshtastic"]["ref"] == (
        "8e59552cd32bc43147d873c991ded7c5f082756f"
    )
    assert data["medre-adapter-lxmf"]["ref"] == (
        "935faefd3d67be13a6fab445846338c8fc2f150d"
    )
    assert data["mmrelay"]["ref"] == ("427cd13fc5518f9ad0dd52f1c31964d3b5ce754c")


def test_openhop_pins_are_exact():
    """The openHop profile's five pins are the qualification SHAs."""
    data = lock(ROOT)
    assert data["medre-openhop"]["ref"] == ("60f79a4797b86dfbf785ca2809329ce933a1aa12")
    # The receive-pump revision with its callbacks fenced to their SDK
    # connection: the idle-link liveness probe on top of the
    # receive-ownership fix, the park-on-silent-window rule with its
    # answered-probe re-arm, the stop-and-wait drain, and the accounted
    # binary channel-data pop.
    assert data["medre-adapter-meshcore"]["ref"] == (
        "b09cdbb55a585be10e05e4fe7d15d8099b20f2cd"
    )
    assert data["medre-adapter-meshtastic-openhop"]["ref"] == (
        "711c65afa7b2baf79f69d93f95f40624ef1cecc1"
    )
    # The openHop core/repeater builds carry the companion frame-admission
    # and queue-ownership revisions.
    assert data["openhop-core"]["ref"] == ("7eb2b9ea44de6ae2b7a02993a93e75d7cb8c49a6")
    assert data["openhop-repeater"]["ref"] == (
        "a261da7d834fe6ea8f638772654b25b98828d9b1"
    )


def test_openhop_urls_are_public_https():
    data = lock(ROOT)
    for name in OPENHOP_SOURCE_GROUP:
        assert data[name]["url"].startswith("https://github.com/")
        assert "@" not in data[name]["url"]


def test_source_groups_are_disjoint_and_complete():
    """The default group and the openHop group partition the sources."""
    default = set(DEFAULT_SOURCE_GROUP)
    openhop = set(OPENHOP_SOURCE_GROUP)
    assert not default & openhop
    assert default | openhop == set(SOURCES)


def test_manifests_cover_their_groups():
    """Each manifest names exactly the MOUNTED sources of its rig.

    mmrelay is baked into the relay images and mounted nowhere, so it is
    qualified by the host-only source check, not by a container mount.
    """
    default_names = {name for names in DEFAULT_MANIFEST.values() for name in names}
    openhop_names = {name for names in OPENHOP_MANIFEST.values() for name in names}
    assert default_names == set(DEFAULT_SOURCE_GROUP) - {"mmrelay"}
    assert openhop_names == set(OPENHOP_SOURCE_GROUP)
    # The default manifest must NOT name the openHop containers: the
    # default scenario suite checks it with live container inspection.
    assert "medre-lab-medre-openhop" not in DEFAULT_MANIFEST
    assert "medre-lab-openhop-repeater" not in DEFAULT_MANIFEST


@pytest.mark.parametrize("change", ["missing", "moving", "credential"])
def test_invalid_source_plan_rejected(tmp_path, change):
    root = Path(__file__).resolve().parents[1]
    data = json.loads((root / "sources.lock.json").read_text())
    if change == "missing":
        del data["medre"]
    if change == "moving":
        data["medre"]["ref"] = "main"
    if change == "credential":
        data["medre"]["url"] = "https://user:password@github.com/owner/repo"
    (tmp_path / "sources.lock.json").write_text(json.dumps(data))
    with pytest.raises(ValueError):
        lock(tmp_path)


def test_duplicate_lock_key_rejected(tmp_path):
    (tmp_path / "sources.lock.json").write_text('{"medre":{},"medre":{}}')
    with pytest.raises(ValueError, match="duplicate"):
        lock(tmp_path)


def test_every_committed_preset_is_structurally_valid():
    """Every lab/presets/*.json validates against the preset schema.

    The library check also fails on an EMPTY presets directory and on a
    library that does not record the current lock's pin set, so a preset
    edit can never silently orphan the rig's own lock.
    """
    library = presets.validate_preset_library(ROOT)
    assert {preset["name"] for preset in library} >= {"base", "openhop-companion"}


def test_the_locks_pin_set_is_recorded_by_a_preset():
    """Membership, not uniqueness: the lock's pin set must equal AT LEAST
    ONE preset's pin set. Distinct presets may share pins (they differ in
    topology, suite or intent) -- the seeds do exactly that."""
    pins = lock(ROOT)
    library = presets.load_library(ROOT)
    matching = [preset["name"] for preset in library if preset["sources"] == pins]
    assert matching, "no preset records the current lock's pin set"


def test_seed_presets_record_the_ruled_choices():
    """base: the plain rig and the compose suite. openhop-companion: the
    qualification rig with the driver's stages. Both record the same pin
    set -- that is the point of the membership rule."""
    base = presets.read_preset(ROOT, "base")
    companion = presets.read_preset(ROOT, "openhop-companion")
    assert base["topology"] == "base"
    assert base["suite"] == ["scenarios"]
    assert companion["topology"] == "openhop"
    assert companion["suite"] == [
        f"qual:{stage}" for stage in presets.qualify_stage_names(ROOT)
    ]
    assert base["sources"] == companion["sources"] == lock(ROOT)


def test_an_empty_preset_library_is_a_failure(tmp_path):
    (tmp_path / "lab" / "presets").mkdir(parents=True)
    with pytest.raises(ValueError, match="empty"):
        presets.load_library(tmp_path)


def test_a_library_without_the_current_pins_is_a_failure(tmp_path):
    (tmp_path / "lab" / "presets").mkdir(parents=True)
    (tmp_path / "lab" / "run-scenarios.sh").write_text("")
    (tmp_path / "sources.lock.json").write_text(
        (ROOT / "sources.lock.json").read_text()
    )
    data = json.loads((ROOT / "lab/presets/base.json").read_text())
    data["name"] = "base"
    data["sources"]["medre"]["ref"] = "0" * 40
    (tmp_path / "lab" / "presets" / "base.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="no preset records"):
        presets.validate_preset_library(tmp_path)
