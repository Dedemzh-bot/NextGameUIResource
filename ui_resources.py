"""NextGameUIResource: classify, pack, publish, import and register with local paths."""
from __future__ import annotations
import argparse
import datetime
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from config import load_settings, select_path, ensure_disjoint, ensure_file_outside
from classify_resources import classify_directory
from prepare_batch import prepare_batch, sha, setting
from verify_packed_batch import verify_packed_batch
from engine_bridge import dispatch_import, wait_for_report, write_json


def load_manifest(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def checked_child(root, relative):
    root = Path(root).resolve()
    relative = Path(relative)
    result = (root / relative).resolve()
    if relative.is_absolute() or not result.is_relative_to(root) or result == root:
        raise ValueError("Manifest path escapes its output root: " + str(relative))
    return result


def check_batch_paths(data, manifest_path):
    roots = [Path(data[key]).resolve() for key in ("source", "stage", "destination")]
    for i, first in enumerate(roots):
        for second in roots[i + 1:]:
            ensure_disjoint(first, second, "Batch directory", "Batch directory")
    for path in (Path(manifest_path), Path(manifest_path).parent / "publication.json"):
        ensure_file_outside(path, roots)


def check_tps_before_pack(data, group):
    project = checked_child(data["stage"], group["project_relative"])
    tree = ET.parse(project)
    settings = tree.getroot().find("struct")
    outputs = {checked_child(data["stage"], group[key]) for key in ("image_relative", "descriptor_relative")}
    input_dirs = {checked_child(data["stage"], item["relative"]).parent for item in group["files"]}
    allowed = outputs | input_dirs
    actual = set()
    for node in tree.iter():
        if (node.tag == "filename" or node.attrib.get("type") == "filename") and node.text:
            path = (project.parent / node.text).resolve()
            if Path(node.text).is_absolute() or path not in allowed:
                raise ValueError("TPS path differs from the batch contract: " + node.text)
            actual.add(path)
    if actual != allowed:
        raise ValueError("TPS input/output paths differ from the batch contract")
    texture_path = (project.parent / setting(settings, "textureFileName").text).resolve()
    descriptor_node = setting(setting(setting(settings, "dataFileNames"), "data"), "name")
    descriptor_path = (project.parent / descriptor_node.text).resolve()
    files = setting(setting(setting(settings, "fileLists"), "default"), "files")
    if (texture_path != checked_child(data["stage"], group["image_relative"])
            or descriptor_path != checked_child(data["stage"], group["descriptor_relative"])
            or {(project.parent / node.text).resolve() for node in files} != input_dirs):
        raise ValueError("TPS input/output roles differ from the batch contract")
    for key, value in (("dataFormat", "unreal-paper2d"), ("textureFormat", "png"),
                       ("outputFormat", "RGBA8888"), ("alphaHandling", "KeepTransparentPixels"),
                       ("multiPackMode", "MultiPackOff")):
        if setting(settings, key).text != value:
            raise ValueError("TPS standard changed: " + key)
    for key in ("allowRotation", "trimSpriteNames", "prependSmartFolderName"):
        if setting(settings, key).tag != "false":
            raise ValueError("TPS standard changed: " + key)
    sprite_settings = setting(settings, "globalSpriteSettings")
    if setting(sprite_settings, "trimMode").text != "None" or float(setting(sprite_settings, "scale").text) != 1:
        raise ValueError("TPS trimming or scaling is forbidden")
    if len(setting(settings, "individualSpriteSettings")):
        raise ValueError("TPS individual sprite overrides are forbidden")
    for item in group["files"]:
        if sha(checked_child(data["stage"], item["relative"])) != item["sha256"]:
            raise ValueError("Staged source changed before packing")
    return project


def pack_batch(manifest_path, texturepacker=None):
    data = load_manifest(manifest_path)
    check_batch_paths(data, manifest_path)
    groups = [g for g in data["groups"] if g["kind"] == "atlas"]
    if groups:
        if not texturepacker or not Path(texturepacker).is_file():
            raise ValueError("Configure NEXTGAME_TEXTUREPACKER_EXE to the local TexturePacker CLI executable.")
        projects = [check_tps_before_pack(data, group) for group in groups]
        for project in projects:
            subprocess.run([str(texturepacker), str(project)], cwd=project.parent, check=True)
    return verify_packed_batch(Path(manifest_path))


def publish_batch(manifest_path):
    data = load_manifest(manifest_path)
    check_batch_paths(data, manifest_path)
    if not data.get("packed_verified") or not data.get("publish_files"):
        raise ValueError("Packing verification must succeed before publication.")
    pairs = []
    for file in data["publish_files"]:
        source = checked_child(data["stage"], file["relative"])
        target = checked_child(data["destination"], file["relative"])
        if source == target or sha(source) != file["sha256"]:
            raise ValueError("Staged content changed or output overlaps staging: " + str(source))
        if target.exists() and (not target.is_file() or sha(target) != file["sha256"]):
            raise FileExistsError("Refusing to overwrite different published content: " + str(target))
        pairs.append((source, target, file["sha256"]))
    for source, target, expected_hash in pairs:
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as src, target.open("xb") as dst:
                shutil.copyfileobj(src, dst)
        if sha(target) != expected_hash:
            raise RuntimeError("Publication hash mismatch: " + str(target))
    report = {"success": True, "manifest_sha256": sha(Path(manifest_path)), "file_count": len(pairs), "destination": data["destination"]}
    write_json(Path(manifest_path).parent / "publication.json", report)
    return report


def prepare(settings, batch, source=None, destination=None):
    source = select_path(source, settings.source_dir, "NEXTGAME_UI_SOURCE_DIR", kind="dir")
    destination = select_path(destination, settings.output_dir, "NEXTGAME_UI_OUTPUT_DIR")
    mapping = select_path(None, settings.system_map, "NEXTGAME_UI_SYSTEM_MAP", kind="file")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", batch) or batch in (".", ".."):
        raise ValueError("Batch name must contain only letters, numbers, underscores, hyphens or dots.")
    work = settings.work_dir / batch
    if work.exists():
        raise FileExistsError("Use a new batch name or resume its existing manifest: " + str(work))
    manifest_path = work / "manifest.json"
    manifest = prepare_batch(source, work / "stage", destination, template=settings.tps_template, system_map=mapping, manifest_path=manifest_path)
    if settings.project_root:
        manifest["project_dir"] = str(settings.project_root.resolve())
        write_json(manifest_path, manifest)
    return manifest_path


def register(settings, manifest, readback, apply, all_resources=False, id_mode="classified"):
    from register_icons import register_resources
    return register_resources(Path(manifest), Path(readback), select_path(None, settings.table, "NEXTGAME_UI_RESOURCE_TABLE", kind="file"), Path(manifest).parent / "registry", apply=apply, catalog_path=settings.id_catalog, assignments_path=settings.id_assignments, id_mode=id_mode, register_all=all_resources)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--env-file", type=Path, help="Optional local environment file; process environment takes precedence")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Read configuration and report local prerequisites")
    c = sub.add_parser("classify", help="Read-only prefix classification")
    c.add_argument("--source", type=Path);c.add_argument("--output", type=Path)
    for command in ("prepare", "run"):
        s = sub.add_parser(command)
        s.add_argument("--batch", default=datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
        s.add_argument("--source", type=Path);s.add_argument("--destination", type=Path)
        if command == "run":
            s.add_argument("--backend", choices=("nxue", "manual"), default="nxue")
            s.add_argument("--apply-standard", action="store_true")
            s.add_argument("--apply-registry", action="store_true")
            s.add_argument("--all-resources", action="store_true")
            s.add_argument("--timeout", type=float, default=300)
    for command in ("pack", "publish", "import", "verify-engine", "register"):
        s = sub.add_parser(command);s.add_argument("--manifest", type=Path, required=True)
        if command in ("import", "verify-engine"):
            s.add_argument("--backend", choices=("nxue", "manual"), default="nxue")
            s.add_argument("--wait", action="store_true");s.add_argument("--timeout", type=float, default=300)
            s.add_argument("--apply-standard", action="store_true")
        if command == "register":
            s.add_argument("--readback", type=Path)
            s.add_argument("--apply", action="store_true")
            s.add_argument("--all-resources", action="store_true")
            s.add_argument("--id-mode", choices=("classified", "sequential"), default="classified")
    args = p.parse_args(argv)
    settings = load_settings(args.env_file)
    if args.command == "doctor":
        report = {}
        for name in ("source_dir", "output_dir", "project_root", "table", "work_dir", "texturepacker", "tps_template", "system_map", "id_catalog", "id_assignments", "nxue_cli"):
            value = getattr(settings, name)
            report[name] = {"configured": value is not None, "path": str(value) if value else None, "exists": value.exists() if value else False}
        print(json.dumps(report, ensure_ascii=False, indent=2));return 0
    if args.command == "classify":
        source = select_path(args.source, settings.source_dir, "NEXTGAME_UI_SOURCE_DIR", kind="dir")
        output = (args.output or settings.work_dir / "classification.json").resolve()
        if output.is_relative_to(source):
            raise ValueError("Classification report must be outside the input directory")
        report = classify_directory(source);write_json(output, report)
        print(json.dumps({"counts":report["counts"],"report":str(output)},ensure_ascii=False));return 0
    if args.command in ("prepare", "run"):
        # Validate configuration before creating a batch when all steps were requested.
        if args.command == "run":
            select_path(None, settings.project_root, "NEXTGAME_PROJECT_ROOT", kind="dir")
            select_path(None, settings.table, "NEXTGAME_UI_RESOURCE_TABLE", kind="file")
            select_path(None, settings.id_catalog, "NEXTGAME_UI_ID_CATALOG", kind="file")
            select_path(None, settings.id_assignments, "NEXTGAME_UI_ID_ASSIGNMENTS", kind="file")
        manifest = prepare(settings,args.batch,args.source,args.destination)
        if args.command == "prepare":
            print(json.dumps({"manifest":str(manifest)},ensure_ascii=False));return 0
        pack_batch(manifest,settings.texturepacker);publish_batch(manifest)
        report=dispatch_import(manifest,settings.project_root,backend=args.backend,apply_standard=args.apply_standard,nxue_cli=settings.nxue_cli)
        wait_for_report(report,args.timeout)
        readback=dispatch_import(manifest,settings.project_root,backend=args.backend,verify_only=True,nxue_cli=settings.nxue_cli)
        wait_for_report(readback,args.timeout)
        result=register(settings,manifest,readback,args.apply_registry,args.all_resources)
    elif args.command == "pack":result=pack_batch(args.manifest,settings.texturepacker)
    elif args.command == "publish":result=publish_batch(args.manifest)
    elif args.command in ("import", "verify-engine"):
        project=select_path(None,settings.project_root,"NEXTGAME_PROJECT_ROOT",kind="dir")
        report=dispatch_import(args.manifest,project,backend=args.backend,verify_only=args.command=="verify-engine",apply_standard=args.apply_standard,nxue_cli=settings.nxue_cli)
        result=wait_for_report(report,args.timeout) if args.wait else {"report":str(report),"request_dispatched":True}
    else:result=register(settings,args.manifest,args.readback or args.manifest.parent/'readback.json',args.apply,args.all_resources,args.id_mode)
    print(json.dumps({"command":args.command,"success":True,"result":{k:v for k,v in result.items() if k in ('applied','new_count','total_count','report','file_count','request_dispatched','status','packed_verified')}},ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print("error: "+str(exc),file=sys.stderr)
        raise SystemExit(1)
