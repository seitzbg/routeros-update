#!/usr/bin/env python3
"""Fetch the latest RouterOS on your machine and push it to MikroTik switches over SSH.

An alternative to RouterOS's built-in `/system package update` for switches that
can't reach upgrade.mikrotik.com (isolated management network) or where the
device-side download is unreliable: the package is fetched once on the machine
running this tool and scp'd to each switch, which installs it on reboot.

Switches are processed independently -- one switch failing NEVER stops the rest --
and a summary table at the end makes any skip/failure impossible to miss.

Mikrotik's NEWEST<major>.<channel> "latest version" markers are abandoned
(frozen at 7.12.1 since Jan 2024 on every mirror), so the target is discovered
by walking the download CDN upward from the newest version installed on the
fleet until the next versions 404. Pin an exact target with --version instead.

SAFE BY DEFAULT: a plain run is a dry run (reports current -> target, changes
nothing). Pass --upgrade to actually download, push, and reboot.

    ./routeros-update.py                             # dry run: report the fleet
    ./routeros-update.py --upgrade                   # upgrade every switch that lags
    ./routeros-update.py --upgrade --only sw1.example.net
    ./routeros-update.py --upgrade --version 7.24.2  # pin an exact target

The switch list is read from an Ansible-format inventory YAML (default:
$ROUTEROS_INVENTORY or ./inventory.yml, override with --inventory); see
example-inventory.yml. Requires netmiko, PyYAML, and rich.
"""
from __future__ import annotations

import argparse
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# --- pure logic (no network / netmiko; unit-tested in tests/test_routeros_update.py) ---

