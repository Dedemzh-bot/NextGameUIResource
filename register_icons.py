"""Register verified resources with project-configured IDs and unchanged old rows."""
import argparse
import codecs
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from resource_ids import Assignments, Catalog, IDAllocator


def digest(data):
    return hashlib.sha256(data).hexdigest()


def parse_table(data):
    text = data.decode("utf-8-sig")
    lines = text.splitlines()
    if len(lines) < 2 or lines[1].split("\t") != ["Id", "Des", "Path"]:
        raise ValueError("Expected two header rows and Id/Des/Path columns.")
    ids, paths, normalized_paths = {}, {}, set()
    for number, line in enumerate(lines[2:], 3):
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 3 or not fields[0].isascii() or not fields[0].isdigit() or not fields[2].startswith("/Game/"):
            raise ValueError(f"Invalid resource row {number}")
        resource_id = int(fields[0])
        if resource_id <= 0 or resource_id in ids or fields[2].casefold() in normalized_paths:
            raise ValueError(f"Duplicate ID/path or invalid ID on row {number}")
        ids[resource_id] = fields[2]
        paths[fields[2]] = resource_id
        normalized_paths.add(fields[2].casefold())
    return ids, paths


def _object_file(path):
    if not isinstance(path, str) or not re.fullmatch(r"/Game/(?:[A-Za-z0-9_]+/)*[A-Za-z0-9_]+\.[A-Za-z0-9_]+", path):
        raise ValueError(f"Invalid Unreal object path: {path!r}")
    package, name = path.rsplit(".", 1)
    if package.rsplit("/", 1)[-1] != name:
        raise ValueError(f"Package and object names differ: {path}")
    return package.removeprefix("/Game/") + ".uasset"


def _verify_readback(manifest_path, readback_path):
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    readback = json.loads(readback_path.read_text(encoding="utf-8-sig"))
    if readback.get("success") is not True or readback.get("manifest_sha256") != digest(manifest_bytes):
        raise ValueError("Successful readback for this manifest is required before registration.")
    rows = readback.get("assets")
    expected = manifest.get("expected_assets")
    if not isinstance(rows, list) or not isinstance(expected, list):
        raise ValueError("Manifest expected_assets and readback assets must be arrays.")
    assets = {}
    for row in rows:
        path = row["path"]
        _object_file(path)
        if path in assets:
            raise ValueError("Duplicate readback asset: " + path)
        assets[path] = row
    if len(expected) != len(set(expected)) or set(assets) != set(expected):
        raise ValueError("Readback asset coverage does not match the manifest.")
    content_roots = []
    for document in (manifest, readback):
        if document.get("project_content_dir"):
            content_roots.append(Path(document["project_content_dir"]).resolve())
        if document.get("project_dir"):
            content_roots.append((Path(document["project_dir"]) / "Content").resolve())
    if content_roots and any(root != content_roots[0] for root in content_roots):
        raise ValueError("Manifest and readback project identities differ.")
    content_root = content_roots[0] if content_roots else None
    expected_classes = {}
    basenames = Counter()
    candidates = []
    for group in manifest["groups"]:
        if group["kind"] not in ("atlas", "standalone_texture"):
            raise ValueError("Unknown resource group kind: " + str(group["kind"]))
        asset_class = "/Script/Paper2D.PaperSprite" if group["kind"] == "atlas" else "/Script/Engine.Texture2D"
        for file in sorted(group["files"], key=lambda item: (item.get("source_relative", ""), item["name"])):
            path = file["asset"]
            if path in expected_classes:
                raise ValueError("Duplicate manifest resource asset: " + path)
            expected_classes[path] = asset_class
            basenames[file["name"]] += 1
            candidates.append((group, file))
        if group["kind"] == "atlas":
            for key, class_name in (("texture_asset", "/Script/Engine.Texture2D"), ("sheet_asset", "/Script/Paper2D.PaperSpriteSheet")):
                path = group[key]
                if path in expected_classes:
                    raise ValueError("Duplicate manifest asset: " + path)
                expected_classes[path] = class_name
    if set(expected_classes) != set(assets):
        raise ValueError("Manifest groups do not describe every expected asset exactly once.")
    # Include textures and sprite sheets, not only the registered sprite frames.
    for path, row in assets.items():
        if row.get("class") != expected_classes[path]:
            raise ValueError("Wrong resource asset class: " + path)
        saved = row["saved_file"]
        saved_path = Path(saved["path"]).resolve()
        if content_root is not None:
            expected_file = (content_root / _object_file(path)).resolve()
            if not expected_file.is_relative_to(content_root) or saved_path != expected_file:
                raise ValueError("Saved asset path does not match its project identity: " + path)
        data = saved_path.read_bytes()
        if not data or digest(data) != saved["sha256"] or ("bytes" in saved and len(data) != saved["bytes"]):
            raise ValueError("Saved asset changed since readback: " + path)
    return manifest, candidates, basenames


