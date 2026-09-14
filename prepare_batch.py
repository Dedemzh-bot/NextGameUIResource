"""Stage classified PNGs and paired TPS projects using a project-owned system map."""
import argparse
import copy
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
import re
import shutil
import struct
import xml.etree.ElementTree as ET
from classify_resources import classify_directory
from config import ensure_disjoint, ensure_file_outside, load_settings, select_path


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def png_size(path):
    with Path(path).open("rb") as handle:
        data = handle.read(24)
    if len(data) != 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError(f"Invalid PNG: {Path(path).name}")
    dimensions = list(struct.unpack(">II", data[16:24]))
    if not all(dimensions):
        raise ValueError(f"PNG has an empty dimension: {Path(path).name}")
    return dimensions


def setting(node, key):
    if node is None:
        raise ValueError("TPS template has no Settings struct")
    children = list(node)
    for i, child in enumerate(children[:-1]):
        if child.tag == "key" and child.text == key:
            return children[i + 1]
    raise ValueError("TPS template is missing setting: " + key)


def _mapping(value):
    if value is None:
        raise ValueError("Configure NEXTGAME_UI_SYSTEM_MAP or supply --system-map")
    if not isinstance(value, (Mapping, str, Path)):
        raise ValueError("System map must be a mapping or a JSON file path")
    mapping = dict(value) if isinstance(value, Mapping) else json.loads(Path(value).read_text(encoding="utf-8-sig"))
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError("System map must be a non-empty JSON object")
    canonical = {}
    for token, system in mapping.items():
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9]+", token):
            raise ValueError("System map tokens must be non-empty filename words without underscores")
        if not isinstance(system, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", system):
            raise ValueError("System map values must be valid Unreal folder names")
        if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", system, re.IGNORECASE):
            raise ValueError("System map cannot use Windows reserved folder names")
        previous = canonical.setdefault(system.casefold(), system)
        if previous != system:
            raise ValueError("System map contains folder names differing only by case")
    return mapping


def _project(template, group):
    tree = copy.deepcopy(template)
    settings = tree.getroot().find("struct")
    system = group["system"]
    setting(settings, "textureFileName").text = system + ".png"
    setting(setting(setting(settings, "dataFileNames"), "data"), "name").text = system + ".paper2dsprites"
    for key, value in (("dataFormat", "unreal-paper2d"), ("textureFormat", "png"),
                       ("outputFormat", "RGBA8888"), ("alphaHandling", "KeepTransparentPixels"),
                       ("multiPackMode", "MultiPackOff")):
        setting(settings, key).text = value
    for key in ("allowRotation", "trimSpriteNames", "prependSmartFolderName"):
        setting(settings, key).tag = "false"
    sprite_settings = setting(settings, "globalSpriteSettings")
    setting(sprite_settings, "trimMode").text = "None"
    setting(sprite_settings, "scale").text = "1"
    individual = setting(settings, "individualSpriteSettings")
    for child in list(individual):
        individual.remove(child)
    files = setting(setting(setting(settings, "fileLists"), "default"), "files")
    for child in list(files):
        files.remove(child)
    ET.SubElement(files, "filename").text = "../../资源/" + system
    ET.indent(tree, space="    ")
    return ET.tostring(tree.getroot(), encoding="utf-8", xml_declaration=True)


def prepare_batch(source, stage, destination, template=None, system_map=None, manifest_path=None):
    """Preflight and copy a new batch; never edit source files or published folders."""
    source = select_path(source, None, "NEXTGAME_UI_SOURCE_DIR", "dir")
    stage = select_path(stage, None, "NEXTGAME_UI_WORK_DIR")
    destination = select_path(destination, None, "NEXTGAME_UI_OUTPUT_DIR")
    for first, second, labels in ((source, stage, ("Source", "Stage")),
                                   (source, destination, ("Source", "Destination")),
                                   (stage, destination, ("Stage", "Destination"))):
        ensure_disjoint(first, second, *labels)
    if stage.exists():
        raise FileExistsError("Stage directory already exists; choose a new batch directory")
    if destination.exists() and not destination.is_dir():
        raise ValueError("Destination must be a directory")
    manifest_path = ensure_file_outside(manifest_path or stage.parent / "manifest.json", [source, stage, destination])
    if manifest_path.exists():
        raise FileExistsError("Manifest already exists; choose a new batch manifest")
    mapping = _mapping(system_map)
    report = classify_directory(source)
    if report["counts"]["unmatched"] or report["counts"]["other_files"]:
        raise ValueError("Resolve unmatched/non-PNG inputs before packing")
    if not report["total_files"]:
        raise ValueError("Source contains no PNG resources")
    groups, used_names = {}, set()
    for kind in ("atlas", "standalone_texture"):
        for item in report["groups"][kind]:
            src = source / item["file"]
            if not src.resolve().is_relative_to(source):
                raise ValueError("Source symlinks must not resolve outside the source directory")
            if not re.fullmatch(r"[A-Za-z0-9_]+", src.stem):
                raise ValueError("Filename needs explicit Unreal name handling: " + src.name)
            token = src.stem.split("_")[1]
            if not token or token not in mapping:
                raise ValueError("Missing system mapping for filename token: " + (token or "<empty>"))
            system = mapping[token]
            key = (item["branch"], kind, system)
            unique = (item["engine_root"].casefold(), system.casefold(), src.name.casefold())
            if unique in used_names:
                raise ValueError("Duplicate flattened asset name: " + src.name)
            used_names.add(unique)
            group = groups.setdefault(key, {"branch": item["branch"], "kind": kind, "system": system,
                "destination": item["engine_root"] + "/" + system,
                "register_icon": item["register_icon"], "files": []})
            rel_dir = Path(item["branch"]) / ("图集/资源" if kind == "atlas" else "单图") / system
            rel = rel_dir / src.name
            group["files"].append({"original": str(src), "source_relative": item["file"],
                "relative": rel.as_posix(), "source": str(destination / rel), "name": src.name,
                "sha256": sha(src), "dimensions": png_size(src)})
    has_atlas = any(group["kind"] == "atlas" for group in groups.values())
    template_path = select_path(template, None, "NEXTGAME_UI_TPS_TEMPLATE", "file") if has_atlas else None
    template_tree = ET.parse(template_path) if has_atlas else None
    directories, projects = [], {}
    for group in groups.values():
        raw = Path(group["files"][0]["relative"]).parent
        directories.append(raw.as_posix())
        if group["kind"] == "atlas":
            out = Path(group["branch"]) / "图集/图集" / group["system"]
            directories.append(out.as_posix())
            group["project_relative"] = (out / (group["system"] + ".tps")).as_posix()
            group["descriptor_relative"] = (out / (group["system"] + ".paper2dsprites")).as_posix()
            group["image_relative"] = (out / (group["system"] + ".png")).as_posix()
            projects[group["project_relative"]] = _project(template_tree, group)
            for node in ET.fromstring(projects[group["project_relative"]]).iter():
                if (node.tag == "filename" or node.attrib.get("type") == "filename") and node.text:
                    target = (stage / out / node.text).resolve()
                    if not target.is_relative_to(stage):
                        raise ValueError("TPS template contains a path outside the stage directory")
    for rel in directories:
        if (destination / rel).exists():
            raise FileExistsError("Published system directory already exists: " + rel)
        if not (destination / rel).resolve().is_relative_to(destination):
            raise ValueError("Destination link escapes the configured output directory")
    # All input and output validation occurs before the first write.
    for rel in directories:
        (stage / rel).mkdir(parents=True)
    for group in groups.values():
        for item in group["files"]:
            shutil.copy2(item["original"], stage / item["relative"])
    for rel, content in projects.items():
        (stage / rel).write_bytes(content)
    manifest = {"source": str(source), "stage": str(stage), "destination": str(destination),
        "template": str(template_path) if template_path else None,
        "template_sha256": sha(template_path) if template_path else None,
        "system_map": mapping, "counts": report["prefix_counts"], "directories": directories,
        "groups": list(groups.values()), "classification": report}
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("source", "stage", "destination", "template", "system-map", "manifest", "env-file"):
        parser.add_argument("--" + flag, type=Path)
    args = parser.parse_args()
    try:
        settings = load_settings(args.env_file)
        result = prepare_batch(
            select_path(args.source, settings.source_dir, "NEXTGAME_UI_SOURCE_DIR", "dir"),
            args.stage or settings.work_dir / "stage",
            select_path(args.destination, settings.output_dir, "NEXTGAME_UI_OUTPUT_DIR"),
            args.template or settings.tps_template,
            select_path(args.system_map, settings.system_map, "NEXTGAME_UI_SYSTEM_MAP", "file"),
            args.manifest or settings.work_dir / "manifest.json")
    except (ValueError, OSError, ET.ParseError) as exc:
        parser.error(str(exc))
    print(json.dumps({"counts": result["counts"], "groups": [{k: g[k] for k in ("branch", "kind", "system", "destination")} for g in result["groups"]]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
