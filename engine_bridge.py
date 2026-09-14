"""Local Unreal request adapter and manual Python-console fallback."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from config import ensure_file_outside
from import_contract import validate_import_contract


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def call_bridge(project_root, script, arguments, *, nxue_cli=None):
    """Use the existing project client, or the documented local NxUE bridge protocol."""
    project_root = Path(project_root).resolve()
    if nxue_cli:
        request = Path(arguments["report"]).with_suffix(".request.json")
        write_json(request, arguments)
        cmd = [sys.executable, str(nxue_cli), "exec-python", "--file", str(script), "--args-file", str(request)]
        if not Path(script).resolve().is_relative_to(project_root):
            cmd.append("--allow-outside-project")
        subprocess.run(cmd, cwd=project_root, check=True)
        return
    instance_path = project_root / ".nxue-agent" / "instance.json"
    instance = json.loads(instance_path.read_text(encoding="utf-8")) if instance_path.is_file() else {}
    url = os.environ.get("NXUE_AGENT_URL") or instance.get("url")
    if not url:
        host = instance.get("host", "127.0.0.1")
        port = os.environ.get("NXUE_AGENT_PORT") or instance.get("port", 8765)
        url = f"http://{host}:{port}"
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in ("localhost", "127.0.0.1", "::1") or parsed.username:
        raise ValueError("NxUE bridge must be a local loopback URL.")
    token = os.environ.get("NXUE_AGENT_TOKEN") or instance.get("token")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-NxUEAgent-Token"] = str(token)
    payload = {"jsonrpc": "2.0", "id": "ui-resource-import", "method": "tools/call", "params": {"name": "exec-python", "arguments": {
        "script_path": str(Path(script).resolve()), "arguments": arguments,
        "allow_outside_project": not Path(script).resolve().is_relative_to(project_root)}}}
    request = urllib.request.Request(url.rstrip("/") + "/bridge", data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.load(response)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Cannot reach the local NxUEAgent bridge. Start the configured Unreal project with NxUEAgent, or use --backend manual.") from exc
    result = body.get("result", body)
    if body.get("error") or result.get("ok") is False or result.get("isError"):
        raise RuntimeError("Unreal rejected the import request; inspect the editor log.")


def dispatch_import(manifest_path, project_root, *, backend="nxue", verify_only=False, apply_standard=False, nxue_cli=None):
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    project_root = Path(project_root).resolve()
    if manifest.get("project_dir") and Path(manifest["project_dir"]).resolve() != project_root:
        raise ValueError("Batch belongs to a different project; do not reuse another machine's manifest.")
    validate_import_contract(manifest)
    roots = [Path(manifest[key]).resolve() for key in ("source", "stage", "destination")]
    for name in (manifest_path.name, "readback.json", "import-result.json", "verify-request.json",
                 "import-request.json", "launch_verify.py", "launch_import.py"):
        ensure_file_outside(manifest_path.parent / name, roots)
    report_path = manifest_path.parent / ("readback.json" if verify_only else "import-result.json")
    # Do not mistake a stale completion report for this request.
    if report_path.exists():
        raise FileExistsError(f"Report already exists: {report_path}. Keep the existing result or use a fresh batch.")
    arguments = {"manifest": str(manifest_path), "report": str(report_path), "project_dir": str(project_root), "apply_texture_standard": bool(apply_standard)}
    if verify_only:
        arguments.update(verify_only=True, original_report=str(manifest_path.parent / "import-result.json"))
    script = Path(__file__).with_name("import_batch.py").resolve()
    request_path = manifest_path.parent / ("verify-request.json" if verify_only else "import-request.json")
    write_json(request_path, arguments)
    if backend == "manual":
        launch = manifest_path.parent / ("launch_verify.py" if verify_only else "launch_import.py")
        launch.write_text("import runpy\nrunpy.run_path(" + repr(str(script)) + ", init_globals={'UI_RESOURCE_ARGS_FILE': " + repr(str(request_path)) + "})\n", encoding="utf-8")
        print(json.dumps({"manual_editor_command": 'py "' + str(launch) + '"', "report": str(report_path)}, ensure_ascii=False), flush=True)
    elif backend == "nxue":
        call_bridge(project_root, script, arguments, nxue_cli=nxue_cli)
    else:
        raise ValueError("Unknown engine backend")
    return report_path


def wait_for_report(path, timeout=300):
    if timeout <= 0:
        raise ValueError("Timeout must be positive")
    deadline = time.monotonic() + timeout
    last_status = None
    while time.monotonic() < deadline:
        try:
            report = json.loads(Path(path).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            report = {}
        status = report.get("status")
        if status and status != last_status:
            print(json.dumps({"engine_status": status}), flush=True)
            last_status = status
        if report.get("success") and status == "complete":
            return report
        if status in ("failed", "preflight_failed"):
            raise RuntimeError(f"Engine step failed: {report.get('error', 'see report')}")
        time.sleep(0.5)
    raise TimeoutError(f"Waiting for Unreal timed out: {path}. The editor task may still be running; inspect the report before retrying.")
