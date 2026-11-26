"""Offline tests for the content-bound selection rule and its fence.

The rule: rig evidence is bound to the CONTENT a selection provides,
never to identifiers. A commit that cannot change what the rig builds,
mounts or runs (docs, tests, CI scaffolding, a re-pin to identical
content) must accept the recorded evidence again; a change to included
content must refuse it and name what moved.

The fixtures are small temporary git repositories (init + commit, no
network, no Docker).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

LAB_DIR = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, LAB_DIR / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


selection = _load("openhop_selection", "openhop_selection.py")
summary = _load("openhop_case_summary", "tools/openhop_case_summary.py")

IDENTITY = ("-c", "user.name=Lab Test", "-c", "user.email=lab@example.invalid")


def run_git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, stderr=subprocess.STDOUT
    )


def commit_all(repo: Path, message: str) -> str:
    run_git(repo, "add", "-A")
    run_git(repo, *IDENTITY, "commit", "-q", "-m", message)
    return run_git(repo, "rev-parse", "HEAD").strip()


def write_files(base: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def init_repo(path: Path, files: dict[str, str], message: str = "base") -> str:
    path.mkdir(parents=True)
    run_git(path, "init", "-q", "-b", "main")
    write_files(path, files)
    return commit_all(path, message)


def seed_checkout(lab: Path, key: str, source_repo: Path) -> Path:
    target = lab / "sources" / key
    subprocess.check_call(["git", "clone", "-q", str(source_repo), str(target)])
    return target


def make_source_repo(path: Path) -> dict[str, str]:
    """A source repository with excluded-path and included-path changes.

    ``base`` carries one source file plus ``.gitattributes`` and
    ``.gitmodules`` (checkout attributes and submodule maps are part of
    the digest). Returns the commits: ``scaffold`` (adds every
    denylisted path), ``scaffold2`` (changes them), ``ignore`` /
    ``rabbit`` / ``gitlabci`` / ``githubonly`` (single excluded-path
    changes), ``content`` (changes the source file), ``nested`` (adds a
    tests/docs directory INSIDE a package), ``attrs`` and ``modules``
    (change checkout attributes / the submodule map).
    """
    commits: dict[str, str] = {}
    commits["base"] = init_repo(
        path,
        {
            "src/lib.py": "value = 1\n",
            ".gitattributes": "* text=auto\n",
            ".gitmodules": '[submodule "vendor/lib"]\n\tpath = vendor/lib\n'
            "\turl = https://example.invalid/lib.git\n",
        },
    )
    write_files(
        path,
        {
            "tests/test_lib.py": "def test_x():\n    assert True\n",
            "docs/page.md": "# docs\n",
            ".github/workflows/ci.yml": "on: push\n",
            ".gitignore": "*.pyc\n",
            "src/.gitignore": "*.cache\n",
            ".trunk/trunk.yaml": "version: 0.1\n",
            ".circleci/config.yml": "version: 2\n",
            ".coderabbit.yaml": "reviews: {}\n",
            ".coderabbit.yml": "reviews: {}\n",
            ".editorconfig": "root = true\n",
            ".pre-commit-config.yaml": "repos: []\n",
            "README.md": "# readme\n",
            "LICENSE": "MIT\n",
        },
    )
    commits["scaffold"] = commit_all(path, "scaffold only")
    write_files(
        path,
        {
            "tests/test_lib.py": "def test_x():\n    assert True  # changed\n",
            "docs/page.md": "# docs 2\n",
            ".github/workflows/ci.yml": "on: push\njobs: {}\n",
            ".gitignore": "*.pyc\n.cache/\n",
            "src/.gitignore": "*.cache\n*.log\n",
            "README.md": "# readme 2\n",
            "LICENSE": "MIT-0\n",
            ".coderabbit.yaml": "reviews: {paths: {}}\n",
            ".coderabbit.yml": "reviews: {paths: {}}\n",
        },
    )
    commits["scaffold2"] = commit_all(path, "excluded paths only")
    write_files(
        path, {".gitignore": "*.pyc\n.cache/\nbuilddir/\n", "src/.gitignore": ""}
    )
    commits["ignore"] = commit_all(path, "root and nested .gitignore only")
    write_files(
        path,
        {
            ".coderabbit.yaml": "early_access: true\n",
            ".coderabbit.yml": "early_access: true\n",
        },
    )
    commits["rabbit"] = commit_all(path, "coderabbit files only")
    write_files(path, {".gitlab-ci.yml": "stages: []\n"})
    commits["gitlabci"] = commit_all(path, "gitlab ci file only")
    write_files(path, {".github/workflows/ci.yml": "on: push\njobs: {build: {}}\n"})
    commits["githubonly"] = commit_all(path, "github workflow only")
    write_files(path, {"src/lib.py": "value = 2\n"})
    commits["content"] = commit_all(path, "content change")
    write_files(
        path,
        {
            "deep/package/tests/inner.py": "value = 3\n",
            "deep/package/docs/note.md": "# note\n",
        },
    )
    commits["nested"] = commit_all(path, "nested package dirs")
    write_files(path, {"src/with space.py": "value = 4\n"})
    commits["space"] = commit_all(path, "path with a space")
    write_files(path, {".gitattributes": "* text=auto eol=lf\n"})
    commits["attrs"] = commit_all(path, "checkout attributes change")
    write_files(
        path,
        {
            ".gitmodules": '[submodule "vendor/lib"]\n\tpath = vendor/lib\n'
            "\turl = https://example.invalid/lib2.git\n",
        },
    )
    commits["modules"] = commit_all(path, "submodule map change")
    return commits


def make_lab(
    root: Path,
    pins: dict[str, tuple[Path, str]],
    *,
    code: str = "DRIVER = 1\n",
    lock_text: str | None = None,
) -> tuple[Path, str]:
    """A synthetic lab repo with committed lock, seeded checkouts, code."""
    lab = root / "lab"
    lab.mkdir()
    run_git(lab, "init", "-q", "-b", "main")
    if lock_text is None:
        lock = {
            key: {
                "url": f"https://example.invalid/{key}.git",
                "ref": commit,
            }
            for key, (_repo, commit) in pins.items()
        }
        lock_text = json.dumps(lock, indent=2, sort_keys=True) + "\n"
    write_files(
        lab,
        {
            ".gitignore": "sources/\n",
            "sources.lock.json": lock_text,
            "lab/tools/driver.py": code,
            "lab/openhop/Dockerfile": "FROM python:3.12\n",
            "lab/tests/test_driver.py": "def test_driver():\n    assert True\n",
        },
    )
    for key, (repo, _commit) in pins.items():
        seed_checkout(lab, key, repo)
    head = commit_all(lab, "lab base")
    return lab, head


def legacy_stamp(lab: Path, revision: str, profile: str = "bridge") -> dict:
    lock_sha = hashlib.sha256(
        subprocess.check_output(
            ["git", "-C", str(lab), "show", f"{revision}:sources.lock.json"]
        )
    ).hexdigest()
    return {
        "lab_revision": revision,
        "lock_sha256": lock_sha,
        "profile": profile,
    }


def write_case(directory: Path, name: str, payload: dict) -> Path:
    path = directory / f"{name}.json"
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return path


def evidence_dir(lab: Path, name: str = "evidence") -> Path:
    directory = lab.parent / name
    directory.mkdir()
    return directory


# --- the content rule --------------------------------------------------------


def test_identical_content_under_different_commits_hashes_equal(tmp_path):
    commits = make_source_repo(tmp_path / "repo")
    assert commits["base"] != commits["scaffold"]
    assert selection.content_digest(
        tmp_path / "repo", commits["base"]
    ) == selection.content_digest(tmp_path / "repo", commits["scaffold"])


def test_a_change_under_an_excluded_path_hashes_equal(tmp_path):
    commits = make_source_repo(tmp_path / "repo")
    assert selection.content_digest(
        tmp_path / "repo", commits["scaffold"]
    ) == selection.content_digest(tmp_path / "repo", commits["scaffold2"])


def test_a_change_under_src_hashes_different(tmp_path):
    commits = make_source_repo(tmp_path / "repo")
    assert selection.content_digest(
        tmp_path / "repo", commits["scaffold"]
    ) != selection.content_digest(tmp_path / "repo", commits["content"])


def test_a_nested_docs_or_tests_directory_is_included(tmp_path):
    commits = make_source_repo(tmp_path / "repo")
    assert selection.content_digest(
        tmp_path / "repo", commits["content"]
    ) != selection.content_digest(tmp_path / "repo", commits["nested"])


def test_a_path_with_a_space_is_included(tmp_path):
    commits = make_source_repo(tmp_path / "repo")
    assert selection.content_digest(
        tmp_path / "repo", commits["nested"]
    ) != selection.content_digest(tmp_path / "repo", commits["space"])


def test_a_gitattributes_change_hashes_different(tmp_path):
    commits = make_source_repo(tmp_path / "repo")
    assert selection.content_digest(
        tmp_path / "repo", commits["nested"]
    ) != selection.content_digest(tmp_path / "repo", commits["attrs"])


def test_a_gitmodules_change_hashes_different(tmp_path):
    commits = make_source_repo(tmp_path / "repo")
    assert selection.content_digest(
        tmp_path / "repo", commits["attrs"]
    ) != selection.content_digest(tmp_path / "repo", commits["modules"])


def test_a_gitignore_change_root_and_nested_hashes_equal(tmp_path):
    commits = make_source_repo(tmp_path / "repo")
    assert selection.content_digest(
        tmp_path / "repo", commits["scaffold2"]
    ) == selection.content_digest(tmp_path / "repo", commits["ignore"])


def test_a_github_change_hashes_equal(tmp_path):
    commits = make_source_repo(tmp_path / "repo")
    assert selection.content_digest(
        tmp_path / "repo", commits["gitlabci"]
    ) == selection.content_digest(tmp_path / "repo", commits["githubonly"])


def test_coderabbit_file_changes_hash_equal(tmp_path):
    commits = make_source_repo(tmp_path / "repo")
    assert selection.content_digest(
        tmp_path / "repo", commits["ignore"]
    ) == selection.content_digest(tmp_path / "repo", commits["rabbit"])


def test_a_root_gitlab_ci_change_hashes_equal(tmp_path):
    commits = make_source_repo(tmp_path / "repo")
    assert selection.content_digest(
        tmp_path / "repo", commits["rabbit"]
    ) == selection.content_digest(tmp_path / "repo", commits["gitlabci"])


# --- the lab-tree rule -------------------------------------------------------


def make_lab_like_repo(path: Path) -> str:
    return init_repo(
        path,
        {
            "sources.lock.json": '{"medre": {"ref": "a"}}\n',
            "src/medre/core.py": "CORE = 1\n",
            "lab/tools/openhop_backlog_matrix.py": "DRIVER = 1\n",
            "lab/tools/openhop_case_summary.py": "SUM = 1\n",
            "lab/openhop_selection.py": "DIGEST = 1\n",
            "lab/openhop/Dockerfile": "FROM python:3.12\n",
            "lab/tests/test_driver.py": "def test_d():\n    assert True\n",
        },
    )


def lab_digest(path: Path) -> str:
    return selection.lab_tooling_digest(path, "HEAD")


def test_lab_tree_ignores_its_tests_the_lock_and_read_side_files(tmp_path):
    repo = tmp_path / "labrepo"
    make_lab_like_repo(repo)
    baseline = lab_digest(repo)
    for name, text in (
        ("lab/tests/test_driver.py", "def test_d():\n    assert True  # x\n"),
        ("sources.lock.json", '{"medre": {"ref": "b"}}\n'),
        ("lab/tools/openhop_case_summary.py", "SUM = 2\n"),
        ("lab/openhop_selection.py", "DIGEST = 2\n"),
    ):
        write_files(repo, {name: text})
        commit_all(repo, f"change {name}")
        assert lab_digest(repo) == baseline, name


def test_lab_tree_includes_what_the_rig_builds_and_drives(tmp_path):
    repo = tmp_path / "labrepo"
    make_lab_like_repo(repo)
    baseline = lab_digest(repo)
    for name, text in (
        ("lab/tools/openhop_backlog_matrix.py", "DRIVER = 2\n"),
        ("lab/openhop/Dockerfile", "FROM python:3.13\n"),
        ("src/medre/core.py", "CORE = 2\n"),
    ):
        write_files(repo, {name: text})
        commit_all(repo, f"change {name}")
        assert lab_digest(repo) != baseline, name


def test_the_lab_rule_differs_from_the_plain_rule(tmp_path):
    repo = tmp_path / "labrepo"
    make_lab_like_repo(repo)
    assert selection.content_digest(repo, "HEAD") != lab_digest(repo)


# --- the fence ---------------------------------------------------------------


def test_bind_stamps_an_empty_directory_and_accepts_it_again(tmp_path):
    commits = make_source_repo(tmp_path / "repo-a")
    lab, head = make_lab(tmp_path, {"key-a": (tmp_path / "repo-a", commits["content"])})
    directory = evidence_dir(lab)
    summary.bind_source_selection(directory, lab, "bridge")
    stamp = json.loads((directory / summary.SCOPE_NAME).read_text(encoding="utf-8"))
    assert stamp["profile"] == "bridge"
    assert stamp["lab_revision"] == head
    assert stamp["selection_sha256"]
    before = (directory / summary.SCOPE_NAME).read_bytes()
    summary.bind_source_selection(directory, lab, "bridge")
    assert (directory / summary.SCOPE_NAME).read_bytes() == before


def test_bind_accepts_an_old_stamp_when_only_excluded_content_changed(tmp_path):
    commits = make_source_repo(tmp_path / "repo-a")
    lab, first = make_lab(tmp_path, {"key-a": (tmp_path / "repo-a", commits["base"])})
    directory = evidence_dir(lab)
    stamp = legacy_stamp(lab, first)
    write_case(directory, "c1-a", {"case": "c1-a", "pass": True})
    (directory / summary.SCOPE_NAME).write_text(
        json.dumps(stamp, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    # Excluded-path lab changes after the recorded run: the lab's own
    # tests, a doc page, and the lock's formatting (same pins).
    write_files(
        lab,
        {
            "lab/tests/test_driver.py": "def test_driver():\n    assert True  # x\n",
            "docs/notes.md": "# notes\n",
        },
    )
    commit_all(lab, "excluded changes only")
    summary.bind_source_selection(directory, lab, "bridge")
    after = json.loads((directory / summary.SCOPE_NAME).read_text(encoding="utf-8"))
    assert after == stamp


def test_bind_refuses_a_changed_pin_and_names_the_key(tmp_path):
    commits = make_source_repo(tmp_path / "repo-a")
    lab, first = make_lab(tmp_path, {"key-a": (tmp_path / "repo-a", commits["base"])})
    directory = evidence_dir(lab)
    write_case(directory, "c1-a", {"case": "c1-a", "pass": True})
    (directory / summary.SCOPE_NAME).write_text(
        json.dumps(legacy_stamp(lab, first), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_files(
        lab,
        {
            "sources.lock.json": json.dumps(
                {
                    "key-a": {
                        "url": "https://example.invalid/key-a.git",
                        "ref": commits["content"],
                    }
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        },
    )
    commit_all(lab, "re-pin to a content change")
    with pytest.raises(ValueError, match=r"key-a.*content differs"):
        summary.bind_source_selection(directory, lab, "bridge")


def test_bind_refuses_changed_lab_tooling(tmp_path):
    commits = make_source_repo(tmp_path / "repo-a")
    lab, first = make_lab(tmp_path, {"key-a": (tmp_path / "repo-a", commits["base"])})
    directory = evidence_dir(lab)
    write_case(directory, "c1-a", {"case": "c1-a", "pass": True})
    (directory / summary.SCOPE_NAME).write_text(
        json.dumps(legacy_stamp(lab, first), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_files(lab, {"lab/tools/driver.py": "DRIVER = 2\n"})
    commit_all(lab, "tooling change")
    with pytest.raises(ValueError, match="lab tooling content differs"):
        summary.bind_source_selection(directory, lab, "bridge")


def test_bind_refuses_unbound_historical_evidence(tmp_path):
    commits = make_source_repo(tmp_path / "repo-a")
    lab, _head = make_lab(tmp_path, {"key-a": (tmp_path / "repo-a", commits["base"])})
    directory = evidence_dir(lab)
    write_case(directory, "c1-a", {"case": "c1-a", "pass": True})
    with pytest.raises(ValueError, match="historical evidence is unbound"):
        summary.bind_source_selection(directory, lab, "bridge")


def test_bind_refuses_an_old_stamp_whose_commit_is_unavailable(tmp_path):
    repo_b = tmp_path / "repo-b"
    seeded = init_repo(repo_b, {"src/b.py": "B = 1\n"}, "b one")
    lab, _head = make_lab(tmp_path, {"key-b": (repo_b, seeded)})
    later = init_repo(tmp_path / "repo-b2", {"src/b.py": "B = 2\n"}, "b two")
    write_files(
        lab,
        {
            "sources.lock.json": json.dumps(
                {
                    "key-b": {
                        "url": "https://example.invalid/key-b.git",
                        "ref": later,
                    }
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        },
    )
    recorded = commit_all(lab, "pin an unseeded commit")
    directory = evidence_dir(lab)
    write_case(directory, "c1-a", {"case": "c1-a", "pass": True})
    (directory / summary.SCOPE_NAME).write_text(
        json.dumps(legacy_stamp(lab, recorded), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=f"key-b {later} is not available"):
        summary.bind_source_selection(directory, lab, "bridge")


def test_bind_refuses_a_tampered_new_format_stamp(tmp_path):
    commits = make_source_repo(tmp_path / "repo-a")
    lab, _head = make_lab(tmp_path, {"key-a": (tmp_path / "repo-a", commits["base"])})
    directory = evidence_dir(lab)
    summary.bind_source_selection(directory, lab, "bridge")
    scope = directory / summary.SCOPE_NAME
    stamp = json.loads(scope.read_text(encoding="utf-8"))
    stamp["pins"]["key-a"]["content_sha256"] = "0" * 64
    scope.write_text(
        json.dumps(stamp, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="not self-consistent"):
        summary.bind_source_selection(directory, lab, "bridge")


def test_bind_requires_a_committed_clean_lab(tmp_path):
    commits = make_source_repo(tmp_path / "repo-a")
    lab, _head = make_lab(tmp_path, {"key-a": (tmp_path / "repo-a", commits["base"])})
    directory = evidence_dir(lab)
    write_files(lab, {"lab/tools/driver.py": "DRIVER = 9\n"})  # uncommitted
    with pytest.raises(ValueError, match="lab source is dirty"):
        summary.bind_source_selection(directory, lab, "bridge")


def test_bind_refuses_a_different_profile(tmp_path):
    commits = make_source_repo(tmp_path / "repo-a")
    lab, _head = make_lab(tmp_path, {"key-a": (tmp_path / "repo-a", commits["base"])})
    directory = evidence_dir(lab)
    summary.bind_source_selection(directory, lab, "bridge")
    with pytest.raises(ValueError, match="the profile differs"):
        summary.bind_source_selection(directory, lab, "other")


# --- the carry-forward command -----------------------------------------------


def bound_legacy_directory(tmp_path, passes=(True, True)):
    commits = make_source_repo(tmp_path / "repo-a")
    lab, first = make_lab(tmp_path, {"key-a": (tmp_path / "repo-a", commits["base"])})
    directory = evidence_dir(lab)
    for index, ok in enumerate(passes):
        write_case(
            directory, f"c{index + 1}-x", {"case": f"c{index + 1}-x", "pass": ok}
        )
    (directory / summary.SCOPE_NAME).write_text(
        json.dumps(legacy_stamp(lab, first), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return lab, directory, first


def test_verify_selection_accepts_and_appends_carry_forward(tmp_path, capsys):
    lab, directory, first = bound_legacy_directory(tmp_path)
    stamp_before = (directory / summary.SCOPE_NAME).read_bytes()
    code = summary.main(["--verify-selection", str(directory)], lab_root=lab)
    assert code == 0
    assert capsys.readouterr().out.strip() == "2 cases: 2 pass"
    carry = json.loads(
        (directory / summary.CARRY_FORWARD_NAME).read_text(encoding="utf-8")
    )
    assert len(carry) == 1
    entry = carry[0]
    assert set(entry) == {"verified_at", "selection_sha256", "from", "to"}
    assert entry["from"]["lab_revision"] == first
    assert entry["to"]["lab_revision"] == run_git(lab, "rev-parse", "HEAD").strip()
    assert entry["to"]["lock_sha256"]
    # The recorded stamp is never rewritten.
    assert (directory / summary.SCOPE_NAME).read_bytes() == stamp_before
    summary.main(["--verify-selection", str(directory)], lab_root=lab)
    carry = json.loads(
        (directory / summary.CARRY_FORWARD_NAME).read_text(encoding="utf-8")
    )
    assert len(carry) == 2


def test_verify_selection_refuses_a_changed_pin(tmp_path, capsys):
    commits = make_source_repo(tmp_path / "repo-b")
    lab, directory, _first = bound_legacy_directory(tmp_path)
    write_files(
        lab,
        {
            "sources.lock.json": json.dumps(
                {
                    "key-a": {
                        "url": "https://example.invalid/key-a.git",
                        "ref": commits["content"],
                    }
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        },
    )
    commit_all(lab, "re-pin to a content change")
    code = summary.main(["--verify-selection", str(directory)], lab_root=lab)
    assert code == 1
    assert "key-a" in capsys.readouterr().err


def test_verify_selection_refuses_an_unbound_directory(tmp_path, capsys):
    commits = make_source_repo(tmp_path / "repo-a")
    lab, _head = make_lab(tmp_path, {"key-a": (tmp_path / "repo-a", commits["base"])})
    directory = evidence_dir(lab)
    write_case(directory, "c1-x", {"case": "c1-x", "pass": True})
    code = summary.main(["--verify-selection", str(directory)], lab_root=lab)
    assert code == 1
    assert "unbound" in capsys.readouterr().err


def test_verify_selection_refuses_when_a_case_is_not_a_pass(tmp_path, capsys):
    lab, directory, _first = bound_legacy_directory(tmp_path, passes=(True, False))
    code = summary.main(["--verify-selection", str(directory)], lab_root=lab)
    assert code == 1
    assert "c2-x" in capsys.readouterr().err


def test_verify_selection_refuses_a_profile_mismatch(tmp_path, capsys):
    lab, directory, _first = bound_legacy_directory(tmp_path)
    code = summary.main(
        ["--verify-selection", str(directory), "--profile", "other"], lab_root=lab
    )
    assert code == 1
    assert "profile differs" in capsys.readouterr().err


def test_the_summary_path_regenerates_with_an_old_format_stamp(tmp_path, capsys):
    lab, directory, first = bound_legacy_directory(tmp_path)
    code = summary.main([str(directory)], lab_root=lab)
    assert code == 0
    built = json.loads(capsys.readouterr().out)
    assert built["case_count"] == 2
    assert built["source_selection"] == {
        "lab_revision": first,
        "lock_sha256": legacy_stamp(lab, first)["lock_sha256"],
        "profile": "bridge",
    }
    stamp_before = (directory / summary.SCOPE_NAME).read_bytes()
    assert summary.main([str(directory), "--write"], lab_root=lab) == 0
    assert (directory / summary.SCOPE_NAME).read_bytes() == stamp_before
    assert (
        json.loads((directory / summary.SUMMARY_NAME).read_text(encoding="utf-8"))[
            "case_count"
        ]
        == 2
    )
