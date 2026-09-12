# routeros-update

Update MikroTik RouterOS across a fleet of switches by **fetching the package on your
machine and pushing it over SSH** — an alternative to RouterOS's built-in
`/system package update` for switches that can't reach `upgrade.mikrotik.com`
(isolated management networks) or where the on-device download is unreliable.

- **Safe by default** — a plain run is a dry run that only reports `current → target`.
- **Fleet-friendly** — each switch is processed independently, so one switch failing
  never stops the rest, and a summary table makes any skip/failure obvious.
- **Never downgrades** — installs only when the target is strictly newer.
- **Complete package set** — uploads the main `routeros` package *and* every extra
  package the switch has installed (RouterOS rejects a version-mismatched upgrade), and
  refuses to reboot a switch whose extra package isn't available rather than half-upgrade it.
- **No hardcoded versions or arch** — discovers the latest release and reads each
  switch's CPU architecture itself.
- **Verified SSH identities** — switch host keys are checked trust-on-first-use
  (`accept-new`): an unknown switch is trusted and recorded, but one whose key later
  changes is rejected. `--insecure-host-key` opts out.

> RouterOS **7 only**. RouterOS 6 uses a different CDN filename layout and upgrading
> across a major version is its own migration, so v6 targets/seeds are rejected.

```
┌────────────────────────────┬──────┬────────┬────────┬───────────────┬──────┐
│ Switch                     │ Arch │ Before │  After │ Status        │ Note │
├────────────────────────────┼──────┼────────┼────────┼───────────────┼──────┤
│ sw1.example.net            │ arm  │   7.24 │ 7.24.2 │ UPGRADED      │      │
│ sw2.example.net            │ arm  │ 7.24.2 │ 7.24.2 │ UP-TO-DATE    │      │
│ sw3.example.net            │  -   │      - │      - │ SKIPPED       │ unre…│
└────────────────────────────┴──────┴────────┴────────┴───────────────┴──────┘
```

## How it works

1. **Discover the target.** MikroTik's `NEWEST<major>.<channel>` "latest version"
   markers are abandoned (frozen at 7.12.1 since Jan 2024 on every mirror), so the
   latest version is found by walking the download CDN
   (`download.mikrotik.com/routeros/<ver>/routeros-<ver>-<arch>.npk`) upward from the
   newest version already installed on the fleet until the next versions 404. Pin an
   exact target with `--version X.Y.Z` to skip discovery.
2. **Per switch:** read the running version, CPU arch, and installed packages over SSH;
   if the target is strictly newer, download the arch-matched `.npk` for the main
   package **plus every installed extra** (extras come from the official
   `all_packages-<arch>-<ver>.zip`; cached locally and integrity-checked against the
   CDN's MD5), `scp` them into the switch's root directory, reboot (RouterOS installs
   any root `.npk` on boot), reconnect and **verify** the running version and package
   set, then upgrade the RouterBOARD firmware if it lags — verifying that too. The
   firmware step runs independently, so a rerun finishes firmware work an interrupted
   run left pending even when RouterOS is already at the target.

## Install

Install it as a standalone CLI with [uv](https://docs.astral.sh/uv/) or
[pipx](https://pipx.pypa.io/) — this puts a `routeros-update` command on your PATH
in its own isolated environment (no manual venv):

```sh
uv tool install git+https://github.com/seitzbg/routeros-update      # recommended
# or
pipx install git+https://github.com/seitzbg/routeros-update
```

Run a one-off without installing:

```sh
uvx --from git+https://github.com/seitzbg/routeros-update routeros-update --help
```

Requires Python 3.10+ and the `scp`/`ssh` clients on your PATH. SSH must reach the
switches with **key auth** (the same key the `ssh` client uses; auto-detected, or
point at one with `--key-file`).

## Inventory

The switch list is read from an **Ansible-format inventory YAML** (the same file you
can already use with Ansible). Only the `routeros` group is read — whether it sits at
the top level or nested under `all.children` — and each host under it is a switch.
`ansible_user` (host- or group-level) defaults to `admin`; `ansible_host` and
`ansible_port` override the connection target and port:

```yaml
routeros:
  hosts:
    sw1.example.net:
      ansible_user: admin
    sw2.example.net: {}          # ansible_user defaults to admin

# nested form works too:
# all:
#   children:
#     routeros:
#       vars: { ansible_user: admin }   # group-level default
#       hosts:
#         sw3.example.net: { ansible_host: 10.0.0.3, ansible_port: 2222 }
```

Copy `example-inventory.yml` to `inventory.yml` (the default path), pass
`--inventory <path>`, or set `ROUTEROS_INVENTORY=/path/to/inventory.yml` so the
bare command always finds it.

## Usage

```sh
# dry run — report every switch, change nothing (default)
routeros-update

# actually upgrade every switch that lags
routeros-update --upgrade

# one switch only
routeros-update --upgrade --only sw1.example.net

# pin an exact target instead of auto-discovery
routeros-update --upgrade --version 7.24.2

# a specific inventory
routeros-update --inventory ~/net/switches.yml
```

### Options

| Flag | Meaning |
|------|---------|
| *(none)* | Dry run: report `current → target`, change nothing |
| `--upgrade` | Actually download, push, and reboot |
| `--only HOST [HOST ...]` | Limit the run to these switches |
| `--version X.Y.Z` | Pin an exact target (skip CDN discovery) |
| `--no-firmware` | Skip the RouterBOARD firmware step |
| `--insecure-host-key` | Disable SSH host-key verification (default is `accept-new` / trust-on-first-use) |
| `--inventory PATH` | Inventory YAML (default `$ROUTEROS_INVENTORY` or `./inventory.yml`) |
| `--cache-dir PATH` | Where downloaded `.npk` files are cached |
| `--disco-arch ARCH` | Arch used to probe the CDN for the latest version (default `arm`) |
| `--key-file PATH` | SSH private key (default: auto-detect `~/.ssh/id_ed25519`, `id_rsa`, …) |

Exit code is non-zero if any switch ended in `FAILED`.

## Tests

Offline unit tests (no switches, no netmiko, no network) cover version parsing and
the never-downgrade gate, CDN-walk discovery and its error handling, package
selection and all-packages extraction, download integrity, SSH transport options,
reboot detection, the firmware step, inventory loading, and CLI exit codes:

```sh
python3 -m pytest tests/test_routeros_update.py
```

## Caveats

- Installing a package **reboots** the switch. Plan for the brief outage; the tool
  processes one switch at a time and waits for each to come back before continuing.
- Discovery assumes contiguous version numbering on the CDN and walks a bounded
  window past the newest installed version. Use `--version` if you need to jump
  further, or across a channel boundary.
- Designed for the **stable channel**. Versions are compared numerically, so a switch
  running a prerelease (e.g. `7.20rc1`) is treated as its numeric version; pinning a
  prerelease with `--version` is rejected.
- With `accept-new`, the **first** contact with a switch trusts and records its key
  (scp appends to `~/.ssh/known_hosts`; the SSH session persists to a tool-managed
  `~/.ssh/known_hosts_routeros-update` so a changed key is caught on later runs). If you
  legitimately reinstall/replace a switch afterwards, remove its stale entries from both
  files (or use `--insecure-host-key`) so the new key is accepted.

## License

MIT — see [LICENSE](LICENSE).
