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

Every package the switch has installed (the main `routeros` package plus any
extras such as `wireless` or `container`) is upgraded together: RouterOS rejects
an upgrade whose package versions do not all match, so uploading only the main
package can reboot a switch into a refused upgrade. Extra packages come from the
official `all_packages-<arch>-<version>.zip`; if a required one is missing there,
the switch is failed *before* any reboot rather than left half-upgraded.

RouterOS 7 only: RouterOS 6 uses a different CDN filename layout and is a
major-version migration with its own upgrade policy, so v6 targets are rejected.

SSH server identities are verified trust-on-first-use (accept-new): an unknown
switch is trusted and recorded, but a switch whose key later *changes* is
rejected (catches impersonation). Pass --insecure-host-key to disable this.

SAFE BY DEFAULT: a plain run is a dry run (reports current -> target, changes
nothing). Pass --upgrade to actually download, push, and reboot.

    routeros-update                             # dry run: report the fleet
    routeros-update --upgrade                   # upgrade every switch that lags
    routeros-update --upgrade --only sw1.example.net
    routeros-update --upgrade --version 7.24.2  # pin an exact target

The switch list is read from an Ansible-format inventory YAML (default:
$ROUTEROS_INVENTORY or ./inventory.yml, override with --inventory); see
example-inventory.yml. Requires netmiko, PyYAML, and rich.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

# --- pure logic (no network / netmiko; unit-tested in tests/test_routeros_update.py) ---

