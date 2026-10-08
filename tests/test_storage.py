from __future__ import annotations

import uuid
from pathlib import Path

from geo_api.config import Settings
from geo_api.processing.storage import (
    cleanup_abandoned_workspaces,
    create_workspace,
    prepare_temp_root,
)


def test_startup_cleanup_removes_recent_abandoned_workspaces(tmp_path: Path) -> None:
    root = prepare_temp_root(Settings(_env_file=None, temp_root=tmp_path / "private"))
    abandoned = create_workspace(root)
    (abandoned / "source.data").write_text("private upload", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    unsafe_link = root / f"request-{uuid.uuid4()}"
    unsafe_link.symlink_to(outside, target_is_directory=True)
    unrelated = root / "notes"
    unrelated.mkdir()

    cleanup_abandoned_workspaces(root)

    assert not abandoned.exists()
    assert unsafe_link.is_symlink()
    assert (outside / "keep.txt").exists()
    assert unrelated.exists()
