"""Project-owned classification dictionaries and deterministic AA BB CCCC IDs."""
from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath


def _integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer from {minimum} to {maximum}.")
    return value


def category_pair(major, minor):
    return (_integer(major, 10, 99, "major"), _integer(minor, 1, 99, "minor"))


def split_resource_id(value):
    """Return classified fields; reject zero fields and legacy IDs."""
    _integer(value, 10000000, 99999999, "resource ID")
    major, tail = divmod(value, 1000000)
    minor, serial = divmod(tail, 10000)
    category_pair(major, minor)
    _integer(serial, 1, 9999, "serial")
    return major, minor, serial


def _text(value, label, *, required=False):
    if not isinstance(value, str) or any(char in value for char in "\t\r\n\x00"):
        raise ValueError(f"{label} must be text without tabs, newlines or NUL.")
    if required and not value.strip():
        raise ValueError(f"{label} must not be blank.")
    return value


def _document(path, key):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
        raise ValueError(f"{path}: expected a JSON object with version: 1.")
    if not isinstance(data.get(key), list):
        raise ValueError(f"{path}: {key} must be an array.")
    return data


@dataclass(frozen=True)
class Category:
    major: int
    minor: int
    name: str
    enabled: bool


class Catalog:
    """Categories are supplied by the project; no game taxonomy is built in."""
    def __init__(self, path):
        document = _document(path, "categories")
        self.categories = {}
        for row in document["categories"]:
            if not isinstance(row, dict):
                raise ValueError("Each category must be an object.")
            pair = category_pair(row.get("major"), row.get("minor"))
            if pair in self.categories:
                raise ValueError(f"Duplicate category: {pair[0]:02d}-{pair[1]:02d}.")
            name = _text(row.get("name"), "category name", required=True)
            enabled = row.get("enabled", True)
            if type(enabled) is not bool:
                raise ValueError("Category enabled must be true or false.")
            self.categories[pair] = Category(*pair, name, enabled)
        reserved = document.get("reserved_ids", [])
        if not isinstance(reserved, list):
            raise ValueError("reserved_ids must be an array of eight-digit IDs.")
        self.reserved_ids = set()
        for resource_id in reserved:
            split_resource_id(resource_id)
            if resource_id in self.reserved_ids:
                raise ValueError(f"Duplicate reserved ID: {resource_id}.")
            self.reserved_ids.add(resource_id)

    def require(self, major, minor):
        pair = category_pair(major, minor)
        category = self.categories.get(pair)
        if category is None or not category.enabled:
            raise ValueError(f"Category {major:02d}-{minor:02d} is missing or disabled; "
                             "add or enable the actual project category in the catalog.")
        return category


def relative_key(value):
    """Keys are portable, case-sensitive source-relative filenames."""
    _text(value, "assignment file", required=True)
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (path.is_absolute() or ":" in normalized or ".." in path.parts
            or any(part in ("", ".") for part in normalized.split("/"))):
        raise ValueError(f"Assignment file must be source-relative: {value}")
    return path.as_posix()


class Assignments:
    def __init__(self, path):
        self.resources = {}
        for row in _document(path, "resources")["resources"]:
            if not isinstance(row, dict):
                raise ValueError("Each assignment must be an object.")
            key = relative_key(row.get("file"))
            if key in self.resources:
                raise ValueError(f"Duplicate assignment: {key}.")
            pair = category_pair(row.get("major"), row.get("minor"))
            description = _text(row.get("description", ""), "resource description")
            self.resources[key] = {"major": pair[0], "minor": pair[1], "description": description}

    def resolve(self, file, manifest, basename_counts):
        key = file.get("source_relative")
        if key:
            key = relative_key(key)
        elif file.get("original") and manifest.get("source"):
            try:
                key = Path(file["original"]).resolve().relative_to(Path(manifest["source"]).resolve()).as_posix()
            except ValueError as exc:
                raise ValueError(f"Original resource is outside manifest source: {file['original']}") from exc
        else:
            key = None  # Older hand-authored manifests may only have a unique name.
        name = file["name"]
        if key and key in self.resources:
            return self.resources[key]
        if name in self.resources:
            if basename_counts[name] != 1:
                raise ValueError(f"Ambiguous assignment filename {name}; use source-relative file keys.")
            return self.resources[name]
        raise ValueError(f"Missing classification assignment for {key or name}; "
                         "add its file, major, minor and optional description to assignments JSON.")


class IDAllocator:
    def __init__(self, catalog, existing_ids):
        self.catalog = catalog
        self.occupied = set(existing_ids) | catalog.reserved_ids
        self.last_serial = Counter()
        for resource_id in self.occupied:
            try:
                major, minor, serial = split_resource_id(resource_id)
            except ValueError:
                continue  # Historical IDs stay occupied without renumbering.
            self.last_serial[(major, minor)] = max(self.last_serial[(major, minor)], serial)

    def allocate(self, major, minor):
        self.catalog.require(major, minor)
        pair = (major, minor)
        serial = self.last_serial[pair] + 1
        if serial > 9999:
            raise ValueError(f"Category {major:02d}-{minor:02d} has exhausted serial 9999; "
                             "configure another category and assignment before registration.")
        resource_id = major * 1000000 + minor * 10000 + serial
        if resource_id in self.occupied:
            raise ValueError(f"Resource ID collision: {resource_id}.")
        self.occupied.add(resource_id)
        self.last_serial[pair] = serial
        return resource_id
