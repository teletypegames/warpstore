# warpstore — the shared core of the WarpEngine store engines

A **store engine** reads a [WarpEngine](https://git.teletypegames.org/engines/warp_engine)
catalog and writes some host platform's own game library format. Only that last
step differs between hosts. Everything before it — talking to the API, picking
which release to install, matching the machine, remembering what was put where —
is this module.

```
                    ┌──────────────────────────────┐
                    │  warpstore.py                │
                    │  catalog · releases · host   │
                    │  state · config · HTTP       │
                    └──────────────┬───────────────┘
                    ┌──────────────┴───────────────┐
                    ▼                              ▼
    warp-engine-batocera-store        warp-engine-retroarch-store
    ROM folders + gamelist.xml        .lpl playlists + thumbnails
                    │                              │
                    ▼                              ▼
             EmulationStation                  RetroArch
```

Single file, Python 3 standard library only. The hosts range from a Batocera box
(`python3`, no `pip`) to a desktop, and neither may be asked to install anything.
An engine's installer drops `warpstore.py` next to the adapter script and the
adapter does `import warpstore`.

## What an adapter has to supply

Three things, and nothing else:

1. **A `DEFAULT_CONFIG`** describing its host — where things live, which catalog
   platform maps to what.
2. **An `accept(spec, sw)` callback** deciding which catalog entries that host can
   run, and how they are grouped.
3. **The code that writes the host's library format.**

```python
import warpstore as ws

def accept(spec, sw):
    """Return (scope, extras), or raise ws.Skip(reason) to reject the title."""
    if spec["system"] not in available_systems:
        raise ws.Skip(f"no '{spec['system']}' ROM folder on this box")
    return spec["system"], {"system": spec["system"]}

cfg = ws.load_config(path, DEFAULT_CONFIG, required=("paths.subfolder",))
ws.init(path)
games, skipped = ws.select_games(cfg, ws.fetch_catalog(cfg, use_cache=True), accept)
```

`scope` is how the host groups its library — a Batocera system, a RetroArch
playlist. It is what `state.json` is keyed by (`<scope>:<name>`), so two hosts
can carry a game of the same name without colliding.

## What is in here

| Area | Functions |
|---|---|
| Logging | `set_tag`, `set_verbose`, `log`, `debug`, `die` |
| Files | `load_json`, `write_json`, `write_atomic`, `within`, `prune_empty_dirs` |
| Config | `deep_merge`, `load_config` |
| Store home | `init`, then `HOME`, `CONFIG_PATH`, `STATE_PATH`, `CATALOG_CACHE` |
| HTTP | `http_get`, `http_download`, `api_url`, `download_url`, `user_agent` |
| Host | `machine`, `host_os`, `host`, `resolve_for_host` |
| Catalog | `fetch_catalog`, `select_games`, `pick_release`, `asset_basename`, `download_image` |
| State | `load_state`, `save_state`, `game_key`, `record_scope`, `records_by_scope`, `match_keys`, `limit_to_names` |

Four of them carry decisions worth knowing about.

**`resolve_for_host(value, host)`** — a config value that may depend on the
machine. A plain string is the same everywhere (that is what a `cartridge` is:
data for an emulator). Where it differs, the value is a map and the most specific
key wins: `{"linux-aarch64": …, "aarch64": …, "linux": …, "*": …}`. With no
matching key and no `*` the answer is `None`, and the caller reports the title as
skipped rather than installing something that cannot run.

**`within(path, root)`** — every delete a store performs is guarded by this. A
store may only remove files from the subtree it owns, never from the user's own
library and never from another store's.

**`prune_empty_dirs(dirs, root)`** — the uninstall side of the same rule. A store
that has removed everything should not leave its folders behind, but only empty
directories go, and only inside `root`: one surprise file is enough to keep a
directory.

**`write_atomic`** — nothing is ever written in place. A store interrupted
mid-sync would otherwise leave a half-written playlist or gamelist, which is
worse than an old one.

## Taking every store off a machine

```sh
curl -fsSL https://git.teletypegames.org/engines/warpstore/raw/branch/master/uninstall.sh | sh
```

A single store is better removed by its own engine's `uninstall.sh`, which knows
that host's extras — a Batocera Ports entry, say. This one is for taking the
whole framework off a machine without having to remember what is installed: it
finds every store home under the known roots, has each store's **own engine**
purge what it installed, and then deletes the store, its launcher and the engine
files.

| Environment variable | Meaning |
|---|---|
| `DRY_RUN` | `1` to print what would go and remove nothing |
| `KEEP_GAMES` | `1` to remove only the scripts and leave the installed games |
| `FORCE` | `1` to purge even while RetroArch is running |
| `BATOCERA_STORE_ROOT`, `BATOCERA_PORTS_DIR` | where to look on a Batocera box |
| `STORE_ROOT`, `BIN_DIR` | where to look on a desktop |

If any engine cannot finish — RetroArch running, a ROMs root unmounted — the run
stops there rather than deleting the engine that knows what it installed.

The script is POSIX `sh`, not bash, so `| sh` works on a machine whose `/bin/sh`
is dash. The engines' own installers and uninstallers are too.

## State

`state.json`, version 2, keyed `<scope>:<name>`:

```json
{
  "version": 2,
  "installed": {
    "c64:blessingofra": { "name": "blessingofra", "scope": "c64", "asset": "blessingofra-2.0.0.prg", "…": "…" }
  }
}
```

`record_scope()` falls back to a record's `system` field, which is what the
Batocera store wrote before this module existed — and there the two were the same
string, so an installed box keeps working with no migration. A `version: 1` file,
keyed by bare software name, is re-keyed on first run.

## Users

- [`warp-engine-batocera-store`](https://git.teletypegames.org/stores/warp-engine-batocera-store) — EmulationStation ROM folders, `gamelist.xml`, Ports launchers
- [`warp-engine-retroarch-store`](https://git.teletypegames.org/stores/warp-engine-retroarch-store) — RetroArch `.lpl` playlists and thumbnail folders

Both pin nothing: they fetch `warpstore.py` from this repository's `master` at
install time. A change here therefore reaches every store on its next install —
which is the point, and also the reason to keep the surface above small.
