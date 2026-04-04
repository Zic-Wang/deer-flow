"""Tests for channel inbound upload bridging helpers."""

from __future__ import annotations

from pathlib import Path

from deerflow.config.paths import Paths

from app.channels.inbound_uploads import stage_inbound_files


class _SandboxStub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes]] = []

    def update_file(self, path: str, content: bytes) -> None:
        self.calls.append((path, content))


class _SandboxProviderStub:
    def __init__(self, sandbox_id: str = "remote") -> None:
        self.sandbox_id = sandbox_id
        self.sandbox = _SandboxStub()
        self.acquired: list[str] = []

    def acquire(self, thread_id: str | None = None) -> str:
        self.acquired.append(thread_id or "")
        return self.sandbox_id

    def get(self, sandbox_id: str):
        return self.sandbox


def test_stage_inbound_files_copies_into_thread_uploads(monkeypatch, tmp_path: Path):
    provider = _SandboxProviderStub(sandbox_id="local")
    monkeypatch.setattr("app.channels.inbound_uploads.get_sandbox_provider", lambda: provider)

    source = tmp_path / "source.txt"
    source.write_text("hello inbound", encoding="utf-8")

    result = _run_stage(
        "thread123",
        [{"filename": "source.txt", "size": 13, "path": str(source), "mime_type": "text/plain"}],
        base_dir=tmp_path,
    )

    assert result == [
        {
            "filename": "source.txt",
            "size": 13,
            "path": "/mnt/user-data/uploads/source.txt",
            "status": "uploaded",
            "mime_type": "text/plain",
        }
    ]

    staged = Paths(str(tmp_path)).sandbox_uploads_dir("thread123") / "source.txt"
    assert staged.exists()
    assert staged.read_text(encoding="utf-8") == "hello inbound"
    assert provider.acquired == ["thread123"]
    assert provider.sandbox.calls == []


def test_stage_inbound_files_renames_duplicates_and_syncs_remote(monkeypatch, tmp_path: Path):
    provider = _SandboxProviderStub(sandbox_id="remote")
    monkeypatch.setattr("app.channels.inbound_uploads.get_sandbox_provider", lambda: provider)

    paths = Paths(str(tmp_path))
    uploads_dir = paths.sandbox_uploads_dir("thread456")
    uploads_dir.mkdir(parents=True, exist_ok=True)
    (uploads_dir / "report.txt").write_text("old", encoding="utf-8")

    source = tmp_path / "report.txt"
    source.write_text("new", encoding="utf-8")

    result = _run_stage(
        "thread456",
        [{"filename": "report.txt", "size": 3, "path": str(source)}],
        base_dir=tmp_path,
    )

    assert result[0]["filename"] == "report_1.txt"
    assert result[0]["path"] == "/mnt/user-data/uploads/report_1.txt"
    assert result[0]["original_filename"] == "report.txt"
    assert provider.sandbox.calls == [("/mnt/user-data/uploads/report_1.txt", b"new")]
    assert (uploads_dir / "report_1.txt").read_text(encoding="utf-8") == "new"


def _run_stage(thread_id: str, files: list[dict], *, base_dir: Path):
    import asyncio

    return asyncio.run(stage_inbound_files(thread_id, files, paths=Paths(str(base_dir))))