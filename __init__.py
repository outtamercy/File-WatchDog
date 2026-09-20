"""File Watchdog: narrow, inspectable routes for known generated output."""
from __future__ import annotations
import configparser, ctypes, html, importlib.util, json, logging, os, re, shutil, subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import quote
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import mobase
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices, QFont, QIcon
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget, QTableWidgetItem, QTextBrowser, QTabWidget, QVBoxLayout, QWidget)

PLUGIN_DIR = Path(__file__).parent
logger = logging.getLogger("FileWatchdog")
if not logger.handlers:
    # Debug is a fresh-session receipt, not an archaeological dig.
    handler = logging.FileHandler(PLUGIN_DIR / "fw_debug.log", mode="w", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler); logger.setLevel(logging.DEBUG)

@dataclass(frozen=True)
class OutputProfile:
    key: str; title: str; mod_name: str; routes: Tuple[str, ...]; name_hints: Tuple[str, ...]; dll_hints: Tuple[str, ...] = (); description: str = ""

# Narrow on purpose. Generic textures/meshes routes are retired. The one
# deliberate parent route is a dedicated, opt-in FW-owned SKSE runtime mod.
OUTPUT_PROFILES = (
    OutputProfile("skse_runtime", "Runtime staging — whole folder", "FileWatchdog - Runtime Staging Output", ("SKSE",), (), (), "MO2's live staging bucket. RDNA sorts it after the game is done."),
    OutputProfile("project_legacy", "Project Legacy", "FileWatchdog - Project Legacy Output", ("SKSE/Plugins/ProjectLegacy",), ("project legacy", "projectlegacy"), ("ProjectLegacy*.dll",), "Project Legacy's own output. CharGen belongs to FAM, full stop."),
    OutputProfile("fam_faces", "FAM Faces / CharGen", "FileWatchdog - FAM Faces Output", ("SKSE/Plugins/CharGen/Exported", "Textures/CharGen/Exported"), ("fam faces", "familiar faces", "chargen"), ("CharGen*.dll",), "CharGen exports, not somebody else's mod."),
    OutputProfile("community_shaders", "Community Shaders", "FileWatchdog - Community Shaders Output", ("SKSE/Plugins/CommunityShaders",), ("community shaders",), ("CommunityShaders*.dll",), "Shader settings and cache bits."),
    OutputProfile("faster_file_copy", "Faster File Copy", "FileWatchdog - Faster File Copy Cache", ("SKSE/Plugins/FasterFileCopy_cache",), ("faster file copy", "fasterfilecopy"), ("FasterFileCopy*.dll",), "The chunky copy cache."),
    OutputProfile("shader_cache", "Shader Cache", "FileWatchdog - Shader Cache", ("ShaderCache",), ("community shaders", "shader cache"), (), "Disposable shader cache."),
    OutputProfile("cell_offsets", "Cell Offsets Cache", "FileWatchdog - Cell Offsets Cache", ("CellOffsets",), ("cell offsets", "grass control"), ("*GrassControl*.dll",), "Generated cell offsets."),
    OutputProfile("other_overwrite", "Other Overwrite files", "FileWatchdog - Other Overwrite Output", (), ("filewatchdog - other overwrite output",), (), "Everything still sitting in Overwrite, path preserved. No mystery deletes."),
)
PROFILES_BY_KEY = {p.key: p for p in OUTPUT_PROFILES}

def _is_skyrim(paths):
    name = (paths.game_name or "").lower()
    root = paths.game_root
    return "skyrim" in name or bool(root and ((root / "SkyrimSE.exe").is_file() or (root / "SkyrimVR.exe").is_file()))

def _profiles_for(paths):
    """Known generator recipes are Skyrim-only; the catch-all is for every game."""
    return OUTPUT_PROFILES if _is_skyrim(paths) else (PROFILES_BY_KEY["other_overwrite"],)

@dataclass
class ManagedRoute:
    profile: str; link: str; target: str; ownership: str = "fw-owned"; executables: Tuple[str, ...] = ()
@dataclass
class RouteRow:
    profile: str; title: str; link: Path; target: Path; target_type: str; status: str; managed: bool = False; legacy: bool = False

class OrganizerPaths:
    def __init__(self, organizer):
        self.organizer = organizer; self.config_path, self.root = self._resolve_instance(); config = configparser.ConfigParser(interpolation=None); config.read(self.config_path, encoding="utf-8"); general = config["General"] if config.has_section("General") else {}
        if not general: raise RuntimeError(f"{self.config_path} has no [General] section. MO2 left us no map.")
        self.mods = self._path(general.get("modsPath", "./mods")); self.overwrite = self._path(general.get("overwritePath", "overwrite")); self.profiles = self._path(general.get("profilesPath", "profiles")); self.profile_name = self._profile_name(general)
        configured_game = self._managed_game_name() or self._decode(general.get("gameName", ""))
        self.game_name = "SkyrimSE" if configured_game == "Skyrim Special Edition" else configured_game
    def _resolve_instance(self):
        """Portable first, then MO2's selected global instance. No random-instance guessing."""
        base = Path(self.organizer.basePath()).resolve(); portable = base / "ModOrganizer.ini"
        if portable.is_file(): return portable, base
        appdata = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "ModOrganizer"; instances = appdata / "instances.ini"
        parser = configparser.ConfigParser(interpolation=None)
        if instances.is_file():
            parser.read(instances, encoding="utf-8"); selected = self._decode(parser.get("General", "selected_instance", fallback=""))
            section = next((name for name in parser.sections() if name.lower() == selected.lower()), None)
            if selected:
                config = appdata / selected / "ModOrganizer.ini"
                if config.is_file():
                    raw = self._decode(parser.get(section, "path", fallback="")) if section else ""; anchor = Path(raw) if raw else config.parent
                    if raw and not anchor.is_absolute(): anchor = appdata / anchor
                    return config, anchor.resolve()
        flat = base.parent / "ModOrganizer.ini"
        if flat.is_file(): return flat, flat.parent.resolve()
        raise RuntimeError(f"MO2 config not found. Checked portable {portable} and the selected global instance in {instances}.")
    def _decode(self, value):
        value = str(value).strip()
        if value.startswith("@ByteArray(") and value.endswith(")"): value = value[11:-1]
        return value.replace("\\\\", "\\").strip().strip("\"'")
    def _managed_game_name(self):
        """MO2 knows which game this instance owns. Ask it before config spelunking."""
        try:
            game = self.organizer.managedGame()
            for attribute in ("gameName", "name"):
                value = getattr(game, attribute, None)
                value = value() if callable(value) else value
                if value: return str(value).strip()
        except Exception:
            pass
        return ""
    def _path(self, value):
        value = self._decode(value); path = Path(value) if value else self.root
        return (path if path.is_absolute() else self.root / path).resolve()
    def _profile_name(self, general):
        saved = self._decode(general.get("selected_profile", ""))
        try:
            runtime = self.organizer.profileName()
            if runtime and (self.profiles / runtime).is_dir(): return runtime
        except Exception: return saved or "Default"
        if saved and (self.profiles / saved).is_dir(): return saved
        return saved or "Default"
    @property
    def profile_dir(self): return self.profiles / self.profile_name
    @property
    def modlist_path(self): return self.profile_dir / "modlist.txt"
    def active_mods(self):
        if not self.modlist_path.is_file(): return []
        return [line[1:].strip() for line in self.modlist_path.read_text(encoding="utf-8").splitlines() if line.startswith("+")]
    def listed_mods(self):
        """Both enabled and disabled profile mods. Root payloads are often off."""
        if not self.modlist_path.is_file(): return []
        return [(line[1:].strip(), line.startswith("+")) for line in self.modlist_path.read_text(encoding="utf-8").splitlines() if line[:1] in "+-" and line[1:].strip()]
    @property
    def data_dir(self): return PLUGIN_DIR.parent / "data" / "file_watchdog" / "profiles" / quote(self.profile_name, safe="")
    @property
    def manifest_path(self): return self.data_dir / "managed_routes.json"
    @property
    def cache_path(self): return self.data_dir / "detection_cache.json"
    @property
    def profile_settings_path(self): return self.profile_dir / "settings.ini"
    @property
    def ui_settings_path(self): return self.data_dir / "ui_colors.json"
    @property
    def game_root(self):
        """MO2 owns the game path. Config fallback is only for older API builds."""
        try:
            game = self.organizer.managedGame(); value = game.gameDirectory()
            if hasattr(value, "absolutePath"): value = value.absolutePath()
            if value: return Path(str(value)).resolve()
        except Exception: pass
        config = configparser.ConfigParser(interpolation=None); config.read(self.config_path, encoding="utf-8")
        raw = self._decode(config.get("General", "gamePath", fallback=""))
        return Path(raw).resolve() if raw else None
    def game_executables(self):
        """Main game EXE first; extra root EXEs are only process guards."""
        root = self.game_root
        if not root or not root.is_dir(): return ()
        compact = re.sub(r"[^a-z0-9]", "", self.game_name.lower())
        exact = root / (compact + ".exe")
        if compact and exact.is_file(): return (exact.name,)
        # MO2 game names and executable stems usually agree. If they don't,
        # don't make up a launcher binding—RootDNA can still discover roots.
        return ()
    def root_executables(self):
        root = self.game_root
        if not root or not root.is_dir(): return ()
        try: return tuple(item.name for item in root.iterdir() if item.is_file() and item.suffix.lower() == ".exe")
        except OSError: return ()
    def game_identity(self):
        root = self.game_root
        return {"name": self.game_name or (root.name if root else "unknown"), "root": str(root).lower() if root else ""}

