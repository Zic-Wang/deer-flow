"""ChannelManager — consumes inbound messages and dispatches them to the DeerFlow agent via LangGraph Server."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx
from langgraph_sdk.errors import ConflictError, NotFoundError

from app.channels.commands import KNOWN_CHANNEL_COMMANDS
from app.channels.inbound_uploads import stage_inbound_files
from app.channels.message_bus import InboundMessage, InboundMessageType, MessageBus, OutboundMessage, ResolvedAttachment
from app.channels.store import ChannelStore

logger = logging.getLogger(__name__)

DEFAULT_LANGGRAPH_URL = "http://localhost:2024"
DEFAULT_GATEWAY_URL = "http://localhost:8001"
DEFAULT_ASSISTANT_ID = "lead_agent"
CUSTOM_AGENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")

DEFAULT_RUN_CONFIG: dict[str, Any] = {"recursion_limit": 100}
DEFAULT_RUN_CONTEXT: dict[str, Any] = {
    "thinking_enabled": True,
    "is_plan_mode": False,
    "subagent_enabled": False,
}
STREAM_UPDATE_MIN_INTERVAL_SECONDS = 0.35
THREAD_BUSY_MESSAGE = "This conversation is already processing another request. Please wait for it to finish and try again."
DEFAULT_TYPING_REFRESH_INTERVAL_SECONDS = 6.0

CHANNEL_CAPABILITIES = {
    "feishu": {"supports_streaming": True},
    "slack": {"supports_streaming": False},
    "telegram": {"supports_streaming": False},
    "wecom": {"supports_streaming": True},
    "wechat": {"supports_streaming": False},
}

InboundFileReader = Callable[[dict[str, Any], httpx.AsyncClient], Awaitable[bytes | None]]


INBOUND_FILE_READERS: dict[str, InboundFileReader] = {}


def register_inbound_file_reader(channel_name: str, reader: InboundFileReader) -> None:
    INBOUND_FILE_READERS[channel_name] = reader


async def _read_http_inbound_file(file_info: dict[str, Any], client: httpx.AsyncClient) -> bytes | None:
    url = file_info.get("url")
    if not isinstance(url, str) or not url:
        return None

    resp = await client.get(url)
    resp.raise_for_status()
    return resp.content


async def _read_wecom_inbound_file(file_info: dict[str, Any], client: httpx.AsyncClient) -> bytes | None:
    data = await _read_http_inbound_file(file_info, client)
    if data is None:
        return None

    aeskey = file_info.get("aeskey") if isinstance(file_info.get("aeskey"), str) else None
    if not aeskey:
        return data

    try:
        from aibot.crypto_utils import decrypt_file
    except Exception:
        logger.exception("[Manager] failed to import WeCom decrypt_file")
        return None

    return decrypt_file(data, aeskey)


register_inbound_file_reader("wecom", _read_wecom_inbound_file)


class InvalidChannelSessionConfigError(ValueError):
    """Raised when IM channel session overrides contain invalid agent config."""


def _is_thread_busy_error(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    if isinstance(exc, ConflictError):
        return True
    return "already running a task" in str(exc)


def _is_thread_not_found_error(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    if isinstance(exc, NotFoundError):
        return True

    status_code = getattr(exc, "status_code", None)
    if status_code == 404:
        return True

    message = str(exc).lower()
    return "404" in message and ("run not found" in message or "thread not found" in message)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _merge_dicts(*layers: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for layer in layers:
        if isinstance(layer, Mapping):
            merged.update(layer)
    return merged


def _normalize_custom_agent_name(raw_value: str) -> str:
    """Normalize legacy channel assistant IDs into valid custom agent names."""
    normalized = raw_value.strip().lower().replace("_", "-")
    if not normalized:
        raise InvalidChannelSessionConfigError("Channel session assistant_id is empty. Use 'lead_agent' or a valid custom agent name.")
    if not CUSTOM_AGENT_NAME_PATTERN.fullmatch(normalized):
        raise InvalidChannelSessionConfigError(f"Invalid channel session assistant_id {raw_value!r}. Use 'lead_agent' or a custom agent name containing only letters, digits, and hyphens.")
    return normalized


def _extract_response_text(result: dict | list) -> str:
    """Extract the last AI message text from a LangGraph runs.wait result.

    ``runs.wait`` returns the final state dict which contains a ``messages``
    list.  Each message is a dict with at least ``type`` and ``content``.

    Handles special cases:
    - Regular AI text responses
    - Clarification interrupts (``ask_clarification`` tool messages)
    - AI messages with tool_calls but no text content
    """
    if isinstance(result, list):
        messages = result
    elif isinstance(result, dict):
        messages = result.get("messages", [])
    else:
        return ""

    # Walk backwards to find usable response text, but stop at the last
    # human message to avoid returning text from a previous turn.
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue

        msg_type = msg.get("type")

        # Stop at the last human message — anything before it is a previous turn
        if msg_type == "human":
            break

        # Check for tool messages from ask_clarification (interrupt case)
        if msg_type == "tool" and msg.get("name") == "ask_clarification":
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                return content

        # Regular AI message with text content
        if msg_type == "ai":
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                return content
            # content can be a list of content blocks
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        parts.append(block)
                text = "".join(parts)
                if text:
                    return text
    return ""


async def _build_run_input(thread_id: str, msg: InboundMessage) -> dict[str, Any]:
    """Build a LangGraph input payload for a channel message.

    For inbound files, mirror them into the thread uploads directory and reuse
    the same ``additional_kwargs.files`` shape produced by the frontend upload
    flow so UploadsMiddleware can expose them to the agent.
    """
    if not msg.files:
        return {"messages": [{"role": "human", "content": msg.text}]}

    uploaded_files = await stage_inbound_files(thread_id, msg.files)
    if not uploaded_files:
        return {"messages": [{"role": "human", "content": msg.text}]}

    return {
        "messages": [
            {
                "type": "human",
                "content": [{"type": "text", "text": msg.text}],
                "additional_kwargs": {"files": uploaded_files},
            }
        ]
    }


def _extract_text_content(content: Any) -> str:
    """Extract text from a streaming payload content field."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
                else:
                    nested = block.get("content")
                    if isinstance(nested, str):
                        parts.append(nested)
        return "".join(parts)
    if isinstance(content, Mapping):
        for key in ("text", "content"):
            value = content.get(key)
            if isinstance(value, str):
                return value
    return ""


