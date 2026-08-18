#!/bin/sh
# Remove every WarpEngine store from this machine — the games, the store
# engines, this shared core and the launchers.
#
#   curl -fsSL https://git.teletypegames.org/engines/warpstore/raw/branch/master/uninstall.sh | sh
#
# A single store is better removed by its own engine's uninstaller, which knows
# that host's extras (a Batocera Ports entry, say). This one is for taking the
# whole framework off a machine without having to remember what is installed:
# it finds every store home under the known roots, has each store's own engine
# purge what it installed, and then deletes the store.
#
# Three engines exist today — Batocera, RetroArch and desktop — and the last two
# share a store root, so a home is identified by which engine file is in it.
#
#   DRY_RUN=1   print what would go, remove nothing
#   KEEP_GAMES=1  leave the installed games alone, remove only the scripts
#   FORCE=1     purge even while RetroArch is running
#
# POSIX sh on purpose: `| sh` has to work on a machine whose /bin/sh is dash.
set -eu

BATOCERA_STORE_ROOT="${BATOCERA_STORE_ROOT:-/userdata/system/batocera-store}"
BATOCERA_PORTS_DIR="${BATOCERA_PORTS_DIR:-/userdata/roms/ports}"
STORE_ROOT="${STORE_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/warp-engine-store}"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
DRY_RUN="${DRY_RUN:-0}"
KEEP_GAMES="${KEEP_GAMES:-0}"
FORCE="${FORCE:-0}"

say() { echo "[uninstall-all] $*"; }
run() {
  if [ "$DRY_RUN" = "1" ]; then echo "[uninstall-all] would: $*"; else "$@"; fi
}

command -v python3 >/dev/null 2>&1 || {
  echo "[uninstall-all] error: python3 is required to ask the engines what they installed" >&2
  exit 1
}

FOUND=0

# `purge` is the engine's own "remove everything I installed": the shell has no
# way of knowing which ROMs, playlists, thumbnails or gamelist entries were a
# store's, and state.json does.
purge_store() {  # purge_store <store home> <engine file> <home env var> <extra flag>
  home="$1"; engine="$2"; homevar="$3"; extra="$4"
  if [ "$KEEP_GAMES" = "1" ]; then
    say "keeping the games of '$(basename "$home")' (KEEP_GAMES=1)"
    return 0
  fi
  if [ ! -f "$home/$engine" ] || [ ! -f "$home/config.json" ]; then
    say "warning: no engine in $home — its games have to go by hand"
    return 0
  fi
  dry=""
  [ "$DRY_RUN" = "1" ] && dry="--dry-run"
  # shellcheck disable=SC2086
  env "$homevar=$home" python3 "$home/$engine" --config "$home/config.json" $dry purge $extra || {
    say "error: the engine in $home could not finish — stopping before anything else is removed."
    say "       Close RetroArch (or FORCE=1), fix what it reported, and run this again."
    exit 1
  }
}

# --- Batocera stores -------------------------------------------------------
if [ -d "$BATOCERA_STORE_ROOT" ]; then
  for home in "$BATOCERA_STORE_ROOT"/*/; do
    [ -f "$home/config.json" ] || continue
    id="$(basename "$home")"
    FOUND=$((FOUND + 1))
    say "batocera store '$id' in $home"
    purge_store "${home%/}" store.py BATOCERA_STORE_HOME ""

    # The Ports entry's name is whatever the store repository chose, not
    # store.name — but it always runs this store's launcher, so it is found by
    # looking inside the scripts rather than by guessing a file name.
    if [ -d "$BATOCERA_PORTS_DIR" ]; then
      for entry in "$BATOCERA_PORTS_DIR"/*.sh; do
        [ -f "$entry" ] || continue
        if grep -q -F "${home%/}" "$entry" 2>/dev/null; then
          run rm -f "$entry"
        fi
      done
    fi
    run rm -f "$BATOCERA_STORE_ROOT/$id-store"
    run rm -rf "${home%/}"
  done
  [ "$DRY_RUN" = "1" ] || rmdir "$BATOCERA_STORE_ROOT" 2>/dev/null || true
fi

# --- RetroArch and desktop stores -----------------------------------------
# Both live under the same root, one directory per store id, told apart by the
# engine file inside. A store id can even carry both.
if [ -d "$STORE_ROOT" ]; then
  extra=""
  [ "$FORCE" = "1" ] && extra="--force"
  for home in "$STORE_ROOT"/*/; do
    [ -f "$home/config.json" ] || continue
    name="$(basename "$home")"
    home="${home%/}"
    if [ -f "$home/retroarch_store.py" ]; then
      id="${name%-retroarch}"
      FOUND=$((FOUND + 1))
      say "retroarch store '$id' in $home"
      purge_store "$home" retroarch_store.py RETROARCH_STORE_HOME "$extra"
      run rm -f "$BIN_DIR/$id-retroarch-store"
    fi
    if [ -f "$home/desktop_store.py" ]; then
      id="${name%-desktop}"
      FOUND=$((FOUND + 1))
      say "desktop store '$id' in $home"
      purge_store "$home" desktop_store.py DESKTOP_STORE_HOME ""
      run rm -f "$BIN_DIR/$id-desktop-store"
    fi
    run rm -rf "$home"
  done
  [ "$DRY_RUN" = "1" ] || rmdir "$STORE_ROOT" 2>/dev/null || true
fi

if [ "$FOUND" = "0" ]; then
  say "no WarpEngine store found. Looked in:"
  say "  $BATOCERA_STORE_ROOT"
  say "  $STORE_ROOT"
  say "Point at them with BATOCERA_STORE_ROOT / STORE_ROOT if they live elsewhere."
  exit 0
fi

if [ "$DRY_RUN" = "1" ]; then
  say "dry run — nothing was removed ($FOUND store(s) found)."
else
  say "done: $FOUND store(s) removed, engines and the shared core with them."
  say "Restart EmulationStation or RetroArch to see the games go."
fi
