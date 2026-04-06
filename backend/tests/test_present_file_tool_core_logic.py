"""Core behavior tests for present_files path normalization."""

import importlib
from types import SimpleNamespace

present_file_tool_module = importlib.import_module("deerflow.tools.builtins.present_file_tool")


def _make_runtime(outputs_path: str, uploads_path: str | None = None) -> SimpleNamespace:
    thread_data = {"outputs_path": outputs_path}
    if uploads_path is not None:
        thread_data["uploads_path"] = uploads_path

    return SimpleNamespace(
        state={"thread_data": thread_data},
        context={"thread_id": "thread-1"},
    )


def test_present_files_normalizes_host_outputs_path(tmp_path):
    outputs_dir = tmp_path / "threads" / "thread-1" / "user-data" / "outputs"
    outputs_dir.mkdir(parents=True)
    artifact_path = outputs_dir / "report.md"
    artifact_path.write_text("ok")

    result = present_file_tool_module.present_file_tool.func(
        runtime=_make_runtime(str(outputs_dir)),
        filepaths=[str(artifact_path)],
        tool_call_id="tc-1",
    )

    assert result.update["artifacts"] == ["/mnt/user-data/outputs/report.md"]
    assert result.update["messages"][0].content == "Successfully presented files"


def test_present_files_keeps_virtual_outputs_path(tmp_path, monkeypatch):
    outputs_dir = tmp_path / "threads" / "thread-1" / "user-data" / "outputs"
    outputs_dir.mkdir(parents=True)
    artifact_path = outputs_dir / "summary.json"
    artifact_path.write_text("{}")

    monkeypatch.setattr(
        present_file_tool_module,
        "get_paths",
        lambda: SimpleNamespace(resolve_virtual_path=lambda thread_id, path: artifact_path),
    )

    result = present_file_tool_module.present_file_tool.func(
        runtime=_make_runtime(str(outputs_dir)),
        filepaths=["/mnt/user-data/outputs/summary.json"],
        tool_call_id="tc-2",
    )

    assert result.update["artifacts"] == ["/mnt/user-data/outputs/summary.json"]


def test_present_files_copies_uploads_path_into_outputs(tmp_path):
    outputs_dir = tmp_path / "threads" / "thread-1" / "user-data" / "outputs"
    uploads_dir = tmp_path / "threads" / "thread-1" / "user-data" / "uploads"
    outputs_dir.mkdir(parents=True)
    uploads_dir.mkdir(parents=True)

    upload_path = uploads_dir / "photo.png"
    upload_path.write_bytes(b"\x89PNG\r\n\x1a\nimage")

    result = present_file_tool_module.present_file_tool.func(
        runtime=_make_runtime(str(outputs_dir), str(uploads_dir)),
        filepaths=[str(upload_path)],
        tool_call_id="tc-uploads",
    )

    assert result.update["artifacts"] == ["/mnt/user-data/outputs/photo.png"]
    assert result.update["messages"][0].content == "Successfully presented files"
    assert (outputs_dir / "photo.png").read_bytes() == b"\x89PNG\r\n\x1a\nimage"


def test_present_files_rejects_paths_outside_outputs(tmp_path):
    outputs_dir = tmp_path / "threads" / "thread-1" / "user-data" / "outputs"
    workspace_dir = tmp_path / "threads" / "thread-1" / "user-data" / "workspace"
    outputs_dir.mkdir(parents=True)
    workspace_dir.mkdir(parents=True)
    leaked_path = workspace_dir / "notes.txt"
    leaked_path.write_text("leak")

    result = present_file_tool_module.present_file_tool.func(
        runtime=_make_runtime(str(outputs_dir)),
        filepaths=[str(leaked_path)],
        tool_call_id="tc-3",
    )

    assert "artifacts" not in result.update
    assert result.update["messages"][0].content == f"Error: Only files in /mnt/user-data/outputs or /mnt/user-data/uploads can be presented: {leaked_path}"