def _merge_stream_text(existing: str, chunk: str) -> str:
    """Merge either delta text or cumulative text into a single snapshot."""
    if not chunk:
        return existing
    if not existing or chunk == existing:
        return chunk or existing
    if chunk.startswith(existing):
        return chunk
    if existing.endswith(chunk):
        return existing
    return existing + chunk


def _extract_stream_message_id(payload: Any, metadata: Any) -> str | None:
    """Best-effort extraction of the streamed AI message identifier."""
    candidates = [payload, metadata]
    if isinstance(payload, Mapping):
        candidates.append(payload.get("kwargs"))

    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        for key in ("id", "message_id"):
            value = candidate.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _accumulate_stream_text(
    buffers: dict[str, str],
    current_message_id: str | None,
    event_data: Any,
) -> tuple[str | None, str | None]:
    """Convert a ``messages-tuple`` event into the latest displayable AI text."""
    payload = event_data
    metadata: Any = None
    if isinstance(event_data, (list, tuple)):
        if event_data:
            payload = event_data[0]
        if len(event_data) > 1:
            metadata = event_data[1]

    if isinstance(payload, str):
        message_id = current_message_id or "__default__"
        buffers[message_id] = _merge_stream_text(buffers.get(message_id, ""), payload)
        return buffers[message_id], message_id

    if not isinstance(payload, Mapping):
        return None, current_message_id

    payload_type = str(payload.get("type", "")).lower()
    if "tool" in payload_type:
        return None, current_message_id

    text = _extract_text_content(payload.get("content"))
    if not text and isinstance(payload.get("kwargs"), Mapping):
        text = _extract_text_content(payload["kwargs"].get("content"))
    if not text:
        return None, current_message_id

    message_id = _extract_stream_message_id(payload, metadata) or current_message_id or "__default__"
    buffers[message_id] = _merge_stream_text(buffers.get(message_id, ""), text)
    return buffers[message_id], message_id


def _extract_artifacts(result: dict | list) -> list[str]:
    """Extract artifact paths from the last AI response cycle only.

    Instead of reading the full accumulated ``artifacts`` state (which contains
    all artifacts ever produced in the thread), this inspects the messages after
    the last human message and collects file paths from ``present_files`` tool
    calls.  This ensures only newly-produced artifacts are returned.
    """
    if isinstance(result, list):
        messages = result
    elif isinstance(result, dict):
        messages = result.get("messages", [])
    else:
        return []

    artifacts: list[str] = []
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        # Stop at the last human message — anything before it is a previous turn
        if msg.get("type") == "human":
            break
        # Look for AI messages with present_files tool calls
        if msg.get("type") == "ai":
            for tc in msg.get("tool_calls", []):
                if isinstance(tc, dict) and tc.get("name") == "present_files":
                    args = tc.get("args", {})
                    paths = args.get("filepaths", [])
                    if isinstance(paths, list):
                        artifacts.extend(p for p in paths if isinstance(p, str))
    return artifacts


