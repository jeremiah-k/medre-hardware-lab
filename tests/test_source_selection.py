import json
import shutil
import subprocess
from pathlib import Path

import pytest

from medre_lab import docker as rig
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


# --- fetch materialization ------------------------------------------------------


def test_baked_sources_are_exactly_the_build_context_sources():
    """Every locked source feeds a Docker build context, so every checkout
    must serve an image bake.

    compose.yaml resolves each source's env var as a named build context and
    the lab Dockerfiles COPY-install from those contexts; the matrix bake
    installs via git+file FROM the copied checkout itself. A bake that meets
    a blob:none partial clone dies on a cold build cache (upload-pack can
    neither serve a clone from a partial clone nor lazy-fetch while
    serving), so fetch materializes every baked checkout's object store.
    """
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    for name, info in SOURCES.items():
        assert f"${{{info[0]}:?" in compose, f"{name} has no build context"
        assert name in rig.BAKED_SOURCES
    assert set(rig.BAKED_SOURCES) == set(SOURCES)


def run_git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Lab Test",
            "-c",
            "user.email=lab@example.invalid",
            *args,
        ],
        text=True,
        stderr=subprocess.STDOUT,
    ).strip()


def make_seed_repo(repos: Path, name: str) -> str:
    """A tiny local history whose HEAD becomes the pin.

    The main tip carries the module files the mount guard checks. A second
    branch adds one blob a blob:none clone never materializes, so a partial
    store is provable by object ids alone.
    """
    repo = repos / name
    repo.mkdir(parents=True)
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "uploadpack.allowFilter", "true")
    for key, info in SOURCES.items():
        file = repo / info[1]
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(f"# {key}\n", encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "module files")
    run_git(repo, "checkout", "-q", "-b", "side")
    (repo / "side.txt").write_text("side\n", encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "side")
    run_git(repo, "checkout", "-q", "main")
    return run_git(repo, "rev-parse", "HEAD")


def store_ids(repo: Path) -> set[str]:
    """Every object id physically present in a repository's store."""
    out = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "cat-file",
            "--batch-all-objects",
            "--batch-check=%(objectname)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return set(out.stdout.split())


def local_git_runner(repos: Path, calls: list):
    """subprocess.run stand-in for rig.fetch: the fake lock URLs resolve to
    the local seeds, so every clone is real plumbing minus the network.
    Refetches need no rewrite: each checkout's origin already points at its
    seed, exactly as production origins point at the reachable lock URL.
    Calls are recorded verbatim, before the rewrite."""
    real_run = subprocess.run

    def run(argv, **kwargs):
        calls.append(list(argv))
        argv = list(argv)
        if argv[:2] == ["git", "clone"]:
            for index, arg in enumerate(argv):
                if arg.startswith("https://github.com/fake/"):
                    argv[index] = f"file://{repos / Path(arg).stem}"
                    break
        return real_run(argv, **kwargs)

    return run


def build_fetch_root(tmp_path: Path, pins: dict) -> Path:
    """A minimal lab root: the given lock plus the real mount guard."""
    root = tmp_path / "labroot"
    root.mkdir()
    (root / "sources.lock.json").write_text(json.dumps(pins, indent=2) + "\n")
    (root / "lab").mkdir()
    (root / "lab" / "config").mkdir()
    shutil.copyfile(
        ROOT / "lab/check_source_mounts.py", root / "lab/check_source_mounts.py"
    )
    return root


def seed_pins(repos: Path) -> dict:
    return {
        name: {
            "url": f"https://github.com/fake/{name}.git",
            "ref": make_seed_repo(repos, name),
        }
        for name in SOURCES
    }


def partial_filter_config(checkout: Path):
    return subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "config",
            "--get",
            "remote.origin.partialclonefilter",
        ],
        capture_output=True,
        text=True,
    )


