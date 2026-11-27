import json
from pathlib import Path

import pytest

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
    assert data["openhop-core"]["ref"] == ("e6df15082b82f1c202086f966f576111819a3b86")
    assert data["openhop-repeater"]["ref"] == (
        "1b3abc0dfd8b5891fcff93a02c371cc80d6b9db3"
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
