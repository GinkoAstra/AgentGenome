"""Durable content-addressed artifact store.

Bytes are written and fsynced before any transaction references them. Reads
recompute the digest, so a modified object is reported as artifact_mismatch
instead of being delivered as the accepted result.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile

from .resources import ControlPlaneError, artifact_ref, require_text


class ArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        (self.root / "objects" / "sha256").mkdir(parents=True, exist_ok=True)

    def _path(self, sha256: str) -> Path:
        return self.root / "objects" / "sha256" / sha256[:2] / sha256

    def put(self, content: bytes | str, media_type: str) -> dict:
        require_text(media_type, "media_type", max_length=256)
        if isinstance(content, str):
            data = content.encode("utf-8")
        elif isinstance(content, bytes):
            data = content
        else:
            raise ControlPlaneError("invalid_argument", "artifact content must be bytes or text")
        sha256 = hashlib.sha256(data).hexdigest()
        path = self._path(sha256)
        if path.exists():
            existing = path.read_bytes()
            if hashlib.sha256(existing).hexdigest() != sha256:
                raise ControlPlaneError("artifact_mismatch", "stored object does not match its id")
            return artifact_ref({"id": f"sha256:{sha256}", "sha256": sha256,
                                 "size": len(existing), "media_type": media_type})
        path.parent.mkdir(parents=True, exist_ok=True)
        # Concurrent writers of identical bytes need distinct temporaries;
        # mkstemp creates the name exclusively in the destination directory so
        # the final link stays atomic and no write unlinks another's file.
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{sha256}.", suffix=".tmp", dir=path.parent)
        temp = Path(temp_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temp, path)
            except FileExistsError:
                pass
        finally:
            temp.unlink(missing_ok=True)
        _fsync_dir(path.parent)
        return artifact_ref({"id": f"sha256:{sha256}", "sha256": sha256,
                             "size": len(data), "media_type": media_type})

    def read(self, ref: dict) -> bytes:
        checked = artifact_ref(ref)
        path = self._path(checked["sha256"])
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise ControlPlaneError("not_found", "artifact content is missing", id=checked["id"])
        if hashlib.sha256(data).hexdigest() != checked["sha256"] or len(data) != checked["size"]:
            raise ControlPlaneError("artifact_mismatch", "artifact content does not match its ref",
                                    id=checked["id"])
        return data

    def exists(self, ref: dict) -> bool:
        checked = artifact_ref(ref)
        return self._path(checked["sha256"]).is_file()


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