def test_fetch_clones_baked_sources_complete_and_keeps_others_partial(
    tmp_path, monkeypatch
):
    """Baked checkouts materialize; non-baked checkouts stay partial.

    rig.BAKED_SOURCES is narrowed to one source so both clone shapes are
    exercised against the same seeds, and the baked checkout proves the
    bake contract directly: a git clone FROM it (what a git+file install
    does) succeeds.
    """
    seeds = tmp_path / "seeds"
    seeds.mkdir()
    pins = seed_pins(seeds)
    root = build_fetch_root(tmp_path, pins)
    calls: list = []
    monkeypatch.setattr(rig.subprocess, "run", local_git_runner(seeds, calls))
    monkeypatch.setattr(rig, "BAKED_SOURCES", frozenset({"medre-adapter-matrix"}))
    rig.fetch(root)

    clones = {
        Path(argv[-1]).name: argv for argv in calls if argv[:2] == ["git", "clone"]
    }
    assert set(clones) == set(SOURCES)
    assert "--filter=blob:none" not in clones["medre-adapter-matrix"]
    assert all(
        "--filter=blob:none" in clones[name]
        for name in SOURCES
        if name != "medre-adapter-matrix"
    )

    baked = root / "sources" / "medre-adapter-matrix"
    assert not (store_ids(seeds / "medre-adapter-matrix") - store_ids(baked))
    assert partial_filter_config(baked).returncode != 0
    subprocess.run(
        ["git", "clone", "-q", f"file://{baked}", str(tmp_path / "servable")],
        check=True,
    )

    partial = root / "sources" / "mmrelay"
    assert partial_filter_config(partial).stdout.strip() == "blob:none"
    assert store_ids(seeds / "mmrelay") - store_ids(partial)


def test_fetch_repairs_an_existing_partial_baked_checkout(tmp_path, monkeypatch):
    """An existing blob:none checkout of a baked source is refetched
    unfiltered in place, without being reset.

    This is the defect's standing damage: checkouts provisioned by an older
    fetch leave every cold bake dead on upload-pack exit 128. The repair
    makes the presets era's manual object-store completion automatic.
    """
    seeds = tmp_path / "seeds"
    seeds.mkdir()
    pins = seed_pins(seeds)
    root = build_fetch_root(tmp_path, pins)
    checkout = root / "sources" / "medre-adapter-matrix"
    checkout.parent.mkdir(parents=True)
    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "--no-checkout",
            "--filter=blob:none",
            f"file://{seeds / 'medre-adapter-matrix'}",
            str(checkout),
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "checkout",
            "-q",
            "--detach",
            pins["medre-adapter-matrix"]["ref"],
        ],
        check=True,
    )
    seed_ids = store_ids(seeds / "medre-adapter-matrix")
    assert seed_ids - store_ids(checkout), "fixture must start genuinely partial"

    calls: list = []
    monkeypatch.setattr(rig.subprocess, "run", local_git_runner(seeds, calls))
    rig.fetch(root)

    refetches = [argv for argv in calls if "--refetch" in argv]
    assert refetches == [
        ["git", "-C", str(checkout), "fetch", "--refetch", "--no-filter", "origin"]
    ]
    assert not (seed_ids - store_ids(checkout))
    assert partial_filter_config(checkout).returncode != 0
    assert run_git(checkout, "rev-parse", "HEAD") == pins["medre-adapter-matrix"]["ref"]
    subprocess.run(
        ["git", "clone", "-q", f"file://{checkout}", str(tmp_path / "servable")],
        check=True,
    )



def test_materialize_failed_fetch_preserves_retry_marker(tmp_path, monkeypatch):
    repo = tmp_path / "checkout"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "remote.origin.partialclonefilter", "blob:none")
    real_run = subprocess.run

    def fail_fetch(argv, **kwargs):
        if "--refetch" in argv:
            raise subprocess.CalledProcessError(128, argv)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(rig.subprocess, "run", fail_fetch)
    with pytest.raises(subprocess.CalledProcessError):
        rig.materialize(repo)
    assert partial_filter_config(repo).stdout.strip() == "blob:none"


def test_materialize_rejects_missing_objects_without_filter(tmp_path):
    seeds = tmp_path / "seeds"
    seeds.mkdir()
    make_seed_repo(seeds, "seed")
    repo = seeds / "seed"
    blob = run_git(repo, "rev-parse", "HEAD:src/medre/core/rendering/text_helpers.py")
    (repo / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
    assert partial_filter_config(repo).returncode != 0
    with pytest.raises(RuntimeError, match="Incomplete baked source"):
        rig.materialize(repo)