VERSION_RE = re.compile(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?")
# a numeric version immediately followed by a prerelease/build marker (7.20rc1, 7.20beta3)
PRERELEASE_RE = re.compile(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?(?:rc|beta|alpha|test|-)", re.IGNORECASE)
# longest arch tokens first so "arm64" wins over "arm"
ARCH_RE = re.compile(r"arm64|armv7|arm|mmips|mipsbe|smips|tile|x86_64|x86|ppc")

CDN_BASE = "https://download.mikrotik.com/routeros"
SUPPORTED_MAJOR = 7  # RouterOS 6 uses a different NPK filename layout; 8 does not exist yet
MAIN_PACKAGE = "routeros"


def parse_version(raw: str) -> str | None:
    """Pull a version out of raw RouterOS output, echo-proof (sw2 leaks the command)."""
    m = VERSION_RE.search(raw or "")
    return m.group(0) if m else None


def is_prerelease(raw: str) -> bool:
    """True if the version string carries a prerelease marker (7.20rc1, 7.20beta2)."""
    return bool(PRERELEASE_RE.search(raw or ""))


def parse_arch(raw: str) -> str | None:
    """Pull the CPU arch token out of raw RouterOS output, echo-proof."""
    m = ARCH_RE.search(raw or "")
    return m.group(0) if m else None


def is_supported_version(v: str) -> bool:
    """A clean numeric RouterOS 7 version we can build CDN URLs for (no prereleases)."""
    if not v or is_prerelease(v):
        return False
    if not re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", v):
        return False
    return version_key(v)[0] == SUPPORTED_MAJOR


def version_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def is_newer(target: str, current: str) -> bool:
    """True only if target is strictly newer than current (the never-downgrade gate)."""
    return version_key(target) > version_key(current)


def newest(versions) -> str:
    return max(versions, key=version_key)


def npk_name(version: str, arch: str, package: str = MAIN_PACKAGE) -> str:
    """CDN/on-disk NPK filename. RouterOS 7 layout: <package>-<version>-<arch>.npk."""
    return f"{package}-{version}-{arch}.npk"


def npk_url(version: str, arch: str, package: str = MAIN_PACKAGE) -> str:
    return f"{CDN_BASE}/{version}/{npk_name(version, arch, package)}"


def all_packages_name(version: str, arch: str) -> str:
    return f"all_packages-{arch}-{version}.zip"


def all_packages_url(version: str, arch: str) -> str:
    return f"{CDN_BASE}/{version}/{all_packages_name(version, arch)}"


def is_main_package(name: str) -> bool:
    """The bundled system package (`routeros`, or the legacy `routeros-<arch>`)."""
    return name == MAIN_PACKAGE or name.startswith(MAIN_PACKAGE + "-")


def required_extra_packages(installed_names) -> list[str]:
    """Installed packages other than the main one, whose matching NPKs must be uploaded too."""
    return sorted({n for n in installed_names if n and not is_main_package(n)})


def parse_packages(raw: str) -> dict[str, str | None]:
    """Parse `:put [/system package print as-value]` output into {name: version}.

    The serialized form concatenates one `.id=...;name=...;version=...;` record per
    package. Splitting on `.id=` keeps each name paired with its own version, and
    both keys are absent from the echoed command, so this is echo-proof.
    """
    packages: dict[str, str | None] = {}
    for record in re.split(r"\.id=", raw or ""):
        n = re.search(r"name=([A-Za-z0-9][\w.-]*)", record)
        if not n:
            continue
        v = re.search(r"version=([0-9][\w.-]*)", record)
        packages[n.group(1)] = v.group(1) if v else None
    return packages


def discover_latest(seed: str, exists, minor_span: int = 4, patch_span: int = 10) -> str:
    """Walk the CDN upward from `seed` (newest installed) to the latest existing release.

    `exists(version_str) -> bool` is injected so this is testable without the network.
    It must return True for a present release, False for a confirmed-absent one (404),
    and *raise* when it cannot tell (network/TLS/5xx) so a failed probe never looks
    like an absence. Never returns below `seed`.
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


def _iter_groups(node):
    """Yield every mapping in an Ansible inventory tree (top level + nested `children`)."""
    if not isinstance(node, dict):
        return
    yield node
    for key in ("all", "children"):
        child = node.get(key)
        if isinstance(child, dict):
            if key == "children":
                for group in child.values():
                    yield from _iter_groups(group)
            else:
                yield from _iter_groups(child)


def _find_routeros_group(data):
    """Locate the `routeros` group anywhere in the inventory (flat or under all.children)."""
    for group in _iter_groups(data):
        children = group.get("children")
        if isinstance(children, dict) and isinstance(children.get("routeros"), dict):
            return children["routeros"]
    # flat top-level form: {routeros: {hosts: ...}}
    if isinstance(data, dict) and isinstance(data.get("routeros"), dict):
        return data["routeros"]
    return None


def parse_inventory(data) -> list[dict]:
    """Ansible-format inventory -> [{'name','host','user','port'}] for the routeros group.

    Honors group-level and host-level `ansible_user`, plus per-host `ansible_host`
    and `ansible_port`. Supports both the flat `routeros:` form and the nested
    `all.children.routeros.hosts` form.
    """
    group = _find_routeros_group(data)
    if not group:
        return []
    group_vars = group.get("vars") or {}
    default_user = group_vars.get("ansible_user", "admin")
    hosts = group.get("hosts") or {}
    switches = []
    for name, opts in hosts.items():
        opts = opts or {}
        switches.append({
            "name": name,
            "host": opts.get("ansible_host", name),
            "user": opts.get("ansible_user", default_user),
            "port": int(opts.get("ansible_port", 22)),
        })
    return switches


def load_switches(inventory_path: Path) -> list[dict]:
    import yaml  # deferred so --help works without pyyaml

    return parse_inventory(yaml.safe_load(inventory_path.read_text()))


# --- network / device I/O ----------------------------------------------------

class CDNError(RuntimeError):
    """A CDN probe could not determine presence/absence (network/TLS/server error)."""


def cdn_exists(version: str, arch: str, timeout: int = 15, retries: int = 3) -> bool:
    """True if the main NPK exists (HTTP 200), False on a confirmed 404.

    Anything else -- DNS/TLS failure, timeout, 429, 5xx -- is *indeterminate*, not an
    absence: retry a bounded number of times, then raise CDNError so discovery cannot
    silently mistake an unreachable CDN for "no newer release" (see finding #4).
    """
    req = urllib.request.Request(npk_url(version, arch), method="HEAD")
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status == 200
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            if e.code == 200:
                return True
            last = e  # 403/429/5xx -> transient/indeterminate
        except Exception as e:  # noqa: BLE001 -- DNS/TLS/timeout
            last = e
        if attempt < retries - 1:
            time.sleep(2)
    raise CDNError(f"could not probe {version} on the CDN: {last}")


def _validate_npk_bytes(data: bytes, etag: str | None) -> None:
    """Reject an obviously-bad download; if the CDN gave a plain MD5 ETag, verify it."""
    if len(data) < 4096:
        raise ValueError(f"download too small to be a package ({len(data)} bytes)")
    if etag:
        tag = etag.strip().strip('"')
        if re.fullmatch(r"[0-9a-fA-F]{32}", tag):  # single-part S3 ETag == MD5 hex
            got = hashlib.md5(data).hexdigest()
            if got != tag.lower():
                raise ValueError(f"MD5 mismatch: got {got}, CDN ETag {tag.lower()}")


def _download_to_cache(url: str, dest: Path, validate=None, timeout: int = 300) -> Path:
    """Download `url` -> `dest` (atomic), integrity-checking and caching the result.

    A `<dest>.md5` sidecar records the verified digest so a later cache hit can detect
    corruption instead of trusting any nonempty file forever (finding #12).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    sidecar = dest.parent / (dest.name + ".md5")
    if dest.exists() and dest.stat().st_size > 0:
        if sidecar.exists():
            if hashlib.md5(dest.read_bytes()).hexdigest() == sidecar.read_text().strip():
                return dest
            dest.unlink()  # cached bytes no longer match their recorded digest -> refetch
        else:
            return dest  # legacy cache entry with no recorded digest
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        data = resp.read()
        etag = resp.headers.get("ETag")
    if validate:
        validate(data, etag)
    tmp = dest.parent / (dest.name + ".part")
    tmp.write_bytes(data)
    tmp.rename(dest)
    sidecar.write_text(hashlib.md5(data).hexdigest())
    return dest


def download_npk(version: str, arch: str, cache_dir: Path, package: str = MAIN_PACKAGE,
                 timeout: int = 300) -> Path:
    return _download_to_cache(
        npk_url(version, arch, package), cache_dir / npk_name(version, arch, package),
        validate=_validate_npk_bytes, timeout=timeout,
    )


def download_all_packages(version: str, arch: str, cache_dir: Path, timeout: int = 600) -> Path:
    return _download_to_cache(
        all_packages_url(version, arch), cache_dir / all_packages_name(version, arch),
        timeout=timeout,
    )


def extract_package_npk(zip_path: Path, package: str, version: str, arch: str,
                        cache_dir: Path) -> Path | None:
    """Pull one extra package's NPK out of the all_packages zip, or None if it isn't there."""
    member = npk_name(version, arch, package)
    with zipfile.ZipFile(zip_path) as z:
        if member not in z.namelist():
            return None
        dest = cache_dir / member
        dest.write_bytes(z.read(member))
    return dest


def resolve_upgrade_files(target: str, arch: str, installed_names, cache_dir: Path):
    """Download every NPK the switch needs. Returns (uploads, missing).

    `uploads` is [(local_path, remote_name), ...] for the main package plus each installed
    extra; `missing` lists extras with no NPK on the CDN. Callers must refuse to reboot a
    switch with any `missing` package rather than install a partial, rejected upgrade.
    """
    uploads = [(download_npk(target, arch, cache_dir), npk_name(target, arch))]
    extras = required_extra_packages(installed_names)
    missing = []
    if extras:
        zip_path = download_all_packages(target, arch, cache_dir)
        for pkg in extras:
            npk = extract_package_npk(zip_path, pkg, target, arch, cache_dir)
            if npk is None:
                missing.append(pkg)
            else:
                uploads.append((npk, npk_name(target, arch, pkg)))
    return uploads, missing


def _ssh_host_key_opts(insecure: bool) -> list[str]:
    if insecure:
        return ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
    # accept-new: trust an unknown switch (TOFU), reject one whose key changed
    return ["-o", "StrictHostKeyChecking=accept-new"]


def scp_push(local: Path, user: str, host: str, remote_name: str, key_file: str | None = None,
             port: int = 22, insecure: bool = False, timeout: int = 300) -> None:
    cmd = ["scp", "-B", *_ssh_host_key_opts(insecure), "-P", str(port)]
    if key_file:
        cmd += ["-i", key_file]
    cmd += [str(local), f"{user}@{host}:{remote_name}"]
    subprocess.run(cmd, check=True, timeout=timeout)


def default_key_file() -> str | None:
    """First existing default SSH identity, mirroring what the ssh client would try."""
    for name in ("id_ed25519", "id_rsa", "id_ecdsa"):
        p = Path.home() / ".ssh" / name
        if p.exists():
            return str(p)
    return None


def connect(host: str, user: str, key_file: str | None = None, port: int = 22,
            insecure: bool = False, timeout: int = 20):
    from netmiko import ConnectHandler  # deferred so unit tests / --help need no netmiko
    kf = key_file or default_key_file()
    params = dict(device_type="mikrotik_routeros", host=host, port=port, username=user,
                  allow_agent=True, conn_timeout=timeout, fast_cli=False)
    # accept-new host-key policy: load ~/.ssh/known_hosts so paramiko rejects a changed
    # key (BadHostKeyException) while AutoAddPolicy still trusts a first-seen switch.
    params["system_host_keys"] = not insecure
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


def read_installed_packages(conn) -> dict[str, str | None]:
    """{name: version} for every installed package (main + extras)."""
    return parse_packages(_put(conn, "/system package print as-value"))


def read_firmware(conn):
    """(current, upgrade) RouterBOARD firmware versions, echo-proofed like version/arch."""
    return (parse_version(_put(conn, "/system routerboard get current-firmware")),
            parse_version(_put(conn, "/system routerboard get upgrade-firmware")))


def _reboot(conn) -> None:
    # :execute avoids RouterOS's interactive "Reboot, yes? [y/N]" prompt
    conn.send_command_timing(':execute script="/system reboot"')
    _safe_disconnect(conn)


def _safe_disconnect(conn) -> None:
    try:
        if conn is not None:
            conn.disconnect()
    except Exception:  # noqa: BLE001 -- cleanup must never mask the real result
        pass


def _port_open(host: str, port: int = 22, timeout: int = 5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_reboot(host: str, port: int = 22, down_timeout: int = 120, up_timeout: int = 300,
                    delay: int = 20) -> None:
    """Wait for the switch to drop off the network and return.

    Raises if it never goes down within `down_timeout` (a rejected/ignored reboot leaves
    SSH open) or never comes back within `up_timeout` -- so a reboot that did not happen
    cannot be mistaken for success (finding #6).
    """
    deadline = time.time() + down_timeout
    went_down = False
    while time.time() < deadline:
        if not _port_open(host, port):
            went_down = True
            break
        time.sleep(3)
    if not went_down:
        raise TimeoutError(f"{host} never rebooted (port {port} stayed open for {down_timeout}s)")
    time.sleep(delay)
    deadline = time.time() + up_timeout
    while time.time() < deadline:
        if _port_open(host, port):
            return
        time.sleep(5)
    raise TimeoutError(f"{host} did not come back within {up_timeout}s")


def reconnect(host: str, user: str, key_file=None, port: int = 22, insecure: bool = False,
              retries: int = 6, delay: int = 15):
    last = None
    for _ in range(retries):
        try:
            return connect(host, user, key_file, port=port, insecure=insecure)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(delay)
    raise last


def maybe_upgrade_firmware(conn, sw, args, log):
    """Flash the RouterBOARD firmware if a strictly-newer version is available.

    Returns (conn, upgraded_bool). Compares echo-proofed versions and verifies the new
    firmware is actually running after the reboot (findings #5, #6).
    """
    host, user, port = sw["host"], sw["user"], sw["port"]
    current, available = read_firmware(conn)
    log(f"    firmware: current={current} available={available}")
    if not current or not available or not is_newer(available, current):
        return conn, False
    log(f"    upgrading RouterBOARD firmware {current} -> {available} + reboot")
    conn.send_command_timing(':execute script="/system routerboard upgrade"')
    time.sleep(10)
    _reboot(conn)
    wait_for_reboot(host, port)
    conn = reconnect(host, user, args.key_file, port=port, insecure=args.insecure_host_key)
    try:
        running, _ = read_firmware(conn)
        if running != available:
            raise RuntimeError(f"firmware still {running} after reboot, expected {available}")
    except Exception:
        _safe_disconnect(conn)
        raise
    return conn, True


# --- per-switch orchestration ------------------------------------------------

class Result:
    def __init__(self, host):
        self.host = host
        self.arch = None
        self.before = None
        self.after = None
        self.status = "?"        # UP-TO-DATE | UPGRADED | WOULD-UPGRADE | SKIPPED | FAILED
        self.note = ""


def _do_os_upgrade(conn, sw, target, args, log, r):
    """Upload the full package set and reboot into the target RouterOS. Returns new conn."""
    host, user, port = sw["host"], sw["user"], sw["port"]
    installed = read_installed_packages(conn)
    uploads, missing = resolve_upgrade_files(target, r.arch, installed, args.cache_dir)
    if missing:
        raise RuntimeError(f"no CDN package(s) for {', '.join(missing)} at {target}/{r.arch}; "
                           "not rebooting into a partial upgrade")
    extras = [p for p in installed if not is_main_package(p)]
    log(f"  {host} [{r.arch}]: {r.before} -> {target}, uploading "
        f"{len(uploads)} package(s){' incl. ' + ', '.join(extras) if extras else ''}")
    for local, remote in uploads:
        scp_push(local, user, host, remote, key_file=args.key_file, port=port,
                 insecure=args.insecure_host_key)
    _reboot(conn)
    wait_for_reboot(host, port)
    conn = reconnect(host, user, args.key_file, port=port, insecure=args.insecure_host_key)
    try:
        r.after, _ = read_version_arch(conn)
        if r.after != target:
            raise RuntimeError(f"post-reboot version {r.after}, expected {target}")
        # every extra that was installed must now report the target version too
        after_pkgs = read_installed_packages(conn)
        behind = [p for p in extras if after_pkgs.get(p) not in (target, None)]
        if behind:
            raise RuntimeError(f"package(s) still behind after reboot: {', '.join(behind)}")
    except Exception:
        _safe_disconnect(conn)  # the freshly-reconnected session is ours to close on failure
        raise
    return conn


def process_switch(sw, target, args, log) -> Result:
    host, user, port = sw["host"], sw["user"], sw["port"]
    r = Result(sw["name"])
    conn = None
    try:
        if not _port_open(host, port):
            r.status, r.note = "SKIPPED", "unreachable"
            return r
        conn = connect(host, user, args.key_file, port=port, insecure=args.insecure_host_key)
        r.before, r.arch = read_version_arch(conn)
        if not r.before or not r.arch:
            r.status, r.note = "FAILED", "could not parse version/arch"
            return r

        os_lags = is_newer(target, r.before)

        if not args.upgrade:  # dry run stays strictly read-only
            if os_lags:
                r.status, r.after = "WOULD-UPGRADE", target
            else:
                r.status, r.after = "UP-TO-DATE", r.before
                if not args.no_firmware:
                    cur, avail = read_firmware(conn)
                    if cur and avail and is_newer(avail, cur):
                        r.status, r.note = "WOULD-UPGRADE", f"firmware {cur} -> {avail}"
            return r

        upgraded = False
        if os_lags:
            conn = _do_os_upgrade(conn, sw, target, args, log, r)
            upgraded = True
        else:
            r.after = r.before

        # firmware is evaluated independently of the OS version, so a rerun can finish
        # firmware work left pending by an interrupted earlier run (finding #7)
        if not args.no_firmware:
            conn, fw_upgraded = maybe_upgrade_firmware(conn, sw, args, log)
            if fw_upgraded and not upgraded:
                r.note = "firmware only"
            upgraded = upgraded or fw_upgraded

        r.status = "UPGRADED" if upgraded else "UP-TO-DATE"
        return r
    except Exception as e:  # noqa: BLE001 -- one switch must never abort the fleet
        r.status, r.note = "FAILED", str(e).splitlines()[0] if str(e) else type(e).__name__
        return r
    finally:
        _safe_disconnect(conn)


def resolve_target(switches, args, log) -> str:
    if args.version:
        log(f"Target pinned to {args.version}")
        return args.version
    installed = []
    for sw in switches:
        if not _port_open(sw["host"], sw["port"]):
            continue
        conn = None
        try:
            conn = connect(sw["host"], sw["user"], args.key_file, port=sw["port"],
                           insecure=args.insecure_host_key)
            v, _ = read_version_arch(conn)
            if v:
                installed.append(v)
        except Exception as e:  # noqa: BLE001
            log(f"  ({sw['name']} unreachable for seed: {e})")
        finally:
            _safe_disconnect(conn)
    if not installed:
        raise SystemExit("No reachable switch reported a version; nothing to do.")
    seed = newest(installed)
    if version_key(seed)[0] != SUPPORTED_MAJOR:
        raise SystemExit(f"Newest installed version {seed} is not RouterOS {SUPPORTED_MAJOR}; "
                         "this tool only supports RouterOS 7. Pin --version to override discovery.")
    try:
        target = discover_latest(seed, lambda v: cdn_exists(v, args.disco_arch))
    except CDNError as e:
        raise SystemExit(f"Could not determine the latest release: {e}")
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
    p.add_argument("--insecure-host-key", action="store_true",
                   help="disable SSH host-key verification (default: accept-new / trust-on-first-use)")
    p.add_argument("--inventory", type=Path, default=Path(os.environ.get("ROUTEROS_INVENTORY", "inventory.yml")),
                   help="Ansible-format inventory YAML with a routeros group "
                        "(default: $ROUTEROS_INVENTORY or ./inventory.yml)")
    p.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache" / "routeros-update")
    p.add_argument("--disco-arch", default="arm", help="arch used to probe the CDN for latest (default: arm)")
    p.add_argument("--key-file", help="SSH private key (default: auto-detect ~/.ssh/id_ed25519, id_rsa, ...)")
    args = p.parse_args(argv)

    if args.version and not is_supported_version(args.version):
        p.error(f"--version {args.version!r} is not a supported RouterOS {SUPPORTED_MAJOR} "
                "release (expected e.g. 7.24.2; prereleases and RouterOS 6 are not supported)")

    def log(msg=""):
        print(msg, flush=True)

    switches = load_switches(args.inventory)
    if args.only:
        wanted = set(args.only)
        switches = [s for s in switches if s["name"] in wanted]
    if not switches:
        log(f"No switches selected from {args.inventory}"
            + (f" matching --only {args.only}" if args.only else " (empty or unrecognized routeros group)"))
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