@contextmanager
def _table_lock(table_path):
    lock_path = table_path.with_name(table_path.name + ".lock")
    owner = json.dumps({"pid": os.getpid(), "token": uuid.uuid4().hex}).encode("utf-8")
    try:
        stream = lock_path.open("xb")
    except FileExistsError as exc:
        raise RuntimeError(f"Resource table is locked: {lock_path}. "
                           "Wait for its owner to finish; inspect a stale lock before removing it manually.") from exc
    try:
        with stream:
            stream.write(owner)
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        if lock_path.exists() and lock_path.read_bytes() == owner:
            lock_path.unlink()


def _write_report(path, report):
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def register_resources(manifest_path, readback_path, table_path, output_dir, *, apply=False,
                       catalog_path=None, assignments_path=None, id_mode="classified", register_all=False):
    """Plan or append verified assets; resource.txt must already exist.

    New IDs use the project's catalog and explicit file assignments. Existing
    object paths keep their current IDs and descriptions. Sequential numbering
    is an explicitly requested legacy mode, never an automatic fallback.
    """
    if id_mode not in ("classified", "sequential"):
        raise ValueError("id_mode must be classified or sequential.")
    manifest_path, readback_path = Path(manifest_path), Path(readback_path)
    table_path, output_dir = Path(table_path).resolve(), Path(output_dir).resolve()
    if not table_path.is_file():
        raise FileNotFoundError(f"Existing resource table required; no table is created automatically: {table_path}")
    manifest, candidates, basenames = _verify_readback(manifest_path, readback_path)
    catalog = Catalog(catalog_path) if catalog_path is not None else None
    assignments = Assignments(assignments_path) if assignments_path is not None else None
    protected = {path.resolve() for path in (table_path, manifest_path, readback_path)}
    protected.update(Path(path).resolve() for path in (catalog_path, assignments_path) if path is not None)
    for name in ("registry-plan.json", "registry-result.json", "resource.prepared.txt"):
        if (output_dir / name).resolve() in protected:
            raise ValueError("Registration output would overwrite an input or the resource table.")
    with _table_lock(table_path):
        old = table_path.read_bytes()
        ids, paths = parse_table(old)
        path_ids = {path.casefold(): resource_id for path, resource_id in paths.items()}
        allocator = IDAllocator(catalog, ids) if catalog is not None else None
        next_id = max(set(ids) | (catalog.reserved_ids if catalog else set()), default=0) + 1
        additions, mappings = [], []
        for group, file in candidates:
            if not register_all and not group.get("register_icon", False):
                continue
            path = file["asset"]
            is_new = path.casefold() not in path_ids
            assignment = None
            if is_new:
                description = ""
                if id_mode == "classified":
                    if allocator is None or assignments is None:
                        raise ValueError("New classified IDs require --catalog and --assignments "
                                         "(or NEXTGAME_UI_ID_CATALOG and NEXTGAME_UI_ID_ASSIGNMENTS).")
                    assignment = assignments.resolve(file, manifest, basenames)
                    resource_id = allocator.allocate(assignment["major"], assignment["minor"])
                    description = assignment["description"]
                else:
                    resource_id = next_id
                    next_id += 1
                path_ids[path.casefold()] = resource_id
                additions.append({"Id": resource_id, "Des": description, "Path": path})
            mapping = {"file": file["name"], "system": group["system"], "Id": path_ids[path.casefold()], "Path": path, "reused": not is_new}
            if assignment is not None:
                mapping.update({"major": assignment["major"], "minor": assignment["minor"], "serial": path_ids[path.casefold()] % 10000})
            mappings.append(mapping)
        newline = b"\r\n" if b"\r\n" in old else b"\n"
        appendix = newline.join(f'{row["Id"]}\t{row["Des"]}\t{row["Path"]}'.encode("utf-8") for row in additions)
        new = old + ((b"" if old.endswith(b"\n") else newline) + appendix + newline if additions else b"")
        new_ids, _ = parse_table(new)
        if any(new_ids[resource_id] != path for resource_id, path in ids.items()):
            raise ValueError("Existing mapping changed.")
        report = {"applied": False, "id_mode": id_mode, "table": str(table_path), "existing_count": len(ids),
                  "new_count": len(additions), "total_count": len(new_ids), "additions": additions, "mappings": mappings,
                  "before_sha256": digest(old), "after_sha256": digest(new), "existing_bytes_preserved": new.startswith(old),
                  "utf8_bom_preserved": new.startswith(codecs.BOM_UTF8) == old.startswith(codecs.BOM_UTF8),
                  "blank_remarks": all(row["Des"] == "" for row in additions), "newline": "CRLF" if newline == b"\r\n" else "LF"}
        output_dir.mkdir(parents=True, exist_ok=True)
        _write_report(output_dir / "registry-plan.json", report)
        if apply and additions:
            backup = output_dir / ("resource.before." + digest(old)[:12] + ".txt")
            if backup.exists() and backup.read_bytes() != old:
                raise ValueError("Backup path conflict.")
            backup.write_bytes(old)
            (output_dir / "resource.prepared.txt").write_bytes(new)
            # Replacement stays beside the destination, including cross-volume output.
            candidate = table_path.parent / ("." + table_path.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                with candidate.open("xb") as stream:
                    stream.write(new)
                    stream.flush()
                    os.fsync(stream.fileno())
                if table_path.read_bytes() != old:
                    raise RuntimeError("Resource table changed concurrently; rerun against the latest IDs.")
                os.replace(candidate, table_path)
            finally:
                if candidate.exists():
                    candidate.unlink()
            if table_path.read_bytes() != new:
                raise RuntimeError("Resource table readback mismatch after replacement.")
            report["backup"] = str(backup.resolve())
        if apply:
            report["applied"] = True
            _write_report(output_dir / "registry-result.json", report)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--readback", type=Path, required=True)
    parser.add_argument("--table", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--assignments", type=Path)
    parser.add_argument("--id-mode", choices=("classified", "sequential"), default="classified")
    parser.add_argument("--all-resources", action="store_true", help="Also register configured gui/pic resources.")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    from config import load_settings, select_path
    settings = load_settings(args.env_file)
    table = select_path(args.table, settings.table, "NEXTGAME_UI_RESOURCE_TABLE", kind="file")
    output = select_path(args.output, settings.work_dir / "registration", "NEXTGAME_UI_WORK_DIR")
    report = register_resources(args.manifest, args.readback, table, output, apply=args.apply,
                                catalog_path=args.catalog or settings.id_catalog,
                                assignments_path=args.assignments or settings.id_assignments,
                                id_mode=args.id_mode, register_all=args.all_resources)
    print(json.dumps({key: report[key] for key in ("applied", "id_mode", "existing_count", "new_count", "total_count", "blank_remarks")}))


if __name__ == "__main__":
    main()
