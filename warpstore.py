#!/usr/bin/env python3
"""warpstore — the part every WarpEngine store engine shares.

A store engine reads a WarpEngine catalog (`GET /api/software`) and writes some
host platform's own library format. Only that last step differs between hosts;
everything before it — talking to the API, choosing which release to install,
matching the machine we are running on, remembering what we put where — is here.

Two engines use it today:

    stores/warp-engine-batocera-store    EmulationStation ROM folders + gamelist.xml
    stores/warp-engine-retroarch-store   RetroArch .lpl playlists + thumbnails

An adapter supplies three things: a `DEFAULT_CONFIG` describing its own host, an
`accept()` callback deciding which catalog entries that host can run, and the
code that writes the host's library format. Everything else it calls from here.

Standard library only. The hosts range from a Batocera box (python3, no pip) to
a desktop, and neither may be asked to install anything.
"""

import json
import os
import platform
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.2.0"

# Bumped when the on-disk shape of state.json changes. v1 keyed `installed` by
# bare software name; v2 keys by `<scope>:<name>`.
STATE_VERSION = 2


class Skip(Exception):
    """Raised by an adapter's `accept()` to reject one catalog entry with a reason.

    The reason is what the user reads as `skipped <name>: <reason>`, so write it
    as an explanation, not as an error: "no 'c64' ROM folder on this box".
    """


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------

# Log prefix and temp-file prefix. `load_config` replaces it with the store id,
# so two stores on one machine are told apart in a shared log.
TAG = "warpstore"
VERBOSE = False


def set_tag(tag):
    global TAG
    TAG = tag


def set_verbose(flag):
    global VERBOSE
    VERBOSE = bool(flag)


def log(msg):
    print(f"[{TAG}] {msg}", flush=True)


def debug(msg):
    if VERBOSE:
        log(msg)


