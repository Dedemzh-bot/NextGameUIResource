"""Preview PNG resource routes from the prefix before the first underscore."""
import argparse
import collections
import datetime
import json
from pathlib import Path
from config import ensure_file_outside, load_settings, select_path

PREFIX_ROUTES = {"gui": "atlas", "icon": "atlas", "pic": "standalone_texture", "por": "standalone_texture"}
PREFIX_DETAILS = {
    "gui": {"branch": "图片", "engine_root": "/Game/UI/UI", "register_icon": False},
    "icon": {"branch": "图标", "engine_root": "/Game/UI/ICON", "register_icon": True},
    "pic": {"branch": "图片", "engine_root": "/Game/UI/Textures", "register_icon": False},
    "por": {"branch": "图标", "engine_root": "/Game/UI/Portrait", "register_icon": True},
}


def classify_directory(source):
    source = Path(source).expanduser().resolve()
    if not source.is_dir():
        raise ValueError("Source directory does not exist")
    groups = {"atlas": [], "standalone_texture": [], "unmatched": [], "other_files": []}
    prefix_counts = collections.Counter()
    for path in sorted(source.rglob("*"), key=lambda p: p.relative_to(source).as_posix()):
        if not path.is_file():
            continue
        prefix, separator, _ = path.stem.partition("_")
        if path.suffix.lower() != ".png":
            category = "other_files"
        elif not separator:
            category = "unmatched"
        else:
            category = PREFIX_ROUTES.get(prefix, "unmatched")
        if path.suffix.lower() == ".png":
            prefix_counts[prefix if separator else "(no underscore)"] += 1
        row = {"file": path.relative_to(source).as_posix(),
            "prefix": prefix if separator else None, "bytes": path.stat().st_size}
        if category in ("atlas", "standalone_texture"):
            row.update(PREFIX_DETAILS[prefix])
        groups[category].append(row)
    return {
        "observed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source_directory": str(source),
        "operation": "classification_preview",
        "rule": {"field": "filename stem before first underscore", "prefix_routes": PREFIX_ROUTES,
            "prefix_details": PREFIX_DETAILS,
            "matching": "exact lowercase prefix", "image_format": "PNG"},
        "counts": {category: len(rows) for category, rows in groups.items()},
        "prefix_counts": dict(sorted(prefix_counts.items())),
        "total_files": sum(len(rows) for rows in groups.values()),
        "groups": groups,
        "source_files_modified": False,
        "atlas_created": False,
        "engine_assets_imported": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, nargs="?")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    try:
        settings = load_settings(args.env_file)
        source = select_path(args.source, settings.source_dir, "NEXTGAME_UI_SOURCE_DIR", "dir")
        output = ensure_file_outside(args.output or settings.work_dir / "classification.json", [source])
        report = classify_directory(source)
    except ValueError as exc:
        parser.error(str(exc))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"counts": report["counts"], "prefix_counts": report["prefix_counts"],
        "total_files": report["total_files"], "report": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