def _snapshot_outputs_dir(thread_id: str) -> dict[str, tuple[int, int]]:
    """Capture the current outputs directory file state for a thread."""
    from deerflow.config.paths import get_paths

    outputs_dir = get_paths().sandbox_outputs_dir(thread_id)
    if not outputs_dir.exists():
        return {}

    snapshot: dict[str, tuple[int, int]] = {}
    for path in outputs_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            stat_result = path.stat()
            rel_path = path.relative_to(outputs_dir).as_posix()
        except (OSError, ValueError):
            continue
        snapshot[rel_path] = (stat_result.st_size, stat_result.st_mtime_ns)
    return snapshot


def _infer_artifacts_from_outputs_delta(
    thread_id: str,
    previous_snapshot: Mapping[str, tuple[int, int]] | None,
) -> list[str]:
    """Infer output artifacts by comparing outputs dir state before/after a run."""
    from deerflow.config.paths import get_paths

    outputs_dir = get_paths().sandbox_outputs_dir(thread_id)
    if not outputs_dir.exists():
        return []

    previous = dict(previous_snapshot or {})
    inferred: list[str] = []
    for path in sorted(outputs_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            stat_result = path.stat()
            rel_path = path.relative_to(outputs_dir).as_posix()
        except (OSError, ValueError):
            continue
        current_state = (stat_result.st_size, stat_result.st_mtime_ns)
        if previous.get(rel_path) != current_state:
            inferred.append(f"{_OUTPUTS_VIRTUAL_PREFIX}{rel_path}")
    return inferred


def _collect_artifacts(
    thread_id: str,
    result: dict | list,
    previous_outputs_snapshot: Mapping[str, tuple[int, int]] | None,
) -> list[str]:
    """Collect artifacts from explicit tool calls, then fall back to outputs delta."""
    artifacts = _extract_artifacts(result)
    if artifacts:
        return artifacts

    inferred = _infer_artifacts_from_outputs_delta(thread_id, previous_outputs_snapshot)
    if inferred:
        logger.info("[Manager] inferred artifacts from outputs delta: thread_id=%s count=%d", thread_id, len(inferred))
    return inferred


def _format_artifact_text(artifacts: list[str]) -> str:
    """Format artifact paths into a human-readable text block listing filenames."""
    import posixpath

    filenames = [posixpath.basename(p) for p in artifacts]
    if len(filenames) == 1:
        return f"Created File: 📎 {filenames[0]}"
    return "Created Files: 📎 " + "、".join(filenames)


_OUTPUTS_VIRTUAL_PREFIX = "/mnt/user-data/outputs/"
_UPLOADS_VIRTUAL_PREFIX = "/mnt/user-data/uploads/"


def _copy_upload_attachment_for_delivery(actual_path, outputs_dir):
    """Copy a thread upload into outputs so channels can deliver it safely."""
    from deerflow.uploads.manager import claim_unique_filename

    source_path = actual_path.resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    target_dir = outputs_dir.resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    source_bytes = source_path.read_bytes()
    if source_path.suffix.lower() in {".jpg", ".jpeg"} and source_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        source_name = f"{source_path.stem}.png"
    elif source_path.suffix.lower() != ".webp" and len(source_bytes) >= 12 and source_bytes.startswith(b"RIFF") and source_bytes[8:12] == b"WEBP":
        source_name = f"{source_path.stem}.webp"
    else:
        source_name = source_path.name
    seen_names = {path.name for path in target_dir.iterdir() if path.is_file()}
    dest_name = claim_unique_filename(source_name, seen_names)
    dest_path = target_dir / dest_name
    shutil.copy2(source_path, dest_path)
    return dest_path


def _resolve_attachments(thread_id: str, artifacts: list[str]) -> list[ResolvedAttachment]:
    """Resolve virtual artifact paths to host filesystem paths with metadata.

    Paths under ``/mnt/user-data/outputs/`` are accepted directly. Paths under
    ``/mnt/user-data/uploads/`` are copied into outputs first so channels can
    send user-provided media back without exposing arbitrary sandbox files.
    Any other virtual path is rejected with a warning.

    Skips artifacts that cannot be resolved (missing files, invalid paths)
    and logs warnings for them.
    """
    from deerflow.config.paths import get_paths

    attachments: list[ResolvedAttachment] = []
    paths = get_paths()
    outputs_dir = paths.sandbox_outputs_dir(thread_id).resolve()
    uploads_dir = paths.sandbox_uploads_dir(thread_id).resolve() if hasattr(paths, "sandbox_uploads_dir") else None
    for virtual_path in artifacts:
        if not virtual_path.startswith((_OUTPUTS_VIRTUAL_PREFIX, _UPLOADS_VIRTUAL_PREFIX)):
            logger.warning("[Manager] rejected non-outputs artifact path: %s", virtual_path)
            continue
        try:
            actual = paths.resolve_virtual_path(thread_id, virtual_path)
            resolved_actual = actual.resolve()
            if virtual_path.startswith(_OUTPUTS_VIRTUAL_PREFIX):
                try:
                    resolved_actual.relative_to(outputs_dir)
                except ValueError:
                    logger.warning("[Manager] artifact path escapes outputs dir: %s -> %s", virtual_path, actual)
                    continue
                delivery_actual = resolved_actual
            else:
                if uploads_dir is None:
                    logger.warning("[Manager] uploads dir unavailable for artifact path: %s", virtual_path)
                    continue
                try:
                    resolved_actual.relative_to(uploads_dir)
                except ValueError:
                    logger.warning("[Manager] artifact path escapes uploads dir: %s -> %s", virtual_path, actual)
                    continue
                delivery_actual = _copy_upload_attachment_for_delivery(resolved_actual, outputs_dir)

            if not delivery_actual.is_file():
                logger.warning("[Manager] artifact not found on disk: %s -> %s", virtual_path, delivery_actual)
                continue
            mime, _ = mimetypes.guess_type(str(delivery_actual))
            mime = mime or "application/octet-stream"
            attachments.append(
                ResolvedAttachment(
                    virtual_path=virtual_path,
                    actual_path=delivery_actual,
                    filename=delivery_actual.name,
                    mime_type=mime,
                    size=delivery_actual.stat().st_size,
                    is_image=mime.startswith("image/"),
                )
            )
        except (ValueError, OSError) as exc:
            logger.warning("[Manager] failed to resolve artifact %s: %s", virtual_path, exc)
    return attachments


def _prepare_artifact_delivery(
    thread_id: str,
    response_text: str,
    artifacts: list[str],
) -> tuple[str, list[ResolvedAttachment]]:
    """Resolve attachments and append filename fallbacks to the text response."""
    attachments: list[ResolvedAttachment] = []
    if not artifacts:
        return response_text, attachments

    attachments = _resolve_attachments(thread_id, artifacts)
    resolved_virtuals = {attachment.virtual_path for attachment in attachments}
    unresolved = [path for path in artifacts if path not in resolved_virtuals]

    if unresolved:
        artifact_text = _format_artifact_text(unresolved)
        response_text = (response_text + "\n\n" + artifact_text) if response_text else artifact_text

    # Always include resolved attachment filenames as a text fallback so files
    # remain discoverable even when the upload is skipped or fails.
    if attachments:
        resolved_text = _format_artifact_text([attachment.virtual_path for attachment in attachments])
        response_text = (response_text + "\n\n" + resolved_text) if response_text else resolved_text

    return response_text, attachments


async def _ingest_inbound_files(thread_id: str, msg: InboundMessage) -> list[dict[str, Any]]:
    if not msg.files:
        return []

    from deerflow.uploads.manager import claim_unique_filename, ensure_uploads_dir, normalize_filename

    uploads_dir = ensure_uploads_dir(thread_id)
    seen_names = {entry.name for entry in uploads_dir.iterdir() if entry.is_file()}

    created: list[dict[str, Any]] = []
    file_reader = INBOUND_FILE_READERS.get(msg.channel_name, _read_http_inbound_file)
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
        for idx, f in enumerate(msg.files):
            if not isinstance(f, dict):
                continue

            ftype = f.get("type") if isinstance(f.get("type"), str) else "file"
            filename = f.get("filename") if isinstance(f.get("filename"), str) else ""

            try:
                data = await file_reader(f, client)
            except Exception:
                logger.exception(
                    "[Manager] failed to read inbound file: channel=%s, file=%s",
                    msg.channel_name,
                    f.get("url") or filename or idx,
                )
                continue

            if data is None:
                logger.warning(
                    "[Manager] inbound file reader returned no data: channel=%s, file=%s",
                    msg.channel_name,
                    f.get("url") or filename or idx,
                )
                continue

            if not filename:
                ext = ".bin"
                if ftype == "image":
                    ext = ".png"
                filename = f"{msg.thread_ts or 'msg'}_{idx}{ext}"

            try:
                safe_name = claim_unique_filename(normalize_filename(filename), seen_names)
            except ValueError:
                logger.warning(
                    "[Manager] skipping inbound file with unsafe filename: channel=%s, file=%r",
                    msg.channel_name,
                    filename,
                )
                continue

            dest = uploads_dir / safe_name
            try:
                dest.write_bytes(data)
            except Exception:
                logger.exception("[Manager] failed to write inbound file: %s", dest)
                continue

            created.append(
                {
                    "filename": safe_name,
                    "size": len(data),
                    "path": f"/mnt/user-data/uploads/{safe_name}",
                    "is_image": ftype == "image",
                }
            )

    return created


def _format_uploaded_files_block(files: list[dict[str, Any]]) -> str:
    lines = [
        "<uploaded_files>",
        "The following files were uploaded in this message:",
        "",
    ]
    if not files:
        lines.append("(empty)")
    else:
        for f in files:
            filename = f.get("filename", "")
            size = int(f.get("size") or 0)
            size_kb = size / 1024 if size else 0
            size_str = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb / 1024:.1f} MB"
            path = f.get("path", "")
            is_image = bool(f.get("is_image"))
            file_kind = "image" if is_image else "file"
            lines.append(f"- {filename} ({size_str})")
            lines.append(f"  Type: {file_kind}")
            lines.append(f"  Path: {path}")
            lines.append("")
    lines.append("Use `read_file` for text-based files and documents.")
    lines.append("Use `view_image` for image files (jpg, jpeg, png, webp) so the model can inspect the image content.")
    lines.append("</uploaded_files>")
    return "\n".join(lines)


