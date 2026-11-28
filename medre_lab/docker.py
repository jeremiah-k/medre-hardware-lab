"""Prepare exact source selection for the existing Docker scenario suite."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import secrets
import shlex
import subprocess
from pathlib import Path

SOURCES = {
    "medre": (
        "MEDRE_CORE_SRC",
        "src/medre/core/rendering/text_helpers.py",
        "medre.core.rendering.text_helpers",
        "/src/core",
    ),
    "medre-adapter-matrix": (
        "MEDRE_MATRIX_SRC",
        "src/medre/adapters/matrix/renderer.py",
        "medre.adapters.matrix.renderer",
        "/src/matrix",
    ),
    "medre-adapter-meshtastic": (
        "MEDRE_MESHTASTIC_SRC",
        "src/medre/adapters/meshtastic/renderer.py",
        "medre.adapters.meshtastic.renderer",
        "/src/meshtastic",
    ),
    "medre-adapter-lxmf": (
        "MEDRE_LXMF_SRC",
        "src/medre/adapters/lxmf/renderer.py",
        "medre.adapters.lxmf.renderer",
        "/src/lxmf",
    ),
    "mmrelay": ("MMRELAY_SRC", "src/mmrelay/main.py", "mmrelay.main", "/src"),
    # openHop profile sources. The medre 0.1.0 pin above is incompatible with
    # the meshcore adapter (it requires medre>=0.2.0), so the openHop profile
    # carries its OWN medre/adapter pins in separate checkouts. The default
    # lab rig keeps using the five original pins unchanged.
    "medre-openhop": (
        "MEDRE_OPENHOP_SRC",
        "src/medre/core/rendering/text_helpers.py",
        "medre.core.rendering.text_helpers",
        "/src/core",
    ),
    "medre-adapter-meshcore": (
        "MEDRE_MESHCORE_SRC",
        "src/medre/adapters/meshcore/renderer.py",
        "medre.adapters.meshcore.renderer",
        "/src/meshcore",
    ),
    "medre-adapter-meshtastic-openhop": (
        "MEDRE_MESHTASTIC_OPENHOP_SRC",
        "src/medre/adapters/meshtastic/renderer.py",
        "medre.adapters.meshtastic.renderer",
        "/src/meshtastic",
    ),
    "openhop-core": (
        "OPENHOP_CORE_SRC",
        "src/openhop_core/__init__.py",
        "openhop_core",
        "/src/openhop-core",
    ),
    "openhop-repeater": (
        "OPENHOP_REPEATER_SRC",
        "repeater/config.py",
        "repeater.config",
        "/src/openhop-repeater",
    ),
}

# Sources whose checkout feeds a Docker build context. compose.yaml resolves
# every *_SRC variable below as a named build context, and the lab recipes
# (lab/mmrelay/Dockerfile, lab/medre/Dockerfile, lab/openhop/Dockerfile,
# lab/medre-openhop/Dockerfile) COPY-install from those contexts -- the
# matrix bake installs via git+file FROM the copied checkout itself. An
# image bake therefore installs from the checkout, so its object store must
# be servable: serving an install from a blob:none partial clone fails
# (upload-pack neither allows filters nor lazy-fetches while serving).
# Today every locked source is baked; a future source that never enters a
# build context can stay out of this set and keep its cheap partial clone.
BAKED_SOURCES = frozenset(SOURCES)

# Source groups are the sources each rig qualifies. The default rig keeps
# the original five sources untouched; the openHop profile adds its own five
# (a second medre/adapter stack plus the two openHop checkouts).
DEFAULT_SOURCE_GROUP = (
    "medre",
    "medre-adapter-matrix",
    "medre-adapter-meshtastic",
    "medre-adapter-lxmf",
    "mmrelay",
)
OPENHOP_SOURCE_GROUP = (
    "medre-openhop",
    "medre-adapter-meshcore",
    "medre-adapter-meshtastic-openhop",
    "openhop-core",
    "openhop-repeater",
)
# The default rig's manifest (checked with live container inspection by
# lab/run-scenarios.sh) and the openHop rig's manifest (checked separately
# while the openHop profile is up) are separate files: see prepare().
# Each names the sources MOUNTED read-only into that container: mmrelay is
# baked into the relay images and mounted nowhere, so it is qualified by
# the host-only source check, not by a container mount.
DEFAULT_MANIFEST = {
    "medre-lab-medre-normal": DEFAULT_SOURCE_GROUP[:4],
}
OPENHOP_MANIFEST = {
    "medre-lab-medre-openhop": OPENHOP_SOURCE_GROUP[:3],
    "medre-lab-openhop-repeater": OPENHOP_SOURCE_GROUP[3:],
}

# Public HTTPS GitHub URLs without credentials; shared with the preset
# schema (medre_lab.presets) so both validate pins identically.
SOURCE_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+(?:\.git)?")


def lock(root):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate source lock key")
            result[key] = value
        return result

    data = json.loads(
        (root / "sources.lock.json").read_text(), object_pairs_hook=unique
    )
    if set(data) != set(SOURCES):
        raise ValueError("source lock must name the Docker dependencies for both rigs")
    for name, entry in data.items():
        if set(entry) != {"url", "ref"} or not re.fullmatch(
            r"[0-9a-f]{40}", entry["ref"]
        ):
            raise ValueError(f"{name}: exact full commit SHA required")
        if not SOURCE_URL_RE.fullmatch(entry["url"]):
            raise ValueError(
                "source URLs must be public HTTPS GitHub URLs without credentials"
            )
    return data


def clone_command(name, url, target):
    """The clone argv for one locked source.

    Baked sources are cloned WITHOUT the blob filter: an image build
    installs from the checkout, so a partial clone would break every cold
    bake. Non-baked sources never feed a build context; the filter keeps
    their clones cheap.
    """
    command = ["git", "clone", "--no-checkout"]
    if name not in BAKED_SOURCES:
        command.append("--filter=blob:none")
    return [*command, url, str(target)]


def materialize(target):
    """Complete the object store before a checkout can serve an image bake.

    Retain the partial-clone marker until the unfiltered fetch succeeds and
    the checked-out revision has a complete local closure. A failed fetch
    must not leave the next invocation believing the store is complete.
    """
    git = ["git", "-C", str(target)]
    partial = subprocess.run(
        [*git, "config", "--get", "remote.origin.partialclonefilter"],
        capture_output=True,
        text=True,
    )
    if partial.returncode not in (0, 1):
        partial.check_returncode()
    filtered = partial.returncode == 0
    if filtered:
        subprocess.run(
            [*git, "fetch", "--refetch", "--no-filter", "origin"], check=True
        )
    # Check physical availability, not merely the checkout's HEAD or the
    # remote configuration. Detached candidate commits may not be reachable
    # from any remote ref, and a previously interrupted repair may already
    # have removed the filter. Do not silently lazy-fetch during this check.
    closure = subprocess.run(
        [*git, "rev-list", "--objects", "--missing=print", "HEAD"],
        env={**os.environ, "GIT_NO_LAZY_FETCH": "1"},
        capture_output=True,
        text=True,
        check=True,
    )
    if any(line.startswith("?") for line in closure.stdout.splitlines()):
        raise RuntimeError(f"Incomplete baked source object closure: {target}")
    if filtered:
        subprocess.run(
            [*git, "config", "--unset", "remote.origin.partialclonefilter"],
            check=True,
        )


def fetch(root):
    for name, entry in lock(root).items():
        target = root / "sources" / name
        if not target.exists():
            subprocess.run(clone_command(name, entry["url"], target), check=True)
            subprocess.run(
                ["git", "-C", str(target), "checkout", "--detach", entry["ref"]],
                check=True,
            )
        if name in BAKED_SOURCES:
            # A bake installs from this checkout, so its object store must
            # be servable. This also repairs a partial clone left behind by
            # an older fetch; existing checkouts are still never reset.
            materialize(target)
        # Existing checkouts are deliberately never reset/rebased/cleaned.
    prepare(root)


def entries(root):
    pins = lock(root)
    return [
        {
            "source": str(root / "sources" / name),
            "ref": pins[name]["ref"],
            "file": info[1],
            "module": info[2],
            "target": info[3],
        }
        for name, info in SOURCES.items()
    ]


def prepare(root):
    root = root.resolve()
    selected = entries(root)
    spec = importlib.util.spec_from_file_location(
        "source_guard", root / "lab/check_source_mounts.py"
    )
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    for name, entry in zip(SOURCES, selected, strict=True):
        # The openHop launchers validate their manifest before using these pins.
        if name in DEFAULT_SOURCE_GROUP:
            guard.check_source(entry)
    state = root / "lab/state"
    state.mkdir(parents=True, exist_ok=True)
    by_name = {entry["source"]: entry for entry in selected}

    def selection(names):
        return [by_name[str(root / "sources" / name)] for name in names]

    # TWO manifests, one per rig. The default manifest names ONLY the
    # default rig's container: lab/run-scenarios.sh checks it with live
    # container inspection (no --host-only), so adding the openHop
    # containers here would demand they be running during the default
    # scenario suite. The openHop manifest is checked separately, and only
    # while the openHop profile is up.
    manifest = state / "sources.json"
    manifest.write_text(
        json.dumps(
            {
                container: selection(names)
                for container, names in DEFAULT_MANIFEST.items()
            },
            indent=2,
        )
        + "\n"
    )
    openhop_manifest = state / "sources-openhop.json"
    openhop_manifest.write_text(
        json.dumps(
            {
                container: selection(names)
                for container, names in OPENHOP_MANIFEST.items()
            },
            indent=2,
        )
        + "\n"
    )
    env = {
        "LAB_ROOT": str(root),
        "LAB_UID": str(os.getuid()),
        "LAB_GID": str(os.getgid()),
        "LAB_SOURCE_MANIFEST": str(manifest),
        "LAB_SOURCE_MANIFEST_OPENHOP": str(openhop_manifest),
    }
    for name, info in SOURCES.items():
        env[info[0]] = str(root / "sources" / name)
    env["MMRELAY_BAKE_CORE_SRC"] = env["MEDRE_CORE_SRC"]
    env["MEDRE_CORE_REF"] = lock(root)["medre"]["ref"]
    env["MMRELAY_BAKE_CORE_REF"] = env["MEDRE_CORE_REF"]
    env["MEDRE_MATRIX_REF"] = lock(root)["medre-adapter-matrix"]["ref"]
    # The openHop profile carries its own pins; the build args record the
    # exact commits the images were baked from.
    env["OPENHOP_CORE_REF"] = lock(root)["openhop-core"]["ref"]
    env["OPENHOP_REPEATER_REF"] = lock(root)["openhop-repeater"]["ref"]
    env["MEDRE_OPENHOP_REF"] = lock(root)["medre-openhop"]["ref"]
    env["MEDRE_MESHCORE_REF"] = lock(root)["medre-adapter-meshcore"]["ref"]
    env["MEDRE_MESHTASTIC_OPENHOP_REF"] = lock(root)[
        "medre-adapter-meshtastic-openhop"
    ]["ref"]
    # This generated file is authoritative; user hardware config is separate.
    target = root / ".env"
    target.write_text(
        "# Generated by medre-lab docker prepare from sources.lock.json\n"
        + "".join(f"{key}={shlex.quote(value)}\n" for key, value in env.items())
    )
    target.chmod(0o600)
    mesh = root / "lab/config/meshnet.env"
    if not mesh.exists():
        lines = ["# Private synthetic-radio keys generated for this installation."]
        for side in "ABC":
            lines += [
                f'MESH_{side}_PRIMARY_NAME="LabPrim{side}"',
                f'MESH_{side}_PRIMARY_PSK="0x{secrets.token_hex(32)}"',
                f"MESH_{side}_SECONDARY_INDEX=2",
                f'MESH_{side}_SECONDARY_NAME="LabSec{side}"',
                f'MESH_{side}_SECONDARY_PSK="0x{secrets.token_hex(32)}"',
            ]
        mesh.write_text("\n".join(lines) + "\n")
        mesh.chmod(0o600)
    return env


def assert_no_other_rig(root):
    # Container names in this migrated topology stay stable for scenario probes.
    result = subprocess.run(
        ["docker", "inspect", "medre-lab-synapse"], capture_output=True, text=True
    )
    if result.returncode == 0:
        container = json.loads(result.stdout)[0]
        working = (
            container.get("Config", {})
            .get("Labels", {})
            .get("com.docker.compose.project.working_dir")
        )
        if working and Path(working).resolve() != root.resolve():
            raise RuntimeError(
                "another checkout owns this Docker topology; stop it before switching"
            )
