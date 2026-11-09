"""Local draft discovery and explicit publication through the same API.

A library entry is data, never an imported program: the scan reads one
manifest and one Graph file, and publication uploads only that Graph document.
"""
from __future__ import annotations
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .client_access import ClientConnection
from .resources import ControlPlaneError

LIBRARY_FORMAT = "agentgenome-library-entry/1"
MANIFEST_NAME = "agentgenome.json"
DEFAULT_GRAPH_NAME = "graph.yaml"
DEFAULT_LIBRARY_ROOT = Path.home() / "graph-Agent"


@dataclass(frozen=True)
class LocalGraphEntry:
    name: str
    root: Path
    graph_path: Path
    description: str
    usage_path: Path | None = None


def _invalid(message: str, **details: Any) -> ControlPlaneError:
    return ControlPlaneError("invalid_argument", message, **details)


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _resolve_member(entry_root: Path, relative: str, *, field: str,
                    entry_name: str) -> Path:
    """Resolve one declared relative file and keep it inside the entry."""
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise _invalid("library entry path must be relative and inside the entry",
                       entry=entry_name, field=field, path=relative)
    try:
        resolved = (entry_root / candidate).resolve(strict=True)
    except FileNotFoundError as error:
        raise ControlPlaneError(
            "not_found",
            f"library entry declares {field} but the file is missing",
            entry=entry_name, field=field, path=relative) from error
    except OSError as error:
        raise _invalid("library entry path cannot be resolved",
                       entry=entry_name, field=field, path=relative) from error
    if not _within(resolved, entry_root):
        raise _invalid("library entry path escapes the entry directory",
                       entry=entry_name, field=field, path=relative)
    if not resolved.is_file():
        raise _invalid("library entry path is not a regular file",
                       entry=entry_name, field=field, path=relative)
    return resolved


