"""Verify every source pixel and seal a manifest for publication and Unreal import."""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import xml.etree.ElementTree as ET
from PIL import Image
from classify_resources import PREFIX_DETAILS, PREFIX_ROUTES
from config import ensure_disjoint, ensure_file_outside, load_settings
from prepare_batch import png_size, sha


def require(condition, message):
    if not condition:
        raise ValueError(message)


def inside(root, relative):
    require(isinstance(relative, str) and relative and "\\" not in relative and ":" not in relative,
            "Manifest contains an invalid relative path")
    path = PurePosixPath(relative)
    require(not path.is_absolute() and ".." not in path.parts, "Manifest path escapes its directory")
    resolved = (root / relative).resolve()
    require(resolved.is_relative_to(root) and resolved != root, "Manifest path escapes its directory")
    return resolved


def _seal(manifest_path, data):
    # Keep the previous manifest intact if writing fails partway through.
    descriptor, temporary = tempfile.mkstemp(prefix=".verified-", suffix=".json", dir=manifest_path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        os.replace(temporary, manifest_path)
    finally:
        temporary.unlink(missing_ok=True)


def verify_packed_batch(manifest_path):
    """Verify using explicit exceptions (also under python -O); return sealed data."""
    manifest_path = Path(manifest_path).expanduser().resolve()
    data = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    source, stage, destination = (Path(data[key]).resolve() for key in ("source", "stage", "destination"))
    ensure_disjoint(source, stage, "Source", "Stage")
    ensure_disjoint(source, destination, "Source", "Destination")
    ensure_disjoint(stage, destination, "Stage", "Destination")
    ensure_file_outside(manifest_path, [source, stage, destination])
    require(source.is_dir() and stage.is_dir(), "Source and stage must be existing directories")
    require(isinstance(data.get("groups"), list) and data["groups"], "Manifest has no resource groups")
    expected, expected_files, original_files, expected_directories = set(), set(), set(), set()
    group_keys = set()
    for group in data["groups"]:
        require(group.get("kind") in ("atlas", "standalone_texture"), "Unknown manifest resource kind")
        require(isinstance(group.get("system"), str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", group["system"]),
                "Invalid manifest system")
        group_key = (group["branch"], group["kind"], group["system"].casefold())
        require(group_key not in group_keys, "Duplicate resource group")
        group_keys.add(group_key)
        require(isinstance(group.get("files"), list) and group["files"], "Resource group is empty")
        for item in group["files"]:
            original = Path(item["original"]).resolve()
            require(original.is_relative_to(source) and original != source, "Original file is outside source")
            require(original not in original_files, "Original file occurs more than once")
            original_files.add(original)
            name = item["name"]
            require(original.name == name and original.suffix.lower() == ".png", "Resource filename changed")
            require(re.fullmatch(r"[A-Za-z0-9_]+", original.stem), "Invalid resource filename")
            prefix = original.stem.partition("_")[0]
            require(prefix in PREFIX_ROUTES and PREFIX_ROUTES[prefix] == group["kind"], "Resource route changed")
            route = PREFIX_DETAILS[prefix]
            require(group["branch"] == route["branch"] and group["register_icon"] == route["register_icon"],
                    "Resource branch changed")
            require(group["destination"] == route["engine_root"] + "/" + group["system"], "Engine destination changed")
            raw_dir = Path(group["branch"]) / ("图集/资源" if group["kind"] == "atlas" else "单图") / group["system"]
            expected_directories.add(raw_dir.as_posix())
            require(item["relative"] == (raw_dir / name).as_posix(), "Staged resource path changed")
            staged_file = inside(stage, item["relative"])
            require(Path(item["source"]).resolve() == inside(destination, item["relative"]), "Published resource path changed")
            require(sha(original) == item["sha256"] == sha(staged_file), "Source or staged bytes changed: " + name)
            require(png_size(original) == item["dimensions"], "Source dimensions changed: " + name)
            with Image.open(original) as original_image:
                original_image.load()
                require(list(original_image.size) == item["dimensions"], "Invalid source image dimensions")
            expected_files.add(item["relative"])
        if group["kind"] == "atlas":
            out = Path(group["branch"]) / "图集/图集" / group["system"]
            expected_directories.add(out.as_posix())
            for key, extension in (("project_relative", ".tps"), ("descriptor_relative", ".paper2dsprites"), ("image_relative", ".png")):
                require(group[key] == (out / (group["system"] + extension)).as_posix(), "Packed output path changed")
                expected_files.add(group[key])
            descriptor = json.loads(inside(stage, group["descriptor_relative"]).read_text(encoding="utf-8-sig"))
            frames = descriptor["frames"]
            require(isinstance(frames, dict) and set(frames) == {f["name"] for f in group["files"]}, "Packed frame set differs from source")
            group["dimensions"] = png_size(inside(stage, group["image_relative"]))
            require(descriptor["meta"]["size"] == dict(zip(("w", "h"), group["dimensions"])), "Atlas dimensions differ from descriptor")
            require(descriptor["meta"]["image"] == group["system"] + ".png", "Descriptor refers to an unexpected image")
            with Image.open(inside(stage, group["image_relative"])) as packed_image:
                packed = packed_image.convert("RGBA")
                for item in group["files"]:
                    frame = frames[item["name"]]
                    require(frame["rotated"] is False and frame["trimmed"] is False, "Rotation or trimming is forbidden")
                    rect = frame["frame"]
                    require(all(type(rect.get(key)) is int for key in ("x", "y", "w", "h")), "Invalid packed frame rectangle")
                    require([rect["w"], rect["h"]] == item["dimensions"], "Packed frame size differs from source")
                    require(rect["x"] >= 0 and rect["y"] >= 0 and rect["x"] + rect["w"] <= packed.width and rect["y"] + rect["h"] <= packed.height,
                            "Packed frame is outside the atlas")
                    crop = packed.crop((rect["x"], rect["y"], rect["x"] + rect["w"], rect["y"] + rect["h"]))
                    with Image.open(item["original"]) as original:
                        require(original.convert("RGBA").tobytes() == crop.tobytes(), "Packed source pixels differ: " + item["name"])
                    item["frame"] = frame
                    asset_name = item["name"].replace(".", "_")
                    item["asset"] = group["destination"] + "/Frames/" + asset_name + "." + asset_name
            for key, subdir in (("sheet_asset", ""), ("texture_asset", "/Textures")):
                group[key] = group["destination"] + subdir + "/" + group["system"] + "." + group["system"]
                expected.add(group[key])
            project = inside(stage, group["project_relative"])
            for node in ET.parse(project).iter():
                if (node.tag == "filename" or node.attrib.get("type") == "filename") and node.text:
                    path = (project.parent / node.text).resolve()
                    require(path.is_relative_to(stage) and path.exists(), "TPS contains an invalid external or missing path")
        else:
            for item in group["files"]:
                name = Path(item["name"]).stem
                item["asset"] = group["destination"] + "/" + name + "." + name
        expected.update(item["asset"] for item in group["files"])
    asset_count = sum(len(g["files"]) + (2 if g["kind"] == "atlas" else 0) for g in data["groups"])
    require(len({asset.casefold() for asset in expected}) == asset_count, "Duplicate Unreal asset path")
    require(set(data["directories"]) == expected_directories and len(data["directories"]) == len(expected_directories),
            "Published directory list differs from resource groups")
    require({p.resolve() for p in source.rglob("*") if p.is_file()} == original_files,
            "Source files were added or omitted from the manifest")
    actual_files = {p.relative_to(stage).as_posix() for p in stage.rglob("*") if p.is_file()}
    require(actual_files == expected_files, "Stage has unexpected or missing files; publication was not sealed")
    data["expected_assets"] = sorted(expected)
    data["publish_files"] = [{"relative": relative, "sha256": sha(inside(stage, relative)), "bytes": inside(stage, relative).stat().st_size}
                             for relative in sorted(actual_files)]
    data["packed_verified"] = True
    data["all_source_pixels_preserved"] = True
    _seal(manifest_path, data)
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, nargs="?")
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    try:
        data = verify_packed_batch(args.manifest or load_settings(args.env_file).work_dir / "manifest.json")
    except (ValueError, OSError, KeyError, TypeError, ET.ParseError) as exc:
        parser.error(str(exc))
    print(json.dumps({"expected_assets": len(data["expected_assets"]), "published_files": len(data["publish_files"]),
        "atlases": [{"system": g["system"], "size": g["dimensions"], "frames": len(g["files"])} for g in data["groups"] if g["kind"] == "atlas"],
        "all_source_pixels_preserved": True}))


if __name__ == "__main__":
    main()
