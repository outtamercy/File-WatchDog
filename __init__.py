from __future__ import annotations

import mobase
from PyQt6.QtWidgets import QMessageBox
from PyQt6.QtGui import QIcon
from pathlib import Path
from typing import List, Dict, Tuple
import subprocess
import shutil
import logging
import sys
import traceback
import configparser
import ctypes

# --- Debug logging: writes to plugin folder so we can see what MO2 did ---
_plugin_dir = Path(__file__).parent
log_file = _plugin_dir / "fw_debug.log"
try:
    handler = logging.FileHandler(str(log_file), mode="w", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
except Exception as e:
    pass

logger = logging.getLogger("FileWatchdog")
logger.info("=== FileWatchdog __init__.py loaded ===")
logger.info(f"Plugin dir: {_plugin_dir}")
logger.info(f"Python version: {sys.version}")

# ------------------------------------------------------------------
# Organizer Wrapper (portable-only, INI-based)
# ------------------------------------------------------------------
class OrganizerWrapper:
    def __init__(self, organizer: mobase.IOrganizer):
        self._mo2_root = Path(organizer.basePath()).resolve()
        self._mo2_ini = self._mo2_root / "ModOrganizer.ini"
        if not self._mo2_ini.is_file():
            raise RuntimeError(f"ModOrganizer.ini not found: {self._mo2_ini}")
        self._parse_ini()

    def _decode_byte_array(self, value: str) -> str:
        value = value.strip()
        if value.startswith('@ByteArray(') and value.endswith(')'):
            value = value[11:-1]
        value = value.replace('\\\\', '\\')
        return value.strip('"').strip("'")

    def _parse_ini(self) -> None:
        config = configparser.ConfigParser()
        config.read(self._mo2_ini, encoding='utf-8')
        if 'General' not in config:
            raise RuntimeError("Missing [General] section")

        # INI first — what MO2 saved on last clean exit
        try:
            ini_profile = self._decode_byte_array(
                config.get('General', 'selected_profile', fallback='Default')
            ).strip()
            if not ini_profile:
                ini_profile = 'Default'
        except Exception:
            ini_profile = 'Default'

        self._profile_name = ini_profile
        self._profile_dir = self._mo2_root / "profiles" / self._profile_name

        # INI pointing at a ghost? Flip to live API which knows the real selection
        if not self._profile_dir.exists():
            logger.warning(
                f"INI profile '{self._profile_name}' missing on disk. "
                "Using MO2 API for live profile."
            )
            try:
                self._profile_name = self._organizer.profileName()
                self._profile_dir = Path(self._organizer.profilePath())
                logger.info(f"Live API profile: {self._profile_name}")
            except Exception as e:
                logger.error(f"API fallback also failed: {e}")
                # Leave the INI value and let the exists check below blow up

        if not self._profile_dir.exists():
            raise RuntimeError(f"Profile not found: {self._profile_dir}")

        try:
            game_raw = self._decode_byte_array(config.get('General', 'gamePath'))
        except configparser.NoOptionError:
            raise RuntimeError("gamePath missing")

        game_path = Path(game_raw)
        if not game_path.is_absolute():
            game_path = (self._mo2_root / game_raw).resolve()
        else:
            game_path = game_path.resolve()
        self._game_path = game_path
        self._game_data_path = (game_path / "Data").resolve()

        mods_raw = self._decode_byte_array(
            config.get('General', 'modsPath', fallback='./mods')
        )
        if not mods_raw:
            mods_raw = './mods'
        mods_path = Path(mods_raw)
        if not mods_path.is_absolute():
            mods_path = (self._mo2_root / mods_raw).resolve()
        else:
            mods_path = mods_path.resolve()
        self._mods_path = mods_path

        ow_raw = self._decode_byte_array(
            config.get('General', 'overwritePath', fallback='overwrite')
        )
        if not ow_raw:
            ow_raw = 'overwrite'
        ow_path = Path(ow_raw)
        if not ow_path.is_absolute():
            ow_path = (self._mo2_root / ow_raw).resolve()
        else:
            ow_path = ow_path.resolve()
        self._overwrite_path = ow_path

    @property
    def mods_path(self) -> Path:
        return self._mods_path

    @property
    def overwrite_path(self) -> Path:
        return self._overwrite_path

    def read_modlist_txt(self) -> Tuple[List[str], List[str]]:
        modlist_path = self._profile_dir / "modlist.txt"
        if not modlist_path.is_file():
            return [], []
        lines = modlist_path.read_text(encoding="utf-8").splitlines()
        active = [ln[1:].strip() for ln in lines if ln.startswith('+')]
        inactive = [ln[1:].strip() for ln in lines if ln.startswith('-')]
        return active, inactive

# ------------------------------------------------------------------
# Scanner
# ------------------------------------------------------------------
# Folders that games / SKSE plugins commonly write into.
# Root Builder's "Root" workspace is EXCLUDED — RB manages its own house.
HOT_FOLDERS: List[str] = [
    "SKSE/Plugins/CommunityShaders",
    "UnifiedWaterCache",
    "ShaderCache",
    "CellOffsets",
    "SSEEdit Cache",
    "enbseries",
    "textures",
    "meshes",
    "interface",
    "MCM",
    "backup",
    "Plugins",
    "SKSE/Plugins",
]

# Never touch these — other tools own them
FORBIDDEN_NAMES: set = {"Root", "root"}


def scan_active_mods(mods_path: Path, active_mods: List[str]) -> Dict[str, Path]:
    redirects: Dict[str, Path] = {}

    for mod_name in reversed(active_mods):
        # Skip dedicated Root Builder mod folders
        if mod_name in FORBIDDEN_NAMES:
            continue

        mod_root = mods_path / mod_name
        if not mod_root.is_dir():
            continue

        for rel in HOT_FOLDERS:
            posix = rel.replace("/", "\\")
            candidate = mod_root / posix
            if candidate.is_dir():
                if posix not in redirects:
                    redirects[posix] = candidate.resolve()

    return redirects


def apply_redirects(overwrite_path: Path, redirects: Dict[str, Path]) -> Tuple[int, int]:
    created = 0
    skipped = 0

    for rel_path, mod_folder in redirects.items():
        junction_point = overwrite_path / rel_path

        # Guard: never create junctions inside Root Builder's workspace
        blocked = False
        for parent in junction_point.parents:
            if parent.name in FORBIDDEN_NAMES:
                logger.warning(f"Skipping {rel_path} — inside Root Builder workspace")
                skipped += 1
                blocked = True
                break
        
        if blocked:
            continue

        result = make_junction(junction_point, mod_folder)
        if result == "created":
            created += 1
        elif result == "skipped":
            skipped += 1

    return created, skipped

# ------------------------------------------------------------------
# Junction Engine
# ------------------------------------------------------------------
def is_junction(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        return path.is_junction() or path.is_symlink()
    except Exception:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs == -1:
            return False
        return bool(attrs & 0x0400)

def is_inside_junction(path: Path) -> bool:
    """True if this path lives inside a junctioned directory."""
    for parent in path.parents:
        if is_junction(parent):
            return True
    return False

def make_junction(link_path: Path, target_path: Path) -> str:
    link_path = Path(link_path)
    target_path = Path(target_path).resolve()

    # CRITICAL FIX: If a parent is already junctioned, this path is already covered.
    # Creating a nested junction here would destroy data by backing up the target.
    if is_inside_junction(link_path):
        logger.info(f"Skipping {link_path} — parent already junctioned")
        return "skipped"

    if is_junction(link_path):
        return "skipped"

    if not target_path.exists():
        target_path.mkdir(parents=True, exist_ok=True)

    if link_path.exists():
        backup = Path(str(link_path) + ".bak")
        if backup.exists():
            shutil.rmtree(backup)
        try:
            link_path.rename(backup)
            logger.info(f"Backed up: {link_path} -> {backup}")
        except Exception as e:
            logger.error(f"Backup failed: {e}")
            return "failed"

    if link_path.exists():
        try:
            link_path.rmdir()
        except Exception:
            pass

    try:
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link_path), str(target_path)],
            capture_output=True,
            text=True,
            check=True,
        )
        logger.info(f"Junctioned: {link_path} -> {target_path}")
        return "created"
    except Exception as e:
        logger.error(f"mklink failed: {e}")
        return "failed"

