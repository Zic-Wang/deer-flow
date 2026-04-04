import shutil
from pathlib import Path
from typing import Annotated

from langchain.tools import InjectedToolCallId, ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from langgraph.typing import ContextT

from deerflow.agents.thread_state import ThreadState
from deerflow.config.paths import VIRTUAL_PATH_PREFIX, get_paths

OUTPUTS_VIRTUAL_PREFIX = f"{VIRTUAL_PATH_PREFIX}/outputs"


def _detect_image_extension(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return ".webp"
    if content.startswith(b"BM"):
        return ".bmp"
    return None


def _is_within(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
    except ValueError:
        return False
    return True


def _normalize_output_filename(source_name: str, content: bytes) -> str:
    detected_extension = _detect_image_extension(content)
    if not detected_extension:
        return source_name

    source_path = Path(source_name)
    if source_path.suffix.lower() == detected_extension:
        return source_name
    return f"{source_path.stem}{detected_extension}"


def _claim_output_path(outputs_dir: Path, preferred_name: str) -> Path:
    from deerflow.uploads.manager import claim_unique_filename

    seen_names = {path.name for path in outputs_dir.iterdir() if path.is_file()}
    return outputs_dir / claim_unique_filename(preferred_name, seen_names)


def _copy_upload_into_outputs(upload_path: Path, outputs_dir: Path) -> Path:
    content = upload_path.read_bytes()
    target_name = _normalize_output_filename(upload_path.name, content)
    target_path = outputs_dir / target_name
    if target_path.exists():
        if target_path.read_bytes() == content:
            return target_path
        target_path = _claim_output_path(outputs_dir, target_name)
    else:
        target_path.parent.mkdir(parents=True, exist_ok=True)

    shutil.copy2(upload_path, target_path)
    return target_path


def _materialize_output_from_upload_if_needed(actual_path: Path, uploads_path: str | None) -> Path:
    resolved_actual = actual_path.resolve()
    if resolved_actual.is_file() and resolved_actual.stat().st_size > 0:
        return resolved_actual
    if not uploads_path:
        return resolved_actual

    uploads_dir = Path(uploads_path).resolve()
    candidate = uploads_dir / resolved_actual.name
    if not candidate.is_file() or candidate.stat().st_size <= 0:
        return resolved_actual

    content = candidate.read_bytes()
    detected_extension = _detect_image_extension(content)
    recovered_path = resolved_actual
    if detected_extension and resolved_actual.suffix.lower() != detected_extension:
        recovered_path = resolved_actual.with_suffix(detected_extension)

    recovered_path.parent.mkdir(parents=True, exist_ok=True)
    recovered_path.write_bytes(content)
    return recovered_path


def _materialize_presented_path(actual_path: Path, *, outputs_dir: Path, uploads_path: str | None) -> Path:
    resolved_actual = actual_path.resolve()
    uploads_dir = Path(uploads_path).resolve() if uploads_path else None

    if _is_within(resolved_actual, outputs_dir):
        return _materialize_output_from_upload_if_needed(resolved_actual, uploads_path)

    if uploads_dir is not None and _is_within(resolved_actual, uploads_dir):
        return _copy_upload_into_outputs(resolved_actual, outputs_dir)

    return resolved_actual


def _normalize_presented_filepath(
    runtime: ToolRuntime[ContextT, ThreadState],
    filepath: str,
) -> str:
    """Normalize a presented file path to the `/mnt/user-data/outputs/*` contract.

    Accepts either:
    - A virtual sandbox path such as `/mnt/user-data/outputs/report.md`
    - A virtual uploads path such as `/mnt/user-data/uploads/photo.png`
    - A host-side thread outputs/uploads path such as
      `/app/backend/.deer-flow/threads/<thread>/user-data/outputs/report.md`

    Returns:
        The normalized virtual outputs path.

    Raises:
        ValueError: If runtime metadata is missing or the path is outside the
            current thread's outputs/uploads directories.
    """
    if runtime.state is None:
        raise ValueError("Thread runtime state is not available")

    thread_id = runtime.context.get("thread_id") if runtime.context else None
    if not thread_id:
        raise ValueError("Thread ID is not available in runtime context")

    thread_data = runtime.state.get("thread_data") or {}
    outputs_path = thread_data.get("outputs_path")
    uploads_path = thread_data.get("uploads_path")
    if not outputs_path:
        raise ValueError("Thread outputs path is not available in runtime state")

    outputs_dir = Path(outputs_path).resolve()
    stripped = filepath.lstrip("/")
    virtual_prefix = VIRTUAL_PATH_PREFIX.lstrip("/")

    if stripped == virtual_prefix or stripped.startswith(virtual_prefix + "/"):
        actual_path = get_paths().resolve_virtual_path(thread_id, filepath)
    else:
        actual_path = Path(filepath).expanduser().resolve()

    actual_path = _materialize_presented_path(actual_path, outputs_dir=outputs_dir, uploads_path=uploads_path)

    try:
        relative_path = actual_path.relative_to(outputs_dir)
    except ValueError as exc:
        raise ValueError(f"Only files in {OUTPUTS_VIRTUAL_PREFIX} or /mnt/user-data/uploads can be presented: {filepath}") from exc

    return f"{OUTPUTS_VIRTUAL_PREFIX}/{relative_path.as_posix()}"


@tool("present_files", parse_docstring=True)
def present_file_tool(
    runtime: ToolRuntime[ContextT, ThreadState],
    filepaths: list[str],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Make files visible to the user for viewing and rendering in the client interface.

    When to use the present_files tool:

    - Making any file available for the user to view, download, or interact with
    - Presenting multiple related files at once
    - After creating files that should be presented to the user

    When NOT to use the present_files tool:
    - When you only need to read file contents for your own processing
    - For temporary or intermediate files not meant for user viewing

    Notes:
    - Files in `/mnt/user-data/outputs` are presented directly.
    - Files in `/mnt/user-data/uploads` are also accepted and will be copied into `/mnt/user-data/outputs` automatically.
    - This tool can be safely called in parallel with other tools. State updates are handled by a reducer to prevent conflicts.

    Args:
        filepaths: List of absolute file paths to present to the user. Files in `/mnt/user-data/outputs` are presented directly; files in `/mnt/user-data/uploads` are copied to outputs first.
    """
    try:
        normalized_paths = [_normalize_presented_filepath(runtime, filepath) for filepath in filepaths]
    except ValueError as exc:
        return Command(
            update={"messages": [ToolMessage(f"Error: {exc}", tool_call_id=tool_call_id)]},
        )

    return Command(
        update={
            "artifacts": normalized_paths,
            "messages": [ToolMessage("Successfully presented files", tool_call_id=tool_call_id)],
        },
    )
