"""Helpers for bridging channel inbound files into DeerFlow upload context."""

from __future__ import annotations

import logging
import os
import shutil
import stat
from pathlib import Path
from typing import Any

from deerflow.config.paths import Paths, get_paths
from deerflow.sandbox.sandbox_provider import get_sandbox_provider
from deerflow.uploads.manager import claim_unique_filename, normalize_filename, upload_virtual_path
from deerflow.utils.file_conversion import CONVERTIBLE_EXTENSIONS, convert_file_to_markdown

logger = logging.getLogger(__name__)


def _make_file_sandbox_writable(file_path: os.PathLike[str] | str) -> None:
    """Ensure uploaded files remain writable when mirrored to non-local sandboxes."""
    file_stat = os.lstat(file_path)
    if stat.S_ISLNK(file_stat.st_mode):
        logger.warning("Skipping sandbox chmod for symlinked upload path: %s", file_path)
        return

    writable_mode = stat.S_IMODE(file_stat.st_mode) | stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    chmod_kwargs = {"follow_symlinks": False} if os.chmod in os.supports_follow_symlinks else {}
    os.chmod(file_path, writable_mode, **chmod_kwargs)


async def stage_inbound_files(
    thread_id: str,
    files: list[dict[str, Any]],
    *,
    paths: Paths | None = None,
) -> list[dict[str, Any]]:
    """Copy inbound channel files into the thread uploads directory.

    Returns metadata shaped like frontend ``additional_kwargs.files`` entries so
    existing upload middleware can expose them to the agent unchanged.
    """
    if not files:
        return []

    resolved_paths = paths or get_paths()
    uploads_dir = resolved_paths.sandbox_uploads_dir(thread_id)
    uploads_dir.mkdir(parents=True, exist_ok=True)

    provider = None
    sandbox = None
    sandbox_id: str | None = None
    try:
        provider = get_sandbox_provider()
        sandbox_id = provider.acquire(thread_id)
        sandbox = provider.get(sandbox_id)
    except Exception:
        logger.warning("[InboundUploads] failed to acquire sandbox for thread %s", thread_id, exc_info=True)

    uploaded_files: list[dict[str, Any]] = []
    seen_names = {path.name for path in uploads_dir.iterdir() if path.is_file()}

    for raw_file in files:
        if not isinstance(raw_file, dict):
            continue

        raw_path = raw_file.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue

        source_path = Path(raw_path)
        if not source_path.is_file():
            logger.warning("[InboundUploads] inbound file path missing on disk: %s", raw_path)
            continue

        raw_filename = str(raw_file.get("filename") or source_path.name or "")
        try:
            safe_filename = normalize_filename(raw_filename)
        except ValueError:
            try:
                safe_filename = normalize_filename(source_path.name)
            except ValueError:
                logger.warning("[InboundUploads] inbound filename is unsafe: %r", raw_filename)
                continue

        dest_name = claim_unique_filename(safe_filename, seen_names)
        dest_path = uploads_dir / dest_name

        try:
            shutil.copy2(source_path, dest_path)
        except OSError:
            logger.warning("[InboundUploads] failed to stage inbound file %s", source_path, exc_info=True)
            continue

        virtual_path = upload_virtual_path(dest_name)
        if sandbox is not None and sandbox_id != "local":
            try:
                _make_file_sandbox_writable(dest_path)
                sandbox.update_file(virtual_path, dest_path.read_bytes())
            except Exception:
                logger.warning(
                    "[InboundUploads] failed to mirror %s into sandbox for thread %s",
                    dest_name,
                    thread_id,
                    exc_info=True,
                )

        if dest_path.suffix.lower() in CONVERTIBLE_EXTENSIONS:
            try:
                markdown_path = await convert_file_to_markdown(dest_path)
            except Exception:
                markdown_path = None
                logger.warning(
                    "[InboundUploads] failed to convert %s to markdown",
                    dest_name,
                    exc_info=True,
                )
            if markdown_path is not None and sandbox is not None and sandbox_id != "local":
                try:
                    markdown_virtual_path = upload_virtual_path(markdown_path.name)
                    _make_file_sandbox_writable(markdown_path)
                    sandbox.update_file(markdown_virtual_path, markdown_path.read_bytes())
                except Exception:
                    logger.warning(
                        "[InboundUploads] failed to mirror markdown companion %s into sandbox for thread %s",
                        markdown_path.name,
                        thread_id,
                        exc_info=True,
                    )

        file_info = {
            "filename": dest_name,
            "size": int(dest_path.stat().st_size),
            "path": virtual_path,
            "status": "uploaded",
        }

        if dest_name != safe_filename:
            file_info["original_filename"] = safe_filename

        for extra_key in ("mime_type", "source", "message_item_type", "full_url"):
            extra_value = raw_file.get(extra_key)
            if extra_value is not None:
                file_info[extra_key] = extra_value

        uploaded_files.append(file_info)

    return uploaded_files