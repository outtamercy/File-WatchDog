from pathlib import Path
from typing import Dict, List

# Sort by depth (deepest first) so nested paths get junctioned before parents
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
    "SKSE/Plugins",  # parent paths go last
]

def scan_active_mods(mods_path: Path, active_mods: List[str]) -> Dict[str, Path]:
    redirects: Dict[str, Path] = {}

    # Iterate backwards so highest-priority mods (lower in modlist) win
    for mod_name in reversed(active_mods):
        mod_root = mods_path / mod_name
        if not mod_root.is_dir():
            continue

        for rel in HOT_FOLDERS:
            posix = rel.replace("/", "\\")
            candidate = mod_root / posix
            if candidate.is_dir():
                # Only map if a higher-priority mod hasn't claimed it yet
                if posix not in redirects:
                    redirects[posix] = candidate.resolve()

    return redirects