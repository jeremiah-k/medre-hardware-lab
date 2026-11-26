#!/usr/bin/env python3
"""Bind rig evidence to the CONTENT a source selection provides.

WHY THIS EXISTS
---------------
Evidence is bound to what the rig measured -- the content a selection
provides -- never to identifiers. A commit that cannot change what the rig
builds, mounts or runs (a docs fix, a test literal, a CI scaffold edit, a
re-pin to byte-identical source content) must not force a full rig repeat.

To that end every selection is summarised by a content digest over the
CANONICAL LISTING of the git tree: ``git ls-tree -r`` filtered by the
DENYLIST below, sorted, hashed with SHA-256. The digest follows content,
so two commits that differ only in denied paths hash equal no matter how
far apart their commit IDs are.

THE DENYLIST (one rule for pinned repositories AND the lab's own tree)
----------------------------------------------------------------------
The list is a denylist on purpose: anything NOT listed is part of the
digest. Over-inclusion costs a re-run (safe); a wrong exclusion would
carry a stale pass forward (unsafe). Each entry is a path no rig reads:

* any path component named ``.github`` -- scaffold CI workflows;
* any path component named ``.gitignore`` -- it only decides which
  UNTRACKED files git reports; it never changes the bytes of the
  committed tree a checkout materialises;
* the root files ``.gitlab-ci.yml``, ``.coderabbit.yaml``,
  ``.coderabbit.yml``, ``.editorconfig``,
  ``.pre-commit-config.yaml`` -- CI and review/editor/lint scaffolding;
* any component named ``.trunk`` or ``.circleci`` -- formatter and CI
  scaffolding;
* ROOT ``docs/`` and ROOT ``tests/`` directories (FIRST path component
  only -- a ``docs`` or ``tests`` directory deeper inside a package is
  part of the package and stays in the digest);
* root ``*.md`` and ``LICENSE*`` -- prose and licence text.

Everything else that starts with ``.git`` STAYS in the digest, most
importantly ``.gitattributes`` and ``.gitmodules``: both are READ when
a checkout is produced. Checkout attributes decide the bytes the
materialised working tree hands to the Docker builds (line-ending
conversion, clean/smudge filters), and the submodule map names the
submodule sources a checkout brings with it -- a change to either can
change what the rig measures, so it must not hash equal.

For the LAB's own tree the digest additionally excludes ``lab/tests/``
(the lab's own test files) and ``sources.lock.json`` (the lock's entries
are compared by their CONTENT digests, so the file's own bytes must not
enter the digest or a re-pin to identical content would look like a
change), plus the READ-SIDE FILES ``lab/tools/openhop_case_summary.py``
and ``lab/openhop_selection.py``. The read-side files are pure functions
rebuilt from the case files and this module whenever they are read: a
change to the aggregation or to the fence is always in effect and cannot
leave a stale judgement behind. Everything that produces, drives or
guards case evidence -- the three matrix drivers, the fault proxy, the
Dockerfiles, compose, bring-up and bridge scripts, ``lab/openhop_pins.py``,
``lab/version_record.py``, ``lab/check_source_mounts.py``, configs --
STAYS in the digest.

Pure git plumbing: no export, no checkout, no Docker.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

# Files whose content is NOT part of lab_tooling_sha256 (and only these):
# see the module docstring for why each is safe to exclude.
READ_SIDE_FILES = (
    "lab/tools/openhop_case_summary.py",
    "lab/openhop_selection.py",
)

COMMIT_RE = re.compile(r"[0-9a-f]{40}")


def _git_bytes(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
    )
    return result.stdout


def _excluded(path: str) -> bool:
    """The shared denylist: a path no rig reads (see module docstring)."""
    parts = path.split("/")
    if any(part in {".github", ".gitignore", ".trunk", ".circleci"} for part in parts):
        return True
    if path in {
        ".gitlab-ci.yml",
        ".coderabbit.yaml",
        ".coderabbit.yml",
        ".editorconfig",
        ".pre-commit-config.yaml",
    }:
        return True
    first = parts[0]
    if first in {"docs", "tests"}:
        return True
    if len(parts) == 1 and first.endswith(".md"):
        return True
    if len(parts) == 1 and first.startswith("LICENSE"):
        return True
    return False


def _lab_excluded(path: str) -> bool:
    """Lab-tree additions: lab/tests/, sources.lock.json, read-side files."""
    parts = path.split("/")
    if len(parts) > 1 and parts[0] == "lab" and parts[1] == "tests":
        return True
    if path == "sources.lock.json":
        return True
    if path in READ_SIDE_FILES:
        return True
    return False


def _tree_digest(repo: Path, revision: str, *, lab_rule: bool) -> str:
    """SHA-256 over the canonical (filtered, sorted) listing of a tree.

    The listing is ``git ls-tree -r -z`` of ``repo`` at ``revision`` --
    one ``<mode> <type> <object>\\t<path>`` entry per path, so a file's
    entry carries its content's object SHA. Entries whose path is
    denied are dropped, the rest are sorted and hashed line by line.
    ``-z`` keeps raw paths, so the digest does not depend on git's path
    quoting.
    """
    raw = _git_bytes(repo, "ls-tree", "-r", "-z", revision)
    lines = []
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        _meta, _, path = entry.partition(b"\t")
        text = path.decode("utf-8", errors="surrogateescape")
        if _excluded(text):
            continue
        if lab_rule and _lab_excluded(text):
            continue
        lines.append(entry.decode("utf-8", errors="surrogateescape"))
    digest = hashlib.sha256()
    for text in sorted(lines):
        digest.update(text.encode("utf-8", errors="surrogateescape") + b"\n")
    return digest.hexdigest()


def content_digest(repo: Path, commit_or_tree: str) -> str:
    """SHA-256 over the canonical listing of one commit or tree."""
    return _tree_digest(repo, commit_or_tree, lab_rule=False)


def lab_tooling_digest(lab_root: Path, revision: str) -> str:
    """Content digest of the lab tree at ``revision`` (lab-tree rule)."""
    return _tree_digest(lab_root, revision, lab_rule=True)


def head_revision(lab_root: Path) -> str:
    """The lab repo's HEAD commit."""
    return _git_bytes(lab_root, "rev-parse", "HEAD").decode().strip()


