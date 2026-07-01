"""Symlink-safe validation for generated output paths."""

from __future__ import annotations

import stat
import os
import secrets
import tempfile
from collections.abc import Mapping
from pathlib import Path
from pathlib import PurePosixPath


class OutputPathError(OSError):
    pass


def _after_parent_open(_path: Path, _fd: int) -> None:
    """Test seam for exercising path replacement after descriptor binding."""


def unsafe_output_parent(path: Path) -> tuple[str, Path] | None:
    """Return the first unsafe component below a trusted control root."""
    parent = Path(path).absolute().parent
    cwd, temporary = Path.cwd(), Path(tempfile.gettempdir())
    roots = (cwd.absolute(), cwd.resolve(), temporary.absolute(), temporary.resolve())
    candidates = [root for root in roots if parent == root or root in parent.parents]
    if not candidates:
        return "outside trusted control roots", parent
    trusted = max(candidates, key=lambda item: len(item.parts))
    current = trusted
    for component in parent.relative_to(trusted).parts:
        current /= component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            return "symbolic-link parent", current
        if not stat.S_ISDIR(mode):
            return "unsafe non-directory parent", current
    return None


def atomic_write_bytes(path: Path, data: bytes) -> None:
    parent_fd, name = _open_output_parent(path)
    temp = f".{name}.{secrets.token_hex(8)}.tmp"
    descriptor = -1
    try:
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise OutputPathError(f"unsafe destination: {path}")
        except FileNotFoundError:
            pass
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             getattr(os, "O_NOFOLLOW", 0), 0o644, dir_fd=parent_fd)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temp, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def atomic_materialize_tree(path: Path, resources: Mapping[str, bytes]) -> None:
    parent_fd, name = _open_output_parent(path)
    staging = f".{name}.staging"
    backup = f".{name}.backup"
    staging_fd = -1
    try:
        _recover_tree_swap(parent_fd, name, staging, backup)
        existing = False
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise OutputPathError(f"unsafe output root: {path}")
            existing = True
        except FileNotFoundError:
            pass
        os.mkdir(staging, 0o755, dir_fd=parent_fd)
        staging_fd = os.open(staging, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
                             getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        for relative, data in sorted(resources.items()):
            _write_tree_member(staging_fd, PurePosixPath(relative), data)
        os.fsync(staging_fd)
        os.close(staging_fd); staging_fd = -1
        if existing:
            os.rename(name, backup, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
            _tree_swap_hook("after_backup")
        os.rename(staging, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
        _tree_swap_hook("after_replace")
        if existing:
            _remove_tree_at(parent_fd, backup)
            os.fsync(parent_fd)
    except BaseException:
        try:
            if _entry_exists(parent_fd, backup):
                if _entry_exists(parent_fd, name):
                    _remove_tree_at(parent_fd, name)
                os.rename(backup, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            if _entry_exists(parent_fd, staging):
                _remove_tree_at(parent_fd, staging)
            os.fsync(parent_fd)
        except OSError:
            pass
        raise
    finally:
        if staging_fd >= 0:
            os.close(staging_fd)
        try:
            _remove_tree_at(parent_fd, staging)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def _recover_tree_swap(parent_fd: int, name: str, staging: str, backup: str) -> None:
    target_exists = _entry_exists(parent_fd, name)
    backup_exists = _entry_exists(parent_fd, backup)
    if backup_exists and not target_exists:
        os.rename(backup, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    elif backup_exists:
        _remove_tree_at(parent_fd, backup)
    if _entry_exists(parent_fd, staging):
        _remove_tree_at(parent_fd, staging)
    os.fsync(parent_fd)


def _entry_exists(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _tree_swap_hook(_label: str) -> None:
    """Test seam for interrupted replacement recovery."""


def _open_output_parent(path: Path) -> tuple[int, str]:
    target = Path(path).absolute()
    unsafe = unsafe_output_parent(target)
    if unsafe is not None:
        reason, component = unsafe
        raise OutputPathError(f"{reason}: {component}")
    trusted = _trusted_root(target.parent)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(trusted, flags)
    try:
        for component in target.parent.relative_to(trusted).parts:
            try:
                next_fd = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(component, 0o755, dir_fd=descriptor)
                next_fd = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
        _after_parent_open(target.parent, descriptor)
        lexical = os.stat(target.parent, follow_symlinks=False)
        bound = os.fstat(descriptor)
        if (not stat.S_ISDIR(lexical.st_mode) or
                (lexical.st_dev, lexical.st_ino) != (bound.st_dev, bound.st_ino)):
            raise OutputPathError(f"output parent changed after opening: {target.parent}")
        return descriptor, target.name
    except BaseException:
        os.close(descriptor)
        raise


def _trusted_root(parent: Path) -> Path:
    cwd, temporary = Path.cwd(), Path(tempfile.gettempdir())
    roots = (cwd.absolute(), cwd.resolve(), temporary.absolute(), temporary.resolve())
    candidates = [root for root in roots if parent == root or root in parent.parents]
    if not candidates:
        raise OutputPathError(f"outside trusted control roots: {parent}")
    return max(candidates, key=lambda item: len(item.parts))


def _write_tree_member(root_fd: int, relative: PurePosixPath, data: bytes) -> None:
    descriptor = os.dup(root_fd)
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        for component in relative.parts[:-1]:
            try:
                next_fd = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(component, 0o755, dir_fd=descriptor)
                next_fd = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor); descriptor = next_fd
        file_fd = os.open(relative.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                          getattr(os, "O_NOFOLLOW", 0), 0o644, dir_fd=descriptor)
        with os.fdopen(file_fd, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_tree_at(parent_fd: int, name: str) -> None:
    directory_fd = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
                           getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
    try:
        for child in os.listdir(directory_fd):
            info = os.stat(child, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                _remove_tree_at(directory_fd, child)
            else:
                os.unlink(child, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    os.rmdir(name, dir_fd=parent_fd)
