"""Offline tests for the repeater-config access grant, refusal and pins.

The three repeater services run as uid 1000 and mount their generated
config read-only; the config is written mode 0600 for the invoking user.
These tests pin the readability decision, the grant (with the acl tools
replaced by doubles, plus one real run), the compose file's identity,
and the CLI refusal a foreign-uid host gets.
"""

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import openhop_config_access as access  # noqa: E402

COMPOSE = Path(__file__).resolve().parents[2] / "compose.yaml"
TOOL = Path(__file__).resolve().parents[1] / "tools" / "openhop_config_access.py"


def fake_stat(uid, gid, mode):
    return SimpleNamespace(st_uid=uid, st_gid=gid, st_mode=mode)


def test_the_container_identity_reads_its_own_file():
    assert access.stat_readable(fake_stat(1000, 1000, 0o600), 1000)


def test_a_file_any_user_may_read_is_readable():
    assert access.stat_readable(fake_stat(1234, 1234, 0o604), 1000)


def test_a_shared_group_with_read_is_readable():
    assert access.stat_readable(fake_stat(1234, 1000, 0o640), 1000)


def test_a_private_foreign_file_is_not_readable():
    assert not access.stat_readable(fake_stat(1234, 1234, 0o600), 1000)


def test_an_effective_acl_entry_makes_the_file_readable(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    reader_uid = path.parent.stat().st_uid + 1
    path.write_text("credentials\n", encoding="utf-8")
    monkeypatch.setattr(access, "stat_readable", lambda _st, _uid: False)
    monkeypatch.setattr(access.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(command, **_kwargs):
        assert command == ["/usr/bin/getfacl", "-n", "-p", str(path)]
        return SimpleNamespace(
            returncode=0,
            stdout=f"user:{reader_uid}:rwx\t#effective:r--\n",
            stderr="",
        )

    monkeypatch.setattr(access.subprocess, "run", fake_run)
    assert access.uid_can_read(path, reader_uid)


def test_an_acl_entry_without_effective_read_is_not_readable(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    reader_uid = path.parent.stat().st_uid + 1
    path.write_text("credentials\n", encoding="utf-8")
    monkeypatch.setattr(access, "stat_readable", lambda _st, _uid: False)
    monkeypatch.setattr(access.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(_command, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=f"user:{reader_uid}:rwx\t#effective:---\n",
            stderr="",
        )

    monkeypatch.setattr(access.subprocess, "run", fake_run)
    assert not access.uid_can_read(path, reader_uid)


def test_grant_leaves_a_file_the_identity_already_reads(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("credentials\n", encoding="utf-8")  # owner read, mode 0644
    path.chmod(0o644)
    mode_before = stat.S_IMODE(os.stat(path).st_mode)

    def fail_run(_command, **_kwargs):
        raise AssertionError("setfacl must not run for a readable file")

    monkeypatch.setattr(access.subprocess, "run", fail_run)
    access.grant(path)
    assert stat.S_IMODE(os.stat(path).st_mode) == mode_before


def test_grant_refuses_when_the_acl_tools_are_missing(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("credentials\n", encoding="utf-8")
    os.chmod(path, 0o000)  # unreadable to its owner too
    monkeypatch.setattr(access.os, "getuid", lambda: 4242)
    monkeypatch.setattr(access.shutil, "which", lambda _name: None)
    with pytest.raises(access.AccessRefused) as refused:
        access.grant(path)
    message = str(refused.value)
    assert str(path) in message
    assert f"uid {access.REPEATER_UID}" in message
    assert "mode 0000" in message
    assert "setfacl/getfacl" in message
    assert "run the lab as uid 1000" in message


def test_grant_runs_setfacl_and_confirms_the_entry(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    reader_uid = path.parent.stat().st_uid + 1
    path.write_text("credentials\n", encoding="utf-8")
    os.chmod(path, 0o000)
    monkeypatch.setattr(access.os, "getuid", lambda: 4242)
    monkeypatch.setattr(access.shutil, "which", lambda name: f"/usr/bin/{name}")
    commands: list[list[str]] = []
    granted = {"now": False}

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[0] == "/usr/bin/setfacl":
            assert command[1:] == [
                "--set",
                f"u::---,u:{reader_uid}:r--,g::---,m::r--,o::---",
                str(path),
            ]
            granted["now"] = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        listing = f"user:{reader_uid}:r\t#effective:r--\n" if granted["now"] else ""
        return SimpleNamespace(returncode=0, stdout=listing, stderr="")

    monkeypatch.setattr(access.subprocess, "run", fake_run)
    access.grant(path, reader_uid)
    assert [command[0] for command in commands] == [
        "/usr/bin/getfacl",
        "/usr/bin/setfacl",
        "/usr/bin/getfacl",
    ]


def test_grant_refuses_when_setfacl_fails(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("credentials\n", encoding="utf-8")
    os.chmod(path, 0o000)
    monkeypatch.setattr(access.os, "getuid", lambda: 4242)
    monkeypatch.setattr(access.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(command, **_kwargs):
        assert command[0] in ("/usr/bin/setfacl", "/usr/bin/getfacl")
        if command[0] == "/usr/bin/setfacl":
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="setfacl: Operation not supported\n",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(access.subprocess, "run", fake_run)
    with pytest.raises(access.AccessRefused) as refused:
        access.grant(path)
    message = str(refused.value)
    assert "Operation not supported" in message
    assert str(path) in message
    assert "uid 1000" in message
    assert "Linux ACL support" in message


def test_grant_refuses_when_the_entry_is_not_confirmed(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("credentials\n", encoding="utf-8")
    os.chmod(path, 0o000)
    monkeypatch.setattr(access.os, "getuid", lambda: 4242)
    monkeypatch.setattr(access.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(_command, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(access.subprocess, "run", fake_run)
    with pytest.raises(access.AccessRefused):
        access.grant(path)


def test_a_real_acl_grant_on_a_real_file(tmp_path):
    """The real tools, on a real file: skipped where they are missing or
    the filesystem rejects ACLs."""
    if not (shutil.which("setfacl") and shutil.which("getfacl")):
        pytest.skip("the acl tools are not installed")
    reader_uid = access.REPEATER_UID + 1337
    if reader_uid == os.getuid():
        pytest.skip("the invoking user holds the probe identity")
    path = tmp_path / "config.yaml"
    path.write_text("credentials\n", encoding="utf-8")
    os.chmod(path, 0o600)
    probe = tmp_path / "probe"
    probe.write_text("x", encoding="utf-8")
    if subprocess.run(
        ["setfacl", "-m", f"u:{reader_uid}:r", str(probe)],
        capture_output=True,
    ).returncode:
        pytest.skip("the filesystem rejects ACLs")
    access.grant(path, uid=reader_uid)
    listed = subprocess.run(
        ["getfacl", "-n", "-p", str(path)], capture_output=True, text=True
    )
    assert f"user:{reader_uid}:" in listed.stdout
    # Still private: the ACL adds the entry, never world access.
    assert stat.S_IMODE(os.stat(path).st_mode) & 0o007 == 0


def test_compose_runs_the_repeaters_as_the_module_identity():
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    expected = f"{access.REPEATER_UID}:{access.REPEATER_UID}"
    for service in (
        "openhop-repeater",
        "openhop-repeater-a",
        "openhop-repeater-b",
    ):
        assert compose["services"][service]["user"] == expected


def test_the_cli_checks_a_readable_file(tmp_path):
    readable = tmp_path / "config.yaml"
    readable.write_text("credentials\n", encoding="utf-8")
    readable.chmod(0o644)
    done = subprocess.run(
        [sys.executable, str(TOOL), "check", str(readable)],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0
    assert done.stdout == ""


def test_the_cli_refuses_an_unreadable_file_with_the_remedy(tmp_path):
    hidden = tmp_path / "config.yaml"
    hidden.write_text("credentials\n", encoding="utf-8")
    os.chmod(hidden, 0o000)
    done = subprocess.run(
        [sys.executable, str(TOOL), "check", str(hidden)],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 1
    assert str(hidden) in done.stderr
    assert f"uid {access.REPEATER_UID}" in done.stderr
    assert "mode 0000" in done.stderr
