"""Pure-stdlib checks binding every Unreal import input to a verified batch."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import struct

ROUTES = {
    "gui": ("atlas", "图片", "/Game/UI/UI", False),
    "icon": ("atlas", "图标", "/Game/UI/ICON", True),
    "pic": ("standalone_texture", "图片", "/Game/UI/Textures", False),
    "por": ("standalone_texture", "图标", "/Game/UI/Portrait", True),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def child(root, relative):
    require(isinstance(relative, str) and relative and "\\" not in relative and ":" not in relative,
            "Invalid relative publication path")
    posix = PurePosixPath(relative)
    require(not posix.is_absolute() and ".." not in posix.parts, "Publication path escapes its root")
    result = (root / relative).resolve()
    require(result != root and result.is_relative_to(root), "Publication path escapes its root")
    return result


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def png_dimensions(path):
    with path.open("rb") as handle:
        data = handle.read(24)
    require(len(data) == 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR", "Import source is not a PNG")
    dimensions = list(struct.unpack(">II", data[16:24]))
    require(all(dimensions), "Import PNG has an empty dimension")
    return dimensions


def _validate(manifest):
    require(isinstance(manifest, dict) and manifest.get("packed_verified") is True,
            "Packed manifest has not passed verification")
    require(isinstance(manifest.get("groups"), list) and manifest["groups"], "Cannot import an empty batch")
    roots = [Path(manifest[key]).resolve() for key in ("source", "stage", "destination")]
    source, stage, destination = roots
    for index, first in enumerate(roots):
        for second in roots[index + 1:]:
            require(not first.is_relative_to(second) and not second.is_relative_to(first),
                    "Source, stage and destination directories must not overlap")
    require(source.is_dir() and destination.is_dir(), "Source and published directories must exist")
    published = {}
    require(isinstance(manifest.get("publish_files"), list) and manifest["publish_files"], "Publication file list is empty")
    for row in manifest["publish_files"]:
        relative = row["relative"]
        path = child(destination, relative)
        key = relative.casefold()
        require(key not in published, "Duplicate published path")
        require(isinstance(row.get("sha256"), str) and re.fullmatch(r"[a-f0-9]{64}", row["sha256"]), "Invalid publication checksum")
        published[key] = (relative, path, row)
    expected_assets, expected_files, expected_directories, originals, group_keys = set(), set(), set(), set(), set()

    def expect_asset(actual, wanted):
        require(actual == wanted, "Manifest asset path differs from its resource route")
        require(wanted.casefold() not in expected_assets, "Duplicate expected asset path")
        expected_assets.add(wanted.casefold())
        return wanted

    def expect_file(relative):
        path = child(destination, relative)
        require(relative.casefold() in published and published[relative.casefold()][0] == relative,
                "Actual import source is missing from the verified publication list")
        require(relative not in expected_files, "Resource publication path is reused")
        expected_files.add(relative)
        return path

    canonical_assets = set()
    for group in manifest["groups"]:
        system = group["system"]
        require(isinstance(system, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", system), "Invalid system folder")
        require(isinstance(group.get("files"), list) and group["files"], "Empty resource group")
        first_prefix = group["files"][0]["name"].partition("_")[0]
        require(first_prefix in ROUTES, "Unknown filename prefix")
        kind, branch, engine_root, register_icon = ROUTES[first_prefix]
        require(group["kind"] == kind and group["branch"] == branch and group["register_icon"] is register_icon,
                "Resource group routing differs from its filename prefix")
        engine_dir = engine_root + "/" + system
        require(group["destination"] == engine_dir, "Engine group destination differs from its resource route")
        group_key = (branch, kind, system.casefold())
        require(group_key not in group_keys, "Duplicate resource group")
        group_keys.add(group_key)
        raw_dir = PurePosixPath(branch) / ("图集/资源" if kind == "atlas" else "单图") / system
        expected_directories.add(raw_dir.as_posix())
        names = set()
        for item in group["files"]:
            name = item["name"]
            require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_]+\.[Pp][Nn][Gg]", name), "Invalid resource filename")
            prefix = name.partition("_")[0]
            require(prefix == first_prefix, "Resource group contains mixed routes")
            require(name.casefold() not in names, "Duplicate filename in resource group")
            names.add(name.casefold())
            relative = (raw_dir / name).as_posix()
            require(item["relative"] == relative, "Staged file path differs from its resource route")
            published_source = expect_file(relative)
            require(Path(item["source"]).resolve() == published_source,
                    "Actual import source differs from the verified published resource")
            original = Path(item["original"]).resolve()
            require(original.is_relative_to(source) and original != source and original.name == name,
                    "Original resource path differs from the source directory or filename")
            require(original not in originals, "Original resource occurs more than once")
            originals.add(original)
            if "source_relative" in item:
                require(item["source_relative"] == original.relative_to(source).as_posix(), "Original source-relative path changed")
            require(item["sha256"] == published[relative.casefold()][2]["sha256"], "Raw publication checksum differs from original resource")
            require(checksum(original) == item["sha256"], "Original input changed")
            require(png_dimensions(original) == item["dimensions"], "Original resource dimensions changed")
            asset_name = name.replace(".", "_") if kind == "atlas" else Path(name).stem
            asset = engine_dir + ("/Frames/" if kind == "atlas" else "/") + asset_name + "." + asset_name
            canonical_assets.add(expect_asset(item["asset"], asset))
        if kind == "atlas":
            out_dir = PurePosixPath(branch) / "图集/图集" / system
            expected_directories.add(out_dir.as_posix())
            for key, extension in (("project_relative", ".tps"), ("descriptor_relative", ".paper2dsprites"), ("image_relative", ".png")):
                relative = (out_dir / (system + extension)).as_posix()
                require(group[key] == relative, "Atlas import descriptor or output path differs from its resource group")
                expect_file(relative)
            canonical_assets.add(expect_asset(group["sheet_asset"], engine_dir + "/" + system + "." + system))
            canonical_assets.add(expect_asset(group["texture_asset"], engine_dir + "/Textures/" + system + "." + system))
            descriptor = json.loads(child(destination, group["descriptor_relative"]).read_text(encoding="utf-8-sig"))
            require(isinstance(descriptor.get("frames"), dict) and set(descriptor["frames"]) == {item["name"] for item in group["files"]},
                    "Published descriptor frame names differ from manifest resources")
            require(descriptor["meta"]["image"] == system + ".png", "Descriptor refers to an unexpected image")
            require(descriptor["meta"]["size"] == dict(zip(("w", "h"), group["dimensions"]))
                    and png_dimensions(child(destination, group["image_relative"])) == group["dimensions"], "Published atlas dimensions changed")
            for item in group["files"]:
                frame = descriptor["frames"][item["name"]]
                require(frame == item["frame"], "Published frame differs from the verified manifest")
                require(frame["rotated"] is False and frame["trimmed"] is False, "Rotated or trimmed frame is forbidden")
                rect = frame["frame"]
                require(all(type(rect.get(k)) is int for k in ("x", "y", "w", "h")), "Invalid sprite rectangle")
                require([rect["w"], rect["h"]] == item["dimensions"] and rect["x"] >= 0 and rect["y"] >= 0
                        and rect["x"] + rect["w"] <= group["dimensions"][0] and rect["y"] + rect["h"] <= group["dimensions"][1],
                        "Sprite rectangle differs from the source dimensions or atlas bounds")
    require({row[0] for row in published.values()} == expected_files, "Publication file list differs from import resources")
    require(isinstance(manifest.get("directories"), list) and set(manifest["directories"]) == expected_directories
            and len(manifest["directories"]) == len(expected_directories), "Publication directory list differs from import resources")
    require(isinstance(manifest.get("expected_assets"), list)
            and set(manifest["expected_assets"]) == canonical_assets
            and len(manifest["expected_assets"]) == len(canonical_assets), "Expected assets differ from actual import targets")
    for relative, path, row in published.values():
        require(path.is_file() and checksum(path) == row["sha256"], "Published source changed: " + relative)
        if "bytes" in row:
            require(type(row["bytes"]) is int and path.stat().st_size == row["bytes"], "Published file size changed")
    return canonical_assets


def validate_import_contract(manifest):
    """Reject stale/edited path mappings before Unreal imports; uses no Unreal or Pillow."""
    try:
        return _validate(manifest)
    except (KeyError, TypeError, AttributeError, IndexError) as exc:
        raise ValueError("Invalid import manifest structure") from exc
