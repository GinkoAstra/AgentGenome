"""One managed-file engine for every interactive client entry.

Adapters in :mod:`client_entries` render exactly one owned entry; this module
plans, previews, applies and removes it together with an ownership record.
No discovery, credential, model call, Host, Api or SQLite is imported.

The same engine backs ``pi_integration``, the public ``agentgenome client``
group and the user-callable module entry::

    agentgenome client plan|install|uninstall --client {pi,codex,qoder,claude}
        [--config-root PATH] [--python PATH] [--data-dir PATH]
        [--dry-run]

    python -m node_runtime.control_plane.client_install plan|install|uninstall
        --client-kind {pi,codex,qoder,claude}
        [--config-root PATH] [--python PATH] [--data-dir PATH]
        [--dry-run]

``--client-kind`` remains an alias of ``--client``. ``plan`` is always
zero-write. ``install`` and ``uninstall`` accept ``--dry-run``. Conflicts are
reported as structured JSON with exit code 3; invalid input/errors use exit
code 2.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from . import client_activation, client_entries
from .client_profiles import (CLIENT_PROFILES, InteractiveClientKind,
                              default_activation_root, default_config_root,
                              native_config_path, profile_for)
from .resources import ControlPlaneError

RECORD_FORMAT = "agentgenome-install-record/1"
RECORD_DIRECTORY = ".agentgenome"
RECORD_FILENAME = "install-record.json"
CLIENT_KINDS: tuple[InteractiveClientKind, ...] = tuple(
    profile.kind for profile in CLIENT_PROFILES)

EXIT_OK = 0
EXIT_ERROR = 2
EXIT_CONFLICT = 3


@dataclass(frozen=True)
class InstallTarget:
    client_kind: InteractiveClientKind
    config_root: Path
    python: str
    data_dir: Path | None = None
    activation_root: Path | None = None
    # True only for a caller-supplied --activation-root. Otherwise an existing
    # record's persisted root keeps ownership stable across environments.
    activation_root_explicit: bool = False


@dataclass(frozen=True)
class FileChange:
    path: Path
    before_sha256: str | None
    after_text: str | None  # None deletes an owned, unchanged file.


@dataclass(frozen=True)
class InstallPlan:
    client_kind: InteractiveClientKind
    state: Literal["ready", "unchanged", "conflict"]
    changes: tuple[FileChange, ...]
    activation_notes: tuple[str, ...]
    conflicts: tuple[str, ...] = ()


def record_path(config_root: str | Path,
                client_kind: InteractiveClientKind) -> Path:
    """One record per client kind, so peers never share ownership state."""
    return (Path(config_root).expanduser() / RECORD_DIRECTORY
            / f"{client_kind}-{RECORD_FILENAME}")


def _record_text(target: InstallTarget, entry_digest: str,
                 block_digest: str | None,
                 activation: dict[str, str]) -> str:
    record = {
        "format": RECORD_FORMAT,
        "client_kind": target.client_kind,
        "entry_digest": entry_digest,
        # Exact managed-block digest; TOML hosts use it to detect user content
        # inserted inside the marker block that leaves the table semantics as-is.
        "block_digest": block_digest,
        "python": target.python,
        "data_dir": str(target.data_dir) if target.data_dir is not None else None,
        # Resolved root this installer owned; removal never guesses it again.
        "activation_root": (str(target.activation_root)
                            if target.activation_root is not None else None),
        # Relative activation path -> digest of the file this installer owns.
        "activation": dict(sorted(activation.items())),
    }
    return json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _read_record(config_root: str | Path, client_kind: InteractiveClientKind
                 ) -> tuple[dict[str, Any] | None, str | None]:
    path = record_path(config_root, client_kind)
    sha, text, error = client_entries.read_managed_text(path)
    if error is not None:
        return None, error
    if text is None:
        return None, None
    try:
        record = json.loads(text)
    except ValueError:
        return None, f"install record is not valid JSON: {path}"
    if (not isinstance(record, dict)
            or record.get("format") != RECORD_FORMAT
            or not isinstance(record.get("entry_digest"), str)):
        return None, f"install record has an unknown format: {path}"
    if record.get("client_kind") != client_kind:
        return None, (f"install record belongs to another client kind: {path}")
    block_digest = record.get("block_digest")
    if block_digest is not None and not isinstance(block_digest, str):
        return None, f"install record has an unknown format: {path}"
    activation_root = record.get("activation_root")
    if activation_root is not None:
        if (not isinstance(activation_root, str) or not activation_root
                or not Path(activation_root).is_absolute()):
            return None, f"install record has an unknown format: {path}"
    activation = record.get("activation", {})
    if not isinstance(activation, dict):
        return None, f"install record has an unknown format: {path}"
    for relative, digest in activation.items():
        if not isinstance(relative, str) or not isinstance(digest, str):
            return None, f"install record has an unknown format: {path}"
        try:
            _relative_path(relative)
        except ValueError:
            return None, f"install record has an unknown format: {path}"
    return record, None


def _relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (path.is_absolute()
            or any(part in ("", ".", "..") for part in path.parts)):
        raise ValueError(f"unsafe activation path: {value!r}")
    return path


@dataclass(frozen=True)
class _ActivationPlan:
    changes: tuple[FileChange, ...]
    digests: dict[str, str]
    notes: tuple[str, ...]
    conflicts: tuple[str, ...] = ()


def _owned_activation(record: dict[str, Any] | None) -> dict[str, str]:
    if record is None:
        return {}
    return dict(record.get("activation", {}))


def _activation_root_conflict(target: InstallTarget,
                              record: dict[str, Any] | None) -> str | None:
    if record is None:
        return None
    stored = record.get("activation_root")
    if stored is None:
        if record.get("activation"):
            return ("install record owns activation assets but records no "
                    "activation root; refusing to guess an ownership root")
        # A common-era record owns no activation assets, so adding the shared
        # skill assets is an additive upgrade; ordinary collision checks below
        # still refuse to adopt or overwrite foreign files.
        return None
    if target.activation_root is None:
        return (f"install record owns activation assets under {stored}, but "
                f"the requested {target.client_kind} root has no activation "
                "area; refusing to abandon them")
    if Path(stored) != target.activation_root:
        return (f"install record owns activation assets under {stored}, but "
                f"the requested activation root is {target.activation_root}; "
                "uninstall with the recorded root or reinstall deliberately")
    return None


def _activation_plan(target: InstallTarget, record: dict[str, Any] | None,
                     *, remove: bool) -> _ActivationPlan:
    assets = client_activation.render_activation_assets(target.client_kind)
    root = target.activation_root
    if not assets or root is None:
        return _ActivationPlan((), {}, ())
    owned = _owned_activation(record)
    desired = {str(asset.relative_path): asset.content for asset in assets}
    changes: list[FileChange] = []
    conflicts: list[str] = []
    digests: dict[str, str] = {}

    if remove:
        for relative, digest in sorted(owned.items()):
            path = root / _relative_path(relative)
            sha, text, error = client_entries.read_managed_text(path)
            if error is not None:
                conflicts.append(error)
            elif text is not None and sha != digest:
                conflicts.append(
                    f"activation asset {path} was modified after installation; "
                    "refusing to remove it.")
            elif text is not None:
                changes.append(FileChange(path, sha, None))
        return _ActivationPlan(tuple(changes), {}, (), tuple(conflicts))

    for relative, content in sorted(desired.items()):
        path = root / _relative_path(relative)
        sha, text, error = client_entries.read_managed_text(path)
        if error is not None:
            conflicts.append(error)
            continue
        wanted = client_entries.sha256_text(content)
        digests[relative] = wanted
        if text is None:
            changes.append(FileChange(path, None, content))
        elif sha == wanted:
            # Only this record may keep an identical file. A shared root or a
            # user-authored identical file must not become deletable just
            # because its bytes match ours.
            if relative not in owned:
                conflicts.append(
                    f"activation asset {path} already exists and is not owned "
                    "by this installer; refusing to claim it.")
        elif owned.get(relative) == sha:
            changes.append(FileChange(path, sha, content))
        else:
            conflicts.append(
                f"activation asset {path} already exists and is not owned by "
                "this installer; refusing to overwrite it.")

    for relative, digest in sorted(owned.items()):
        if relative in desired:
            continue
        path = root / _relative_path(relative)
        sha, text, error = client_entries.read_managed_text(path)
        if error is not None:
            conflicts.append(error)
        elif text is not None and sha != digest:
            conflicts.append(
                f"activation asset {path} was modified after installation; "
                "refusing to remove it.")
        elif text is not None:
            changes.append(FileChange(path, sha, None))

    notes: tuple[str, ...] = ()
    if changes or digests:
        trigger = {"pi": "/agentgenome",
                   "codex": "$agentgenome (or /skills)"}.get(
                       target.client_kind, "/agentgenome")
        notes = (
            f"Owned activation assets under: {root}.",
            f"Native trigger after reopen: {trigger}.",
        )
    return _ActivationPlan(tuple(changes), digests, notes, tuple(conflicts))


def installed_record(config_root: str | Path, client_kind: InteractiveClientKind
                     ) -> tuple[dict[str, Any] | None, str | None]:
    """The record this installer wrote for ``client_kind``, if any."""
    return _read_record(config_root, client_kind)


def _with_recorded_root(target: InstallTarget,
                        record: dict[str, Any] | None) -> InstallTarget:
    """Keep removal/update on the root this record actually owns."""
    if record is None or target.activation_root_explicit:
        return target
    stored = record.get("activation_root")
    if stored is None:
        return target
    return replace(target, activation_root=Path(stored))


def _plan(target: InstallTarget, *, remove: bool) -> InstallPlan:
    profile_for(target.client_kind)
    record, record_error = _read_record(target.config_root, target.client_kind)
    if record_error is not None:
        return InstallPlan(target.client_kind, "conflict", (),
                           (), (record_error,))
    target = _with_recorded_root(target, record)
    root_conflict = _activation_root_conflict(target, record)
    if root_conflict is not None:
        return InstallPlan(target.client_kind, "conflict", (), (),
                           (root_conflict,))
    if remove:
        native = client_entries.plan_uninstall(target, record)
    else:
        native = client_entries.plan_install(target, record)
    if native.conflicts:
        return InstallPlan(target.client_kind, "conflict", (),
                           native.notes, native.conflicts)

    activation = _activation_plan(target, record, remove=remove)
    notes = native.notes + activation.notes
    if activation.conflicts:
        return InstallPlan(target.client_kind, "conflict", (), notes,
                           activation.conflicts)

    changes: list[FileChange] = []
    if native.changed:
        changes.append(FileChange(native.path, native.before_sha256,
                                  native.content))
    changes.extend(activation.changes)
    path = record_path(target.config_root, target.client_kind)
    if remove:
        sha, text, error = client_entries.read_managed_text(path)
        if error is not None:
            return InstallPlan(target.client_kind, "conflict", (), notes,
                               (error,))
        if text is not None:
            changes.append(FileChange(path, sha, None))
    else:
        assert native.entry_digest is not None
        sha, text, error = client_entries.read_managed_text(path)
        if error is not None:
            return InstallPlan(target.client_kind, "conflict", (), notes,
                               (error,))
        wanted = _record_text(target, native.entry_digest, native.block_digest,
                              activation.digests)
        if wanted != text:
            changes.append(FileChange(path, sha, wanted))
    state = "ready" if changes else "unchanged"
    return InstallPlan(target.client_kind, state, tuple(changes), notes)


def plan_install(target: InstallTarget) -> InstallPlan:
    """Read the native config and plan one owned entry; never write."""
    return _plan(target, remove=False)


def plan_uninstall(target: InstallTarget) -> InstallPlan:
    """Plan removal of only our unmodified entry; never write."""
    return _plan(target, remove=True)


def _atomic_write(path: Path, text: str) -> None:
    """Replace atomically without widening the target's permissions.

    The temporary file is created 0600 so merged native content is never
    group/world-readable, then chmodded to the existing file mode (0600 for a
    new file) immediately before it replaces the target.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = 0o600
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    try:
        descriptor = os.open(temporary,
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def apply_plan(plan: InstallPlan) -> None:
    """Recheck every preimage, then apply only the planned changes."""
    if plan.state == "conflict":
        raise ControlPlaneError(
            "invalid_argument",
            "the install plan has conflicts; nothing was written",
            client_kind=plan.client_kind, conflicts=list(plan.conflicts))
    observed: list[str | None] = []
    for change in plan.changes:
        sha, text, error = client_entries.read_managed_text(change.path)
        if error is not None:
            raise ControlPlaneError("invalid_argument", error,
                                    path=str(change.path))
        if sha != change.before_sha256:
            raise ControlPlaneError(
                "invalid_argument",
                "a managed file changed while installing; nothing was written",
                path=str(change.path))
        observed.append(text)
    for change, current in zip(plan.changes, observed):
        try:
            if change.after_text is None:
                if current is not None:
                    change.path.unlink()
            elif change.after_text != current:
                _atomic_write(change.path, change.after_text)
        except OSError as error:
            raise ControlPlaneError(
                "internal", "cannot write the managed client entry",
                path=str(change.path), error=repr(error)) from error


def _target(client_kind: InteractiveClientKind, config_root: str | Path | None,
            python: str, data_dir: str | Path | None,
            activation_root: str | Path | None = None) -> InstallTarget:
    root = (Path(config_root).expanduser() if config_root is not None
            else default_config_root(client_kind))
    try:
        root = root.resolve()
    except (OSError, RuntimeError) as error:
        raise ControlPlaneError(
            "invalid_argument", "config root cannot be resolved",
            config_root=str(config_root)) from error
    if activation_root is not None and client_kind == "pi":
        raise ControlPlaneError(
            "invalid_argument",
            "Pi uses its native /agentgenome command and installs no skill",
            activation_root=str(activation_root))
    chosen_activation = (Path(activation_root).expanduser()
                         if activation_root is not None
                         else default_activation_root(
                             client_kind, root,
                             explicit=config_root is not None))
    if chosen_activation is not None:
        try:
            chosen_activation = chosen_activation.resolve()
        except (OSError, RuntimeError) as error:
            raise ControlPlaneError(
                "invalid_argument", "activation root cannot be resolved",
                activation_root=str(activation_root)) from error
    from . import discovery
    chosen_data = (discovery.resolve_data_dir(data_dir)
                   if data_dir is not None else None)
    interpreter = os.path.abspath(os.path.expanduser(python))
    return InstallTarget(client_kind, root, interpreter, chosen_data,
                         chosen_activation, activation_root is not None)


def _action(change: FileChange) -> str:
    if change.after_text is None:
        return "delete"
    if change.before_sha256 is None:
        return "create"
    if change.before_sha256 == client_entries.sha256_text(change.after_text):
        return "unchanged"
    return "update"


def _payload(command: str, target: InstallTarget, plan: InstallPlan,
             *, dry_run: bool) -> dict[str, Any]:
    activation_root = target.activation_root
    if not target.activation_root_explicit:
        record, _ = _read_record(target.config_root, target.client_kind)
        recorded = None if record is None else record.get("activation_root")
        if recorded is not None:
            activation_root = Path(recorded)
    return {
        "ok": plan.state != "conflict",
        "command": command,
        "client_kind": target.client_kind,
        "config_root": str(target.config_root),
        "native_path": str(native_config_path(target.client_kind,
                                              target.config_root)),
        "python": target.python,
        "data_dir": str(target.data_dir) if target.data_dir is not None else None,
        "activation_root": (str(activation_root)
                            if activation_root is not None else None),
        "dry_run": dry_run,
        "state": plan.state,
        "changes": [{
            "path": str(change.path),
            "action": _action(change),
            "before_sha256": change.before_sha256,
            "after_sha256": (None if change.after_text is None
                             else client_entries.sha256_text(change.after_text)),
        } for change in plan.changes],
        "conflicts": list(plan.conflicts),
        "activation_notes": list(plan.activation_notes),
    }


def _error_payload(command: str, error: ControlPlaneError) -> dict[str, Any]:
    return {
        "ok": False,
        "command": command,
        "error": {"code": error.code, "message": error.message,
                  "details": error.details},
    }


def _add_commands(sub: argparse._SubParsersAction) -> None:
    for name in ("plan", "install", "uninstall"):
        item = sub.add_parser(name)
        item.add_argument("--client-kind", "--client", dest="client_kind",
                          required=True, choices=CLIENT_KINDS, metavar="KIND",
                          help="interactive client kind")
        item.add_argument("--config-root", default=None,
                          help="explicit isolated config root")
        item.add_argument("--python", default=sys.executable,
                          help="interpreter recorded in the native entry "
                               "(default: this interpreter)")
        item.add_argument("--data-dir", default=None,
                          help="optional isolated service data directory")
        item.add_argument("--activation-root", default=None,
                          help="explicit skill discovery root for non-Pi hosts "
                               "(default: the host's documented skills root)")
        if name != "plan":
            item.add_argument("--dry-run", action="store_true",
                              help="preview only; write nothing")


def _parser(prog: str = "python -m node_runtime.control_plane.client_install"
            ) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Plan, install or remove the AgentGenome entry for one "
                    "interactive CLI using an isolated config root.")
    sub = parser.add_subparsers(dest="command", required=True)
    _add_commands(sub)
    return parser


