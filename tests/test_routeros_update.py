"""Offline unit tests for tools/routeros-update.py -- no switches, no netmiko, no CDN.

    pytest tests/test_routeros_update.py
"""
import importlib.util
from pathlib import Path

import pytest

# load the hyphen-named CLI module by path
_SPEC = importlib.util.spec_from_file_location(
    "routeros_update", Path(__file__).resolve().parent.parent / "routeros_update.py"
)
ru = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ru)


@pytest.mark.parametrize("raw,want", [
    ("7.24.2 (stable)", "7.24.2"),
    ("7.9 (stable)", "7.9"),
    # command echo leaks into the output (the sw2 symptom) -> still find the version
    (":put [/system resource get version]\r\n7.24.2 (stable)", "7.24.2"),
    ("no version here", None),
])
def test_parse_version_is_echo_proof(raw, want):
    assert ru.parse_version(raw) == want


@pytest.mark.parametrize("raw,want", [
    ("arm", "arm"),
    ("arm64", "arm64"),               # longer token must win over "arm"
    ("mmips", "mmips"),
    # the exact garbled echo seen on sw2 -> must still resolve to "arm"
    ("[:put [/system resource get architecture-nam\n<t [/system resource get architecture-name]\narm]", "arm"),
])
def test_parse_arch_is_echo_proof(raw, want):
    assert ru.parse_arch(raw) == want


@pytest.mark.parametrize("target,current,expect", [
    ("7.24.2", "7.24", True),     # newer patch
    ("7.24.2", "7.24.2", False),  # already current
    ("7.12.1", "7.24.2", False),  # NEVER downgrade (dead-endpoint guard)
    ("7.10", "7.9", True),        # numeric, not lexical
])
def test_is_newer_never_downgrades(target, current, expect):
    assert ru.is_newer(target, current) is expect


def test_newest_picks_highest_version():
    assert ru.newest(["7.24", "7.24.1", "7.24.2", "7.23.5"]) == "7.24.2"
    assert ru.newest(["7.9", "7.10", "7.10.1"]) == "7.10.1"


def _exists_from(available):
    s = set(available)
    return lambda v: v in s


def test_discover_finds_newest_patch():
    # fleet on 7.24, CDN has 7.24 / .1 / .2, nothing higher
    got = ru.discover_latest("7.24", _exists_from(["7.24", "7.24.1", "7.24.2"]))
    assert got == "7.24.2"


def test_discover_noop_when_seed_is_latest():
    got = ru.discover_latest("7.24.2", _exists_from(["7.24", "7.24.1", "7.24.2"]))
    assert got == "7.24.2"


def test_discover_crosses_into_a_new_minor():
    got = ru.discover_latest("7.24", _exists_from(["7.25", "7.25.1"]))
    assert got == "7.25.1"


def test_discover_never_returns_below_seed():
    # CDN knows nothing newer -> stay on what's installed, never downgrade
    got = ru.discover_latest("7.24.2", _exists_from([]))
    assert got == "7.24.2"


def test_load_switches_reads_routeros_group(tmp_path):
    inv = tmp_path / "02-routeros.yml"
    inv.write_text(
        "routeros:\n"
        "  hosts:\n"
        "    sw1.example.net:\n"
        "      ansible_user: admin\n"
        "    sw9-nouser.example.net: {}\n"
    )
    switches = ru.load_switches(inv)
    assert {"host": "sw1.example.net", "user": "admin"} in switches
    # missing ansible_user defaults to admin
    assert {"host": "sw9-nouser.example.net", "user": "admin"} in switches
