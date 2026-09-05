# routeros-update

Update MikroTik RouterOS across a fleet of switches by **fetching the package on your
machine and pushing it over SSH** — an alternative to RouterOS's built-in
`/system package update` for switches that can't reach `upgrade.mikrotik.com`
(isolated management networks) or where the on-device download is unreliable.

- **Safe by default** — a plain run is a dry run that only reports `current → target`.
- **Fleet-friendly** — each switch is processed independently, so one switch failing
  never stops the rest, and a summary table makes any skip/failure obvious.
- **Never downgrades** — installs only when the target is strictly newer.
- **No hardcoded versions or arch** — discovers the latest release and reads each
  switch's CPU architecture itself.

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
2. **Per switch:** read the running version and CPU arch over SSH; if the target is
   strictly newer, download the arch-matched `.npk` (cached locally), `scp` it into
   the switch's root directory, reboot (RouterOS installs any root `.npk` on boot),
   reconnect and **verify** the running version, then upgrade the RouterBOARD
   firmware if it lags.

## Requirements

Python 3.9+, plus the packages in `requirements.txt` (`netmiko`, `PyYAML`, `rich`),
and the `scp`/`ssh` clients on your PATH. SSH must reach the switches with **key
auth** (the same key the `ssh` client uses; auto-detected, or point at one with
`--key-file`).

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Inventory

The switch list is read from an **Ansible-format inventory YAML** (the same file you
can already use with Ansible). Only the `routeros` group is read; each host under it
is a switch, and `ansible_user` defaults to `admin`:

```yaml
routeros:
  hosts:
    sw1.example.net:
      ansible_user: admin
    sw2.example.net: {}          # ansible_user defaults to admin
```

Copy `example-inventory.yml` to `inventory.yml` (the default path) or pass
`--inventory <path>`.

## Usage

```sh
# dry run — report every switch, change nothing (default)
.venv/bin/python routeros-update.py

# actually upgrade every switch that lags
.venv/bin/python routeros-update.py --upgrade

# one switch only
.venv/bin/python routeros-update.py --upgrade --only sw1.example.net

# pin an exact target instead of auto-discovery
.venv/bin/python routeros-update.py --upgrade --version 7.24.2
```

Tip: drop a small wrapper on your `PATH` so you can just type `routeros-update`:

```sh
#!/usr/bin/env bash
exec /path/to/.venv/bin/python /path/to/routeros-update.py "$@"
```

### Options

| Flag | Meaning |
|------|---------|
| *(none)* | Dry run: report `current → target`, change nothing |
| `--upgrade` | Actually download, push, and reboot |
| `--only HOST [HOST ...]` | Limit the run to these switches |
| `--version X.Y.Z` | Pin an exact target (skip CDN discovery) |
| `--no-firmware` | Skip the RouterBOARD firmware step |
| `--inventory PATH` | Inventory YAML (default `./inventory.yml`) |
| `--cache-dir PATH` | Where downloaded `.npk` files are cached |
| `--disco-arch ARCH` | Arch used to probe the CDN for the latest version (default `arm`) |
| `--key-file PATH` | SSH private key (default: auto-detect `~/.ssh/id_ed25519`, `id_rsa`, …) |

Exit code is non-zero if any switch ended in `FAILED`.

## Tests

Offline unit tests (no switches, no netmiko, no network) cover version parsing,
the never-downgrade gate, CDN-walk discovery, and inventory loading:

```sh
python3 -m pytest tests/test_routeros_update.py
```

## Caveats

- Installing a package **reboots** the switch. Plan for the brief outage; the tool
  processes one switch at a time and waits for each to come back before continuing.
- Discovery assumes contiguous version numbering on the CDN and walks a bounded
  window past the newest installed version. Use `--version` if you need to jump
  further, or across a channel boundary.

## License

MIT — see [LICENSE](LICENSE).