def add_public_group(sub: argparse._SubParsersAction) -> None:
    """Attach ``agentgenome client {plan,install,uninstall}`` to a root parser."""
    client = sub.add_parser(
        "client", help="plan, install or remove one interactive CLI entry")
    commands = client.add_subparsers(dest="command", required=True)
    _add_commands(commands)


def main(argv: list[str] | None = None,
         prog: str = "python -m node_runtime.control_plane.client_install"
         ) -> int:
    args = _parser(prog).parse_args(argv)
    try:
        target = _target(args.client_kind, args.config_root, args.python,
                         args.data_dir, args.activation_root)
        if args.command == "plan":
            plan = plan_install(target)
            payload = _payload("plan", target, plan, dry_run=True)
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return EXIT_CONFLICT if plan.state == "conflict" else EXIT_OK
        if args.command == "install":
            plan = plan_install(target)
            payload = _payload("install", target, plan, dry_run=args.dry_run)
            if plan.state == "conflict":
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
                return EXIT_CONFLICT
            if not args.dry_run:
                apply_plan(plan)
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return EXIT_OK
        plan = plan_uninstall(target)
        payload = _payload("uninstall", target, plan, dry_run=args.dry_run)
        if plan.state == "conflict":
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return EXIT_CONFLICT
        if not args.dry_run:
            apply_plan(plan)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return EXIT_OK
    except ControlPlaneError as error:
        print(json.dumps(_error_payload(args.command, error),
                         ensure_ascii=False, sort_keys=True),
              file=sys.stderr)
        return EXIT_ERROR
    except (OSError, RuntimeError, ValueError) as error:
        payload = {
            "ok": False,
            "command": args.command,
            "error": {"code": "internal", "message": str(error),
                      "details": {}},
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True),
              file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
