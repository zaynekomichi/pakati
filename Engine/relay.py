#!/usr/bin/env python3
"""Local, versioned Git task handoffs. Uses only Python's standard library."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid


class RelayError(Exception):
    pass


TASK_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
VERSION_RE = re.compile(r"[0-9]{8}T[0-9]{12}Z-[a-f0-9]{8}\Z")
SCHEMA = 1


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


def git(project, *args, check=True, input_data=None):
    env = os.environ.copy()
    # A calling hook must not accidentally redirect Git to its own repository.
    for key in list(env):
        if key.startswith("GIT_"):
            env.pop(key)
    env["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C", str(project), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, input=input_data,
    )
    if check and result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()[-2000:]
        raise RelayError(f"Git {' '.join(args[:2])} failed: {detail}")
    return result.stdout if check else result


def json_read(path):
    if path.is_symlink() or not path.is_file():
        raise RelayError(f"Missing or unsafe file: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise RelayError(f"Invalid JSON in {path}: {exc}") from exc


def save_bytes(path, data):
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def save_json(path, value):
    save_bytes(path, (json.dumps(value, indent=2, ensure_ascii=True) + "\n").encode())


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sha_file(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return {"sha256": digest.hexdigest(), "size": size}


def safe_relative(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise RelayError(f"Unsupported or unsafe relative filename: {value!r}")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts) or parts[0].lower() == ".git":
        raise RelayError(f"Unsafe relative filename: {value!r}")
    if PurePosixPath(value).is_absolute():
        raise RelayError(f"Unsafe absolute filename: {value!r}")
    return parts


def secret_name(value):
    name = PurePosixPath(value).name.lower()
    if name in (".env.example", ".env.sample", ".env.template"):
        return False
    return (
        name == ".env" or name.startswith(".env.")
        or name in ("credentials", "credential", "secrets", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
        or name.startswith(("credentials.", "secrets.", "private_key", "private-key"))
        or name.endswith((".key", ".pem", ".p12", ".pfx", ".jks", ".keystore"))
    )


def project_info(project):
    project = Path(project).expanduser().resolve()
    if not project.is_dir():
        raise RelayError(f"Project directory does not exist: {project}")
    root = Path(os.fsdecode(git(project, "rev-parse", "--show-toplevel").strip())).resolve()
    common = Path(os.fsdecode(git(root, "rev-parse", "--git-common-dir").strip()))
    if not common.is_absolute():
        common = root / common
    common = common.resolve()
    head = git(root, "rev-parse", "--verify", "HEAD").decode().strip()
    branch_result = git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    return root, common, {
        "path": str(root), "common_dir": str(common),
        "identity": hashlib.sha256(os.fsencode(str(common))).hexdigest(),
        "head": head,
        "branch": branch_result.stdout.decode("utf-8", "replace").strip() if branch_result.returncode == 0 else None,
        "commit_subject": git(root, "log", "-1", "--format=%s", head).decode("utf-8", "replace").strip(),
    }


def store_path(args, root=None, common=None):
    if args.store:
        path = Path(args.store).expanduser().resolve()
    else:
        if common is None:
            if not getattr(args, "project", None):
                raise RelayError("Supply --store, or --project to find that repository's default store.")
            root, common, _ = project_info(args.project)
        path = common / "agent-relay"
    if root and path.is_relative_to(root) and not path.is_relative_to(common):
        raise RelayError("The store must be outside the checkout or inside its Git common directory (the default).")
    return path


def task_dir(store, task):
    if not TASK_RE.fullmatch(task):
        raise RelayError("Task IDs must be 1–64 letters, digits, underscores or hyphens, starting with a letter or digit.")
    path = store / "tasks" / task
    if path.is_symlink() or (store / "tasks").is_symlink():
        raise RelayError(f"Task storage cannot be a symlink: {path}")
    return path


@contextlib.contextmanager
def task_lock(path):
    try:
        import fcntl
    except ImportError as exc:
        raise RelayError("This prototype's checkpoint locking requires macOS or Linux.") from exc
    path.mkdir(parents=True, exist_ok=True)
    fd = os.open(path / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    deadline = time.monotonic() + 15
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RelayError("Another checkpoint still holds the task lock; retry when it finishes.")
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)


def latest_version(task_path):
    pointer = task_path / "latest.json"
    if not pointer.exists() and not pointer.is_symlink():
        return None
    data = json_read(pointer)
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise RelayError(f"Invalid latest checkpoint pointer: {pointer}")
    return version


def load_capsule(store, task, version=None, verify=True):
    task_path = task_dir(store, task)
    version = version or latest_version(task_path)
    if version is None:
        raise RelayError(f"No checkpoint exists for task {task!r} in {store}.")
    if not VERSION_RE.fullmatch(version):
        raise RelayError("Invalid checkpoint version.")
    path = task_path / "versions" / version
    if path.is_symlink() or (task_path / "versions").is_symlink():
        raise RelayError(f"Unsafe checkpoint directory: {path}")
    metadata = json_read(path / "metadata.json")
    if not isinstance(metadata, dict) or metadata.get("schema_version") != SCHEMA or metadata.get("task") != task or metadata.get("version") != version:
        raise RelayError(f"Checkpoint metadata does not match task/version: {path}")
    project = metadata.get("project")
    if not isinstance(project, dict) or any(not isinstance(project.get(key), str) for key in ("path", "common_dir", "identity", "head", "commit_subject")):
        raise RelayError("Invalid project metadata.")
    if any(not isinstance(metadata.get(key), str) for key in ("snapshot_at", "notes_at", "agent")):
        raise RelayError("Invalid checkpoint timestamps or agent metadata.")
    excluded = metadata.get("excluded_untracked")
    if not isinstance(excluded, list) or any(not isinstance(name, str) for name in excluded):
        raise RelayError("Invalid excluded-file metadata.")
    artifacts = metadata.get("artifacts")
    required = {"notes.md", "snapshot.bundle", "staged.patch", "unstaged.patch"}
    if not isinstance(artifacts, dict) or not required.issubset(artifacts):
        raise RelayError(f"Incomplete artifact manifest: {path}")
    untracked = metadata.get("untracked")
    if not isinstance(untracked, list):
        raise RelayError("Invalid untracked file manifest.")
    names = set()
    for item in untracked:
        if not isinstance(item, dict):
            raise RelayError("Invalid untracked file entry.")
        safe_relative(item.get("path"))
        if item["path"] in names:
            raise RelayError("Duplicate untracked filename in checkpoint.")
        names.add(item["path"])
        artifact = item.get("artifact")
        if not isinstance(artifact, str) or not re.fullmatch(r"untracked/[0-9]{6}\.bin", artifact) or artifact not in artifacts:
            raise RelayError("Invalid untracked artifact reference.")
        if type(item.get("mode")) is not int or not 0 <= item["mode"] <= 0o777:
            raise RelayError("Invalid untracked file mode.")
    if set(artifacts) != required | {item["artifact"] for item in untracked}:
        raise RelayError("Unexpected artifacts in checkpoint manifest.")
    if verify:
        for name, expected in artifacts.items():
            file_path = path / name
            if file_path.is_symlink() or not file_path.is_file() or (file_path.parent != path and file_path.parent.is_symlink()):
                raise RelayError(f"Missing or unsafe artifact: {file_path}")
            if not isinstance(expected, dict) or sha_file(file_path) != expected:
                raise RelayError(f"Artifact integrity check failed: {file_path}")
    try:
        notes = (path / "notes.md").read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise RelayError("Checkpoint notes are not UTF-8.") from exc
    return path, metadata, notes


def validate_repo(root):
    if git(root, "ls-files", "--unmerged", "-z"):
        raise RelayError("Resolve the repository's merge conflicts before checkpointing; unmerged indexes are unsupported.")
    if git(root, "diff", "--name-only", "--diff-filter=A", "-z"):
        raise RelayError("Intent-to-add files are unsupported. Stage their contents with git add, or remove their intent-to-add entry before checkpointing.")
    flags = git(root, "ls-files", "-v", "-z")
    if any(entry[:1] == b"S" or entry[:1].islower() for entry in flags.split(b"\0") if entry):
        raise RelayError("Sparse checkouts, skip-worktree and assume-unchanged index flags are unsupported because they can hide edited files. Clear those flags or use a full checkout before checkpointing.")
    index = git(root, "ls-files", "--stage", "-z")
    tree = git(root, "ls-tree", "-r", "-z", "HEAD")
    if any(entry.startswith(b"160000 ") for entry in index.split(b"\0") + tree.split(b"\0")):
        raise RelayError("Submodules are unsupported: a parent repository's Git bundle cannot preserve submodule contents.")
    attribute_paths = set()
    for entry in tree.split(b"\0"):
        if b"\t" in entry:
            name = entry.split(b"\t", 1)[1]
            if name.rsplit(b"/", 1)[-1] == b".gitattributes":
                data = git(root, "show", "HEAD:" + os.fsdecode(name))
                if re.search(rb"filter\s*=\s*lfs\b", data):
                    raise RelayError("Git LFS repositories are unsupported: bundles do not contain LFS file objects.")
                attribute_paths.add(os.fsdecode(name))
    tracked_files = git(root, "ls-files", "-z")
    for options in ([], ["--cached"]):
        attributes = git(root, "check-attr", *options, "-z", "--stdin", "filter", input_data=tracked_files).split(b"\0")
        if b"lfs" in attributes[2::3]:
            raise RelayError("Git LFS repositories are unsupported: bundles do not contain LFS file objects.")
    for name in tracked_files.split(b"\0"):
        if name and name.rsplit(b"/", 1)[-1] == b".gitattributes":
            name_text = os.fsdecode(name)
            data = git(root, "show", ":" + name_text, check=False)
            if data.returncode == 0 and re.search(rb"filter\s*=\s*lfs\b", data.stdout):
                raise RelayError("Git LFS repositories are unsupported: bundles do not contain LFS file objects.")
            attribute_paths.add(name_text)
    for name in attribute_paths:
        path = root / name
        if path.is_file() and re.search(rb"filter\s*=\s*lfs\b", path.read_bytes()):
            raise RelayError("Git LFS repositories are unsupported: bundles do not contain LFS file objects.")


def state(root):
    return {
        "head": git(root, "rev-parse", "HEAD"),
        "index": git(root, "ls-files", "--stage", "-z"),
        "staged": git(root, "diff", "--cached", "--binary", "--full-index", "--src-prefix=a/", "--dst-prefix=b/", "--no-ext-diff", "--no-textconv", "HEAD", "--"),
        "unstaged": git(root, "diff", "--binary", "--full-index", "--src-prefix=a/", "--dst-prefix=b/", "--no-ext-diff", "--no-textconv", "--"),
        "untracked": git(root, "ls-files", "--others", "--exclude-standard", "-z"),
    }


@contextlib.contextmanager
def open_source(root, relative):
    """Open through directory descriptors, rejecting links in every component."""
    parts = safe_relative(relative)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise RelayError(f"Untracked file is not a regular file: {relative!r}. Remove, ignore or separately handle it.")
            yield stream
    except OSError as exc:
        raise RelayError(f"Cannot safely capture untracked file {relative!r}; symlinks and nested repositories are unsupported. Remove, ignore or separately handle it. ({exc.strerror})") from exc
    finally:
        os.close(fd)


def copy_untracked(root, relative, destination=None):
    digest = hashlib.sha256()
    size = 0
    with open_source(root, relative) as source:
        before = os.fstat(source.fileno())
        sink = destination.open("xb") if destination else contextlib.nullcontext(None)
        with sink as output:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                if output:
                    output.write(block)
                digest.update(block)
                size += len(block)
            if output:
                output.flush()
                os.fsync(output.fileno())
        after = os.fstat(source.fileno())
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise RelayError(f"File changed while checkpointing: {relative!r}. Stop the other writer and retry.")
    return {"size": size, "sha256": digest.hexdigest(), "mode": stat.S_IMODE(after.st_mode) & 0o777}


def checkpoint(args):
    root, common, project = project_info(args.project)
    store = store_path(args, root, common)
    task_path = task_dir(store, args.task)
    with task_lock(task_path):
        previous = latest_version(task_path)
        if args.expect_version is not None and args.expect_version != (previous or "none"):
            raise RelayError("The latest checkpoint changed since it was read. Re-read it before checkpointing this checkout.")
        version = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
        snapshot_at = now()
        notes_reused = False
        if args.notes:
            note_path = Path(args.notes).expanduser().resolve()
            notes = note_path.read_bytes()
            try:
                text_notes = notes.decode("utf-8")
            except UnicodeError as exc:
                raise RelayError("Notes must be UTF-8 Markdown.") from exc
            if not text_notes.strip():
                raise RelayError("Notes cannot be empty; describe the goal, progress and next steps.")
            notes_at, notes_source_version = snapshot_at, None
            if previous:
                prior_path, prior, _ = load_capsule(store, args.task, previous)
                if notes == (prior_path / "notes.md").read_bytes():
                    notes_at, notes_source_version = prior["notes_at"], previous
                    notes_reused = True
        elif previous:
            prior_path, prior, _ = load_capsule(store, args.task, previous)
            notes = (prior_path / "notes.md").read_bytes()
            notes_at, notes_source_version = prior["notes_at"], previous
            notes_reused = True
        else:
            raise RelayError("The first checkpoint requires --notes FILE with the goal, progress and next steps.")
        validate_repo(root)
        before = state(root)
        if before["head"].decode().strip() != project["head"]:
            raise RelayError("HEAD changed before checkpointing. Retry after the other writer stops.")
        versions = task_path / "versions"
        versions.mkdir(exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".pending-", dir=versions))
        try:
            save_bytes(temporary / "notes.md", notes)
            save_bytes(temporary / "staged.patch", before["staged"])
            save_bytes(temporary / "unstaged.patch", before["unstaged"])
            git(root, "bundle", "create", str(temporary / "snapshot.bundle"), "HEAD")
            git(root, "bundle", "verify", str(temporary / "snapshot.bundle"))
            with (temporary / "snapshot.bundle").open("rb") as bundle:
                os.fsync(bundle.fileno())
            artifacts = {name: sha_file(temporary / name) for name in ("notes.md", "staged.patch", "unstaged.patch", "snapshot.bundle")}
            untracked, excluded = [], []
            (temporary / "untracked").mkdir()
            for filename in before["untracked"].split(b"\0"):
                if not filename:
                    continue
                name = os.fsdecode(filename)
                if secret_name(name):
                    excluded.append(name)
                    continue
                artifact = f"untracked/{len(untracked):06d}.bin"
                info = copy_untracked(root, name, temporary / artifact)
                untracked.append({"path": name, "artifact": artifact, **info})
                artifacts[artifact] = {key: info[key] for key in ("sha256", "size")}
            for item in untracked:
                if copy_untracked(root, item["path"]) != {key: item[key] for key in ("size", "sha256", "mode")}:
                    raise RelayError(f"Untracked file changed during checkpointing: {item['path']!r}. Retry; the previous checkpoint is intact.")
            if state(root) != before:
                raise RelayError("Repository changed during checkpointing. Stop the other writer and retry; the previous checkpoint is intact.")
            metadata = {
                "schema_version": SCHEMA, "task": args.task, "version": version,
                "snapshot_at": snapshot_at, "agent": args.agent,
                "notes_at": notes_at, "notes_source_version": notes_source_version,
                "project": project, "artifacts": artifacts, "untracked": untracked,
                "excluded_untracked": excluded,
                "index_preservation": "staged and unstaged contents; index flags and extensions are not copied",
            }
            save_json(temporary / "metadata.json", metadata)
            fsync_dir(temporary / "untracked")
            fsync_dir(temporary)
            final = versions / version
            temporary.rename(final)
            fsync_dir(versions)
            pointer_temp = task_path / (".latest-" + uuid.uuid4().hex + ".json")
            try:
                save_json(pointer_temp, {"version": version})
                os.replace(pointer_temp, task_path / "latest.json")
                fsync_dir(task_path)
            finally:
                pointer_temp.unlink(missing_ok=True)
            return {"task": args.task, "version": version, "store": str(store), "capsule_path": str(final), "snapshot_at": snapshot_at, "notes_at": notes_at, "notes_reused": notes_reused, "excluded_untracked": excluded}
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)


def show(args):
    store = store_path(args)
    path, metadata, notes = load_capsule(store, args.task, args.version)
    return {"store": str(store), "capsule_path": str(path), "metadata": metadata, "notes": notes}


def restore(args):
    store = store_path(args)
    path, metadata, _ = load_capsule(store, args.task, args.version)
    raw_into = Path(args.into).expanduser()
    if not raw_into.is_absolute():
        raise RelayError("--into must be an absolute path to a new, absent directory.")
    if raw_into.exists() or raw_into.is_symlink():
        raise RelayError(f"Restore destination already exists; choose a new directory: {raw_into}")
    parent = raw_into.parent.resolve()
    if not parent.is_dir():
        raise RelayError(f"Restore destination's parent directory must exist: {parent}")
    into = parent / raw_into.name
    if not raw_into.name or into.name in (".", "..") or into.exists() or into.is_symlink():
        raise RelayError(f"Unsafe or existing restore destination: {into}")
    head = metadata.get("project", {}).get("head")
    if not isinstance(head, str) or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", head):
        raise RelayError("Invalid source HEAD in checkpoint metadata.")
    temporary = Path(tempfile.mkdtemp(prefix=".relay-restore-", dir=parent))
    try:
        git(temporary, "clone", "--quiet", "--no-checkout", "--", str(path / "snapshot.bundle"), str(temporary))
        git(temporary, "bundle", "verify", str(path / "snapshot.bundle"))
        git(temporary, "checkout", "--quiet", "--detach", head)
        for name, index in (("staged.patch", True), ("unstaged.patch", False)):
            if metadata["artifacts"][name]["size"]:
                options = ["--index"] if index else []
                git(temporary, "apply", "--binary", "--whitespace=nowarn", *options, str(path / name))
        for item in metadata["untracked"]:
            parts = safe_relative(item["path"])
            folder = temporary
            for part in parts[:-1]:
                folder = folder / part
                if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
                    raise RelayError(f"Untracked path conflicts with a restored tracked file: {item['path']!r}")
                folder.mkdir(exist_ok=True)
            destination = folder / parts[-1]
            if destination.exists() or destination.is_symlink():
                raise RelayError(f"Untracked file would overwrite a restored file: {item['path']!r}")
            with destination.open("xb") as output, (path / item["artifact"]).open("rb") as source:
                shutil.copyfileobj(source, output)
            destination.chmod(item["mode"])
        # Restoring cannot change captured config/code merely to record adoption.
        save_json(temporary / ".git" / "agent-relay-restore.json", {
            "schema_version": SCHEMA, "task": args.task, "version": metadata["version"],
            "store": str(store), "head": head, "restored_at": now(),
        })
        # mkdir is the exclusive reservation: an existing directory, even empty,
        # cannot be replaced if another restore wins the race to this pathname.
        try:
            into.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise RelayError(f"Restore destination was created by another process: {into}") from exc
        try:
            temporary.rename(into)
        except BaseException:
            # Remove only the empty directory we reserved; never recursively
            # remove a destination another process may now be writing into.
            try:
                into.rmdir()
            except OSError:
                pass
            raise
        fsync_dir(parent)
        return {"task": args.task, "version": metadata["version"], "store": str(store), "project": str(into), "head": head, "notes_path": str(path / "notes.md"), "notes_at": metadata["notes_at"], "snapshot_at": metadata["snapshot_at"], "excluded_untracked": metadata["excluded_untracked"]}
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    for name in ("checkpoint", "show", "restore"):
        command = commands.add_parser(name)
        command.add_argument("--task", required=True, help="Stable shared task ID")
        command.add_argument("--store", help="Shared capsule directory; default: Git common-dir/agent-relay")
        if name == "checkpoint":
            command.add_argument("--project", required=True, help="Source Git checkout")
            command.add_argument("--agent", choices=("codex", "claude"), required=True)
            command.add_argument("--notes", help="UTF-8 Markdown goal/progress/next steps; omit only to reuse existing notes")
            command.add_argument("--expect-version", help="Require this latest version under lock; 'none' requires no existing checkpoint")
            command.set_defaults(run=checkpoint)
        else:
            command.add_argument("--project", help="Git checkout used to locate the default store")
            command.add_argument("--version", help="Specific immutable version; default: latest")
            if name == "restore":
                command.add_argument("--into", required=True, help="Absolute path to a new, absent checkout directory")
                command.set_defaults(run=restore)
            else:
                command.set_defaults(run=show)
    return cli


def main():
    args = parser().parse_args()
    try:
        result = args.run(args)
    except (RelayError, OSError, KeyError, TypeError) as exc:
        print(f"relay: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