def die(msg, code=1):
    print(f"[{TAG}] error: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


# --------------------------------------------------------------------------
# filesystem
# --------------------------------------------------------------------------


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        log(f"warning: cannot read {path}: {exc}")
        return default


def write_json(path, data):
    write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"))


def write_atomic(path, blob):
    """Write bytes to `path` via a temp file in the same dir, then rename.

    A store may be interrupted mid-sync — a half-written playlist or gamelist is
    worse than an old one, so nothing is ever written in place.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{TAG}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def within(path, root):
    """True when `path` is `root` or sits inside it.

    Every delete a store performs is guarded by this: a store may only ever
    remove files from the subtree it owns, never from the user's own library or
    from another store's.
    """
    if not path or not root:
        return False
    path = os.path.abspath(path)
    root = os.path.abspath(root)
    return path == root or path.startswith(root + os.sep)


def prune_empty_dirs(dirs, root=None, dry_run=False):
    """Remove those of `dirs` that are now empty, deepest first.

    A store that has uninstalled everything should not leave its folders behind.
    Only empty directories go, and only inside `root` when one is given — so one
    surprise file is enough to keep a directory, and a wrong path cannot reach
    outside the store's own subtree.
    """
    ordered = sorted({os.path.abspath(d) for d in dirs if d},
                     key=lambda p: p.count(os.sep), reverse=True)
    for path in ordered:
        if root and not within(path, root):
            log(f"warning: refusing to remove {path} (outside {root})")
            continue
        if not os.path.isdir(path):
            continue
        if os.listdir(path):
            debug(f"keeping {path} (not empty)")
            continue
        if dry_run:
            log(f"would remove the empty {path}")
            continue
        try:
            os.rmdir(path)
            debug(f"removed the empty {path}")
        except OSError as exc:
            debug(f"cannot remove {path}: {exc}")


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


def deep_merge(base, overlay):
    """Recursively merge `overlay` into a copy of `base`; dicts merge, rest wins."""
    merged = dict(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path, defaults, overrides=None, required=()):
    """Read a store config, merge it onto the engine's defaults, validate it.

    `overrides` maps `"section.leaf"` to a CLI value (None means "not given"),
    and `required` names `"section.leaf"` paths that must end up non-empty.
    Also sets the log tag from `store.id` — from here on every message names
    the store it came from.
    """
    user = load_json(path, default=None)
    if user is None:
        log(f"warning: no config at {path} — falling back to the built-in defaults")
    cfg = deep_merge(json.loads(json.dumps(defaults)), user or {})

    for key, value in (overrides or {}).items():
        if value is None:
            continue
        section, _, leaf = key.partition(".")
        cfg.setdefault(section, {})[leaf] = value

    cfg["store"]["base_url"] = str(cfg["store"].get("base_url") or "").rstrip("/")
    for key in ("store.id",) + tuple(required):
        section, _, leaf = key.partition(".")
        if not (cfg.get(section) or {}).get(leaf):
            die(f"config: {key} is required")
    set_tag(f"{cfg['store']['id']}-store")
    return cfg


# --------------------------------------------------------------------------
# store home
# --------------------------------------------------------------------------

# A store keeps its config, state, catalog cache and log in one directory, so a
# machine can carry several stores side by side without them treading on each
# other. The config file's own directory is that home.
CONFIG_PATH = ""
HOME = ""
STATE_PATH = ""
CATALOG_CACHE = ""


def init(config_path):
    """Anchor the store's state and cache next to the config that selected it."""
    global CONFIG_PATH, HOME, STATE_PATH, CATALOG_CACHE
    CONFIG_PATH = os.path.abspath(config_path)
    HOME = os.path.dirname(CONFIG_PATH)
    STATE_PATH = os.path.join(HOME, "state.json")
    CATALOG_CACHE = os.path.join(HOME, "catalog.json")
    return HOME


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def base_url(cfg):
    return cfg["store"]["base_url"]


def api_url(cfg, endpoint, params=None):
    path = cfg["store"]["api"][endpoint]
    url = base_url(cfg) + "/" + path.lstrip("/")
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return url


def download_url(cfg, asset):
    """`GET /api/download` rather than `/file/`, so downloads are counted."""
    return api_url(cfg, "download", {"path": asset})


def user_agent(cfg):
    return f"warpstore/{VERSION} ({cfg['store']['id']})"


def _opener(cfg):
    if cfg["behavior"].get("insecure"):
        import ssl

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener()


def http_get(cfg, url):
    """Return (body_bytes, content_type)."""
    req = urllib.request.Request(url, headers={"User-Agent": user_agent(cfg)})
    with _opener(cfg).open(req, timeout=cfg["behavior"]["timeout"]) as resp:
        return resp.read(), resp.headers.get("Content-Type", "")


def http_download(cfg, url, dest):
    """Stream `url` into `dest` atomically. Returns bytes written."""
    req = urllib.request.Request(url, headers={"User-Agent": user_agent(cfg)})
    directory = os.path.dirname(dest) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{TAG}-", suffix=".part")
    written = 0
    try:
        with _opener(cfg).open(req, timeout=cfg["behavior"]["timeout"]) as resp, os.fdopen(fd, "wb") as out:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                written += len(chunk)
        if written == 0:
            raise IOError("empty response")
        os.replace(tmp, dest)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return written


# --------------------------------------------------------------------------
# the host machine
# --------------------------------------------------------------------------


def machine():
    """This machine's architecture, `uname -m` normalised.

    A native binary only starts on the architecture it was built for, so the
    release picker has to know: an x86_64 build installs on a Raspberry Pi and
    then does nothing, which is worse than not offering it at all.
    """
    m = (platform.machine() or "").lower()
    if m in ("x86_64", "amd64"):
        return "x86_64"
    if m in ("aarch64", "arm64"):
        return "aarch64"
    if m.startswith("armv") or m == "arm":
        return "armhf"
    if m in ("i386", "i486", "i586", "i686"):
        return "x86"
    return m or "unknown"


def host_os():
    """`linux`, `darwin`, `windows` or `android`.

    Batocera only ever answered `linux`, but a RetroArch host can be any of
    them, and it decides such things as what a libretro core file is called.
    """
    if os.environ.get("ANDROID_ROOT") and os.environ.get("ANDROID_DATA"):
        return "android"
    p = sys.platform
    if p.startswith("linux"):
        return "linux"
    if p == "darwin":
        return "darwin"
    if p.startswith("win") or p == "cygwin":
        return "windows"
    return p or "unknown"


def host():
    return {"os": host_os(), "arch": machine()}


def resolve_for_host(value, host_info=None):
    """Resolve a config value that may depend on the machine we run on.

    A plain string is the same everywhere — that is how a `cartridge` is
    described, being data for an emulator. Where it differs, the value is a map
    and the most specific key wins:

        {"linux-aarch64": …, "aarch64": …, "linux": …, "*": …}

    With no matching key and no `*`, the answer is None and the caller reports
    the title as skipped rather than installing something that cannot run.
    """
    if not isinstance(value, dict):
        return value
    h = host_info or host()
    for key in (f"{h['os']}-{h['arch']}", h["arch"], h["os"], "*"):
        if key in value:
            return value[key]
    return None


# --------------------------------------------------------------------------
# catalog
# --------------------------------------------------------------------------


def fetch_catalog(cfg, use_cache=False):
    """The whole catalog, cached next to the config so a sync survives an outage."""
    owner = cfg["catalog"].get("owner_id")
    url = api_url(cfg, "catalog", {"owner_id": owner} if owner is not None else None)
    try:
        body, _ = http_get(cfg, url)
        data = json.loads(body.decode("utf-8"))
        write_json(CATALOG_CACHE, data)
        return data
    except (urllib.error.URLError, OSError, ValueError) as exc:
        cached = load_json(CATALOG_CACHE) if use_cache else None
        if cached is not None:
            log(f"warning: catalog fetch failed ({exc}) — using cached copy")
            return cached
        die(f"cannot fetch catalog from {url}: {exc}")


def asset_basename(path):
    """`/file/blessingofra-2.0.0.prg` -> `blessingofra-2.0.0.prg`."""
    return os.path.basename((path or "").rstrip("/"))


def pick_release(entry, kinds, ext):
    """Newest non-dev release that carries an asset kind we can use.

    `releases` comes back from the API already sorted newest-first, and
    `latestRelease` is the newest stable one — check that first, then walk the
    rest so a game whose newest build is missing the asset still installs.

    `kinds` may be one kind or several in order of preference. Release order
    wins over kind order: the newest release that has *any* acceptable kind is
    taken, and within it the most preferred kind. That is what a desktop host
    wants — on Apple Silicon `mac_universal` beats `mac_x64`, which would need
    Rosetta, but not at the price of installing an older release.

    Returns (release, asset_name, kind, asset_path).
    """
    if isinstance(kinds, str) or kinds is None:
        kinds = [kinds]
    kinds = [k for k in kinds if k]

    candidates = []
    latest = entry.get("latestRelease")
    if latest:
        candidates.append(latest)
    candidates.extend(entry.get("releases") or [])

    seen = set()
    for release in candidates:
        rid = release.get("id")
        if rid in seen:
            continue
        seen.add(rid)
        if str(release.get("version", "")).startswith("dev-"):
            continue
        assets = release.get("assets") or []
        for kind in kinds:
            for asset in assets:
                if asset.get("kind") != kind:
                    continue
                path = asset.get("path", "")
                name = asset_basename(path)
                if not name or (ext and not name.lower().endswith(ext.lower())):
                    continue
                return release, name, kind, path
    return None, None, None, None


def select_games(cfg, catalog, accept, host_info=None):
    """Catalog -> (list of installable records, list of skip reasons).

    `accept(spec, sw)` is the adapter's veto and its labelling in one: given the
    `platforms` entry and the catalog's software dict, it returns

        (scope, extras)

    where `scope` groups records the way the host groups them — a Batocera
    system, a RetroArch playlist — and `extras` are the host-specific fields the
    adapter wants on the record. To reject the title, it raises `Skip(reason)`.
    """
    host_info = host_info or host()
    statuses = {s.lower() for s in cfg["catalog"].get("statuses") or []}
    only = {n.lower() for n in cfg["catalog"].get("only") or []}
    exclude = {n.lower() for n in cfg["catalog"].get("exclude") or []}

    games, skipped = [], []
    for entry in catalog.get("softwares") or []:
        sw = entry.get("software") or {}
        name = sw.get("name")
        if not name:
            continue

        spec = (cfg["platforms"] or {}).get(sw.get("platform"))
        if not spec or not spec.get("enabled", True):
            continue
        if statuses and str(sw.get("status", "")).lower() not in statuses:
            continue
        if only and name.lower() not in only:
            continue
        if name.lower() in exclude:
            continue

        try:
            scope, extras = accept(spec, sw)
        except Skip as reason:
            skipped.append(f"{name}: {reason}")
            continue

        wanted = resolve_for_host(spec.get("kind"), host_info)
        if not wanted:
            skipped.append(f"{name}: {sw.get('platform')} has no asset kind for "
                           f"{host_info['os']}/{host_info['arch']}")
            continue
        ext = resolve_for_host(spec.get("ext", ""), host_info) or ""

        release, file_name, kind, asset_path = pick_release(entry, wanted, ext)
        if not release:
            listed = wanted if isinstance(wanted, str) else "/".join(wanted)
            skipped.append(f"{name}: no '{listed}' asset in any release")
            continue

        record = {
            "name": name,
            "scope": scope,
            "platform": sw.get("platform"),
            "kind": kind,
            "version": str(release.get("version", "")),
            "asset": file_name,
            # The catalog-side path, kept because not every asset is a download:
            # an `html` build is a hosted directory, and a store may want its URL.
            "asset_path": asset_path,
            "title": sw.get("title") or name,
            "desc": (sw.get("desc") or "").strip(),
            "author": (sw.get("author") or "").strip(),
            "image_url": sw.get("imageUrl"),
            "created_at": release.get("createdAt"),
        }
        record.update(extras or {})
        games.append(record)
    return games, skipped


def image_url(cfg, game):
    """The catalog gives a server-relative `imageUrl`; absolute ones pass through."""
    url = game["image_url"]
    return url if url.startswith(("http://", "https://")) else base_url(cfg) + url


IMAGE_EXTS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}


