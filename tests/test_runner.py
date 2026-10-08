from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from geo_api.config import Settings
from geo_api.processing.errors import ProcessingFailure
from geo_api.processing.runner import (
    _terminate,
    iter_feature_results,
    run_processor,
)


def _install_fake_child(monkeypatch: pytest.MonkeyPatch, program: str) -> None:
    original = asyncio.create_subprocess_exec

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        return await original(sys.executable, "-c", program, *args[3:], **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)


def test_child_protocol_caps_raw_records_and_json_depth(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        max_feature_output_bytes=128,
        max_child_output_bytes=1024,
    )
    path = tmp_path / "features.jsonl"
    path.write_bytes(b"x" * 129 + b"\n")
    with pytest.raises(ProcessingFailure, match="record exceeds"):
        list(iter_feature_results(path, settings))

    path.write_bytes(b"{" * 33 + b"0" + b"}" * 33 + b"\n")
    with pytest.raises(ProcessingFailure, match="nesting exceeds"):
        list(iter_feature_results(path, settings))


@pytest.mark.asyncio
async def test_run_processor_times_out_and_reaps_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_child(monkeypatch, "import time; time.sleep(60)")
    workspace = tmp_path / "timeout"
    workspace.mkdir()

    with pytest.raises(ProcessingFailure) as failure:
        await run_processor(
            tmp_path / "input.kml",
            "KML",
            workspace,
            Settings(_env_file=None, child_timeout_seconds=1),
        )

    assert failure.value.code == "PROCESSING_TIMEOUT"


@pytest.mark.asyncio
async def test_run_processor_reports_crashes_without_exposing_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _install_fake_child(
        monkeypatch, "import sys; sys.stderr.write('private child detail'); sys.exit(7)"
    )
    workspace = tmp_path / "crash"
    workspace.mkdir()

    with pytest.raises(ProcessingFailure) as failure:
        await run_processor(tmp_path / "input.kml", "KML", workspace, Settings(_env_file=None))

    assert failure.value.code == "PROCESSOR_CRASHED"
    assert failure.value.details == {"exit_code": 7}
    assert "private child detail" not in caplog.text


@pytest.mark.asyncio
async def test_run_processor_offloads_result_validation_from_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = {
        "protocol_version": 1,
        "format": "KML",
        "status": "COMPLETED",
        "source_crs": {"name": "WGS 84"},
        "feature_count": 0,
        "measured_count": 0,
        "not_applicable_count": 0,
        "unsupported_count": 0,
        "error_count": 0,
        "has_issues": False,
    }
    program = (
        "import json, pathlib, sys; root=pathlib.Path(sys.argv[2]); "
        f"root.joinpath('manifest.json').write_text({json.dumps(manifest)!r}); "
        "root.joinpath('features.jsonl').write_bytes(b'')"
    )
    _install_fake_child(monkeypatch, program)
    entered = threading.Event()
    release = threading.Event()

    def slow_validator(*args: Any) -> None:
        entered.set()
        release.wait(timeout=2)

    monkeypatch.setattr("geo_api.processing.runner._validate_completed_output", slow_validator)
    workspace = tmp_path / "validation"
    workspace.mkdir()
    task = asyncio.create_task(
        run_processor(tmp_path / "input.kml", "KML", workspace, Settings(_env_file=None))
    )

    assert await asyncio.to_thread(entered.wait, 2)
    await asyncio.sleep(0.01)
    assert not task.done()
    release.set()
    result = await task
    assert result.manifest.feature_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result_kind", ["missing_manifest", "truncated", "count_mismatch", "order"]
)
async def test_run_processor_rejects_malformed_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result_kind: str
) -> None:
    workspace = tmp_path / result_kind
    workspace.mkdir()
    if result_kind == "missing_manifest":
        program = "pass"
    else:
        manifest = {
            "protocol_version": 1,
            "format": "KML",
            "status": "COMPLETED",
            "source_crs": {"name": "WGS 84"},
            "feature_count": 1,
            "measured_count": 0 if result_kind != "count_mismatch" else 1,
            "not_applicable_count": 0,
            "unsupported_count": 0,
            "error_count": 1 if result_kind != "count_mismatch" else 0,
            "has_issues": True,
        }
        feature = {
            "feature_index": 1 if result_kind == "order" else 0,
            "measurement_status": "ERROR",
        }
        results = (
            b'{"feature_index":0'
            if result_kind == "truncated"
            else (json.dumps(feature, separators=(",", ":")).encode() + b"\n")
        )
        program = (
            "import json, pathlib, sys; root=pathlib.Path(sys.argv[2]); "
            f"root.joinpath('manifest.json').write_text({json.dumps(manifest)!r}); "
            f"root.joinpath('features.jsonl').write_bytes({results!r})"
        )
    _install_fake_child(monkeypatch, program)

    with pytest.raises(ProcessingFailure) as failure:
        await run_processor(tmp_path / "input.kml", "KML", workspace, Settings(_env_file=None))

    assert failure.value.code == "INVALID_PROCESSOR_PROTOCOL"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="geo-api subprocess groups use POSIX signals")
async def test_cancelling_run_processor_reaps_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program = (
        "import os, pathlib, sys, time; "
        "pathlib.Path(sys.argv[2], 'child.pid').write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    _install_fake_child(monkeypatch, program)
    workspace = tmp_path / "cancel"
    workspace.mkdir()
    task = asyncio.create_task(
        run_processor(tmp_path / "input.kml", "KML", workspace, Settings(_env_file=None))
    )
    pid_path = workspace / "child.pid"
    for _ in range(100):
        if pid_path.exists():
            break
        await asyncio.sleep(0.01)
    assert pid_path.exists()
    child_pid = int(pid_path.read_text(encoding="ascii"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    probe = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(child_pid)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert not probe.stdout.strip() or probe.stdout.strip().startswith("Z")


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="geo-api subprocess groups use POSIX signals")
async def test_termination_kills_descendants_after_worker_exit(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    child_program = (
        "import pathlib, subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        child_program,
        str(pid_file),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    await process.wait()
    child_pid = int(pid_file.read_text(encoding="ascii"))

    await _terminate(process)

    state = ""
    for _ in range(40):
        probe = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(child_pid)],
            check=False,
            capture_output=True,
            text=True,
        )
        state = probe.stdout.strip()
        if not state or state.startswith("Z"):
            break
        await asyncio.sleep(0.05)
    assert not state or state.startswith("Z")