class RouteManifest:
    def __init__(self, path, game_identity=None):
        self.path = path; self.game_identity = game_identity or {}; self.routes: Dict[str, ManagedRoute] = {}; self.history = []; self.conflict = None
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                known_game = raw.get("game_identity")
                if known_game and known_game != self.game_identity:
                    self.conflict = "FW manifest belongs to another managed game. It won't mix routes."; return
                self.routes = {item["link"]: ManagedRoute(**item) for item in raw.get("routes", [])}; self.history = list(raw.get("history", []))
            except Exception as exc: logger.warning("Manifest is unreadable; leaving it alone: %s", exc)
    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"version": 7, "game_identity": self.game_identity, "routes": [asdict(r) for r in self.routes.values()], "history": self.history}, indent=2), encoding="utf-8")
    def _note(self, action, route):
        entry = asdict(route); entry["action"] = action; entry["when"] = datetime.now(timezone.utc).isoformat(timespec="seconds"); self.history.append(entry)
    def add(self, profile, link, target, executables=()):
        route = ManagedRoute(profile.key, str(link), str(target), executables=tuple(executables)); self.routes[str(link)] = route; self._note("moved out of Overwrite", route); self.save()
    def remove(self, link, action="restored to Overwrite"):
        route = self.routes.pop(str(link), None)
        if route: self._note(action, route)
        self.save()
    def note(self, profile, link, target, action):
        self._note(action, ManagedRoute(profile.key, str(link), str(target))); self.save()

def _is_link(path):
    try:
        if not path.exists() and not path.is_symlink(): return False
        return path.is_symlink() or bool(ctypes.windll.kernel32.GetFileAttributesW(str(path)) & 0x0400)
    except OSError: return False
def _same_target(link, target):
    try: return link.resolve(strict=True) == target.resolve(strict=True)
    except OSError: return False
def _profile_ini(paths):
    ini = configparser.ConfigParser(interpolation=None); ini.optionxform = str
    if paths.profile_settings_path.is_file(): ini.read(paths.profile_settings_path, encoding="utf-8")
    if not ini.has_section("custom_overwrites"): ini.add_section("custom_overwrites")
    return ini
def _binding_key(executable): return quote(executable, safe="")
def _bound_output_mod(paths, executable):
    return _profile_ini(paths).get("custom_overwrites", _binding_key(executable), fallback="")