def download_image(cfg, game):
    """Box art as (bytes, extension), or (None, None) if it cannot be had.

    Where the bytes go is the host's business — a gamelist wants them next to
    the ROM, RetroArch wants them under a playlist-named thumbnail folder — so
    this only fetches them.
    """
    try:
        body, ctype = http_get(cfg, image_url(cfg, game))
    except (urllib.error.URLError, OSError) as exc:
        log(f"warning: box art for {game['name']} failed: {exc}")
        return None, None
    ext = IMAGE_EXTS.get(ctype.split(";")[0].strip().lower(), ".png")
    return body, ext


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------


def record_scope(record):
    """The scope a state record belongs to.

    `scope` is what the engines write today. `system` is what the Batocera store
    wrote before this module existed, and there the two were the same string —
    so an installed box keeps working without a migration.
    """
    return record.get("scope") or record.get("system") or ""


def game_key(record):
    """Two scopes may both carry a game called `foo` — key on both."""
    return f"{record_scope(record)}:{record['name']}"


def load_state():
    state = load_json(STATE_PATH, default=None) or {}
    version = state.get("version")
    if version == 1:
        state = migrate_state_v1(state)
    elif version != STATE_VERSION:
        state = {"version": STATE_VERSION, "installed": {}}
    state.setdefault("installed", {})
    return state


def migrate_state_v1(state):
    """v1 keyed `installed` by bare software name; v2 keys by `<scope>:<name>`."""
    installed = {}
    for record in (state.get("installed") or {}).values():
        if record_scope(record) and record.get("name"):
            installed[game_key(record)] = record
    log(f"migrated {len(installed)} state entries to the per-scope key format")
    return {"version": STATE_VERSION, "installed": installed}


def save_state(state):
    state["version"] = STATE_VERSION
    write_json(STATE_PATH, state)


def records_by_scope(installed):
    grouped = {}
    for record in installed.values():
        grouped.setdefault(record_scope(record), []).append(record)
    return grouped


def match_keys(installed, names):
    """Resolve user-typed `name` or `scope:name` arguments to state keys."""
    wanted = {n.lower() for n in names}
    return [
        key for key, record in installed.items()
        if key.lower() in wanted or record["name"].lower() in wanted
    ]


def limit_to_names(games, names):
    """Keep only the games the user named, by bare name or `scope:name`."""
    wanted = {n.lower() for n in names}
    return [g for g in games if g["name"].lower() in wanted or game_key(g).lower() in wanted]
