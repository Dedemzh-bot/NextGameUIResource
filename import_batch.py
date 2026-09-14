"""Import a verified batch on Slate ticks, then validate and save its assets only.

Run through NxUE exec-python with --args-file containing manifest and report paths.
Use verify_only=true for independent readback after the import has completed.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path
import runpy
import time
import traceback
import unreal

validate_import_contract = runpy.run_path(str(Path(__file__).resolve().with_name("import_contract.py")))["validate_import_contract"]

request_file = globals().get("UI_RESOURCE_ARGS_FILE") or os.environ.get("NEXTGAME_UI_IMPORT_ARGS")
if request_file:
    args = json.loads(Path(request_file).read_text(encoding="utf-8-sig"))
elif hasattr(unreal, "get_nxue_args"):
    args = unreal.get_nxue_args()
else:
    raise RuntimeError("Use the generated launch_import.py or set NEXTGAME_UI_IMPORT_ARGS to an import request JSON file.")
manifest_path = Path(args["manifest"])
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
report_path = Path(args["report"])
registry = unreal.AssetRegistryHelpers.get_asset_registry()
expected = set(manifest["expected_assets"])
expected_packages = {p.split(".")[0] for p in expected}
content_dir = Path(unreal.Paths.project_content_dir()).resolve()
project_dir = content_dir.parent
state = {"handle": None, "busy": False, "group": 0, "phase": "import", "ticks": 0, "deadline": 0}
report = {"status": "preflight", "success": False, "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    "project_dir": str(project_dir), "project_content_dir": str(content_dir),
    "property_overrides": [], "imports": [], "saved_assets": [], "assets": []}


def emit():
    report["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def dirty():
    return {p.get_path_name() for p in unreal.EditorLoadingAndSavingUtils.get_dirty_content_packages()}


def value(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, unreal.Object):
        return v.get_path_name()
    if isinstance(v, unreal.Vector2D):
        return [v.x, v.y]
    return str(v)


def props(obj, names):
    return {name: value(obj.get_editor_property(name)) for name in names}


def check_sources():
    for requested_project in (args.get("project_dir"), manifest.get("project_dir")):
        if requested_project and Path(requested_project).resolve() != project_dir:
            raise RuntimeError("Running editor project does not match NEXTGAME_PROJECT_ROOT.")
    if validate_import_contract(manifest) != expected:
        raise RuntimeError("Validated import targets differ from the scheduled asset set.")


def textures():
    for group in manifest["groups"]:
        if group["kind"] == "atlas":
            yield group["texture_asset"], group["dimensions"]
        else:
            for file in group["files"]:
                yield file["asset"], file["dimensions"]


def get_dimensions(texture):
    return [texture.blueprint_get_size_x(), texture.blueprint_get_size_y()]


def normalize_textures():
    standards = {"compression_settings": unreal.TextureCompressionSettings.TC_BC7,
        "lod_group": unreal.TextureGroup.TEXTUREGROUP_UI,
        "mip_gen_settings": unreal.TextureMipGenSettings.TMGS_NO_MIPMAPS, "srgb": True}
    tracked = tuple(standards) + ("never_stream", "filter", "address_x", "address_y", "virtual_texture_streaming",
        "compression_no_alpha", "max_texture_size", "lod_bias", "power_of_two_mode")
    for path, dims in textures():
        texture = unreal.load_asset(path)
        changes = {k: v for k, v in standards.items() if texture.get_editor_property(k) != v}
        if not changes:
            continue
        row = {"path": path, "before": props(texture, tracked), "dimensions": get_dimensions(texture),
            "source_id": texture.blueprint_get_texture_source_id_string(),
            "source_files": list(texture.get_editor_property("asset_import_data").extract_filenames()),
            "changed_fields": sorted(changes)}
        for key, v in changes.items():
            texture.set_editor_property(key, v)
        report["property_overrides"].append(row)


def verify_normalization():
    for row in report["property_overrides"]:
        texture = unreal.load_asset(row["path"])
        after = props(texture, tuple(row["before"]))
        if any(after[k] != v for k, v in row["before"].items() if k not in row["changed_fields"]):
            raise RuntimeError("Unrelated property changed: " + row["path"])
        if get_dimensions(texture) != row["dimensions"] or texture.blueprint_get_texture_source_id_string() != row["source_id"]:
            raise RuntimeError("Texture source changed during normalization: " + row["path"])
        if list(texture.get_editor_property("asset_import_data").extract_filenames()) != row["source_files"]:
            raise RuntimeError("Texture import reference changed: " + row["path"])
        row["after"] = after


def verify():
    check_sources()
    actual = set()
    for group in manifest["groups"]:
        actual.update(str(a.package_name) + "." + str(a.asset_name)
            for a in registry.get_assets_by_path(group["destination"], recursive=True))
    if actual != expected:
        raise RuntimeError("Asset set mismatch: " + json.dumps({"missing": sorted(expected-actual), "unexpected": sorted(actual-expected)}))
    rows = {}
    for path in sorted(expected):
        obj = unreal.load_asset(path)
        if not obj:
            raise RuntimeError("Cannot load: " + path)
        rows[path] = {"path": path, "class": obj.get_class().get_path_name()}
    errors = []
    for path, dims in textures():
        texture = unreal.load_asset(path)
        if not isinstance(texture, unreal.Texture2D):
            raise RuntimeError("Expected Texture2D: " + path)
        properties = props(texture, ("compression_settings", "lod_group", "mip_gen_settings", "srgb", "never_stream",
            "filter", "address_x", "address_y", "virtual_texture_streaming", "compression_no_alpha", "max_texture_size", "lod_bias", "power_of_two_mode"))
        rows[path].update({"dimensions": get_dimensions(texture), "properties": properties,
            "source_files": list(texture.get_editor_property("asset_import_data").extract_filenames())})
        checks = {"dimensions": get_dimensions(texture) == dims,
            "BC7": texture.get_editor_property("compression_settings") == unreal.TextureCompressionSettings.TC_BC7,
            "UI_group": texture.get_editor_property("lod_group") == unreal.TextureGroup.TEXTUREGROUP_UI,
            "NoMipmaps": texture.get_editor_property("mip_gen_settings") == unreal.TextureMipGenSettings.TMGS_NO_MIPMAPS,
            "sRGB": texture.get_editor_property("srgb") is True}
        if not all(checks.values()):
            errors.append({"path": path, "checks": checks, "observed": rows[path]})
    for group in manifest["groups"]:
        if group["kind"] != "atlas":
            continue
        sheet = unreal.load_asset(group["sheet_asset"])
        texture = unreal.load_asset(group["texture_asset"])
        if not isinstance(sheet, unreal.PaperSpriteSheet) or sheet.get_editor_property("texture") != texture:
            raise RuntimeError("Invalid sprite sheet texture reference.")
        sprites = list(sheet.get_editor_property("sprites"))
        if {s.get_path_name() for s in sprites if s} != {f["asset"] for f in group["files"]} or len(sprites) != len(group["files"]):
            raise RuntimeError("Sprite sheet membership mismatch.")
        if set(sheet.get_editor_property("sprite_names")) != {f["name"] for f in group["files"]}:
            raise RuntimeError("Sprite sheet source name mismatch.")
        rows[group["sheet_asset"]].update({"texture": texture.get_path_name(), "sprite_count": len(sprites),
            "source_files": list(sheet.get_editor_property("asset_import_data").extract_filenames())})
        for file in group["files"]:
            sprite = unreal.load_asset(file["asset"])
            if not isinstance(sprite, unreal.PaperSprite):
                raise RuntimeError("Expected PaperSprite: " + file["asset"])
            properties = props(sprite, ("source_texture", "source_texture_dimension", "source_uv", "source_dimension",
                "rotated_in_source_image", "trimmed_in_source_image", "pivot_mode", "pixels_per_unreal_unit",
                "default_material", "alternate_material", "snap_pivot_to_pixel_grid"))
            rows[file["asset"]]["properties"] = properties
            rect = file["frame"]["frame"]
            if (properties["source_texture"] != group["texture_asset"] or properties["source_texture_dimension"] != group["dimensions"]
                or properties["source_uv"] != [rect["x"], rect["y"]] or properties["source_dimension"] != [rect["w"], rect["h"]]
                or properties["rotated_in_source_image"] != file["frame"]["rotated"] or properties["trimmed_in_source_image"] != file["frame"]["trimmed"]):
                raise RuntimeError("Sprite rectangle or reference mismatch: " + file["asset"])
    report["assets"] = list(rows.values())
    report["parameter_mismatches"] = errors
    if errors:
        raise RuntimeError("Imported engine parameters differ from the agreed standard; inspect parameter_mismatches.")
    return rows


def verify_saved(rows):
    for path, row in rows.items():
        asset_file = content_dir / (path.split(".")[0].removeprefix("/Game/") + ".uasset")
        if not asset_file.is_file() or asset_file.stat().st_size == 0:
            raise RuntimeError("Missing saved file: " + str(asset_file))
        row["saved_file"] = {"path": str(asset_file), "bytes": asset_file.stat().st_size,
            "sha256": hashlib.sha256(asset_file.read_bytes()).hexdigest()}
    after = dirty()
    report["dirty_targets_after"] = sorted(after & expected_packages)
    report["new_dirty_outside_targets"] = sorted(after - set(report["dirty_before"]) - expected_packages)
    if report["dirty_targets_after"]:
        raise RuntimeError("Batch assets remain dirty after import.")
    report["dirty_after"] = sorted(after)
    report["counts"] = {name: sum(row["class"].rsplit(".", 1)[-1] == name for row in rows.values())
        for name in ("PaperSpriteSheet", "PaperSprite", "Texture2D")}
    report["counts"]["total"] = len(rows)
    report["status"] = "complete"
    report["success"] = True


def on_tick(delta):
    if state["busy"]:
        return
    state["busy"] = True
    unreal.unregister_slate_post_tick_callback(state["handle"])
    state["handle"] = None
    try:
        if state["phase"] == "import":
            # A staged import runs over multiple Slate ticks; recheck immediately
            # before each group's asset mutation, not only when scheduling starts.
            check_sources()
            group = manifest["groups"][state["group"]]
            report["status"] = "importing_" + group["system"] + "_" + group["kind"]
            emit()
            files = [str(Path(manifest["destination"]) / group["descriptor_relative"])] if group["kind"] == "atlas" else [f["source"] for f in group["files"]]
            tasks = []
            for filename in files:
                task = unreal.AssetImportTask()
                for key, v in {"filename": filename, "destination_path": group["destination"], "automated": True,
                    "async_": False, "replace_existing": False, "replace_existing_settings": False, "save": False}.items():
                    task.set_editor_property(key, v)
                if group["kind"] == "atlas":
                    task.set_editor_property("factory", unreal.PaperSpriteSheetImportFactory())
                tasks.append(task)
            unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks(tasks)
            report["imports"].append({"destination": group["destination"], "task_paths": [list(t.get_editor_property("imported_object_paths")) for t in tasks]})
            state["group"] += 1
            if state["group"] == len(manifest["groups"]):
                state["phase"] = "wait"
                state["deadline"] = time.monotonic() + 180
                report["status"] = "waiting_for_texture_builds"
            emit()
        elif state["phase"] == "wait":
            state["ticks"] += 1
            if time.monotonic() > state["deadline"]:
                raise RuntimeError("Texture compilation timed out; no automatic reimport.")
            ready = state["ticks"] >= 2
            for path, dims in textures():
                texture = unreal.load_asset(path)
                ready = ready and isinstance(texture, unreal.Texture2D) and get_dimensions(texture) == dims
            if ready:
                if args.get("apply_texture_standard") and not state.get("normalized"):
                    normalize_textures()
                    state["normalized"] = True
                    state["ticks"] = 0
                    state["deadline"] = time.monotonic() + 180
                    report["status"] = "waiting_after_texture_standard"
                    emit()
                    return
                report["status"] = "verifying_before_save"
                emit()
                verify_normalization()
                rows = verify()
                # Other editor work may be active concurrently; never save those packages.
                report["outside_dirty_before_save"] = sorted(dirty() - expected_packages)
                report["status"] = "saving"
                emit()
                for path in sorted(expected):
                    if not unreal.EditorAssetLibrary.save_loaded_asset(unreal.load_asset(path), only_if_is_dirty=False):
                        raise RuntimeError("Save failed: " + path)
                    report["saved_assets"].append(path)
                rows = verify()
                verify_normalization()
                verify_saved(rows)
                state["phase"] = "done"
                emit()
                unreal.log("UI batch imported and saved: " + str(len(expected)) + " assets")
    except Exception as exc:
        report.update({"status": "failed", "error": str(exc), "traceback": traceback.format_exc()})
        state["phase"] = "failed"
        emit()
        unreal.log_error("UI batch failed: " + str(exc))
    finally:
        state["busy"] = False
        if state["phase"] not in ("done", "failed"):
            state["handle"] = unreal.register_slate_post_tick_callback(on_tick)


try:
    check_sources()
    report["dirty_before"] = sorted(dirty())
    if args.get("verify_only"):
        original_report = json.loads(Path(args["original_report"]).read_text(encoding="utf-8"))
        if not original_report["success"] or original_report["manifest_sha256"] != report["manifest_sha256"]:
            raise RuntimeError("Original completed import does not match this manifest.")
        report["dirty_before"] = original_report["dirty_before"]
        verify_saved(verify())
        report["operation"] = "independent_readback"
        emit()
        print(json.dumps({"success": True, "counts": report["counts"]}))
    elif args.get("resume_report"):
        previous = json.loads(Path(args["resume_report"]).read_text(encoding="utf-8"))
        if (previous["status"] != "failed" or previous["manifest_sha256"] != report["manifest_sha256"]
            or len(previous["imports"]) != len(manifest["groups"]) or previous["saved_assets"]
            or previous.get("error") not in ("Imported engine parameters differ from the agreed standard; inspect parameter_mismatches.",
                "Import dirtied assets outside the batch.") or {r["path"] for r in previous["assets"]} != expected):
            raise RuntimeError("Previous import is not eligible for parameter correction without reimport.")
        for row in previous["assets"]:
            obj = unreal.load_asset(row["path"])
            if not obj or obj.get_class().get_path_name() != row["class"]:
                raise RuntimeError("Imported asset changed before resume.")
            if "properties" in row and props(obj, tuple(row["properties"])) != row["properties"]:
                raise RuntimeError("Asset has intervening edits: " + row["path"])
        report["dirty_before"] = previous["dirty_before"]
        report["imports"] = previous["imports"]
        report["property_overrides"] = previous.get("property_overrides", [])
        report["resumed_from"] = args["resume_report"]
        state.update({"phase": "wait", "deadline": time.monotonic() + 180})
        state["normalized"] = bool(report["property_overrides"])
        report["status"] = "resuming_validation_without_reimport"
        emit()
        state["handle"] = unreal.register_slate_post_tick_callback(on_tick)
        print(json.dumps({"status": report["status"], "expected_assets": len(expected)}))
    else:
        for group in manifest["groups"]:
            folder = content_dir / group["destination"].removeprefix("/Game/")
            if list(registry.get_assets_by_path(group["destination"], recursive=True)) or (folder.exists() and any(folder.rglob("*.uasset"))):
                raise RuntimeError("Destination already contains assets: " + group["destination"])
        report["status"] = "scheduled"
        emit()
        state["handle"] = unreal.register_slate_post_tick_callback(on_tick)
        print(json.dumps({"status": "scheduled", "expected_assets": len(expected), "report": str(report_path)}))
except Exception as exc:
    report.update({"status": "preflight_failed", "error": str(exc), "traceback": traceback.format_exc()})
    emit()
    raise