def _write_output_overrides(paths, updates):
    """Touch only [custom_overwrites]; MO2's geometry soup gets left alone."""
    path = paths.profile_settings_path; text = path.read_text(encoding="utf-8") if path.is_file() else ""
    newline = "\r\n" if "\r\n" in text else "\n"; lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.strip().lower() == "[custom_overwrites]"), None)
    if start is None:
        if lines and lines[-1].strip(): lines.append(newline)
        lines.extend(["[custom_overwrites]" + newline]); start = len(lines) - 1; end = len(lines)
    else:
        end = next((i for i in range(start + 1, len(lines)) if re.match(r"^\s*\[.*\]\s*$", lines[i])), len(lines))
    seen = set(); rewritten = []
    for line in lines[start + 1:end]:
        match = re.match(r"^([^=\r\n]+)=(.*?)(\r?\n)?$", line)
        key = match.group(1) if match else None
        if key not in updates:
            rewritten.append(line); continue
        seen.add(key); value = updates[key]
        if value is not None: rewritten.append(f"{key}={value}{newline}")
    for key, value in updates.items():
        if key not in seen and value is not None: rewritten.append(f"{key}={value}{newline}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines[:start + 1] + rewritten + lines[end:]), encoding="utf-8", newline="")
def _set_output_bindings(paths, executables, mod_name):
    """Use MO2's actual per-profile output-mod setting. No fake junction plumbing."""
    ini = _profile_ini(paths)
    updates = {}
    for executable in executables:
        old = ini.get("custom_overwrites", _binding_key(executable), fallback="")
        if old and old != mod_name:
            raise RuntimeError(f"{executable} already writes to {old}. FW won't steal somebody else's output mod.")
        updates[_binding_key(executable)] = mod_name
    if updates: _write_output_overrides(paths, updates)
def _replace_output_binding(paths, old_name, new_name):
    ini = _profile_ini(paths); updates = {}
    for key, value in ini.items("custom_overwrites"):
        if value == old_name: updates[key] = new_name
    if updates: _write_output_overrides(paths, updates)
def _clear_output_bindings(paths, executables, mod_name):
    ini = _profile_ini(paths); updates = {}
    for executable in executables:
        key = _binding_key(executable)
        if ini.get("custom_overwrites", key, fallback="") == mod_name:
            updates[key] = None
    if updates: _write_output_overrides(paths, updates)
    return bool(updates)
def _runtime_executables(profile):
    # These are the two real game launch entries in this instance. Everything
    # else gets left alone until it has its own explicit output profile.
    return ("Start the Mayhem!", "SKSE") if profile.key == "skse_runtime" else ()
def _has_link_parent(path): return any(_is_link(parent) for parent in path.parents)
def _has_nested_link(path):
    for root, dirs, _files in os.walk(path, topdown=True, followlinks=False):
        for name in list(dirs):
            candidate = Path(root) / name
            if _is_link(candidate): return True
    return False
def _fingerprint(paths):
    try:
        stat = paths.modlist_path.stat(); bits = [f"{paths.game_identity()}:{paths.profile_name}:{stat.st_mtime_ns}:{stat.st_size}"]
    except OSError: bits = [f"{paths.profile_name}:missing"]
    # Direct route stamps catch fresh Overwrite output without crawling its files.
    for profile in _profiles_for(paths):
        for route in profile.routes:
            try:
                stat = (paths.overwrite / route).stat(); bits.append(f"{route}:{stat.st_mtime_ns}")
            except OSError: bits.append(f"{route}:missing")
    return "|".join(bits)

def _detected_profile_keys(paths):
    """No active-mod rglob. Names plus direct SKSE filename checks are plenty."""
    fingerprint = _fingerprint(paths)
    try:
        cache = json.loads(paths.cache_path.read_text(encoding="utf-8"))
        if cache.get("fingerprint") == fingerprint: return list(cache.get("profiles", []))
    except (OSError, ValueError, TypeError): pass
    active = paths.active_mods(); names = [name.lower() for name in active]; detected = []
    for profile in _profiles_for(paths):
        found = any(any(hint in name for hint in profile.name_hints) for name in names)
        if not found and profile.dll_hints:
            for name in active:
                plugin_dir = paths.mods / name / "SKSE" / "Plugins"
                if plugin_dir.is_dir() and any(any(plugin_dir.glob(hint)) for hint in profile.dll_hints): found = True; break
        if not found: found = any((paths.overwrite / route).exists() for route in profile.routes)
        if found: detected.append(profile.key)
    paths.cache_path.parent.mkdir(parents=True, exist_ok=True); paths.cache_path.write_text(json.dumps({"fingerprint": fingerprint, "profiles": detected}), encoding="utf-8")
    return detected

def _legacy_links(overwrite, manifest):
    """Directory entries only; prune links and leave .bak history alone."""
    found = []; known = set(manifest.routes)
    if not overwrite.is_dir(): return found
    for root, dirs, _files in os.walk(overwrite, topdown=True, followlinks=False):
        dirs[:] = [name for name in dirs if not name.lower().endswith(".bak")]
        for name in list(dirs):
            candidate = Path(root) / name
            if _is_link(candidate):
                dirs.remove(name)
                if str(candidate) not in known: found.append(candidate)
    return found
def _row_status(paths, link, target, managed, executables=()):
    if executables and all(_bound_output_mod(paths, exe) == target.parents[len(Path(link.relative_to(paths.overwrite)).parts) - 1].name for exe in executables): return "MO2 output mod bound"
    if _has_link_parent(link): return "covered by parent junction"
    if _is_link(link): return "linked" if _same_target(link, target) else ("collision" if managed else "legacy linked")
    if target.exists(): return "already unlinked" if managed else "unlinked — restore available"
    return "ready"

def _build_rows(paths, manifest):
    rows = {}; detected = set(_detected_profile_keys(paths))
    for route in manifest.routes.values():
        profile = PROFILES_BY_KEY.get(route.profile); link, target = Path(route.link), Path(route.target)
        rows[(str(link), route.profile)] = RouteRow(route.profile, profile.title if profile else f"Unknown manifest profile ({route.profile})", link, target, "FW-owned", _row_status(paths, link, target, True, route.executables), True)
    for profile in _profiles_for(paths):
        for route in profile.routes:
            link = paths.overwrite / route
            # Runtime staging is a permanent MO2 bucket. It must still be
            # visible when Overwrite is clean, otherwise its binding gets lost.
            if profile.key != "skse_runtime" and profile.key not in detected and not link.exists(): continue
            key = (str(link), profile.key)
            if key not in rows:
                # MO2 exposes a mod's root as Data. Keep SKSE routes as
                # <FW mod>\SKSE\Plugins\..., never as a made-up folder name.
                target = paths.mods / profile.mod_name / route
                rows[key] = RouteRow(profile.key, profile.title, link, target, "FW-owned", _row_status(paths, link, target, False, _runtime_executables(profile)))
    # The catch-all preserves Overwrite's relative paths at the mod root, so it
    # has no normal child route to discover. Give it a real return row anyway.
    other = PROFILES_BY_KEY["other_overwrite"]; other_root = paths.mods / other.mod_name
    if other_root.is_dir() and any(item.is_file() and item.name.lower() != "meta.ini" for item in other_root.rglob("*")):
        rows[(str(paths.overwrite), other.key)] = RouteRow(other.key, other.title, paths.overwrite, other_root, "FW-owned", "restore available", True)
    for link in _legacy_links(paths.overwrite, manifest):
        target = link.resolve(); kind = "legacy FW-owned mod" if target.name.lower().startswith("filewatchdog -") else "legacy normal mod"
        rows[(str(link), "legacy")] = RouteRow("legacy", "Legacy recovery", link, target, kind, "legacy linked", legacy=True)
    return sorted(rows.values(), key=lambda r: (str(r.link).lower(), r.profile))

def _check_restore_collision(link, target, skipped=()):
    if not target.is_dir(): raise RuntimeError(f"Target is missing: {target}")
    for root, _dirs, files in os.walk(target):
        base = Path(root)
        for name in files:
            if base.relative_to(target).joinpath(name).as_posix() in skipped: continue
            destination = link / base.relative_to(target) / name
            if destination.exists(): raise RuntimeError(f"Won't overwrite an Overwrite file: {destination}")
def _move_tree(source, target, skipped=()):
    """Move, never copy. Same-drive moves are renames; cross-drive is copy+delete."""
    for root, dirs, files in os.walk(source):
        base = Path(root)
        for name in dirs: (target / base.relative_to(source) / name).mkdir(parents=True, exist_ok=True)
        for name in files:
            if base.relative_to(source).joinpath(name).as_posix() in skipped: continue
            item = base / name; destination = target / item.relative_to(source); destination.parent.mkdir(parents=True, exist_ok=True); shutil.move(str(item), str(destination))
    _prune_empty_dirs(source)
    remaining = [item for item in source.rglob("*") if item.is_file() and item.relative_to(source).as_posix() not in skipped] if source.is_dir() else []
    if remaining:
        raise RuntimeError(f"Move did not finish; {len(remaining)} source file(s) are still in {source}. Nothing gets called done until they're gone.")
def _prune_empty_dirs(root):
    for folder in sorted((path for path in root.rglob("*") if path.is_dir()), key=lambda path: len(path.parts), reverse=True):
        try: folder.rmdir()
        except OSError: pass
def _prepare_fw_mod(paths, profile):
    """Give MO2 a proper mod receipt, then park all FW outputs at profile bottom."""
    (paths.mods / profile.mod_name).mkdir(parents=True, exist_ok=True)
    _sync_fw_mods(paths)
def _migrate_runtime_staging_name(paths, manifest):
    """One-time rename. Same files, same mapping, less misleading name."""
    old_name = "FileWatchdog - SKSE Runtime Output"; new_name = PROFILES_BY_KEY["skse_runtime"].mod_name
    old_root, new_root = paths.mods / old_name, paths.mods / new_name
    if old_root.is_dir() and not new_root.exists(): shutil.move(str(old_root), str(new_root))
    _replace_output_binding(paths, old_name, new_name)
    if old_root.is_dir() or not new_root.is_dir(): return
    if paths.modlist_path.is_file():
        text = paths.modlist_path.read_text(encoding="utf-8")
        paths.modlist_path.write_text(text.replace(f"+{old_name}", f"+{new_name}").replace(f"-{old_name}", f"-{new_name}"), encoding="utf-8")
    changed = False
    for route in manifest.routes.values():
        if old_name in route.target:
            route.target = route.target.replace(old_name, new_name); changed = True
            route.executables = tuple(route.executables)
    for entry in manifest.history:
        if old_name in entry.get("target", ""): entry["target"] = entry["target"].replace(old_name, new_name); changed = True
    if changed: manifest.save()
def _retire_obsolete_routes(paths, manifest):
    """Drop any old manifest route whose profile no longer owns that path."""
    retired = []
    for route in list(manifest.routes.values()):
        profile = PROFILES_BY_KEY.get(route.profile)
        if not profile: continue
        row = RouteRow(route.profile, profile.title, Path(route.link), Path(route.target), "FW-owned", "retired", True)
        if _route_is_current(paths, row): continue
        manifest.remove(row.link, "retired overlapping route")
        note = _remove_empty_fw_mod(paths, row, manifest)
        retired.append(f"Retired {row.title}.{note}")
    return retired
def _prune_empty_fw_mods(paths, manifest):
    """Generic cleanup for empty FW-made mods. No profile-name special cases."""
    bound = {value for _key, value in _profile_ini(paths).items("custom_overwrites")}
    active_roots = {str(root) for route in manifest.routes.values() if (root := _fw_mod_root(RouteRow(route.profile, "", Path(route.link), Path(route.target), "", "", True))) is not None}
    removed = []
    for profile in OUTPUT_PROFILES:
        root = paths.mods / profile.mod_name; meta = root / "meta.ini"
        try: owned = "FW-owned generated runtime output" in meta.read_text(encoding="utf-8")
        except OSError: owned = False
        if not owned or profile.mod_name in bound or str(root) in active_roots: continue
        if any(item.is_file() and item.name.lower() != "meta.ini" for item in root.rglob("*")): continue
        try:
            _prune_empty_dirs(root); meta.unlink(); root.rmdir(); removed.append(root.name)
        except OSError: pass
    if removed: _sync_fw_mods(paths)
    return removed
def _move_remaining_overwrite(paths, manifest):
    """One explicit mop-up. Keep paths, leave Overwrite itself there and empty."""
    profile = PROFILES_BY_KEY["other_overwrite"]; target = paths.mods / profile.mod_name
    _prepare_fw_mod(paths, profile); target.mkdir(parents=True, exist_ok=True)
    files = [item for item in paths.overwrite.rglob("*") if item.is_file()]
    if not files:
        _prune_empty_dirs(paths.overwrite)
        return "Overwrite is already empty. Cleared its empty folder crumbs too."
    _check_restore_collision(paths.overwrite, target); _move_tree(paths.overwrite, target); _prune_empty_dirs(paths.overwrite)
    manifest.note(profile, paths.overwrite, target, "moved remaining Overwrite files")
    return f"Moved {len(files)} remaining Overwrite file(s) to {target}. Overwrite is empty now."
def _has_files(path):
    return path.is_dir() and any(item.is_file() for item in path.rglob("*"))
def _final_cleanup(paths, manifest):
    """Post-game mop-up: known routes first, then one honest home for leftovers."""
    messages = []
    rows = sorted(_build_rows(paths, manifest), key=lambda row: len(row.link.parts))
    for row in rows:
        if row.legacy or row.profile == "other_overwrite" or not _has_files(row.link): continue
        if not _route_is_current(paths, row): continue
        parent = _managed_parent(manifest, row.link)
        if parent: continue
        messages.append(_make_route(paths, manifest, row))
    messages.append(_move_remaining_overwrite(paths, manifest))
    return "\n\n".join(messages)
def _process_runtime_staging(paths, manifest):
    """Post-game pass. Drain the MO2 bucket with moves, then keep it bound."""
    staging = PROFILES_BY_KEY["skse_runtime"]
    staging_root = paths.mods / staging.mod_name
    _prepare_fw_mod(paths, staging)
    _set_output_bindings(paths, _runtime_executables(staging), staging.mod_name)
    messages = []; moved = 0
    for profile in OUTPUT_PROFILES:
        if profile.key in {"skse_runtime", "other_overwrite"}: continue
        for route in profile.routes:
            source = staging_root / route
            if not _has_files(source): continue
            target = paths.mods / profile.mod_name / route
            _check_restore_collision(target, source)
            _prepare_fw_mod(paths, profile); target.mkdir(parents=True, exist_ok=True)
            count = sum(1 for item in source.rglob("*") if item.is_file())
            _move_tree(source, target); moved += count
            manifest.note(profile, source, target, "moved out of runtime staging")
            messages.append(f"Moved {count} file(s): {source} -> {target}")
    leftovers = [item for item in staging_root.rglob("*") if item.is_file() and item.name.lower() != "meta.ini"]
    if leftovers:
        other = PROFILES_BY_KEY["other_overwrite"]; target = paths.mods / other.mod_name
        # meta.ini is MO2's receipt for the staging bucket, not runtime output.
        # Check the real destination (not staging backwards like a dingus), then
        # leave that receipt behind while draining everything else.
        _prepare_fw_mod(paths, other); _check_restore_collision(target, staging_root, ("meta.ini",))
        _move_tree(staging_root, target, ("meta.ini",)); moved += len(leftovers)
        manifest.note(other, staging_root, target, "moved unclassified runtime staging files")
        messages.append(f"Moved {len(leftovers)} unclassified file(s) to {target}.")
    _prune_empty_dirs(staging_root)
    return "\n\n".join(messages) if messages else "Runtime staging is empty. Good. It stays bound for the next game run."
def _sync_fw_mods(paths):
    """Keep existing FW output mods in MO2's Output block without creating any."""
    managed_profiles = [item for item in OUTPUT_PROFILES if (paths.mods / item.mod_name).is_dir()]
    for item in managed_profiles:
        meta = paths.mods / item.mod_name / "meta.ini"
        if not meta.exists():
            meta.write_text("[General]\n"
                            f"gameName={paths.game_name}\nmodid=0\nversion=FileWatchdog\nnewestVersion=\n"
                            "category=\"-1,\"\nnexusFileStatus=1\ninstallationFile=\nrepository=\n"
                            "ignoredVersion=\ncomments=FW-owned generated runtime output. File Watchdog keeps this mod last.\n"
                            "notes=Do not put normal mod files here.\nconverted=false\nvalidated=false\ntracked=0\n"
                            "\n[installedFiles]\nsize=1\n", encoding="utf-8")
    if not paths.modlist_path.is_file(): return
    names = {item.mod_name for item in managed_profiles}; all_fw_names = {item.mod_name for item in OUTPUT_PROFILES}
    lines = paths.modlist_path.read_text(encoding="utf-8").splitlines()
    fw_lines = [line for line in lines if line[:1] in "+-" and line[1:].strip() in names]
    present = {line[1:].strip() for line in fw_lines}
    # New FW output mods start enabled. Existing entries keep the user's exact
    # order and enabled state; this manager doesn't get to rearrange their desk.
    fw_lines.extend(f"+{item.mod_name}" for item in OUTPUT_PROFILES
                    if item.mod_name in names and item.mod_name not in present)
    # Empty FW mods get deleted on restore. Their stale modlist entry goes too,
    # otherwise MO2 will keep showing a dead thing like a bad houseplant.
    kept = [line for line in lines if not (line[:1] in "+-" and line[1:].strip() in all_fw_names)]
    # MO2 serializes the visual list backwards. The user's Output block is
    # immediately after End of List in the file, as their profile proves.
    boundary = next((index + 1 for index, line in enumerate(kept)
                     if line[1:].strip() == "End of List_separator"), 0)
    paths.modlist_path.write_text("\n".join(kept[:boundary] + fw_lines + kept[boundary:]) + "\n", encoding="utf-8")
def _refresh_mo2(paths):
    """Make MO2 notice changed routes only after the manager has closed."""
    try:
        paths.organizer.refresh(True)
    except TypeError:
        paths.organizer.refresh()
    except Exception as exc:
        # The file action succeeded; a stale panel isn't reason to lie about it.
        logger.warning("MO2 refresh did not run cleanly: %s", exc)
def _fw_running_processes(paths):
    # Any EXE sitting in the active game root can own this VFS. Don't do
    # file surgery underneath it just because this isn't a Bethesda install.
    names = list(paths.root_executables())
    if not names: return []
    try: listing = subprocess.check_output(["tasklist", "/FO", "CSV", "/NH"], text=True, creationflags=0x08000000).lower()
    except OSError: return []
    return [name for name in names if f'"{name.lower()}"' in listing]
@contextmanager
def _fw_mutation(paths):
    """No folder surgery while the game owns the VFS, or another FW owns us."""
    running = _fw_running_processes(paths)
    if running: raise RuntimeError("FW won't move output while this is running: " + ", ".join(running))
    lock = paths.data_dir / "fw_operation.lock"; lock.parent.mkdir(parents=True, exist_ok=True)
    try: handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError: raise RuntimeError("FW is already moving files, or a previous run left its lock. Check fw_debug.log first.")
    try:
        os.write(handle, str(os.getpid()).encode("ascii")); yield
    finally:
        os.close(handle)
        try: lock.unlink()
        except OSError: pass
def _rootdna_panel(paths, parent):
    """Find the separately shipped RootDNA by its marker, not its folder name."""
    plugin_root = PLUGIN_DIR.parent
    candidates = [plugin_root / "root_dna"]
    try:
        candidates.extend(folder for folder in plugin_root.iterdir()
                          if folder.is_dir() and folder.name.lower() != "root_dna")
    except OSError as exc:
        logger.warning("Couldn't check for optional RootDNA: %s", exc); return None
    matches = []
    for module_dir in candidates:
        marker_path = module_dir / "root_dna.module.json"
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            if marker.get("module") == "RootDNA" and marker.get("api") == 1:
                matches.append((module_dir, marker))
        except (OSError, ValueError, TypeError): pass
    if not matches: return None
    if len(matches) > 1:
        logger.warning("More than one RootDNA install was found; using %s and ignoring the rest.", matches[0][0])
    module_dir, marker = matches[0]
    try:
        entry = (module_dir / marker["entry"]).resolve()
        if entry.parent != module_dir.resolve() or not entry.is_file(): return None
        spec = importlib.util.spec_from_file_location("file_watchdog_rootdna", entry)
        if not spec or not spec.loader: return None
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module.RootDNAPanel(paths, PLUGIN_DIR, parent, lazy=True)
    except Exception as exc:
        logger.warning("RootDNA module was ignored: %s", exc)
        return None
def _managed_parent(manifest, link):
    """Find a live FW route that already owns this child path."""
    for route in manifest.routes.values():
        parent = Path(route.link)
        if parent == link: continue
        try:
            link.relative_to(parent)
            return route
        except ValueError: pass
    return None
def _route_is_current(paths, row):
    profile = PROFILES_BY_KEY.get(row.profile)
    if not profile: return True
    try: route = row.link.relative_to(paths.overwrite).as_posix().lower()
    except ValueError: return False
    return route in {item.lower() for item in profile.routes}
def _absorb_skse_children(paths, manifest, root_target):
    """Whole-SKSE wins. Fold earlier narrow FW outputs into that one home."""
    root_link = paths.overwrite / "SKSE"; children = {}; known = {str(Path(route.target)): route for route in manifest.routes.values()}; descendant_routes = []
    for route in list(manifest.routes.values()):
        child_link = Path(route.link)
        if child_link == root_link: continue
        try: relative = child_link.relative_to(root_link)
        except ValueError: continue
        descendant_routes.append(route)
        child_target = Path(route.target)
        if child_target.is_dir(): children[str(child_target)] = (route, child_link, child_target, root_target / relative)
    # A previous select-all run may have made a real FW mod then overwritten
    # its manifest slot with a second profile using the same Overwrite route.
    # The marker is our receipt, so fold those orphaned FW folders too.
    for profile in OUTPUT_PROFILES:
        if profile.key == "skse_runtime": continue
        for route_text in profile.routes:
            child_link = paths.overwrite / route_text
            try: relative = child_link.relative_to(root_link)
            except ValueError: continue
            child_target = paths.mods / profile.mod_name / route_text; meta = paths.mods / profile.mod_name / "meta.ini"
            try: owned = "FW-owned generated runtime output" in meta.read_text(encoding="utf-8")
            except OSError: owned = False
            if owned and child_target.is_dir():
                route = known.get(str(child_target), ManagedRoute(profile.key, str(child_link), str(child_target)))
                children[str(child_target)] = (route, child_link, child_target, root_target / relative)
    # Check everything before we touch anything. Half a merge is still a mess.
    for _route, _link, child_target, destination in children.values(): _check_restore_collision(destination, child_target)
    notes = []
    removed_links = set()
    for route, child_link, child_target, destination in children.values():
        if child_target.is_dir(): _move_tree(child_target, destination)
        if str(child_link) not in removed_links: manifest.remove(child_link, "folded into whole-SKSE output"); removed_links.add(str(child_link))
        profile = PROFILES_BY_KEY.get(route.profile)
        if profile:
            row = RouteRow(route.profile, profile.title, child_link, child_target, "FW-owned", "folded", True)
            notes.append(_remove_empty_fw_mod(paths, row, manifest).strip())
    # Cull the redundant records too, even when a previous run only created
    # empty folders. The whole route owns all of SKSE now.
    for route in descendant_routes:
        child_link, child_target = Path(route.link), Path(route.target)
        if str(child_link) not in removed_links:
            manifest.remove(child_link, "superseded by whole-SKSE output"); removed_links.add(str(child_link))
        profile = PROFILES_BY_KEY.get(route.profile)
        if profile:
            row = RouteRow(route.profile, profile.title, child_link, child_target, "FW-owned", "superseded", True)
            notes.append(_remove_empty_fw_mod(paths, row, manifest).strip())
    return " ".join(note for note in notes if note)
def _make_route(paths, manifest, row):
    profile = PROFILES_BY_KEY[row.profile]; link, target = row.link, row.target
    if not _route_is_current(paths, row): raise RuntimeError(f"{link} is an old overlapping route. Remove it; don't bring that mess back.")
    parent = _managed_parent(manifest, link)
    if parent: raise RuntimeError(f"{link} is already inside FW route {parent.link}. Whole-route owner gets it; don't make duplicate junk.")
    previous = manifest.routes.get(str(link))
    if previous and previous.profile != profile.key:
        old_target = Path(previous.target)
        if old_target.is_dir(): raise RuntimeError(f"{link} already belongs to {previous.profile}. FW won't play folder tug-of-war.")
        manifest.remove(link, "replaced stale overlapping route")
        old_profile = PROFILES_BY_KEY.get(previous.profile)
        if old_profile:
            old_row = RouteRow(previous.profile, old_profile.title, link, old_target, "FW-owned", "stale", True)
            _remove_empty_fw_mod(paths, old_row, manifest)
    if _has_link_parent(link): raise RuntimeError(f"{link}: a parent junction already owns this route. Hands off.")
    if _is_link(link):
        raise RuntimeError(f"{link}: old junction still exists. Restore it first; MO2 can't hide files behind a junction.")
    _prepare_fw_mod(paths, profile); target.mkdir(parents=True, exist_ok=True)
    if link.exists():
        if _has_nested_link(link): raise RuntimeError(f"{link} contains a nested junction. Restore it first; no mixed plumbing.")
        _check_restore_collision(link, target); _move_tree(link, target); shutil.rmtree(link)
    folded = _absorb_skse_children(paths, manifest, target) if profile.key == "skse_runtime" else ""
    executables = _runtime_executables(profile)
    _set_output_bindings(paths, executables, profile.mod_name)
    manifest.add(profile, link, target, executables)
    routed = f" MO2 now sends {', '.join(executables)} output here." if executables else ""
    return f"Moved out of Overwrite: {link} -> {target}.{routed}" + (f" {folded}" if folded else "")
def _restore(paths, row, manifest):
    if _has_link_parent(row.link): raise RuntimeError(f"{row.link} is covered by a parent junction. Restore the parent route instead.")
    if _is_link(row.link):
        if not _same_target(row.link, row.target): raise RuntimeError(f"{row.link} changed since the scan. Leaving it alone.")
        # Before unlinking, link and target are the same directory, so comparing
        # them would report every file as a fake collision.
        row.link.rmdir()
        if row.link.exists(): raise RuntimeError(f"{row.link} reappeared while restoring. Leaving it alone.")
    elif row.legacy or row.target_type != "FW-owned":
        raise RuntimeError(f"{row.link} is not a verified FW-owned recovery route. Hands off.")
    if row.link.exists() and not row.link.is_dir(): raise RuntimeError(f"{row.link} is a file, not an Overwrite folder. Hands off.")
    row.link.mkdir(parents=True, exist_ok=True)
    skipped = ("meta.ini",) if row.profile == "other_overwrite" else ()
    _check_restore_collision(row.link, row.target, skipped)
    _move_tree(row.target, row.link, skipped)
    route = manifest.routes.get(str(row.link)); profile = PROFILES_BY_KEY.get(row.profile); mod_root = _fw_mod_root(row)
    executables = route.executables if route else (_runtime_executables(profile) if profile else ())
    if executables and mod_root: _clear_output_bindings(paths, executables, mod_root.name)
    if route: manifest.remove(row.link)
    return f"Restored to Overwrite: {row.link}"
def _fw_mod_root(row):
    profile = PROFILES_BY_KEY.get(row.profile)
    if not profile: return None
    if profile.key == "other_overwrite": return row.target
    for route in profile.routes:
        route_parts = Path(route).parts
        if tuple(part.lower() for part in row.target.parts[-len(route_parts):]) == tuple(part.lower() for part in route_parts):
            return row.target.parents[len(route_parts) - 1]
    return None
def _remove_empty_fw_mod(paths, row, manifest):
    """Remove only an empty, marker-owned FW mod after the last route is gone."""
    if row.legacy or any(route.profile == row.profile for route in manifest.routes.values()): return ""
    mod_root = _fw_mod_root(row)
    if mod_root is None: return ""
    meta = mod_root / "meta.ini"
    try:
        if "FW-owned generated runtime output" not in meta.read_text(encoding="utf-8"): return ""
        for folder in sorted((path for path in mod_root.rglob("*") if path.is_dir()), key=lambda path: len(path.parts), reverse=True):
            folder.rmdir()
        meta.unlink(); mod_root.rmdir(); _sync_fw_mods(paths)
        return f" Removed empty FW mod: {mod_root.name}."
    except OSError:
        return " FW mod still has something in it, so it stays put."
def _unlink_and_remove(paths, row, manifest):
    restored = _restore(paths, row, manifest)
    return restored + _remove_empty_fw_mod(paths, row, manifest)
def _file_index(root, limit=500):
    """Exact route preview only. Big cache trees get a sane visible cap."""
    result = {}
    if not root.is_dir(): return result, False
    for base, _dirs, files in os.walk(root):
        base_path = Path(base)
        for name in files:
            item = base_path / name
            try: result[str(item.relative_to(root))] = item.stat().st_size
            except OSError: pass
            if len(result) >= limit: return result, True
    return result, False

class FileWatchdogWindow(QDialog):
    HEADERS = ("", "Tool / profile", "Overwrite route", "Target (full path)", "Target type", "Status")
    def __init__(self, paths, manifest, parent=None):
        super().__init__(parent); self.paths, self.manifest, self.rows, self.changed, self.status_events = paths, manifest, [], False, []; self.setWindowTitle("File Watchdog — Route Manager"); self.resize(1450, 840)
        layout = QVBoxLayout(self); tabs = QTabWidget(self); self.tabs = tabs
        tab_font = QFont(tabs.tabBar().font()); tab_font.setBold(True); tabs.tabBar().setFont(tab_font)
        self.color_scheme = QComboBox(tabs); self.color_scheme.addItems(("MO2 blue", "Neon green", "Ember", "Rainbow")); self.color_scheme.setToolTip("Accent colors only. MO2 keeps its actual theme.")
        tabs.setCornerWidget(self.color_scheme, Qt.Corner.TopRightCorner)
        routes_page = QWidget(self); routes_layout = QVBoxLayout(routes_page); tabs.addTab(routes_page, "File Watchdog"); layout.addWidget(tabs)
        routes_layout.addWidget(QLabel("Known generated output only. Ordinary installed mods are not targets. Ever."))
        body = QHBoxLayout(); left = QVBoxLayout(); right = QVBoxLayout(); body.addLayout(left, 4); body.addLayout(right, 1); routes_layout.addLayout(body)
        self.table = QTableWidget(0, len(self.HEADERS), self); self.table.setHorizontalHeaderLabels(self.HEADERS); self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows); self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader(); header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed); header.resizeSection(0, 42)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents); header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch); header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.select_all = QCheckBox(header); self.select_all.setToolTip("Select all displayed routes"); self.select_all.toggled.connect(self._set_all)
        header.sectionResized.connect(lambda *_: self._position_select_all()); header.geometriesChanged.connect(self._position_select_all); self.table.itemSelectionChanged.connect(self._refresh_preview); left.addWidget(self.table)
        self.preview_note = QLabel("Select a route to inspect its Overwrite and FW-target files side by side.", self); left.addWidget(self.preview_note)
        self.preview = QTableWidget(0, 3, self); self.preview.setHorizontalHeaderLabels(("Route", "Overwrite", "FW target")); self.preview.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preview.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents); self.preview.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch); self.preview.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch); left.addWidget(self.preview)
        buttons = QHBoxLayout()
        for text, handler in (("Link 'em", self.create_selected), ("Unlink", self.remove_selected), ("Scan again", self.refresh), ("Open it", self.open_selected)):
            button = QPushButton(text, self); button.clicked.connect(handler)
            if text == "Link 'em": button.setToolTip("Create the selected routes, then evacuate every remaining Overwrite file into its known output or FW Other Output. Overwrite gets left clean.")
            if text == "Unlink": button.setToolTip("Restore files to Overwrite, clear FW's MO2 output binding, then remove the empty FW-owned mod.")
            if text == "Scan again": button.setToolTip("Recheck Overwrite and known generator routes. Doesn't move a damn thing.")
            if text == "Open it": button.setToolTip("Open the selected FW target folder in Explorer.")
            buttons.addWidget(button)
        mop_up = QPushButton("Clean OW", self); mop_up.setToolTip("After a game run: move known FW output to its real home, then put every leftover file in FileWatchdog - Other Overwrite Output."); mop_up.clicked.connect(self.final_cleanup); buttons.addWidget(mop_up)
        if _is_skyrim(self.paths):
            process = QPushButton("Sort staging", self); process.setToolTip("After the game: move staging files into known FW outputs. Anything FW can't place yet goes to Other Output. The empty staging bucket stays bound."); process.clicked.connect(self.process_runtime_staging); buttons.addWidget(process)
        left.addLayout(buttons)
        right.addWidget(QLabel("Activity log — click a path to open it."))
        self.status = QTextBrowser(self); self.status.setOpenLinks(False); self.status.anchorClicked.connect(self._open_status_link); self.status.setPlaceholderText("Route activity and safety messages show here. No popup nonsense."); right.addWidget(self.status)
        clear_log = QPushButton("Clear log", self); clear_log.clicked.connect(self._clear_status); right.addWidget(clear_log)
        rootdna = _rootdna_panel(paths, tabs)
        if rootdna is not None:
            rootdna_index = tabs.addTab(rootdna, "RootDNA")
            tabs.currentChanged.connect(lambda index: rootdna.activate() if index == rootdna_index else None)
        close = QPushButton("Close", self); close.clicked.connect(self.accept); layout.addWidget(close); self.refresh()
        self.color_scheme.currentTextChanged.connect(self._apply_color_scheme); self.color_scheme.setCurrentText(self._saved_color_scheme()); self._apply_color_scheme(self.color_scheme.currentText())
        self._set_status(f"Using MO2 profile: {self.paths.profile_name}\nNo path guessing. These are the paths MO2 handed over.", (self.paths.config_path, self.paths.profile_dir, self.paths.mods, self.paths.overwrite))
    def _saved_color_scheme(self):
        try:
            data = json.loads(self.paths.ui_settings_path.read_text(encoding="utf-8"))
            if data.get("game_identity") == self.paths.game_identity(): return data.get("scheme", "MO2 blue")
        except (OSError, ValueError, TypeError): pass
        return "MO2 blue"
    def _apply_color_scheme(self, name):
        accents = {
            "MO2 blue": ("#55c7ff", "#a6dfff", "#183c58"),
            "Neon green": ("#62e78f", "#b4ffc7", "#173d28"),
            "Ember": ("#ff9a4d", "#ffd0a8", "#542b15"),
            "Rainbow": ("#ffffff", "#ffffff", "#35215e"),
        }
        accent, text, hover = accents.get(name, accents["MO2 blue"])
        rainbow = "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #ff5c7a, stop:.25 #ffcb4c, stop:.50 #68e788, stop:.75 #56b8ff, stop:1 #c877ff)"
        tab_base = "background-color: #21162b; color: #b5a9c3; border: 2px solid #5c3978; border-top-color: #9b75bd; border-left-color: #8053a6; border-right-color: #321a47; border-bottom-color: #1d0d2a;"
        if name == "Rainbow":
            selected = f"background: {rainbow}; color: #101018; border: 2px solid #ffffff; border-top-color: #ffffff; border-left-color: #ffe7fb; border-right-color: #a76bea; border-bottom-color: #633785;"
        else:
            selected = f"background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 {hover}, stop:0.48 #171d27, stop:1 #0e1118); color: {text}; border: 2px solid {accent}; border-top-color: {text}; border-left-color: {accent}; border-right-color: #241138; border-bottom-color: #160a22;"
        # Static bevel: visible at rest, brightened on hover. Rainbow stays
        # reserved for the active tab so the eye has one obvious landing spot.
        button_base = "background-color: #21162b; color: #eee7f8; border: 2px solid #7e45ad; border-top-color: #d9afff; border-left-color: #bd83ea; border-right-color: #4b236e; border-bottom-color: #2a123e; padding: 3px 8px;"
        hover_fill = f"background: {rainbow}; color: #101018;" if name == "Rainbow" else f"background: {hover}; color: {text};"
        # Accent only. No padding, geometry, or full palette hijack—MO2 has
        # enough opinions already.
        self.setStyleSheet(f"QTabBar::tab {{ {tab_base} }} QTabBar::tab:selected {{ {selected} }} "
                           f"QPushButton {{ {button_base} }} QPushButton:hover {{ {hover_fill} }} "
                           "QPushButton:pressed { background-color: #100a16; border-top-color: #2a123e; border-left-color: #4b236e; border-right-color: #d9afff; border-bottom-color: #bd83ea; } "
                           f"QCheckBox::indicator:checked {{ background: {accent}; border: 1px solid {text}; }}")
        try:
            self.paths.ui_settings_path.parent.mkdir(parents=True, exist_ok=True)
            self.paths.ui_settings_path.write_text(json.dumps({"game_identity": self.paths.game_identity(), "scheme": name}, indent=2), encoding="utf-8")
        except OSError as exc: logger.warning("Couldn't save FW color scheme: %s", exc)
    def refresh(self):
        self.select_all.blockSignals(True); self.select_all.setChecked(False); self.select_all.blockSignals(False)
        self.rows = _build_rows(self.paths, self.manifest); self.table.setRowCount(len(self.rows))
        for index, row in enumerate(self.rows):
            box = QCheckBox(self.table); box.toggled.connect(lambda _checked, row=index: self._refresh_preview()); self.table.setCellWidget(index, 0, box)
            for column, value in enumerate((row.title, str(row.link), str(row.target), row.target_type, row.status), 1): self.table.setItem(index, column, QTableWidgetItem(value))
        self.table.resizeRowsToContents()
        self._position_select_all()
        self._refresh_preview()
    def _position_select_all(self):
        header = self.table.horizontalHeader(); width, height = self.select_all.sizeHint().width(), self.select_all.sizeHint().height()
        x = header.sectionViewportPosition(0) + max(0, (header.sectionSize(0) - width) // 2)
        self.select_all.setGeometry(x, max(0, (header.height() - height) // 2), width, height)
    def _set_all(self, checked):
        for index in range(self.table.rowCount()):
            box = self.table.cellWidget(index, 0)
            if box: box.blockSignals(True); box.setChecked(checked); box.blockSignals(False)
        self._refresh_preview()
    def selected(self): return [row for index, row in enumerate(self.rows) if (box := self.table.cellWidget(index, 0)) and box.isChecked()]
    def _preview_rows(self):
        checked = self.selected()
        if checked: return checked
        return [self.rows[index.row()] for index in self.table.selectionModel().selectedRows()] if self.table.selectionModel() else []
    def _preview_add(self, route, source, target):
        index = self.preview.rowCount(); self.preview.insertRow(index)
        for column, value in enumerate((route, source, target)): self.preview.setItem(index, column, QTableWidgetItem(value))
    def _refresh_preview(self):
        if not hasattr(self, "preview"): return
        rows = self._preview_rows(); self.preview.setRowCount(0)
        if not rows:
            self.preview_note.setText("Select a route to inspect its Overwrite and FW-target files side by side."); return
        legacy_warning = " Legacy normal-mod route selected: restore is explicit and collision-checked." if any(row.legacy and row.target_type == "legacy normal mod" for row in rows) else ""
        self.preview_note.setText("Exact route preview. Files are shown by relative path and size; big caches stop at 500 entries." + legacy_warning)
        for row in rows:
            source, source_capped = _file_index(row.link); target, target_capped = _file_index(row.target)
            for name in sorted(set(source) | set(target), key=str.lower):
                self._preview_add(row.title, f"{name} ({source[name]} bytes)" if name in source else "—", f"{name} ({target[name]} bytes)" if name in target else "—")
            if source_capped or target_capped: self._preview_add(row.title, "Preview capped", "Preview capped; the route still moves as one unit.")
            if not source and not target: self._preview_add(row.title, "(empty)", "(empty)")
        self.preview.resizeRowsToContents()
    def _run(self, action, warning=None):
        selected = self.selected()
        if not selected: self._set_status("Pick at least one route. The checkbox is there for a reason."); return
        messages = []
        try:
            with _fw_mutation(self.paths):
                for row in selected:
                    try: messages.append(action(row)); self.changed = True
                    except Exception as exc: logger.exception("Route action failed: %s", row.link); messages.append(f"Skipped {row.link}: {exc}")
        except Exception as exc: messages.append(f"Didn't touch anything: {exc}")
        self.refresh(); self._set_status("\n\n".join(messages), [path for row in selected for path in (row.link, row.target)])
    def _set_status(self, message, paths=()):
        # This is a status panel, not a scrapbook. Latest action wins.
        links = tuple(dict.fromkeys(str(path) for path in paths)); self.status_events = [(message, links)]
        link_html = "".join(f'<br><a href="{html.escape(QUrl.fromLocalFile(path).toString(), quote=True)}">Open: {html.escape(path)}</a>' for path in links)
        self.status.setHtml(f"<p>{html.escape(message).replace(chr(10), '<br>')}{link_html}</p>")
        self.status.verticalScrollBar().setValue(self.status.verticalScrollBar().maximum())
    def _clear_status(self):
        self.status_events.clear(); self.status.clear()
    def _open_status_link(self, url):
        if url.isLocalFile(): QDesktopServices.openUrl(url)
    def create_selected(self):
        selected = self.selected()
        if not selected: self._set_status("Pick at least one route. The checkbox is there for a reason."); return
        messages = []
        try:
            with _fw_mutation(self.paths):
                for row in selected:
                    try:
                        if row.legacy: raise RuntimeError("Legacy routes are recovery-only.")
                        messages.append(_make_route(self.paths, self.manifest, row)); self.changed = True
                    except Exception as exc:
                        logger.exception("Route action failed: %s", row.link); messages.append(f"Skipped {row.link}: {exc}")
                # A half-clean Overwrite is how we got here. Once FW is asked to route
                # output, it finishes the whole cleanup pass and preserves every path.
                messages.append(_final_cleanup(self.paths, self.manifest)); self.changed = True
        except Exception as exc:
            logger.exception("Post-create Overwrite cleanup failed"); messages.append(f"FW stopped: {exc}")
        self.refresh(); self._set_status("\n\n".join(messages), [path for row in selected for path in (row.link, row.target)] + [self.paths.overwrite])
    def remove_selected(self):
        selected = self.selected()
        if not selected: self._set_status("Pick at least one route. The checkbox is there for a reason."); return
        self._run(lambda row: _unlink_and_remove(self.paths, row, self.manifest))
    def final_cleanup(self):
        try:
            with _fw_mutation(self.paths): message = _final_cleanup(self.paths, self.manifest); self.changed = True
            self.refresh(); self._set_status(message, (self.paths.overwrite, self.paths.mods / PROFILES_BY_KEY["other_overwrite"].mod_name))
        except Exception as exc:
            logger.exception("Final Overwrite cleanup failed"); self._set_status(f"Couldn't clean current Overwrite files: {exc}", (self.paths.overwrite,))
    def process_runtime_staging(self):
        try:
            with _fw_mutation(self.paths): message = _process_runtime_staging(self.paths, self.manifest); self.changed = True
            self.refresh()
            staging = self.paths.mods / PROFILES_BY_KEY["skse_runtime"].mod_name
            self._set_status(message, (staging, self.paths.overwrite))
        except Exception as exc:
            logger.exception("Runtime staging cleanup failed"); self._set_status(f"Couldn't process runtime staging: {exc}", (self.paths.mods / PROFILES_BY_KEY["skse_runtime"].mod_name,))
    def open_selected(self):
        selected = self.selected()
        if len(selected) != 1: self._set_status("Select one target to open. One door at a time."); return
        target = selected[0].target
        self._set_status(f"Opening target: {target}" if QDesktopServices.openUrl(QUrl.fromLocalFile(str(target))) else f"Couldn't open target: {target}", (target,))

class FileWatchdogPlugin(mobase.IPluginTool):
    def __init__(self): super().__init__(); self._organizer: Optional[mobase.IOrganizer] = None
    def init(self, organizer): self._organizer = organizer; logger.info("File Watchdog booted. No generic-folder shenanigans today."); return True
    def name(self): return "FileWatchdog"
    def author(self): return "SkyGen"
    def description(self): return "Routes known generated output into dedicated File Watchdog mods."
    def version(self): return mobase.VersionInfo("4.1.0", mobase.VersionScheme.DISCOVER)
    def isActive(self): return True
    def settings(self): return []
    def displayName(self): return "File Watchdog"
    def tooltip(self): return "Inspect, create, recover, or unlink narrow FW output routes"
    def icon(self): return QIcon()
    def group(self): return mobase.PluginSettingGroup.Tools
    def display(self):
        try:
            if self._organizer is None: raise RuntimeError("MO2 handed FW an empty leash somehow.")
            paths = OrganizerPaths(self._organizer); manifest = RouteManifest(paths.manifest_path, paths.game_identity())
            if manifest.conflict: raise RuntimeError(manifest.conflict)
            _migrate_runtime_staging_name(paths, manifest); _retire_obsolete_routes(paths, manifest); _prune_empty_fw_mods(paths, manifest); _sync_fw_mods(paths)
            window = FileWatchdogWindow(paths, manifest); window.exec()
            if window.changed: _refresh_mo2(paths)
        except Exception as exc:
            logger.exception("FW window failed: %s", exc)
def createPlugin(): return FileWatchdogPlugin()
