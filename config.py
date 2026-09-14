"""Local, portable path configuration. Values never leave this process implicitly."""
from dataclasses import dataclass
import os
from pathlib import Path
import re

TOOL_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    source_dir: Path | None
    output_dir: Path | None
    project_root: Path | None
    table: Path | None
    work_dir: Path
    texturepacker: Path | None
    tps_template: Path
    system_map: Path | None
    id_catalog: Path | None
    id_assignments: Path | None
    editor_exe: Path | None
    nxue_cli: Path | None


def _read_env(path):
    """Read literal KEY=value lines; no shell execution or variable interpolation."""
    values = {}
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid .env assignment at line {number}")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f"Unclosed .env quote at line {number}")
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        values[key] = value
    return values


def load_settings(env_file=None):
    """Load .env then environment. Relative .env paths use its parent directory."""
    env_path = Path(env_file).expanduser().resolve() if env_file is not None else TOOL_ROOT / ".env"
    if env_file is not None and not env_path.is_file():
        raise ValueError("Explicit --env-file does not exist or is not a file")
    local = _read_env(env_path) if env_path.is_file() else {}

    def value(name):
        raw = os.environ.get(name, local.get(name, "")).strip()
        if not raw:
            return None
        result = Path(raw).expanduser()
        if not result.is_absolute():
            result = (Path.cwd() if name in os.environ else env_path.parent) / result
        return result.resolve()

    project = value("NEXTGAME_PROJECT_ROOT")
    return Settings(
        source_dir=value("NEXTGAME_UI_SOURCE_DIR"),
        output_dir=value("NEXTGAME_UI_OUTPUT_DIR"),
        project_root=project,
        table=value("NEXTGAME_UI_RESOURCE_TABLE") or (project / "Content/Settings/resource/resource.txt" if project else None),
        work_dir=value("NEXTGAME_UI_WORK_DIR") or TOOL_ROOT / ".local/work",
        texturepacker=value("NEXTGAME_TEXTUREPACKER_EXE"),
        tps_template=value("NEXTGAME_UI_TPS_TEMPLATE") or TOOL_ROOT / "templates/default.tps",
        system_map=value("NEXTGAME_UI_SYSTEM_MAP"),
        id_catalog=value("NEXTGAME_UI_ID_CATALOG"),
        id_assignments=value("NEXTGAME_UI_ID_ASSIGNMENTS"),
        editor_exe=value("NEXTGAME_UNREAL_EDITOR_EXE"),
        nxue_cli=value("NEXTGAME_NXUE_CLI"),
    )


def select_path(explicit, configured, env_name, kind=None):
    """CLI takes precedence; require existing file/directory only when requested."""
    selected = explicit if explicit is not None else configured
    if selected is None:
        raise ValueError(f"Configure {env_name} or supply its command-line path")
    result = Path(selected).expanduser().resolve()
    if kind == "file" and not result.is_file():
        raise ValueError(f"{env_name} must point to an existing file")
    if kind == "dir" and not result.is_dir():
        raise ValueError(f"{env_name} must point to an existing directory")
    return result


def ensure_disjoint(first, second, first_label="Source", second_label="Output"):
    """Reject equal paths and ancestor/descendant overlap, including symlinks."""
    first, second = Path(first).resolve(), Path(second).resolve()
    if first.is_relative_to(second) or second.is_relative_to(first):
        raise ValueError(f"{first_label} and {second_label} directories must not overlap")


def ensure_file_outside(path, roots):
    result = Path(path).resolve()
    for root in roots:
        root = Path(root).resolve()
        if result.is_relative_to(root) or root.is_relative_to(result):
            raise ValueError("Report/manifest must be outside source, stage and destination directories")
    if result.exists() and not result.is_file():
        raise ValueError("Report/manifest path is not a file")
    return result