def is_committed_clean(lab_root: Path) -> bool:
    """True when the lab tree has no uncommitted TRACKED changes."""
    return not _git_bytes(
        lab_root, "status", "--porcelain", "--untracked-files=no"
    ).strip()


def lock_bytes_at(lab_root: Path, revision: str) -> bytes:
    """The lock file's exact bytes as committed at ``revision``."""
    return _git_bytes(lab_root, "show", f"{revision}:sources.lock.json")


def _pin_content_digest(checkout: Path, key: str, commit: str) -> str:
    if not COMMIT_RE.fullmatch(commit):
        raise ValueError(
            f"cannot establish the selection: {key} pin {commit} is not a "
            "full commit id"
        )
    try:
        return content_digest(checkout, commit)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(
            f"cannot establish the selection: {key} {commit} is not "
            f"available in {checkout}"
        ) from exc


def selection_record(
    lab_root: Path, profile: str, *, revision: str | None = None
) -> dict:
    """Record the content a selection provides to the rig.

    Pins are read from ``sources.lock.json`` AS COMMITTED at ``revision``
    (default HEAD), so the same function reconstructs the selection of a
    recorded run from its lab revision alone. Every lock key is resolved
    to the seeded ``sources/<key>`` checkout (the same resolution
    ``lab/openhop-bridge/seed_candidate_sources.sh`` and ``lab/up.sh``
    use) and digested at its pinned commit.
    """
    if revision is None:
        revision = head_revision(lab_root)
    lock = json.loads(lock_bytes_at(lab_root, revision).decode("utf-8"))
    pins: dict[str, dict[str, str]] = {}
    for key, entry in lock.items():
        commit = entry["ref"]
        checkout = Path(lab_root) / "sources" / key
        pins[key] = {
            "commit": commit,
            "content_sha256": _pin_content_digest(checkout, key, commit),
        }
    record = {
        "profile": profile,
        "pins": pins,
        "lab_tooling_sha256": lab_tooling_digest(lab_root, revision),
    }
    record["selection_sha256"] = selection_digest(record)
    return record


def selection_digest(record: dict) -> str:
    """SHA-256 of the canonical JSON of a record's three content fields."""
    fields = {
        "profile": record["profile"],
        "pins": record["pins"],
        "lab_tooling_sha256": record["lab_tooling_sha256"],
    }
    canonical = json.dumps(
        fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def describe_difference(old: dict, new: dict) -> str | None:
    """Name the first content difference between two selections, or None.

    Compares the FIELDS, not the stored digests, so the message can point
    at the lock key whose content moved (with the old and new commit) or
    at the lab tooling.
    """
    if old["profile"] != new["profile"]:
        return (
            f"the profile differs (was {old['profile']!r}, " f"now {new['profile']!r})"
        )
    old_pins, new_pins = old["pins"], new["pins"]
    for key in sorted(set(old_pins) | set(new_pins)):
        was, now = old_pins.get(key), new_pins.get(key)
        if was is None:
            return f"pin {key!r} is new"
        if now is None:
            return f"pin {key!r} was removed"
        if was["content_sha256"] != now["content_sha256"]:
            return (
                f"pin {key!r} content differs (was {was['commit']}, "
                f"now {now['commit']})"
            )
    if old["lab_tooling_sha256"] != new["lab_tooling_sha256"]:
        return "the lab tooling content differs"
    return None