def _load_manifest(path: Path, entry_name: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _invalid("library entry manifest is not valid JSON",
                       entry=entry_name) from error
    if not isinstance(data, dict):
        raise _invalid("library entry manifest must be a JSON object",
                       entry=entry_name)
    if data.get("format") != LIBRARY_FORMAT:
        raise _invalid("library entry manifest has an unknown format",
                       entry=entry_name,
                       expected_format=LIBRARY_FORMAT)
    for field in ("name", "graph"):
        value = data.get(field)
        if not isinstance(value, str) or not value:
            raise _invalid(f"library entry manifest {field} must be a "
                           "nonempty string", entry=entry_name, field=field)
    description = data.get("description", "")
    if not isinstance(description, str):
        raise _invalid("library entry manifest description must be a string",
                       entry=entry_name, field="description")
    usage = data.get("usage")
    if usage is not None and (not isinstance(usage, str) or not usage):
        raise _invalid("library entry manifest usage must be a nonempty string",
                       entry=entry_name, field="usage")
    return {"name": data["name"], "graph": data["graph"],
            "description": description, "usage": usage}


def _entry(path: Path, resolved: Path) -> LocalGraphEntry | None:
    manifest_path = path / MANIFEST_NAME
    if manifest_path.is_symlink() or manifest_path.exists():
        if (manifest_path.is_symlink()
                and not _within(manifest_path.resolve(), resolved)):
            raise _invalid("library entry manifest escapes the entry directory",
                           entry=path.name, field="manifest")
        if not manifest_path.is_file():
            raise _invalid("library entry manifest is not a regular file",
                           entry=path.name, field="manifest")
        manifest = _load_manifest(manifest_path, path.name)
        graph_path = _resolve_member(resolved, manifest["graph"],
                                     field="graph", entry_name=path.name)
        usage_path = None
        if manifest["usage"] is not None:
            usage_path = _resolve_member(resolved, manifest["usage"],
                                         field="usage", entry_name=path.name)
        return LocalGraphEntry(name=manifest["name"], root=resolved,
                               graph_path=graph_path,
                               description=manifest["description"],
                               usage_path=usage_path)

    graph_candidate = path / DEFAULT_GRAPH_NAME
    if not (graph_candidate.exists() or graph_candidate.is_symlink()):
        return None
    graph_path = _resolve_member(resolved, DEFAULT_GRAPH_NAME,
                                 field="graph", entry_name=path.name)
    return LocalGraphEntry(name=path.name, root=resolved,
                           graph_path=graph_path, description="")


def scan_library(root: str | Path | None = None) -> list[LocalGraphEntry]:
    """Inspect one directory level (default ~/graph-Agent), without writes.

    Hidden entries skipped. Manifest optional; graph.yaml default. Reject
    escaping relative paths/symlinks and malformed entries, naming the entry
    in the error. No automatic publication, recursive file upload or imports.
    """
    library = (DEFAULT_LIBRARY_ROOT if root is None
               else Path(root).expanduser())
    try:
        library_resolved = library.resolve(strict=True)
    except FileNotFoundError as error:
        raise ControlPlaneError("not_found", "graph library does not exist",
                                root=str(library)) from error
    except OSError as error:
        raise _invalid("graph library cannot be resolved",
                       root=str(library)) from error
    if not library_resolved.is_dir():
        raise _invalid("graph library is not a directory", root=str(library))

    entries: list[LocalGraphEntry] = []
    seen: dict[str, Path] = {}
    for child in sorted(library_resolved.iterdir(),
                        key=lambda item: item.name):
        if child.name.startswith(".") or not child.is_dir():
            continue
        try:
            resolved = child.resolve(strict=True)
        except OSError:
            continue
        if not _within(resolved, library_resolved):
            raise _invalid("library entry escapes the library root",
                           entry=child.name)
        if not resolved.is_dir():
            continue
        entry = _entry(child, resolved)
        if entry is None:
            continue
        previous = seen.get(entry.name)
        if previous is not None:
            raise _invalid("duplicate library entry name", name=entry.name,
                           first=str(previous), second=str(entry.root))
        seen[entry.name] = entry.root
        entries.append(entry)
    entries.sort(key=lambda item: item.name)
    return entries


def _publication_source(entry: LocalGraphEntry) -> Path:
    """Revalidate the scanned boundary at read time, before any publish.

    The directory or file can be replaced after scan_library returned; a
    swapped symlink outside the entry must never be uploaded.
    """
    try:
        entry_root = entry.root.resolve(strict=True)
    except FileNotFoundError as error:
        raise ControlPlaneError(
            "not_found", "library entry directory is missing",
            entry=entry.name, path=str(entry.root)) from error
    except OSError as error:
        raise _invalid("library entry directory cannot be resolved",
                       entry=entry.name, path=str(entry.root)) from error
    if entry_root != entry.root:
        raise _invalid("library entry directory changed since scan",
                       entry=entry.name, path=str(entry.root))
    try:
        graph_path = entry.graph_path.resolve(strict=True)
    except FileNotFoundError as error:
        raise ControlPlaneError(
            "not_found", "selected Graph file is missing",
            entry=entry.name, path=str(entry.graph_path)) from error
    except OSError as error:
        raise _invalid("selected Graph file cannot be resolved",
                       entry=entry.name, path=str(entry.graph_path)) from error
    if not _within(graph_path, entry_root):
        raise _invalid("selected Graph file escapes the entry directory",
                       entry=entry.name, path=str(entry.graph_path))
    if not graph_path.is_file():
        raise _invalid("selected Graph file is not a regular file",
                       entry=entry.name, path=str(entry.graph_path))
    return graph_path


def publish_entry(client: ClientConnection, entry: LocalGraphEntry, *,
                  request_id: str, base_revision: int | None = None) -> dict[str, Any]:
    """Read selected YAML, validate/publish using existing APIs and CAS.

    Neither run nor deploy code. A base revision conflict must reach caller.
    The caller owns request_id so a retry reuses one idempotency key.
    """
    if not isinstance(request_id, str) or not request_id:
        raise _invalid("request_id must be a nonempty string")
    if base_revision is not None:
        if (isinstance(base_revision, bool)
                or not isinstance(base_revision, int)
                or base_revision < 0):
            raise _invalid("base_revision must be a non-negative integer")
    source = _publication_source(entry)
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ControlPlaneError("not_found", "selected Graph file cannot be read",
                                entry=entry.name,
                                path=str(source)) from error
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise _invalid("selected Graph file is not valid YAML",
                       entry=entry.name) from error
    if not isinstance(document, dict):
        raise _invalid("selected Graph file must contain a YAML object",
                       entry=entry.name)
    if base_revision is not None:
        document["base_revision"] = base_revision

    client.call("validate_definition", {"document": document})
    return client.call("publish_definition",
                       {"request_id": request_id, "document": document})