VERSION_RE = re.compile(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?")
# longest arch tokens first so "arm64" wins over "arm"
ARCH_RE = re.compile(r"arm64|armv7|arm|mmips|mipsbe|smips|tile|x86_64|x86|ppc")

CDN_BASE = "https://download.mikrotik.com/routeros"


def parse_version(raw: str) -> str | None:
    """Pull a version out of raw RouterOS output, echo-proof (sw2 leaks the command)."""
    m = VERSION_RE.search(raw or "")
    return m.group(0) if m else None


def parse_arch(raw: str) -> str | None:
    """Pull the CPU arch token out of raw RouterOS output, echo-proof."""
    m = ARCH_RE.search(raw or "")
    return m.group(0) if m else None


def version_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def is_newer(target: str, current: str) -> bool:
    """True only if target is strictly newer than current (the never-downgrade gate)."""
    return version_key(target) > version_key(current)


def newest(versions) -> str:
    return max(versions, key=version_key)


def npk_name(version: str, arch: str) -> str:
    return f"routeros-{version}-{arch}.npk"


def npk_url(version: str, arch: str) -> str:
    return f"{CDN_BASE}/{version}/{npk_name(version, arch)}"


def discover_latest(seed: str, exists, minor_span: int = 4, patch_span: int = 10) -> str:
    """Walk the CDN upward from `seed` (newest installed) to the latest existing release.

    `exists(version_str) -> bool` is injected so this is testable without the network.
    Never returns below `seed`.
    """
    parts = version_key(seed)
    maj, minr = parts[0], parts[1]
    pat = parts[2] if len(parts) > 2 else 0

    best_minor = minr
    for j in range(1, minor_span + 1):
        if exists(f"{maj}.{minr + j}"):
            best_minor = max(best_minor, minr + j)

    # a newer minor's .0 is already confirmed above, so patch-walk from 0;
    # otherwise walk above the installed patch on the current minor
    base_patch = 0 if best_minor > minr else pat
    best_patch = base_patch
    for k in range(base_patch + 1, base_patch + patch_span + 1):
        if exists(f"{maj}.{best_minor}.{k}"):
            best_patch = k

    target = f"{maj}.{best_minor}" if best_patch == 0 else f"{maj}.{best_minor}.{best_patch}"
    return newest([seed, target])


def load_switches(inventory_path: Path):
    """Read the routeros group from an ansible inventory -> [{'host':.., 'user':..}]."""
    import yaml  # deferred so --help works without pyyaml

    data = yaml.safe_load(inventory_path.read_text())
    hosts = (data.get("routeros") or {}).get("hosts") or {}
    return [
        {"host": name, "user": (opts or {}).get("ansible_user", "admin")}
        for name, opts in hosts.items()
    ]


# --- network / device I/O ----------------------------------------------------

def cdn_exists(version: str, arch: str, timeout: int = 15) -> bool:
    req = urllib.request.Request(npk_url(version, arch), method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        return e.code == 200
    except Exception:
        return False


def download_npk(version: str, arch: str, cache_dir: Path, timeout: int = 180) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / npk_name(version, arch)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    tmp = dest.with_suffix(".npk.part")
    with urllib.request.urlopen(npk_url(version, arch), timeout=timeout) as resp:
        tmp.write_bytes(resp.read())
    tmp.rename(dest)
    return dest


def scp_push(local: Path, user: str, host: str, remote_name: str, timeout: int = 300) -> None:
    subprocess.run(
        ["scp", "-B", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
         str(local), f"{user}@{host}:{remote_name}"],
        check=True, timeout=timeout,
    )


def default_key_file() -> str | None:
    """First existing default SSH identity, mirroring what the ssh client would try."""
    for name in ("id_ed25519", "id_rsa", "id_ecdsa"):
        p = Path.home() / ".ssh" / name
        if p.exists():
            return str(p)
    return None


def connect(host: str, user: str, key_file: str | None = None, timeout: int = 20):
    from netmiko import ConnectHandler  # deferred so unit tests / --help need no netmiko
    kf = key_file or default_key_file()
    params = dict(device_type="mikrotik_routeros", host=host, username=user,
                  allow_agent=True, conn_timeout=timeout, fast_cli=False)
    # allow_agent + look_for_keys(=use_keys) means paramiko still falls back to the
    # ssh-agent and other default keys if kf is not the one the switch accepts.
    if kf:
        params.update(use_keys=True, key_file=kf)
    else:
        params.update(use_keys=False)
    return ConnectHandler(**params)


def _put(conn, path: str) -> str:
    return conn.send_command_timing(f":put [{path}]")


def read_version_arch(conn):
    return (parse_version(_put(conn, "/system resource get version")),
            parse_arch(_put(conn, "/system resource get architecture-name")))


def _reboot(conn) -> None:
    # :execute avoids RouterOS's interactive "Reboot, yes? [y/N]" prompt
    conn.send_command_timing(':execute script="/system reboot"')
    try:
        conn.disconnect()
    except Exception:
        pass


def _port_open(host: str, port: int = 22, timeout: int = 5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_reboot(host: str, down_timeout: int = 120, up_timeout: int = 300, delay: int = 20) -> None:
    deadline = time.time() + down_timeout
    while time.time() < deadline and _port_open(host):
        time.sleep(3)
    time.sleep(delay)
    deadline = time.time() + up_timeout
    while time.time() < deadline:
        if _port_open(host):
            return
        time.sleep(5)
    raise TimeoutError(f"{host} did not come back within {up_timeout}s")


def reconnect(host: str, user: str, key_file=None, retries: int = 6, delay: int = 15):
    last = None
    for _ in range(retries):
        try:
            return connect(host, user, key_file)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(delay)
    raise last


def maybe_upgrade_firmware(conn, host: str, user: str, key_file, log) -> object:
    current = (_put(conn, "/system routerboard get current-firmware") or "").strip()
    upgrade = (_put(conn, "/system routerboard get upgrade-firmware") or "").strip()
    log(f"    firmware: current={current} available={upgrade}")
    if not current or not upgrade or current == upgrade:
        return conn
    log("    upgrading RouterBOARD firmware + reboot")
    conn.send_command_timing(':execute script="/system routerboard upgrade"')
    time.sleep(10)
    _reboot(conn)
    wait_for_reboot(host)
    return reconnect(host, user, key_file)


# --- per-switch orchestration ------------------------------------------------

class Result:
    def __init__(self, host):
        self.host = host
        self.arch = None
        self.before = None
        self.after = None
        self.status = "?"        # UP-TO-DATE | UPGRADED | WOULD-UPGRADE | SKIPPED | FAILED
        self.note = ""


def process_switch(sw, target, args, log) -> Result:
    host, user = sw["host"], sw["user"]
    r = Result(host)
    try:
        if not _port_open(host):
            r.status, r.note = "SKIPPED", "unreachable"
            return r
        conn = connect(host, user, args.key_file)
        r.before, r.arch = read_version_arch(conn)
        if not r.before or not r.arch:
            r.status, r.note = "FAILED", "could not parse version/arch"
            conn.disconnect()
            return r

        if not is_newer(target, r.before):
            r.status, r.after = "UP-TO-DATE", r.before
            conn.disconnect()
            return r

        if not args.upgrade:
            r.status, r.after = "WOULD-UPGRADE", target
            conn.disconnect()
            return r

        log(f"  {host} [{r.arch}]: {r.before} -> {target}, upgrading")
        local = download_npk(target, r.arch, args.cache_dir)
        scp_push(local, user, host, npk_name(target, r.arch))
        _reboot(conn)
        wait_for_reboot(host)
        conn = reconnect(host, user, args.key_file)
        r.after, _ = read_version_arch(conn)
        if r.after != target:
            r.status, r.note = "FAILED", f"post-reboot version {r.after}, expected {target}"
            conn.disconnect()
            return r
        if not args.no_firmware:
            conn = maybe_upgrade_firmware(conn, host, user, args.key_file, log)
        conn.disconnect()
        r.status = "UPGRADED"
        return r
    except Exception as e:  # noqa: BLE001 -- one switch must never abort the fleet
        r.status, r.note = "FAILED", str(e).splitlines()[0] if str(e) else type(e).__name__
        return r


def resolve_target(switches, args, log) -> str:
    if args.version:
        log(f"Target pinned to {args.version}")
        return args.version
    installed = []
    for sw in switches:
        if not _port_open(sw["host"]):
            continue
        try:
            conn = connect(sw["host"], sw["user"], args.key_file)
            v, _ = read_version_arch(conn)
            conn.disconnect()
            if v:
                installed.append(v)
        except Exception as e:  # noqa: BLE001
            log(f"  ({sw['host']} unreachable for seed: {e})")
    if not installed:
        raise SystemExit("No reachable switch reported a version; nothing to do.")
    seed = newest(installed)
    target = discover_latest(seed, lambda v: cdn_exists(v, args.disco_arch))
    log(f"Newest installed = {seed}; latest on CDN = {target}")
    return target


STATUS_STYLE = {
    "UPGRADED": "bold green",
    "UP-TO-DATE": "cyan",
    "WOULD-UPGRADE": "yellow",
    "SKIPPED": "dim yellow",
    "FAILED": "bold red",
}


def render_summary(results, target, dry_run: bool) -> None:
    """Print the fleet summary as a rich table; fall back to plain text if rich is absent."""
    try:
        from rich import box
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        print("=" * 64)
        print(f"{'SWITCH':<28}{'ARCH':<7}{'BEFORE':<9}{'AFTER':<9}STATUS")
        print("-" * 64)
        for r in results:
            note = f"  ({r.note})" if r.note else ""
            print(f"{r.host:<28}{(r.arch or '-'):<7}{(r.before or '-'):<9}"
                  f"{(r.after or '-'):<9}{r.status}{note}")
        print("=" * 64)
        return

    title = f"RouterOS fleet → {target}" + ("   (dry run)" if dry_run else "")
    table = Table(title=title, box=box.ROUNDED, header_style="bold",
                  title_style="bold", title_justify="left")
    table.add_column("Switch", no_wrap=True)
    table.add_column("Arch", justify="center")
    table.add_column("Before", justify="right")
    table.add_column("After", justify="right")
    table.add_column("Status", no_wrap=True)
    table.add_column("Note", style="dim")
    for r in results:
        style = STATUS_STYLE.get(r.status, "")
        status = f"[{style}]{r.status}[/]" if style else r.status
        table.add_row(r.host, r.arch or "-", r.before or "-", r.after or "-", status, r.note or "")
    Console().print(table)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--upgrade", action="store_true", help="actually push + reboot (default: dry run)")
    p.add_argument("--only", nargs="+", metavar="HOST", help="limit to these switch hostnames")
    p.add_argument("--version", help="pin an exact target version (skip CDN discovery)")
    p.add_argument("--no-firmware", action="store_true", help="skip the RouterBOARD firmware step")
    p.add_argument("--inventory", type=Path, default=Path(os.environ.get("ROUTEROS_INVENTORY", "inventory.yml")),
                   help="Ansible-format inventory YAML with a routeros group "
                        "(default: $ROUTEROS_INVENTORY or ./inventory.yml)")
    p.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache" / "routeros-update")
    p.add_argument("--disco-arch", default="arm", help="arch used to probe the CDN for latest (default: arm)")
    p.add_argument("--key-file", help="SSH private key (default: auto-detect ~/.ssh/id_ed25519, id_rsa, ...)")
    args = p.parse_args(argv)

    def log(msg=""):
        print(msg, flush=True)

    switches = load_switches(args.inventory)
    if args.only:
        wanted = set(args.only)
        switches = [s for s in switches if s["host"] in wanted]
        if not switches:
            log(f"No switches in {args.inventory} matched --only {args.only}")
            return 2

    if not args.upgrade:
        log("DRY RUN (no changes) -- pass --upgrade to push + reboot\n")
    target = resolve_target(switches, args, log)
    log("")

    results = [process_switch(sw, target, args, log) for sw in switches]

    log("")
    render_summary(results, target, dry_run=not args.upgrade)

    failed = [r for r in results if r.status == "FAILED"]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