class ChannelManager:
    """Core dispatcher that bridges IM channels to the DeerFlow agent.

    It reads from the MessageBus inbound queue, creates/reuses threads on
    the LangGraph Server, sends messages via ``runs.wait``, and publishes
    outbound responses back through the bus.
    """

    def __init__(
        self,
        bus: MessageBus,
        store: ChannelStore,
        *,
        max_concurrency: int = 5,
        langgraph_url: str = DEFAULT_LANGGRAPH_URL,
        gateway_url: str = DEFAULT_GATEWAY_URL,
        assistant_id: str = DEFAULT_ASSISTANT_ID,
        default_session: dict[str, Any] | None = None,
        channel_sessions: dict[str, Any] | None = None,
    ) -> None:
        self.bus = bus
        self.store = store
        self._max_concurrency = max_concurrency
        self._langgraph_url = langgraph_url
        self._gateway_url = gateway_url
        self._assistant_id = assistant_id
        self._default_session = _as_dict(default_session)
        self._channel_sessions = dict(channel_sessions or {})
        self._client = None  # lazy init — langgraph_sdk async client
        self._semaphore: asyncio.Semaphore | None = None
        self._running = False
        self._task: asyncio.Task | None = None
        self._registered_channels: dict[str, Any] = {}

    def register_channel(self, name: str, channel: Any) -> None:
        self._registered_channels[name] = channel

    def unregister_channel(self, name: str) -> None:
        self._registered_channels.pop(name, None)

    def _get_registered_channel(self, channel_name: str) -> Any | None:
        return self._registered_channels.get(channel_name)

    def _create_typing_task(self, msg: InboundMessage) -> asyncio.Task | None:
        channel = self._get_registered_channel(msg.channel_name)
        if channel is None:
            return None

        send_typing = getattr(channel, "send_typing", None)
        if not callable(send_typing):
            return None

        context_token = msg.metadata.get("context_token") if isinstance(msg.metadata, dict) else None
        if not isinstance(context_token, str) or not context_token.strip():
            return None

        refresh_interval_raw = getattr(channel, "config", {}).get("typing_refresh_interval", DEFAULT_TYPING_REFRESH_INTERVAL_SECONDS)
        try:
            refresh_interval = max(float(refresh_interval_raw), 0.1)
        except (TypeError, ValueError):
            refresh_interval = DEFAULT_TYPING_REFRESH_INTERVAL_SECONDS

        async def _typing_loop() -> None:
            while True:
                try:
                    await send_typing(msg.chat_id, context_token)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("[Manager] typing loop failed for channel=%s chat_id=%s", msg.channel_name, msg.chat_id)
                    return
                await asyncio.sleep(refresh_interval)

        return asyncio.create_task(_typing_loop())

    async def _cancel_typing_task(self, task: asyncio.Task | None) -> None:
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _channel_supports_streaming(channel_name: str) -> bool:
        return CHANNEL_CAPABILITIES.get(channel_name, {}).get("supports_streaming", False)

    def _resolve_session_layer(self, msg: InboundMessage) -> tuple[dict[str, Any], dict[str, Any]]:
        channel_layer = _as_dict(self._channel_sessions.get(msg.channel_name))
        users_layer = _as_dict(channel_layer.get("users"))
        user_layer = _as_dict(users_layer.get(msg.user_id))
        return channel_layer, user_layer

    def _resolve_run_params(self, msg: InboundMessage, thread_id: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
        channel_layer, user_layer = self._resolve_session_layer(msg)

        assistant_id = user_layer.get("assistant_id") or channel_layer.get("assistant_id") or self._default_session.get("assistant_id") or self._assistant_id
        if not isinstance(assistant_id, str) or not assistant_id.strip():
            assistant_id = self._assistant_id

        run_config = _merge_dicts(
            DEFAULT_RUN_CONFIG,
            self._default_session.get("config"),
            channel_layer.get("config"),
            user_layer.get("config"),
        )

        run_context = _merge_dicts(
            DEFAULT_RUN_CONTEXT,
            self._default_session.get("context"),
            channel_layer.get("context"),
            user_layer.get("context"),
            {"thread_id": thread_id},
        )

        # Custom agents are implemented as lead_agent + agent_name context.
        # Keep backward compatibility for channel configs that set
        # assistant_id: <custom-agent-name> by routing through lead_agent.
        if assistant_id != DEFAULT_ASSISTANT_ID:
            run_context.setdefault("agent_name", _normalize_custom_agent_name(assistant_id))
            assistant_id = DEFAULT_ASSISTANT_ID

        return assistant_id, run_config, run_context

    # -- LangGraph SDK client (lazy) ----------------------------------------

    def _get_client(self):
        """Return the ``langgraph_sdk`` async client, creating it on first use."""
        if self._client is None:
            from langgraph_sdk import get_client

            self._client = get_client(url=self._langgraph_url)
        return self._client

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Start the dispatch loop."""
        if self._running:
            return
        self._running = True
        self._semaphore = asyncio.Semaphore(self._max_concurrency)
        self._task = asyncio.create_task(self._dispatch_loop())
        logger.info("ChannelManager started (max_concurrency=%d)", self._max_concurrency)

    async def stop(self) -> None:
        """Stop the dispatch loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("ChannelManager stopped")

    # -- dispatch loop -----------------------------------------------------

    async def _dispatch_loop(self) -> None:
        logger.info("[Manager] dispatch loop started, waiting for inbound messages")
        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.get_inbound(), timeout=1.0)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            logger.info(
                "[Manager] received inbound: channel=%s, chat_id=%s, type=%s, text=%r",
                msg.channel_name,
                msg.chat_id,
                msg.msg_type.value,
                msg.text[:100] if msg.text else "",
            )
            task = asyncio.create_task(self._handle_message(msg))
            task.add_done_callback(self._log_task_error)

    @staticmethod
    def _log_task_error(task: asyncio.Task) -> None:
        """Surface unhandled exceptions from background tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error("[Manager] unhandled error in message task: %s", exc, exc_info=exc)

    async def _handle_message(self, msg: InboundMessage) -> None:
        async with self._semaphore:
            try:
                if msg.msg_type == InboundMessageType.COMMAND:
                    await self._handle_command(msg)
                else:
                    await self._handle_chat(msg)
            except InvalidChannelSessionConfigError as exc:
                logger.warning(
                    "Invalid channel session config for %s (chat=%s): %s",
                    msg.channel_name,
                    msg.chat_id,
                    exc,
                )
                await self._send_error(msg, str(exc))
            except Exception:
                logger.exception(
                    "Error handling message from %s (chat=%s)",
                    msg.channel_name,
                    msg.chat_id,
                )
                await self._send_error(msg, "An internal error occurred. Please try again.")

    # -- chat handling -----------------------------------------------------

    async def _create_thread(self, client, msg: InboundMessage) -> str:
        """Create a new thread on the LangGraph Server and store the mapping."""
        thread = await client.threads.create()
        thread_id = thread["thread_id"]
        self.store.set_thread_id(
            msg.channel_name,
            msg.chat_id,
            thread_id,
            topic_id=msg.topic_id,
            user_id=msg.user_id,
        )
        logger.info("[Manager] new thread created on LangGraph Server: thread_id=%s for chat_id=%s topic_id=%s", thread_id, msg.chat_id, msg.topic_id)
        return thread_id

    async def _recreate_missing_thread(self, client, msg: InboundMessage, stale_thread_id: str) -> str:
        """Replace a stale thread mapping after the LangGraph server reports it missing."""
        removed = self.store.remove(msg.channel_name, msg.chat_id, topic_id=msg.topic_id)
        logger.warning(
            "[Manager] stale thread mapping detected; recreating thread: old_thread_id=%s removed=%s chat_id=%s topic_id=%s",
            stale_thread_id,
            removed,
            msg.chat_id,
            msg.topic_id,
        )
        return await self._create_thread(client, msg)

    async def _handle_chat(self, msg: InboundMessage, extra_context: dict[str, Any] | None = None) -> None:
        client = self._get_client()

        # Look up existing DeerFlow thread.
        # topic_id may be None (e.g. Telegram private chats) — the store
        # handles this by using the "channel:chat_id" key without a topic suffix.
        thread_id = self.store.get_thread_id(msg.channel_name, msg.chat_id, topic_id=msg.topic_id)
        if thread_id:
            logger.info("[Manager] reusing thread: thread_id=%s for topic_id=%s", thread_id, msg.topic_id)

        # No existing thread found — create a new one
        if thread_id is None:
            thread_id = await self._create_thread(client, msg)

        typing_task = self._create_typing_task(msg)
        original_text = msg.text
        retried_missing_thread = False

        try:
            while True:
                outputs_snapshot = _snapshot_outputs_dir(thread_id)

                assistant_id, run_config, run_context = self._resolve_run_params(msg, thread_id)
                if extra_context:
                    run_context.update(extra_context)

                uploaded = await _ingest_inbound_files(thread_id, msg)
                if uploaded:
                    msg.text = f"{_format_uploaded_files_block(uploaded)}\n\n{original_text}".strip()
                else:
                    msg.text = original_text

                if self._channel_supports_streaming(msg.channel_name):
                    run_input = await _build_run_input(thread_id, msg)
                    await self._handle_streaming_chat(
                        client,
                        msg,
                        thread_id,
                        assistant_id,
                        run_config,
                        run_context,
                        run_input,
                        outputs_snapshot,
                    )
                    return

                logger.info("[Manager] invoking runs.wait(thread_id=%s, text=%r)", thread_id, msg.text[:100])
                run_input = await _build_run_input(thread_id, msg)
                try:
                    result = await client.runs.wait(
                        thread_id,
                        assistant_id,
                        input=run_input,
                        config=run_config,
                        context=run_context,
                    )
                except Exception as exc:
                    msg.text = original_text
                    if not retried_missing_thread and _is_thread_not_found_error(exc):
                        thread_id = await self._recreate_missing_thread(client, msg, thread_id)
                        retried_missing_thread = True
                        continue
                    raise
                break

            response_text = _extract_response_text(result)
            artifacts = _collect_artifacts(thread_id, result, outputs_snapshot)

            logger.info(
                "[Manager] agent response received: thread_id=%s, response_len=%d, artifacts=%d",
                thread_id,
                len(response_text) if response_text else 0,
                len(artifacts),
            )

            response_text, attachments = _prepare_artifact_delivery(thread_id, response_text, artifacts)

            if not response_text:
                if attachments:
                    response_text = _format_artifact_text([a.virtual_path for a in attachments])
                else:
                    response_text = "(No response from agent)"

            outbound = OutboundMessage(
                channel_name=msg.channel_name,
                chat_id=msg.chat_id,
                thread_id=thread_id,
                text=response_text,
                artifacts=artifacts,
                attachments=attachments,
                thread_ts=msg.thread_ts,
            )
            logger.info("[Manager] publishing outbound message to bus: channel=%s, chat_id=%s", msg.channel_name, msg.chat_id)
            await self.bus.publish_outbound(outbound)
        finally:
            msg.text = original_text
            await self._cancel_typing_task(typing_task)

    async def _handle_streaming_chat(
        self,
        client,
        msg: InboundMessage,
        thread_id: str,
        assistant_id: str,
        run_config: dict[str, Any],
        run_context: dict[str, Any],
        run_input: dict[str, Any],
        outputs_snapshot: Mapping[str, tuple[int, int]] | None,
    ) -> None:
        logger.info("[Manager] invoking runs.stream(thread_id=%s, text=%r)", thread_id, msg.text[:100])

        last_values: dict[str, Any] | list | None = None
        streamed_buffers: dict[str, str] = {}
        current_message_id: str | None = None
        latest_text = ""
        last_published_text = ""
        last_publish_at = 0.0
        stream_error: BaseException | None = None

        try:
            async for chunk in client.runs.stream(
                thread_id,
                assistant_id,
                input=run_input,
                config=run_config,
                context=run_context,
                stream_mode=["messages-tuple", "values"],
                multitask_strategy="reject",
            ):
                event = getattr(chunk, "event", "")
                data = getattr(chunk, "data", None)

                if event == "messages-tuple":
                    accumulated_text, current_message_id = _accumulate_stream_text(streamed_buffers, current_message_id, data)
                    if accumulated_text:
                        latest_text = accumulated_text
                elif event == "values" and isinstance(data, (dict, list)):
                    last_values = data
                    snapshot_text = _extract_response_text(data)
                    if snapshot_text:
                        latest_text = snapshot_text

                if not latest_text or latest_text == last_published_text:
                    continue

                now = time.monotonic()
                if last_published_text and now - last_publish_at < STREAM_UPDATE_MIN_INTERVAL_SECONDS:
                    continue

                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel_name=msg.channel_name,
                        chat_id=msg.chat_id,
                        thread_id=thread_id,
                        text=latest_text,
                        is_final=False,
                        thread_ts=msg.thread_ts,
                    )
                )
                last_published_text = latest_text
                last_publish_at = now
        except Exception as exc:
            stream_error = exc
            if _is_thread_busy_error(exc):
                logger.warning("[Manager] thread busy (concurrent run rejected): thread_id=%s", thread_id)
            else:
                logger.exception("[Manager] streaming error: thread_id=%s", thread_id)
        finally:
            result = last_values if last_values is not None else {"messages": [{"type": "ai", "content": latest_text}]}
            response_text = _extract_response_text(result)
            artifacts = _collect_artifacts(thread_id, result, outputs_snapshot)
            response_text, attachments = _prepare_artifact_delivery(thread_id, response_text, artifacts)

            if not response_text:
                if attachments:
                    response_text = _format_artifact_text([attachment.virtual_path for attachment in attachments])
                elif stream_error:
                    if _is_thread_busy_error(stream_error):
                        response_text = THREAD_BUSY_MESSAGE
                    else:
                        response_text = "An error occurred while processing your request. Please try again."
                else:
                    response_text = latest_text or "(No response from agent)"

            logger.info(
                "[Manager] streaming response completed: thread_id=%s, response_len=%d, artifacts=%d, error=%s",
                thread_id,
                len(response_text),
                len(artifacts),
                stream_error,
            )
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel_name=msg.channel_name,
                    chat_id=msg.chat_id,
                    thread_id=thread_id,
                    text=response_text,
                    artifacts=artifacts,
                    attachments=attachments,
                    is_final=True,
                    thread_ts=msg.thread_ts,
                )
            )

    # -- command handling --------------------------------------------------

    async def _handle_command(self, msg: InboundMessage) -> None:
        text = msg.text.strip()
        parts = text.split(maxsplit=1)
        command = parts[0].lower().lstrip("/")

        if command == "bootstrap":
            from dataclasses import replace as _dc_replace

            chat_text = parts[1] if len(parts) > 1 else "Initialize workspace"
            chat_msg = _dc_replace(msg, text=chat_text, msg_type=InboundMessageType.CHAT)
            await self._handle_chat(chat_msg, extra_context={"is_bootstrap": True})
            return

        if command == "new":
            # Create a new thread on the LangGraph Server
            client = self._get_client()
            thread = await client.threads.create()
            new_thread_id = thread["thread_id"]
            self.store.set_thread_id(
                msg.channel_name,
                msg.chat_id,
                new_thread_id,
                topic_id=msg.topic_id,
                user_id=msg.user_id,
            )
            reply = "New conversation started."
        elif command == "status":
            thread_id = self.store.get_thread_id(msg.channel_name, msg.chat_id, topic_id=msg.topic_id)
            reply = f"Active thread: {thread_id}" if thread_id else "No active conversation."
        elif command == "models":
            reply = await self._fetch_gateway("/api/models", "models")
        elif command == "memory":
            reply = await self._fetch_gateway("/api/memory", "memory")
        elif command == "help":
            reply = (
                "Available commands:\n"
                "/bootstrap — Start a bootstrap session (enables agent setup)\n"
                "/new — Start a new conversation\n"
                "/status — Show current thread info\n"
                "/models — List available models\n"
                "/memory — Show memory status\n"
                "/help — Show this help"
            )
        else:
            available = " | ".join(sorted(KNOWN_CHANNEL_COMMANDS))
            reply = f"Unknown command: /{command}. Available commands: {available}"

        outbound = OutboundMessage(
            channel_name=msg.channel_name,
            chat_id=msg.chat_id,
            thread_id=self.store.get_thread_id(msg.channel_name, msg.chat_id) or "",
            text=reply,
            thread_ts=msg.thread_ts,
        )
        await self.bus.publish_outbound(outbound)

    async def _fetch_gateway(self, path: str, kind: str) -> str:
        """Fetch data from the Gateway API for command responses."""
        import httpx

        try:
            async with httpx.AsyncClient() as http:
                resp = await http.get(f"{self._gateway_url}{path}", timeout=10)
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            logger.exception("Failed to fetch %s from gateway", kind)
            return f"Failed to fetch {kind} information."

        if kind == "models":
            names = [m["name"] for m in data.get("models", [])]
            return ("Available models:\n" + "\n".join(f"• {n}" for n in names)) if names else "No models configured."
        elif kind == "memory":
            facts = data.get("facts", [])
            return f"Memory contains {len(facts)} fact(s)."
        return str(data)

    # -- error helper ------------------------------------------------------

    async def _send_error(self, msg: InboundMessage, error_text: str) -> None:
        outbound = OutboundMessage(
            channel_name=msg.channel_name,
            chat_id=msg.chat_id,
            thread_id=self.store.get_thread_id(msg.channel_name, msg.chat_id) or "",
            text=error_text,
            thread_ts=msg.thread_ts,
        )
        await self.bus.publish_outbound(outbound)
