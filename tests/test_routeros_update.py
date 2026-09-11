"""Offline unit tests for routeros_update.py -- no switches, no netmiko, no CDN.

    pytest tests/test_routeros_update.py
"""
import importlib.util
import io
import zipfile
from pathlib import Path

import pytest

# load the module by path
_SPEC = importlib.util.spec_from_file_location(
    "routeros_update", Path(__file__).resolve().parent.parent / "routeros_update.py"
)
ru = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ru)


# --- version parsing & gating -------------------------------------------------

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
    ("[:put [/system resource get architecture-nam\n<t [/system resource get architecture-name]\narm]", "arm"),
])
def test_parse_arch_is_echo_proof(raw, want):
    assert ru.parse_arch(raw) == want


@pytest.mark.parametrize("raw,want", [
    ("7.20rc1 (testing)", True),
    ("7.20beta3", True),
    ("7.20.1-alpha", True),
    ("7.20.1 (stable)", False),
    ("7.20", False),
])
def test_is_prerelease(raw, want):
    assert ru.is_prerelease(raw) is want


@pytest.mark.parametrize("v,want", [
    ("7.24.2", True),
    ("7.24", True),
    ("7.9", True),
    ("6.49.19", False),     # RouterOS 6 uses a different NPK layout -> unsupported
    ("8.0", False),         # doesn't exist yet; refuse rather than guess a URL
    ("7.20rc1", False),     # prerelease pin must be rejected, not silently reinterpreted
    ("garbage", False),
    ("", False),
])
def test_is_supported_version(v, want):
    assert ru.is_supported_version(v) is want


@pytest.mark.parametrize("target,current,expect", [
    ("7.24.2", "7.24", True),
    ("7.24.2", "7.24.2", False),
    ("7.12.1", "7.24.2", False),   # NEVER downgrade (dead-endpoint guard)
    ("7.10", "7.9", True),         # numeric, not lexical
])
def test_is_newer_never_downgrades(target, current, expect):
    assert ru.is_newer(target, current) is expect


def test_newest_picks_highest_version():
    assert ru.newest(["7.24", "7.24.1", "7.24.2", "7.23.5"]) == "7.24.2"
    assert ru.newest(["7.9", "7.10", "7.10.1"]) == "7.10.1"


# --- package naming & selection ----------------------------------------------

def test_npk_name_layout_and_package():
    assert ru.npk_name("7.16", "arm") == "routeros-7.16-arm.npk"
    assert ru.npk_name("7.16", "arm", "wireless") == "wireless-7.16-arm.npk"


def test_all_packages_url_layout():
    # verified against the live CDN: all_packages-<arch>-<version>.zip
    assert ru.all_packages_name("7.16", "arm") == "all_packages-arm-7.16.zip"
    assert ru.all_packages_url("7.16", "arm").endswith("/routeros/7.16/all_packages-arm-7.16.zip")


@pytest.mark.parametrize("name,is_main", [
    ("routeros", True),
    ("routeros-arm", True),     # legacy main-package spelling
    ("wireless", False),
    ("switch-marvell", False),
])
def test_is_main_package(name, is_main):
    assert ru.is_main_package(name) is is_main


def test_required_extra_packages_excludes_main():
    installed = ["routeros", "wireless", "container", "routeros-arm"]
    assert ru.required_extra_packages(installed) == ["container", "wireless"]


def test_parse_packages_from_as_value():
    # representative serialized `:put [/system package print as-value]` output
    raw = (":put [/system package print as-value]\r\n"
           ".id=*1;name=routeros;version=7.16;scheduled=;"
           ".id=*2;name=wireless;version=7.16;scheduled=;"
           ".id=*3;name=container;version=7.15;scheduled=scheduled")
    got = ru.parse_packages(raw)
    assert got == {"routeros": "7.16", "wireless": "7.16", "container": "7.15"}


def test_parse_packages_empty():
    assert ru.parse_packages("") == {}
    assert ru.parse_packages("no packages here") == {}


# --- all_packages zip extraction ---------------------------------------------

