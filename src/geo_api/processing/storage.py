import os
import shutil
import stat
import uuid
from pathlib import Path

from geo_api.config import Settings


def prepare_temp_root(settings: Settings) -> Path:
    candidate = settings.temp_root.expanduser().absolute()
    try:
        if candidate.is_symlink():
            raise RuntimeError("geo-api temp root must not be a symlink")
        candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
        root_stat = candidate.lstat()
    except OSError as exc:
        raise RuntimeError("geo-api temp root is unavailable") from exc
    if not stat.S_ISDIR(root_stat.st_mode) or root_stat.st_uid != os.getuid():
        raise RuntimeError("geo-api temp root must be an app-owned directory")
    candidate.chmod(0o700)
    return candidate.resolve(strict=True)


def cleanup_abandoned_workspaces(root: Path) -> None:
    root = root.resolve(strict=True)
    for child in root.iterdir():
        if not child.name.startswith("request-"):
            continue
        try:
            uuid.UUID(child.name.removeprefix("request-"))
            info = child.lstat()
            if (
                stat.S_ISDIR(info.st_mode)
                and info.st_uid == os.getuid()
                and child.resolve(strict=True).parent == root
            ):
                shutil.rmtree(child)
        except (OSError, ValueError):
            continue


def create_workspace(root: Path) -> Path:
    workspace = root / f"request-{uuid.uuid4()}"
    workspace.mkdir(mode=0o700)
    return workspace


def remove_workspace(root: Path, workspace: Path) -> None:
    root = root.resolve(strict=True)
    if workspace.is_symlink():
        raise RuntimeError("Refusing to remove a symlink workspace")
    info = workspace.lstat()
    resolved = workspace.resolve(strict=True)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or resolved.parent != root
        or not resolved.name.startswith("request-")
    ):
        raise RuntimeError("Refusing to remove an unowned workspace")
    try:
        uuid.UUID(resolved.name.removeprefix("request-"))
    except ValueError as exc:
        raise RuntimeError("Refusing to remove an unowned workspace") from exc
    shutil.rmtree(resolved)