def apply_redirects(overwrite_path: Path, redirects: Dict[str, Path]) -> Tuple[int, int]:
    created = 0
    skipped = 0
    for rel_path, mod_folder in redirects.items():
        junction_point = overwrite_path / rel_path
        result = make_junction(junction_point, mod_folder)
        if result == "created":
            created += 1
        elif result == "skipped":
            skipped += 1
    return created, skipped

# ------------------------------------------------------------------
# Plugin Class
# ------------------------------------------------------------------
class FileWatchdogPlugin(mobase.IPluginTool):
    def __init__(self):
        super().__init__()
        self._organizer = None

    def init(self, organizer: mobase.IOrganizer) -> bool:
        self._organizer = organizer
        return True

    def name(self) -> str:
        return "FileWatchdog"

    def author(self) -> str:
        return "SkyGen"

    def description(self) -> str:
        return "Redirects overwrite writes to active mod folders via junctions"

    def version(self) -> mobase.VersionInfo:
        return mobase.VersionInfo("1.0.0", mobase.VersionScheme.DISCOVER)

    def isActive(self) -> bool:
        return True

    def settings(self) -> List[mobase.PluginSetting]:
        return []

    def displayName(self) -> str:
        return "File Watchdog"

    def tooltip(self) -> str:
        return "Pre-junction overwrite folders before launching"

    def icon(self) -> QIcon:
        return QIcon()

    def group(self) -> int:
        return mobase.PluginSettingGroup.Tools

    def display(self):
        try:
            self._run_sweep()
        except Exception as e:
            logger.exception("Sweep failed")
            QMessageBox.critical(None, "File Watchdog", f"Error: {e}")

    def _run_sweep(self):
        ow = OrganizerWrapper(self._organizer)
        active_mods, _ = ow.read_modlist_txt()

        redirects = scan_active_mods(ow.mods_path, active_mods)
        created, skipped = apply_redirects(ow.overwrite_path, redirects)

        total = len(redirects)
        status_msg = (
            f"Alright, I dug through {len(active_mods)} active mods and found {total} "
            f"folders that games love to dump crap into.\n\n"
        )

        if created > 0 and skipped > 0:
            status_msg += (
                f"Hooked up {created} new redirects and left {skipped} alone "
                f"(they were already sorted from last time).\n\n"
            )
        elif created > 0:
            status_msg += f"Freshly hooked up {created} new redirects for you.\n\n"
        elif skipped > 0 and total > 0:
            status_msg += (
                f"Everything's already tidy — {skipped} redirects were already in place. "
                f"Nothing to do but drink coffee.\n\n"
            )
        else:
            status_msg += "Didn't find any hot folders worth redirecting. Your mods are surprisingly well-behaved.\n\n"

        status_msg += "Your overwrite folder is clean. Go break some stuff."

        QMessageBox.information(None, "File Watchdog", status_msg)

# ------------------------------------------------------------------
# Factory
# ------------------------------------------------------------------
def createPlugin() -> mobase.IPlugin:
    logger.info("createPlugin called")
    try:
        return FileWatchdogPlugin()
    except Exception:
        logger.exception("createPlugin failed")
        raise