def _make_zip(path: Path, members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in members:
            z.writestr(name, b"x" * 5000)
    path.write_bytes(buf.getvalue())


def test_extract_package_npk_found(tmp_path):
    zp = tmp_path / "all_packages-arm-7.16.zip"
    _make_zip(zp, ["wireless-7.16-arm.npk", "container-7.16-arm.npk"])
    out = ru.extract_package_npk(zp, "wireless", "7.16", "arm", tmp_path)
    assert out == tmp_path / "wireless-7.16-arm.npk"
    assert out.read_bytes() == b"x" * 5000


def test_extract_package_npk_missing_returns_none(tmp_path):
    zp = tmp_path / "all_packages-arm-7.16.zip"
    _make_zip(zp, ["wireless-7.16-arm.npk"])
    assert ru.extract_package_npk(zp, "switch-marvell", "7.16", "arm", tmp_path) is None


def test_resolve_upgrade_files_main_only(tmp_path, monkeypatch):
    monkeypatch.setattr(ru, "download_npk",
                        lambda v, a, cd, **k: tmp_path / ru.npk_name(v, a))
    uploads, missing = ru.resolve_upgrade_files("7.16", "arm", ["routeros"], tmp_path)
    assert missing == []
    assert [rn for _, rn in uploads] == ["routeros-7.16-arm.npk"]


def test_resolve_upgrade_files_uploads_all_extras(tmp_path, monkeypatch):
    monkeypatch.setattr(ru, "download_npk",
                        lambda v, a, cd, **k: tmp_path / ru.npk_name(v, a))
    zp = tmp_path / "all_packages-arm-7.16.zip"
    _make_zip(zp, ["wireless-7.16-arm.npk", "container-7.16-arm.npk"])
    monkeypatch.setattr(ru, "download_all_packages", lambda v, a, cd, **k: zp)
    uploads, missing = ru.resolve_upgrade_files(
        "7.16", "arm", ["routeros", "wireless", "container"], tmp_path)
    assert missing == []
    assert sorted(rn for _, rn in uploads) == [
        "container-7.16-arm.npk", "routeros-7.16-arm.npk", "wireless-7.16-arm.npk"]


def test_resolve_upgrade_files_reports_missing_extra(tmp_path, monkeypatch):
    # an installed extra that the CDN has no NPK for must surface as `missing`
    # so the caller refuses to reboot into a partial upgrade (finding #2)
    monkeypatch.setattr(ru, "download_npk",
                        lambda v, a, cd, **k: tmp_path / ru.npk_name(v, a))
    zp = tmp_path / "all_packages-arm-7.16.zip"
    _make_zip(zp, ["wireless-7.16-arm.npk"])
    monkeypatch.setattr(ru, "download_all_packages", lambda v, a, cd, **k: zp)
    uploads, missing = ru.resolve_upgrade_files(
        "7.16", "arm", ["routeros", "wireless", "switch-marvell"], tmp_path)
    assert missing == ["switch-marvell"]


# --- CDN discovery ------------------------------------------------------------

class _FakeResp:
    def __init__(self, status=200, data=b"", headers=None):
        self.status = status
        self._data = data
        self.headers = headers or {}

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(mapping):
    """mapping: url-substring -> _FakeResp | Exception (raised)."""
    def _open(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        for key, val in mapping.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return val
        raise AssertionError(f"unexpected url {url}")
    return _open


def test_cdn_exists_true_on_200(monkeypatch):
    monkeypatch.setattr(ru.urllib.request, "urlopen", _fake_urlopen({"7.16": _FakeResp(200)}))
    assert ru.cdn_exists("7.16", "arm") is True


def test_cdn_exists_false_on_404(monkeypatch):
    import urllib.error
    err = urllib.error.HTTPError("u", 404, "not found", {}, None)
    monkeypatch.setattr(ru.urllib.request, "urlopen", _fake_urlopen({"7.99": err}))
    assert ru.cdn_exists("7.99", "arm") is False


def test_cdn_exists_raises_on_network_error(monkeypatch):
    import urllib.error
    monkeypatch.setattr(ru.time, "sleep", lambda *_: None)
    monkeypatch.setattr(ru.urllib.request, "urlopen",
                        _fake_urlopen({"7.16": urllib.error.URLError("DNS failed")}))
    with pytest.raises(ru.CDNError):
        ru.cdn_exists("7.16", "arm")


def test_cdn_exists_raises_on_5xx(monkeypatch):
    import urllib.error
    monkeypatch.setattr(ru.time, "sleep", lambda *_: None)
    err = urllib.error.HTTPError("u", 503, "unavailable", {}, None)
    monkeypatch.setattr(ru.urllib.request, "urlopen", _fake_urlopen({"7.16": err}))
    with pytest.raises(ru.CDNError):
        ru.cdn_exists("7.16", "arm")


def test_discovery_error_never_looks_up_to_date():
    # a probe that raises must abort discovery, not resolve to the seed (finding #4)
    def boom(_v):
        raise ru.CDNError("network down")
    with pytest.raises(ru.CDNError):
        ru.discover_latest("7.20", boom)


def test_discover_finds_newest_patch():
    got = ru.discover_latest("7.24", (lambda a: (lambda v: v in a))(
        {"7.24", "7.24.1", "7.24.2"}))
    assert got == "7.24.2"


def test_discover_crosses_into_a_new_minor():
    got = ru.discover_latest("7.24", (lambda a: (lambda v: v in a))({"7.25", "7.25.1"}))
    assert got == "7.25.1"


def test_discover_never_returns_below_seed():
    got = ru.discover_latest("7.24.2", lambda v: False)
    assert got == "7.24.2"


# --- download integrity -------------------------------------------------------

def test_download_validates_md5_etag(tmp_path, monkeypatch):
    import hashlib
    data = b"y" * 6000
    good = hashlib.md5(data).hexdigest()
    monkeypatch.setattr(ru.urllib.request, "urlopen",
                        _fake_urlopen({"routeros": _FakeResp(200, data, {"ETag": f'"{good}"'})}))
    out = ru.download_npk("7.16", "arm", tmp_path)
    assert out.read_bytes() == data
    assert (tmp_path / (out.name + ".md5")).read_text() == good


def test_download_rejects_md5_mismatch(tmp_path, monkeypatch):
    # a plausible 32-hex ETag that does not match the payload -> reject the download
    monkeypatch.setattr(ru.urllib.request, "urlopen",
                        _fake_urlopen({"routeros": _FakeResp(
                            200, b"z" * 6000, {"ETag": '"' + "0" * 32 + '"'})}))
    with pytest.raises(ValueError):
        ru.download_npk("7.16", "arm", tmp_path)


def test_download_rejects_too_small(tmp_path, monkeypatch):
    monkeypatch.setattr(ru.urllib.request, "urlopen",
                        _fake_urlopen({"routeros": _FakeResp(200, b"nope", {})}))
    with pytest.raises(ValueError):
        ru.download_npk("7.16", "arm", tmp_path)


def test_download_reuses_cache_and_revalidates_sidecar(tmp_path, monkeypatch):
    import hashlib
    data = b"y" * 6000
    dest = tmp_path / ru.npk_name("7.16", "arm")
    dest.write_bytes(data)
    (tmp_path / (dest.name + ".md5")).write_text(hashlib.md5(data).hexdigest())

    def _boom(*a, **k):
        raise AssertionError("must not hit the network on a valid cache hit")
    monkeypatch.setattr(ru.urllib.request, "urlopen", _boom)
    assert ru.download_npk("7.16", "arm", tmp_path).read_bytes() == data


def test_download_refetches_when_cache_corrupt(tmp_path, monkeypatch):
    import hashlib
    dest = tmp_path / ru.npk_name("7.16", "arm")
    dest.write_bytes(b"corrupt-bytes-that-are-long-enough" * 200)
    (tmp_path / (dest.name + ".md5")).write_text(hashlib.md5(b"the-real-bytes").hexdigest())
    fresh = b"y" * 6000
    good = hashlib.md5(fresh).hexdigest()
    monkeypatch.setattr(ru.urllib.request, "urlopen",
                        _fake_urlopen({"routeros": _FakeResp(200, fresh, {"ETag": f'"{good}"'})}))
    assert ru.download_npk("7.16", "arm", tmp_path).read_bytes() == fresh


# --- SSH transport (host keys, key file, port) -------------------------------

def _capture_scp(monkeypatch):
    calls = []
    monkeypatch.setattr(ru.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or None)
    return calls


def test_scp_push_passes_key_and_port(monkeypatch):
    calls = _capture_scp(monkeypatch)
    ru.scp_push(Path("/tmp/x.npk"), "admin", "sw1", "x.npk", key_file="/k/id", port=2222)
    cmd = calls[0]
    assert "-i" in cmd and "/k/id" in cmd
    assert "-P" in cmd and "2222" in cmd
    assert "admin@sw1:x.npk" in cmd


def test_scp_push_accept_new_by_default(monkeypatch):
    calls = _capture_scp(monkeypatch)
    ru.scp_push(Path("/tmp/x.npk"), "admin", "sw1", "x.npk")
    joined = " ".join(calls[0])
    assert "StrictHostKeyChecking=accept-new" in joined
    assert "UserKnownHostsFile=/dev/null" not in joined


def test_scp_push_insecure_opts(monkeypatch):
    calls = _capture_scp(monkeypatch)
    ru.scp_push(Path("/tmp/x.npk"), "admin", "sw1", "x.npk", insecure=True)
    joined = " ".join(calls[0])
    assert "StrictHostKeyChecking=no" in joined
    assert "UserKnownHostsFile=/dev/null" in joined


def test_ssh_host_key_opts():
    assert ru._ssh_host_key_opts(False) == ["-o", "StrictHostKeyChecking=accept-new"]
    assert "UserKnownHostsFile=/dev/null" in ru._ssh_host_key_opts(True)


# --- reboot detection ---------------------------------------------------------

def test_wait_for_reboot_raises_if_never_down(monkeypatch):
    monkeypatch.setattr(ru.time, "sleep", lambda *_: None)
    clock = {"t": 0.0}
    monkeypatch.setattr(ru.time, "time", lambda: clock.__setitem__("t", clock["t"] + 5) or clock["t"])
    monkeypatch.setattr(ru, "_port_open", lambda *a, **k: True)  # SSH stayed open == no reboot
    with pytest.raises(TimeoutError):
        ru.wait_for_reboot("sw1", down_timeout=30)


def test_wait_for_reboot_succeeds_on_down_then_up(monkeypatch):
    monkeypatch.setattr(ru.time, "sleep", lambda *_: None)
    clock = {"t": 0.0}
    monkeypatch.setattr(ru.time, "time", lambda: clock.__setitem__("t", clock["t"] + 1) or clock["t"])
    seq = iter([True, False, False, True, True])  # up, down, down, up ...
    monkeypatch.setattr(ru, "_port_open", lambda *a, **k: next(seq, True))
    ru.wait_for_reboot("sw1", down_timeout=60, up_timeout=60, delay=0)  # returns without raising


# --- firmware ----------------------------------------------------------------

class FakeConn:
    """Maps command substrings to canned responses; records reboots/disconnects."""
    def __init__(self, responses):
        self.responses = responses
        self.sent = []
        self.disconnected = False

    def send_command_timing(self, cmd):
        self.sent.append(cmd)
        for key, val in self.responses.items():
            if key in cmd:
                return val
        return ""

    def disconnect(self):
        self.disconnected = True


def _args(**over):
    d = dict(key_file=None, insecure_host_key=False, no_firmware=False, upgrade=True,
             cache_dir=Path("/tmp"), disco_arch="arm", version=None, only=None)
    d.update(over)
    return type("Args", (), d)()


def test_firmware_no_upgrade_when_equal_even_with_echo(monkeypatch):
    # echoed command in the output must not make equal firmware look unequal (finding #5)
    conn = FakeConn({
        "current-firmware": ":put [/system routerboard get current-firmware]\r\n7.16",
        "upgrade-firmware": ":put [/system routerboard get upgrade-firmware]\r\n7.16",
    })
    sw = {"host": "sw1", "user": "admin", "port": 22, "name": "sw1"}
    out, upgraded = ru.maybe_upgrade_firmware(conn, sw, _args(), lambda *a: None)
    assert upgraded is False
    assert not any("routerboard upgrade" in s for s in conn.sent)


def test_firmware_no_upgrade_when_available_is_older(monkeypatch):
    conn = FakeConn({"current-firmware": "7.16", "upgrade-firmware": "7.15"})
    sw = {"host": "sw1", "user": "admin", "port": 22, "name": "sw1"}
    _, upgraded = ru.maybe_upgrade_firmware(conn, sw, _args(), lambda *a: None)
    assert upgraded is False


def test_firmware_upgrades_and_verifies(monkeypatch):
    monkeypatch.setattr(ru.time, "sleep", lambda *_: None)
    monkeypatch.setattr(ru, "wait_for_reboot", lambda *a, **k: None)
    # firmware reads 7.15 -> 7.16 pre-reboot; the reconnected session reports 7.16
    pre = FakeConn({"current-firmware": "7.15", "upgrade-firmware": "7.16"})
    post = FakeConn({"current-firmware": "7.16", "upgrade-firmware": "7.16"})
    monkeypatch.setattr(ru, "reconnect", lambda *a, **k: post)
    sw = {"host": "sw1", "user": "admin", "port": 22, "name": "sw1"}
    out, upgraded = ru.maybe_upgrade_firmware(pre, sw, _args(), lambda *a: None)
    assert upgraded is True
    assert any("routerboard upgrade" in s for s in pre.sent)
    assert out is post


def test_firmware_upgrade_unverified_raises(monkeypatch):
    monkeypatch.setattr(ru.time, "sleep", lambda *_: None)
    monkeypatch.setattr(ru, "wait_for_reboot", lambda *a, **k: None)
    pre = FakeConn({"current-firmware": "7.15", "upgrade-firmware": "7.16"})
    post = FakeConn({"current-firmware": "7.15", "upgrade-firmware": "7.16"})  # never changed
    monkeypatch.setattr(ru, "reconnect", lambda *a, **k: post)
    sw = {"host": "sw1", "user": "admin", "port": 22, "name": "sw1"}
    with pytest.raises(RuntimeError):
        ru.maybe_upgrade_firmware(pre, sw, _args(), lambda *a: None)


# --- inventory ----------------------------------------------------------------

def test_load_switches_reads_flat_routeros_group(tmp_path):
    inv = tmp_path / "02-routeros.yml"
    inv.write_text(
        "routeros:\n"
        "  hosts:\n"
        "    sw1.example.net:\n"
        "      ansible_user: admin\n"
        "    sw9-nouser.example.net: {}\n"
    )
    switches = ru.load_switches(inv)
    by_name = {s["name"]: s for s in switches}
    assert by_name["sw1.example.net"]["user"] == "admin"
    assert by_name["sw9-nouser.example.net"]["user"] == "admin"  # defaults to admin


def test_parse_inventory_nested_children():
    # standard Ansible nested form -- must NOT load as an empty fleet (finding #11)
    data = {"all": {"children": {"routeros": {"hosts": {
        "sw1.example.net": {}, "sw2.example.net": {}}}}}}
    switches = ru.parse_inventory(data)
    assert sorted(s["name"] for s in switches) == ["sw1.example.net", "sw2.example.net"]


def test_parse_inventory_honors_host_and_port_and_group_user():
    data = {"routeros": {
        "vars": {"ansible_user": "netadmin"},
        "hosts": {
            "sw1": {"ansible_host": "10.0.0.1", "ansible_port": 2222},
            "sw2": {"ansible_user": "root"},
        }}}
    by_name = {s["name"]: s for s in ru.parse_inventory(data)}
    assert by_name["sw1"]["host"] == "10.0.0.1"
    assert by_name["sw1"]["port"] == 2222
    assert by_name["sw1"]["user"] == "netadmin"   # group-level default
    assert by_name["sw2"]["user"] == "root"       # host-level override
    assert by_name["sw2"]["port"] == 22           # default port


def test_parse_inventory_empty_group_is_empty_list():
    assert ru.parse_inventory({"routeros": {"hosts": {}}}) == []
    assert ru.parse_inventory({"other": {"hosts": {"x": {}}}}) == []


# --- main() exit codes --------------------------------------------------------

def test_main_empty_fleet_with_pinned_version_does_not_succeed(tmp_path, capsys):
    # a valid-but-unrecognized inventory + pinned version must not exit 0 (finding #11)
    inv = tmp_path / "inv.yml"
    inv.write_text("all:\n  children:\n    switches:\n      hosts:\n        sw1: {}\n")
    rc = ru.main(["--version", "7.24.2", "--inventory", str(inv)])
    assert rc == 2


def test_main_only_no_match_returns_2(tmp_path):
    inv = tmp_path / "inv.yml"
    inv.write_text("routeros:\n  hosts:\n    sw1.example.net: {}\n")
    rc = ru.main(["--version", "7.24.2", "--inventory", str(inv), "--only", "nope.example.net"])
    assert rc == 2


def test_main_rejects_prerelease_version(tmp_path):
    inv = tmp_path / "inv.yml"
    inv.write_text("routeros:\n  hosts:\n    sw1.example.net: {}\n")
    with pytest.raises(SystemExit):  # argparse error -> SystemExit(2)
        ru.main(["--version", "7.20rc1", "--inventory", str(inv)])


def test_main_rejects_routeros6_version(tmp_path):
    inv = tmp_path / "inv.yml"
    inv.write_text("routeros:\n  hosts:\n    sw1.example.net: {}\n")
    with pytest.raises(SystemExit):
        ru.main(["--version", "6.49.19", "--inventory", str(inv)])
