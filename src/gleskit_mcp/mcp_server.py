from __future__ import annotations

from dataclasses import dataclass
import atexit
import ipaddress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import mimetypes
import os
from pathlib import Path
import re
import secrets
from threading import Lock
from threading import Thread
import time
from typing import Any, Literal
from urllib.parse import quote, urlsplit, urlunsplit

from gleskit_mcp.services import GLESKitService, build_default_service

try:
    from mcp.server.fastmcp import Context, FastMCP, Image
    from mcp.types import CallToolResult, ResourceLink, TextContent
except ImportError:  # pragma: no cover
    Context = Any
    FastMCP = None
    Image = None
    CallToolResult = None
    ResourceLink = None
    TextContent = None


def _require_loopback_host(host: str) -> str:
    """No network-facing unauthenticated server until remote auth is designed."""
    cleaned = str(host).strip()
    if cleaned == "localhost":
        return "127.0.0.1"
    try:
        if ipaddress.ip_address(cleaned).is_loopback:
            return cleaned
    except ValueError:
        pass
    raise ValueError("Public MCP/download binding is blocked: authenticated remote deployment is required. Use loopback or stdio; never publicly tunnel an unauthenticated endpoint.")


def _clean_figure_structured_payload(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(payload)
    cleaned.pop("_inline_image_bytes", None)
    cleaned.pop("_inline_file_bytes", None)
    return cleaned


def _extract_markdown_image_target(markdown: str) -> str:
    token = str(markdown or "").strip()
    if not token:
        return ""
    match = re.search(r"!\[[^\]]*\]\(([^)]+)\)", token)
    if not match:
        return ""
    return str(match.group(1) or "").strip()


def _is_embed_target_supported(target: str) -> bool:
    token = str(target or "").strip()
    if not token:
        return False
    lower = token.lower()
    if lower.startswith(("https://", "http://", "file://", "data:image/")):
        return True
    if token.startswith("/"):
        return True
    return len(token) > 2 and token[1] == ":" and token[2] in ("/", "\\")


def _figure_embed_candidates(
    title: str,
    *,
    figure_payload: dict[str, Any],
    preferred_download_url: str = "",
    download_links: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    normalized_title = str(title or "GLESKit figure").strip() or "GLESKit figure"
    links = download_links or []
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()

    def add_target(raw_target: str, source: str) -> None:
        target = str(raw_target or "").strip()
        if not target or target in seen or not _is_embed_target_supported(target):
            return
        seen.add(target)
        candidates.append(
            {
                "target": target,
                "markdown": f"![{normalized_title}]({target})",
                "source": source,
            }
        )

    add_target(preferred_download_url, "preferred_download_url")
    for row in links:
        add_target(str(row.get("http_url") or "").strip(), "download_links.http_url")
    for row in links:
        add_target(str(row.get("uri") or "").strip(), "download_links.uri")

    image_markdown = str(figure_payload.get("image_markdown") or "").strip()
    report_markdown = str(figure_payload.get("report_markdown") or "").strip()
    add_target(_extract_markdown_image_target(image_markdown), "image_markdown")
    add_target(_extract_markdown_image_target(report_markdown), "report_markdown")

    add_target(str(figure_payload.get("display_path") or "").strip(), "display_path")
    add_target(str(figure_payload.get("png_path") or "").strip(), "png_path")
    return candidates


def _env_positive_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return max(minimum, int(default))
    try:
        value = int(raw)
    except ValueError:
        return max(minimum, int(default))
    return max(minimum, value)


def _env_flag(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class _FigureDownloadEntry:
    data: bytes
    mime_type: str
    filename: str
    created_at: float
    expires_at: float


class _FigureDownloadRegistry:
    def __init__(self, *, ttl_seconds: int = 1800, max_entries: int = 256, web_exports=None) -> None:
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.max_entries = max(16, int(max_entries))
        self._entries: dict[str, _FigureDownloadEntry] = {}
        self._lock = Lock()
        self.web_exports = web_exports

    def _purge_locked(self, *, now: float | None = None) -> None:
        current = now if now is not None else time.time()
        expired = [token for token, entry in self._entries.items() if entry.expires_at <= current]
        for token in expired:
            self._entries.pop(token, None)
        while len(self._entries) > self.max_entries:
            oldest_token = next(iter(self._entries), None)
            if oldest_token is None:
                break
            self._entries.pop(oldest_token, None)

    def register(self, *, data: bytes, mime_type: str, filename: str) -> tuple[str, str]:
        now = time.time()
        with self._lock:
            self._purge_locked(now=now)
            token = secrets.token_urlsafe(18)
            self._entries[token] = _FigureDownloadEntry(
                data=data,
                mime_type=mime_type or "application/octet-stream",
                filename=filename,
                created_at=now,
                expires_at=now + self.ttl_seconds,
            )
            if self.web_exports is not None:
                self.web_exports.store(token, data, mime_type, filename, now + self.ttl_seconds)
            self._purge_locked(now=now)
        return token, f"gleskit://figure-download/{token}"

    def register_bytes(self, *, data: bytes, mime_type: str, filename: str) -> str:
        _token, uri = self.register(
            data=data,
            mime_type=mime_type,
            filename=filename,
        )
        return uri

    def get(self, token: str) -> _FigureDownloadEntry | None:
        cleaned = str(token or "").strip()
        if not cleaned:
            return None
        with self._lock:
            self._purge_locked()
            return self._entries.get(cleaned)


class _FigureDownloadHttpServer:
    def __init__(
        self,
        registry: _FigureDownloadRegistry,
        *,
        bind_host: str,
        port: int,
        public_base_url: str = "",
        path_prefix: str = "/figure-download",
    ) -> None:
        normalized_prefix = "/" + str(path_prefix or "").strip().strip("/")
        self._registry = registry
        self._path_prefix = normalized_prefix or "/figure-download"
        self._server = self._build_server(bind_host=bind_host, port=port)
        self._thread = Thread(target=self._server.serve_forever, name="gleskit-figure-downloads", daemon=True)
        self._thread.start()

        host, assigned_port = self._server.server_address[:2]
        runtime_host = str(host or bind_host or "127.0.0.1").strip() or "127.0.0.1"
        if runtime_host == "0.0.0.0":
            runtime_host = "127.0.0.1"
        if runtime_host == "::":
            runtime_host = "localhost"
        self.public_base_url = _normalize_public_base(
            public_base_url,
            streamable_http_path=self._path_prefix,
        ) or f"http://{runtime_host}:{int(assigned_port)}"

    def _build_server(self, *, bind_host: str, port: int) -> ThreadingHTTPServer:
        bind_host = _require_loopback_host(bind_host)
        registry = self._registry
        path_prefix = self._path_prefix

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                raw_path = str(self.path or "").split("?", 1)[0]
                if not raw_path.startswith(path_prefix + "/"):
                    self.send_error(404, "Not found")
                    return
                token = raw_path[len(path_prefix) + 1 :].strip()
                if not token:
                    self.send_error(404, "Not found")
                    return
                entry = registry.get(token)
                if entry is None:
                    body = b"This figure download link is unavailable or has expired."
                    self.send_response(404)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store, max-age=0")
                    self.end_headers()
                    self.wfile.write(body)
                    return

                safe_filename = (entry.filename or "gleskit-figure").replace('"', "")
                payload = entry.data
                self.send_response(200)
                self.send_header("Content-Type", entry.mime_type or "application/octet-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(safe_filename)}")
                self.send_header("Cache-Control", "no-store, max-age=0")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover
                return

        return ThreadingHTTPServer((bind_host, port), _Handler)

    def stop(self) -> None:
        try:
            self._server.shutdown()
        except Exception:
            pass
        try:
            self._server.server_close()
        except Exception:
            pass

    def url_for_token(self, token: str) -> str:
        cleaned = str(token or "").strip()
        if not cleaned or not self.public_base_url:
            return ""
        return f"{self.public_base_url}{self._path_prefix}/{cleaned}"


def _guess_mime_type(path_value: str) -> str:
    guessed, _encoding = mimetypes.guess_type(path_value)
    return guessed or "application/octet-stream"


def _slugify_filename_stem(value: str) -> str:
    token = "".join(ch if ch.isalnum() else "-" for ch in str(value or "").lower()).strip("-")
    while "--" in token:
        token = token.replace("--", "-")
    return token or "gleskit-figure"


def _figure_file_candidates(figure_payload: dict[str, Any]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for raw_path in (figure_payload.get("figure_files") or []):
        token = str(raw_path or "").strip()
        if token and token not in seen:
            seen.add(token)
            ordered.append(token)
    for key in ("png_path", "svg_path", "display_path"):
        token = str(figure_payload.get(key) or "").strip()
        if token and token not in seen:
            seen.add(token)
            ordered.append(token)
    return ordered


def _figure_inline_file_map(payload: dict[str, Any]) -> dict[str, bytes]:
    inline_map: dict[str, bytes] = {}
    raw_map = payload.get("_inline_file_bytes")
    if isinstance(raw_map, dict):
        for raw_path, raw_bytes in raw_map.items():
            token = str(raw_path or "").strip()
            if not token:
                continue
            if isinstance(raw_bytes, (bytes, bytearray)):
                inline_map[token] = bytes(raw_bytes)
    figure_payload = payload.get("payload") or {}
    png_path = str(figure_payload.get("png_path") or "").strip()
    inline_image_bytes = payload.get("_inline_image_bytes")
    if png_path and isinstance(inline_image_bytes, (bytes, bytearray)) and png_path not in inline_map:
        inline_map[png_path] = bytes(inline_image_bytes)
    return inline_map


def _figure_download_blocks(
    payload: dict[str, Any],
    download_registry: _FigureDownloadRegistry | None,
    *,
    http_url_builder: Any | None = None,
) -> list[Any]:
    if ResourceLink is None or download_registry is None:  # pragma: no cover
        return []

    figure_payload = payload.get("payload") or {}
    title = str(payload.get("title") or figure_payload.get("title") or "GLESKit figure").strip() or "GLESKit figure"
    inline_files = _figure_inline_file_map(payload)
    candidates = _figure_file_candidates(figure_payload)
    if not candidates:
        candidates = list(inline_files.keys())

    links: list[Any] = []
    used_filenames: set[str] = set()
    file_index = 1
    for raw_path in candidates:
        token = str(raw_path or "").strip()
        if not token:
            continue

        data = inline_files.get(token)
        if data is None:
            file_path = Path(token)
            if not file_path.exists():
                continue
            try:
                data = file_path.read_bytes()
            except OSError:
                continue
        if not data:
            continue

        suffix = Path(token).suffix.lower()
        mime_type = _guess_mime_type(token)
        filename = f"{_slugify_filename_stem(title)}{suffix}"
        if not suffix:
            filename = f"{_slugify_filename_stem(title)}-{file_index}"
        while filename in used_filenames:
            file_index += 1
            filename = f"{_slugify_filename_stem(title)}-{file_index}{suffix}"
        used_filenames.add(filename)
        file_index += 1

        token, uri = download_registry.register(
            data=data,
            mime_type=mime_type,
            filename=filename,
        )
        http_download_url = ""
        if callable(http_url_builder):
            try:
                http_download_url = str(http_url_builder(token) or "").strip()
            except Exception:
                http_download_url = ""
        label_suffix = suffix[1:].upper() if suffix.startswith(".") and len(suffix) > 1 else "FILE"
        description = "Temporary download link for this generated figure file."
        if http_download_url:
            description += " Browser-friendly HTTP download URL is also available in metadata."
        links.append(
            ResourceLink(
                type="resource_link",
                name=filename,
                title=f"Download {title} ({label_suffix})",
                description=description,
                uri=uri,
                mimeType=mime_type,
                size=len(data),
                _meta={"http_download_url": http_download_url} if http_download_url else None,
            )
        )
    return links


def _download_link_payloads(links: list[Any]) -> list[dict[str, Any]]:
    descriptors: list[dict[str, Any]] = []
    for link in links:
        if getattr(link, "type", None) != "resource_link":
            continue
        uri = str(getattr(link, "uri", "") or "").strip()
        if not uri:
            continue
        row = {
            "name": str(getattr(link, "name", "") or "").strip(),
            "title": str(getattr(link, "title", "") or "").strip(),
            "description": str(getattr(link, "description", "") or "").strip(),
            "uri": uri,
            "mime_type": str(getattr(link, "mimeType", "") or "").strip(),
            "size_bytes": int(getattr(link, "size", 0) or 0),
        }
        meta = getattr(link, "meta", None)
        if isinstance(meta, dict):
            http_download_url = str(meta.get("http_download_url") or "").strip()
            if http_download_url:
                row["http_url"] = http_download_url
        descriptors.append(row)
    return descriptors


def _preferred_download_link(download_links: list[dict[str, Any]]) -> dict[str, Any]:
    for row in download_links:
        http_url = str(row.get("http_url") or "").strip()
        if http_url:
            return row
    return download_links[0] if download_links else {}


def _normalize_public_base(raw: str, *, streamable_http_path: str) -> str:
    token = str(raw or "").strip().rstrip("/")
    if not token:
        return ""
    parsed = urlsplit(token)
    path = parsed.path or ""
    if streamable_http_path and path.endswith(streamable_http_path):
        path = path[: -len(streamable_http_path)]
    if not path:
        path = ""
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _public_base_from_request(
    request: Any | None,
    *,
    public_base_url: str = "",
    normalized_stream_path: str = "",
) -> str:
    if public_base_url:
        return public_base_url
    if request is None:
        return ""

    headers = getattr(request, "headers", None)
    safe_headers = headers if headers is not None else {}

    forwarded = str(safe_headers.get("forwarded", "") or "").strip()
    forwarded_proto = ""
    forwarded_host = ""
    if forwarded:
        first = forwarded.split(",", 1)[0]
        for segment in first.split(";"):
            key, sep, value = segment.partition("=")
            if not sep:
                continue
            norm_key = key.strip().lower()
            norm_value = value.strip().strip('"')
            if norm_key == "proto" and norm_value and not forwarded_proto:
                forwarded_proto = norm_value
            elif norm_key == "host" and norm_value and not forwarded_host:
                forwarded_host = norm_value

    forwarded_host_header = str(safe_headers.get("x-forwarded-host", "") or "").split(",", 1)[0].strip()
    host_header = str(safe_headers.get("host", "") or "").split(",", 1)[0].strip()
    forwarded_proto_header = str(safe_headers.get("x-forwarded-proto", "") or "").split(",", 1)[0].strip()
    forwarded_prefix = str(safe_headers.get("x-forwarded-prefix", "") or "").split(",", 1)[0].strip()
    authority_header = str(safe_headers.get(":authority", "") or "").split(",", 1)[0].strip()
    origin_header = str(safe_headers.get("origin", "") or "").split(",", 1)[0].strip()
    referer_header = str(safe_headers.get("referer", "") or "").split(",", 1)[0].strip()

    request_url = getattr(request, "url", None)
    request_url_scheme = str(getattr(request_url, "scheme", "") or "").strip()
    request_url_netloc = str(getattr(request_url, "netloc", "") or "").strip()
    request_base_url = getattr(request, "base_url", None)
    request_base_url_str = str(request_base_url or "").strip()

    origin_base = _normalize_public_base(origin_header, streamable_http_path=normalized_stream_path or "/mcp")
    referer_base = _normalize_public_base(referer_header, streamable_http_path=normalized_stream_path or "/mcp")
    base_url_base = _normalize_public_base(request_base_url_str, streamable_http_path=normalized_stream_path or "/mcp")

    if origin_base:
        return origin_base
    if referer_base:
        return referer_base
    if base_url_base:
        return base_url_base

    scheme = forwarded_proto or forwarded_proto_header or request_url_scheme
    host_token = forwarded_host or forwarded_host_header or host_header or authority_header or request_url_netloc
    if not host_token:
        return ""
    if not scheme:
        scheme = "https"

    request_path = str(getattr(request_url, "path", "") or "").strip()
    base_path = ""
    if normalized_stream_path and request_path.endswith(normalized_stream_path):
        base_path = request_path[: -len(normalized_stream_path)]
    elif forwarded_prefix:
        base_path = forwarded_prefix
    base_path = str(base_path or "").strip()
    if base_path and not base_path.startswith("/"):
        base_path = "/" + base_path
    if base_path == "/":
        base_path = ""

    return f"{scheme}://{host_token}{base_path}".rstrip("/")


def _figure_content_blocks(payload: dict[str, Any]) -> list[Any]:
    if Image is None:  # pragma: no cover
        return []

    figure_payload = payload.get("payload") or {}
    inline_image_bytes = payload.get("_inline_image_bytes")
    png_path = str(figure_payload.get("png_path") or "").strip()
    content: list[Any] = []
    if inline_image_bytes:
        content.append(Image(data=inline_image_bytes, format="png").to_image_content())
    elif png_path and Path(png_path).exists():
        content.append(Image(path=png_path).to_image_content())
    return content


def _figure_tool_result(
    payload: dict[str, Any],
    *,
    download_registry: _FigureDownloadRegistry | None = None,
    http_url_builder: Any | None = None,
) -> Any:
    if CallToolResult is None or Image is None or TextContent is None:  # pragma: no cover
        return payload

    figure_payload = payload.get("payload") or {}
    title = str(payload.get("title") or figure_payload.get("title") or "GLESKit figure").strip() or "GLESKit figure"
    caption = str(payload.get("summary") or figure_payload.get("caption") or "").strip()

    content = _figure_content_blocks(payload)
    download_blocks = _figure_download_blocks(
        payload,
        download_registry,
        http_url_builder=http_url_builder,
    )
    if download_blocks:
        content.extend(download_blocks)
    download_links = _download_link_payloads(download_blocks)
    preferred_link = _preferred_download_link(download_links)
    preferred_target = str(preferred_link.get("http_url") or preferred_link.get("uri") or "").strip()
    embed_candidates = _figure_embed_candidates(
        title,
        figure_payload=figure_payload,
        preferred_download_url=preferred_target,
        download_links=download_links,
    )

    text_lines = [title]
    if caption and caption != title:
        text_lines.append(caption)
    source_info = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    source_id = str((source_info or {}).get("figure_source_id") or payload.get("figure_source_id") or "").strip()
    if source_id:
        text_lines.append(f"Figure source id: `{source_id}`.")
    artifact_id = str(payload.get("artifact_id") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()
    if artifact_id and session_id:
        text_lines.append(f"Saved as figure artifact `{artifact_id}` in session `{session_id}`.")
    if download_blocks:
        text_lines.append(f"Attached {len(download_blocks)} temporary download link(s) for this figure.")
        text_lines.append(
            "Use attached resource links for downloads; local payload paths such as png_path/svg_path/figure_files are not chat-download URLs."
        )
    if download_links:
        text_lines.append("Direct download URIs:")
        for row in download_links[:3]:
            label = str(row.get("name") or row.get("title") or "figure").strip() or "figure"
            target = str(row.get("http_url") or row.get("uri") or "").strip()
            if target:
                text_lines.append(f"- {label}: {target}")
        if preferred_target:
            text_lines.append(f"Preferred user-facing download URL: {preferred_target}")
    if embed_candidates:
        text_lines.append(f"Preferred markdown embed: {embed_candidates[0]['markdown']}")
        if len(embed_candidates) > 1:
            text_lines.append("Alternative markdown embeds:")
            for row in embed_candidates[1:4]:
                text_lines.append(f"- {row['markdown']}")

    content.append(TextContent(type="text", text="\n".join(text_lines)))
    structured = _clean_figure_structured_payload(payload)
    if download_links:
        structured["download_links"] = download_links
        structured["download_links_note"] = (
            "Prefer preferred_download_url when present. Otherwise use download_links/resource_link URIs for user-facing downloads. "
            "Local payload paths (png_path/svg_path/figure_files) are internal file-system paths."
        )
        if preferred_target:
            structured["preferred_download_url"] = preferred_target
            structured["preferred_download_name"] = str(
                preferred_link.get("name") or preferred_link.get("title") or "figure"
            ).strip() or "figure"
    if embed_candidates:
        structured["preferred_embed_markdown"] = embed_candidates[0]["markdown"]
        structured["preferred_embed_target"] = embed_candidates[0]["target"]
        structured["embed_markdown_candidates"] = [row["markdown"] for row in embed_candidates]
        structured["embed_candidates"] = embed_candidates
    return CallToolResult(content=content, structuredContent=structured)


def _auto_figure_tool_result(
    tool_name: str,
    payload: dict[str, Any],
    svc: GLESKitService,
    *,
    download_registry: _FigureDownloadRegistry | None = None,
    http_url_builder: Any | None = None,
) -> Any:
    if CallToolResult is None or Image is None or TextContent is None:  # pragma: no cover
        return payload
    if _env_flag("GLESKIT_DISABLE_INLINE_FIGURES"):
        return svc.preview_tool_response(payload, source_kind="artifact")
    figures = svc.auto_figures_for_result(tool_name, payload)
    if not figures:
        return svc.preview_tool_response(payload, source_kind="artifact")

    content: list[Any] = []
    download_count = 0
    all_download_links: list[dict[str, Any]] = []
    all_embed_candidates: list[dict[str, str]] = []
    cleaned_figures: list[dict[str, Any]] = []
    for figure in figures:
        content.extend(_figure_content_blocks(figure))
        download_blocks = _figure_download_blocks(
            figure,
            download_registry,
            http_url_builder=http_url_builder,
        )
        if download_blocks:
            content.extend(download_blocks)
            download_count += len(download_blocks)
        cleaned = _clean_figure_structured_payload(figure)
        figure_download_links = _download_link_payloads(download_blocks)
        if figure_download_links:
            cleaned["download_links"] = figure_download_links
            all_download_links.extend(figure_download_links)
        figure_payload = cleaned.get("payload") if isinstance(cleaned.get("payload"), dict) else {}
        figure_title = str(figure.get("title") or (figure_payload or {}).get("title") or "GLESKit figure").strip() or "GLESKit figure"
        preferred_figure_link = _preferred_download_link(figure_download_links)
        preferred_figure_target = str(
            preferred_figure_link.get("http_url") or preferred_figure_link.get("uri") or ""
        ).strip()
        figure_embed_candidates = _figure_embed_candidates(
            figure_title,
            figure_payload=figure_payload if isinstance(figure_payload, dict) else {},
            preferred_download_url=preferred_figure_target,
            download_links=figure_download_links,
        )
        if figure_embed_candidates:
            cleaned["preferred_embed_markdown"] = figure_embed_candidates[0]["markdown"]
            cleaned["preferred_embed_target"] = figure_embed_candidates[0]["target"]
            cleaned["embed_markdown_candidates"] = [row["markdown"] for row in figure_embed_candidates]
            cleaned["embed_candidates"] = figure_embed_candidates
            all_embed_candidates.extend(figure_embed_candidates)
        cleaned_figures.append(cleaned)

    text_lines: list[str] = []
    summary = str(payload.get("summary") or "").strip()
    if summary:
        text_lines.append(summary)
    top_terms = [
        item
        for item in (payload.get("top_terms") or [])
        if isinstance(item, dict) and str(item.get("term_name") or "").strip()
    ]
    if top_terms:
        text_lines.append("Top terms:")
        for row in top_terms[:3]:
            term_name = str(row.get("term_name") or "").strip()
            adjusted = row.get("adjusted_p_value")
            if adjusted not in (None, ""):
                text_lines.append(f"- {term_name} (adjusted p={adjusted})")
            else:
                text_lines.append(f"- {term_name}")
    if len(figures) == 1:
        text_lines.append("GLESKit auto-generated one native figure for this result.")
    else:
        text_lines.append(f"GLESKit auto-generated {len(figures)} native figures for this result.")
    text_lines.extend(
        f"- {str(figure.get('title') or 'GLESKit figure').strip()}"
        for figure in figures
    )
    source_id = str(payload.get("figure_source_id") or "").strip()
    if source_id:
        text_lines.append(f"Figure source id: `{source_id}`.")
        text_lines.append(
            "For a specific visualization type, call list_figure_options with this figure_source_id, then call generate_figure with the chosen figure_type."
        )
        text_lines.append(
            "If the user asked for a network/graph, do not stop at a default bar plot: list options and generate the matching network figure type."
        )
    if download_count:
        text_lines.append(f"Attached {download_count} temporary download link(s) for generated figure file(s).")
        text_lines.append(
            "Use attached resource links for downloads; local payload paths such as png_path/svg_path/figure_files are not chat-download URLs."
        )
    if all_download_links:
        text_lines.append("Direct download URIs:")
        for row in all_download_links[:3]:
            label = str(row.get("name") or row.get("title") or "figure").strip() or "figure"
            target = str(row.get("http_url") or row.get("uri") or "").strip()
            if target:
                text_lines.append(f"- {label}: {target}")
        preferred_link = _preferred_download_link(all_download_links)
        preferred_target = str(preferred_link.get("http_url") or preferred_link.get("uri") or "").strip()
        if preferred_target:
            text_lines.append(f"Preferred user-facing download URL: {preferred_target}")
    if all_embed_candidates:
        deduped_embed_markdown: list[str] = []
        seen_embed_markdown: set[str] = set()
        for row in all_embed_candidates:
            markdown = str(row.get("markdown") or "").strip()
            if not markdown or markdown in seen_embed_markdown:
                continue
            seen_embed_markdown.add(markdown)
            deduped_embed_markdown.append(markdown)
        if deduped_embed_markdown:
            text_lines.append(f"Preferred markdown embed: {deduped_embed_markdown[0]}")
            if len(deduped_embed_markdown) > 1:
                text_lines.append("Alternative markdown embeds:")
                for markdown in deduped_embed_markdown[1:4]:
                    text_lines.append(f"- {markdown}")

    content.append(TextContent(type="text", text="\n".join(text_lines)))
    structured = dict(payload)
    structured["auto_figures"] = cleaned_figures
    if all_download_links:
        preferred_link = _preferred_download_link(all_download_links)
        preferred_target = str(preferred_link.get("http_url") or preferred_link.get("uri") or "").strip()
        structured["download_links"] = all_download_links
        structured["download_links_note"] = (
            "Prefer preferred_download_url when present. Otherwise use download_links/resource_link URIs for user-facing downloads. "
            "Local payload paths (png_path/svg_path/figure_files) are internal file-system paths."
        )
        if preferred_target:
            structured["preferred_download_url"] = preferred_target
            structured["preferred_download_name"] = str(
                preferred_link.get("name") or preferred_link.get("title") or "figure"
            ).strip() or "figure"
    if all_embed_candidates:
        deduped_embed_candidates: list[dict[str, str]] = []
        seen_targets: set[str] = set()
        for row in all_embed_candidates:
            target = str(row.get("target") or "").strip()
            if not target or target in seen_targets:
                continue
            seen_targets.add(target)
            deduped_embed_candidates.append(row)
        if deduped_embed_candidates:
            structured["preferred_embed_markdown"] = deduped_embed_candidates[0]["markdown"]
            structured["preferred_embed_target"] = deduped_embed_candidates[0]["target"]
            structured["embed_markdown_candidates"] = [row["markdown"] for row in deduped_embed_candidates]
            structured["embed_candidates"] = deduped_embed_candidates
    return CallToolResult(content=content, structuredContent=structured)


def build_server(
    service: GLESKitService | None = None,
    *,
    project_root: str | Path | None = None,
    host: str | None = None,
    port: int | None = None,
    streamable_http_path: str | None = None,
    start_local_download_server: bool = False,
):
    if FastMCP is None:  # pragma: no cover
        raise RuntimeError(
            "The Python MCP package is not installed. Install the optional dependency with "
            "`pip install 'gleskit-mcp[mcp]'` or `pip install mcp`."
        )

    svc = service or build_default_service(Path(project_root) if project_root is not None else None)
    settings = svc.settings
    web_exports = None
    if os.environ.get("GLESKIT_WEB_PROFILE") or os.environ.get("GLESKIT_WEB_ORIGIN"):
        from gleskit_mcp.web.files import FigureDownloads
        web_profile = Path(os.environ.get("GLESKIT_WEB_PROFILE", ""))
        if not web_profile.is_absolute() or not (web_profile / "web-profile.json").is_file():
            raise ValueError("A private UI profile is required for web figure delivery.")
        web_exports = FigureDownloads(web_profile, os.environ.get("GLESKIT_WEB_ORIGIN", ""))
    download_registry = _FigureDownloadRegistry(
        ttl_seconds=max(1, int(settings.figure_download_ttl_seconds or 1800)),
        max_entries=max(1, int(settings.figure_download_max_entries or 256)),
        web_exports=web_exports,
    )
    resolved_host = _require_loopback_host(host or settings.fastmcp_host)
    resolved_port = port if port is not None else max(0, int(settings.fastmcp_port or 8000))
    resolved_streamable_http_path = streamable_http_path or settings.fastmcp_streamable_http_path
    normalized_stream_path = "/" + str(resolved_streamable_http_path or "").strip().strip("/")
    public_base_raw = str(settings.public_base_url or "").strip()
    download_public_base_raw = str(settings.download_public_base_url or "").strip()
    download_path_prefix = "/" + str(settings.download_path_prefix or "/figure-download").strip().strip("/")
    download_server: _FigureDownloadHttpServer | None = None

    public_base_url = _normalize_public_base(
        public_base_raw,
        streamable_http_path=resolved_streamable_http_path,
    )
    download_public_base_url = _normalize_public_base(
        download_public_base_raw,
        streamable_http_path=download_path_prefix,
    )

    if start_local_download_server and web_exports is None:
        download_bind_host = str(settings.download_bind_host or "127.0.0.1").strip() or "127.0.0.1"
        download_port = max(0, int(settings.download_port or 0))
        try:
            download_server = _FigureDownloadHttpServer(
                download_registry,
                bind_host=download_bind_host,
                port=download_port,
                public_base_url=download_public_base_url,
                path_prefix=download_path_prefix,
            )
        except OSError:
            download_server = None
        else:
            atexit.register(download_server.stop)

    def _public_base_from_context(ctx: Context | None) -> str:
        if ctx is None:
            return _public_base_from_request(
                None,
                public_base_url=public_base_url,
                normalized_stream_path=normalized_stream_path,
            )
        try:
            request = ctx.request_context.request
        except Exception:
            request = None
        return _public_base_from_request(
            request,
            public_base_url=public_base_url,
            normalized_stream_path=normalized_stream_path,
        )

    def _http_download_url_for_token(token: str, *, ctx: Context | None = None) -> str:
        cleaned = str(token or "").strip()
        if not cleaned:
            return ""
        if web_exports is not None:
            return web_exports.url(cleaned)
        if download_public_base_url:
            return f"{download_public_base_url}{download_path_prefix}/{cleaned}"
        if download_server is not None:
            return download_server.url_for_token(cleaned)

        base_url = _public_base_from_context(ctx)
        if not base_url:
            return ""
        return f"{base_url}{download_path_prefix}/{cleaned}"

    server = FastMCP(
        "GLESKit",
        host=resolved_host,
        port=resolved_port,
        streamable_http_path=resolved_streamable_http_path,
    )

    @server.tool()
    def workspace_status() -> dict[str, Any]:
        """Report whether the optional desktop workspace bridge is currently available."""

        return svc.workspace_status()

    @server.tool()
    def reference_status() -> dict[str, Any]:
        """Report whether the local reference bundle is installed and which datasets are available."""

        return svc.reference_status()

    @server.tool()
    def update_references() -> dict[str, Any]:
        """Refresh the local reference bundle from upstream curated sources."""

        return svc.update_references()

    @server.tool()
    def list_report_templates() -> dict[str, Any]:
        """List the available report templates that can guide authored report writing."""

        return svc.list_report_templates()

    @server.tool()
    def get_report_template(template_id: str) -> dict[str, Any]:
        """Fetch the full section layout and guidance for one report template."""

        return svc.get_report_template(template_id)

    @server.tool()
    def create_session(title: str, description: str, species: str = "human", tags: list[str] | None = None) -> dict[str, Any]:
        """Create a saved workspace session. Only use when the user explicitly asks to persist work."""

        return svc.create_session(title=title, description=description, species=species, tags=tags or [])

    @server.tool()
    def update_session(session_id: str, title: str = "", description: str = "") -> dict[str, Any]:
        """Update the title and/or description of a saved session."""

        return svc.update_session(
            session_id,
            title=title if title.strip() else None,
            description=description if description.strip() else None,
        )

    @server.tool()
    def add_session_note(
        session_id: str,
        title: str,
        body_markdown: str,
        author: str = "assistant",
        linked_artifact_ids: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Add a markdown note to a saved session."""

        return svc.add_session_note(
            session_id,
            title=title,
            body_markdown=body_markdown,
            author=author,
            linked_artifact_ids=linked_artifact_ids or [],
            tags=tags or [],
        )

    @server.tool()
    def list_session_notes(session_id: str) -> dict[str, Any]:
        """List markdown notes for a saved session."""

        return svc.list_session_notes(session_id)

    @server.tool()
    def get_session_note(session_id: str, note_id: str) -> dict[str, Any]:
        """Fetch one markdown note from a saved session."""

        return svc.get_session_note(session_id, note_id)

    @server.tool()
    def update_session_note(
        session_id: str,
        note_id: str,
        title: str,
        body_markdown: str,
        author: str = "assistant",
        linked_artifact_ids: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Update a markdown note in a saved session."""

        return svc.update_session_note(
            session_id,
            note_id,
            title=title,
            body_markdown=body_markdown,
            author=author,
            linked_artifact_ids=linked_artifact_ids or [],
            tags=tags or [],
        )

    @server.tool()
    def delete_session_note(session_id: str, note_id: str) -> dict[str, Any]:
        """Delete a markdown note from a saved session. Only use after explicit user request."""

        return svc.delete_session_note(session_id, note_id)

    @server.tool()
    def delete_session_notes(session_id: str, note_ids: list[str]) -> dict[str, Any]:
        """Delete multiple markdown notes from a saved session. Only use after explicit user request."""

        return svc.delete_session_notes(session_id, note_ids)

    @server.tool()
    def list_sessions(query: str = "", status: str = "") -> dict[str, Any]:
        """List saved workspace sessions. Use only when the user explicitly asks to browse local storage."""

        return svc.list_sessions(query=query, status=status)

    @server.tool()
    def get_session_summary(session_id: str) -> dict[str, Any]:
        """Get the high-level summary for a saved session."""

        return svc.get_session_summary(session_id)

    @server.tool()
    def list_session_artifacts(session_id: str, kind: str = "") -> dict[str, Any]:
        """List stored artifacts for a saved session."""

        return svc.list_artifacts(session_id, kind=kind or None)

    @server.tool()
    def list_session_uploads(session_id: str) -> dict[str, Any]:
        """List user-provided uploads for a saved session."""

        return svc.list_uploads(session_id)

    @server.tool()
    def get_upload(session_id: str = "", upload_id: str = "") -> dict[str, Any]:
        """Fetch one user-provided upload from a saved session."""

        return svc.get_upload(session_id.strip() or None, upload_id.strip())

    @server.tool()
    def save_analysis_manifest_to_session(
        session_id: str,
        manifest_path: str,
        kind: str = "analysis",
        title: str = "",
        summary: str = "",
        provider: str = "gleskit_local",
    ) -> dict[str, Any]:
        """Persist one generated analysis manifest to a specified session without changing the active session."""

        return svc.save_analysis_manifest_to_session(
            session_id=session_id,
            manifest_path=manifest_path,
            kind=kind,
            title=title,
            summary=summary,
            provider=provider,
        )

    @server.tool()
    def analysis_runtime_status() -> dict[str, Any]:
        """Check registered R analysis jobs after a timeout or before retrying a long analysis."""

        return svc.analysis_runtime_status()

    @server.tool()
    def cancel_analysis_job(job_id: str) -> dict[str, Any]:
        """Request cancellation of one registered R analysis process group."""

        return svc.cancel_analysis_job(job_id)

    @server.tool()
    def delete_session_uploads(session_id: str, upload_ids: list[str]) -> dict[str, Any]:
        """Delete uploads from a saved session. Only use after explicit user request."""

        return svc.delete_session_uploads(session_id, upload_ids)

    @server.tool()
    def create_figure(
        session_id: str,
        artifact_id: str,
        figure_type: str,
        title: str = "",
        top_n: int = 10,
        ctx: Context | None = None,
    ) -> Any:
        """Legacy save-backed figure creation from a saved artifact. Use this only for explicit save/reopen workflows; for normal plotting prefer list_figure_options + generate_figure with figure_source_id (no session required)."""

        payload = svc.create_figure(
            session_id,
            artifact_id,
            figure_type=figure_type,
            title=title,
            top_n=top_n,
        )
        return _figure_tool_result(
            payload,
            download_registry=download_registry,
            http_url_builder=(lambda token: _http_download_url_for_token(token, ctx=ctx)),
        )

    @server.tool()
    def list_figure_options(
        figure_source_id: str = "",
        session_id: str = "",
        artifact_id: str = "",
    ) -> dict[str, Any]:
        """List figure options for one source. Prefer figure_source_id from an upstream analysis result (stateless); use session_id/artifact_id only for saved artifacts."""

        return svc.list_figure_options(
            figure_source_id=figure_source_id,
            session_id=session_id,
            artifact_id=artifact_id,
        )

    @server.tool()
    def generate_figure(
        figure_type: str,
        figure_source_id: str = "",
        session_id: str = "",
        artifact_id: str = "",
        title: str = "",
        top_n: int = 10,
        save_to_session_id: str = "",
        ctx: Context | None = None,
    ) -> Any:
        """Primary figure tool. Generate from figure_source_id (stateless) or a saved artifact; session is optional and only needed when saving via save_to_session_id."""

        payload = svc.generate_figure(
            figure_type=figure_type,
            figure_source_id=figure_source_id,
            session_id=session_id,
            artifact_id=artifact_id,
            title=title,
            top_n=top_n,
            save_to_session_id=save_to_session_id,
        )
        return _figure_tool_result(
            payload,
            download_registry=download_registry,
            http_url_builder=(lambda token: _http_download_url_for_token(token, ctx=ctx)),
        )

    @server.tool()
    def list_session_reports(session_id: str) -> dict[str, Any]:
        """List saved markdown reports for a saved session."""

        return svc.list_session_reports(session_id)

    @server.tool()
    def get_session_report(session_id: str, report_id: str = "") -> dict[str, Any]:
        """Fetch one saved markdown report from a saved session. Defaults to the latest report."""

        return svc.get_session_report(session_id, report_id or None)

    @server.tool()
    def create_session_report(
        session_id: str,
        body_markdown: str,
        template_id: str,
        title: str = "",
        author: str = "assistant",
        focus: str = "",
        custom_guidance: str = "",
        linked_artifact_ids: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a saved authored markdown report for a session. Use only after the report type was explicitly chosen."""

        return svc.create_session_report(
            session_id,
            body_markdown=body_markdown,
            template_id=template_id,
            title=title,
            author=author,
            focus=focus,
            custom_guidance=custom_guidance,
            linked_artifact_ids=linked_artifact_ids or [],
            tags=tags or [],
        )

    @server.tool()
    def update_session_report(
        session_id: str,
        report_id: str,
        body_markdown: str,
        title: str | None = None,
        author: str | None = None,
        focus: str | None = None,
        template_id: str | None = None,
        custom_guidance: str | None = None,
        linked_artifact_ids: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Update a saved authored markdown report in a session."""

        return svc.update_session_report(
            session_id,
            report_id,
            title=title,
            body_markdown=body_markdown,
            author=author,
            focus=focus,
            template_id=template_id,
            custom_guidance=custom_guidance,
            linked_artifact_ids=linked_artifact_ids,
            tags=tags,
        )

    @server.tool()
    def delete_session_report(session_id: str, report_id: str) -> dict[str, Any]:
        """Delete a saved report from a session. Only use after explicit user request."""

        return svc.delete_session_report(session_id, report_id)

    @server.tool()
    def delete_session_reports(session_id: str, report_ids: list[str]) -> dict[str, Any]:
        """Delete multiple saved reports from a session. Only use after explicit user request."""

        return svc.delete_session_reports(session_id, report_ids)

    @server.tool()
    def get_artifact(session_id: str = "", artifact_id: str = "", ctx: Context | None = None) -> Any:
        """Fetch one stored artifact from a saved session. Prefer calling with both session_id and artifact_id."""

        payload = svc.get_artifact(session_id.strip() or None, artifact_id.strip())
        if str(payload.get("kind") or "").strip() == "figure":
            return _figure_tool_result(
                payload,
                download_registry=download_registry,
                http_url_builder=(lambda token: _http_download_url_for_token(token, ctx=ctx)),
            )
        return payload

    @server.tool()
    def delete_session_artifacts(session_id: str, artifact_ids: list[str]) -> dict[str, Any]:
        """Delete stored artifacts from a saved session. Only use after the user has explicitly confirmed deletion."""

        return svc.delete_session_artifacts(session_id, artifact_ids)

    @server.tool()
    def archive_session(session_id: str) -> dict[str, Any]:
        """Archive a saved session."""

        return svc.archive_session(session_id)

    @server.tool()
    def plan_delete_sessions(session_ids: list[str]) -> dict[str, Any]:
        """Preview which saved sessions would be deleted. Use before destructive deletion."""

        return svc.plan_delete_sessions(session_ids)

    @server.tool()
    def delete_sessions(session_ids: list[str]) -> dict[str, Any]:
        """Delete saved sessions. Only use after the user has explicitly confirmed deletion."""

        return svc.delete_sessions(session_ids)

    @server.tool()
    def create_session_from_artifacts(
        title: str,
        description: str,
        sources: list[dict[str, str]],
        species: str = "human",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a new saved session by copying artifacts from existing sessions."""

        return svc.create_session_from_artifacts(
            title=title,
            description=description,
            sources=sources,
            species=species,
            tags=tags or [],
        )

    @server.tool()
    def resolve_gene(query: str, species: str = "human") -> dict[str, Any]:
        """Resolve one gene symbol or alias. Do not call this repeatedly for a gene list; use resolve_gene_set or set-level tools such as investigate_gene_set, enrich_gene_set, gene_set_disease_context, or compare_gene_programs because those tools normalize genes internally."""

        return svc.resolve_gene(query=query, species=species)

    @server.tool()
    def resolve_gene_set(genes: list[str], species: str = "human", limit: int = 300) -> dict[str, Any]:
        """Resolve a whole gene list in one call. Use this only when you need an explicit batch normalization audit; ordinary set-level analysis tools already normalize inputs internally."""

        return svc.resolve_gene_set(genes=genes, species=species, limit=limit)

    @server.tool()
    def gene_summary(query: str, species: str = "human") -> dict[str, Any]:
        """Build a gene summary from local gene normalization plus Ensembl, UniProt, and QuickGO context."""

        return svc.gene_summary(query=query, species=species)

    @server.tool()
    def resolve_protein(query: str, species: str = "human") -> dict[str, Any]:
        """Resolve a protein accession, gene-linked protein, or protein name using UniProt."""

        return svc.resolve_protein(query=query, species=species)

    @server.tool()
    def protein_summary(query: str, species: str = "human") -> dict[str, Any]:
        """Build a protein summary from UniProt and linked gene context. This tool is stateless unless a desktop workspace session is active."""

        return svc.protein_summary(query=query, species=species)

    @server.tool()
    def drug_target_context(gene: str, species: str = "human", limit: int = 10) -> dict[str, Any]:
        """Retrieve ChEMBL drug-target and indication context for a human gene."""

        return svc.drug_target_context(gene=gene, species=species, limit=limit)

    @server.tool()
    def variant_trait_associations(gene: str, species: str = "human", limit: int = 10) -> dict[str, Any]:
        """Retrieve GWAS Catalog variant-trait associations for a human gene."""

        return svc.variant_trait_associations(gene=gene, species=species, limit=limit)

    @server.tool()
    def interaction_network(genes: list[str], species: str = "human", limit: int = 25) -> dict[str, Any]:
        """Retrieve STRING interaction-network context for a gene set. This tool is stateless unless a desktop workspace session is active."""

        return svc.interaction_network(genes=genes, species=species, limit=limit)

    @server.tool()
    def pathway_context(genes: list[str], species: str = "human", limit: int = 5) -> dict[str, Any]:
        """Build Reactome-focused pathway context for a gene set. This tool is stateless unless a desktop workspace session is active."""

        return svc.pathway_context(genes=genes, species=species, limit=limit)

    @server.tool()
    def expression_context(gene: str, species: str = "human", limit: int = 10, ctx: Context | None = None) -> dict[str, Any]:
        """Build reference expression context for a gene using GTEx and Expression Atlas."""

        payload = svc.expression_context(gene=gene, species=species, limit=limit)
        payload = svc.attach_figure_source_id("expression_context", payload)
        return _auto_figure_tool_result(
            "expression_context",
            payload,
            svc,
            download_registry=download_registry,
            http_url_builder=(lambda token: _http_download_url_for_token(token, ctx=ctx)),
        )

    @server.tool()
    def analyze_rnaseq_result(
        result_path: str = "",
        table_text: str = "",
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
        species: str = "human",
        label: str = "",
        comparison: str = "",
        gene_column: str = "",
        log2_fold_change_column: str = "",
        padj_column: str = "",
        p_value_column: str = "",
        base_mean_column: str = "",
        dispersion_column: str = "",
        stat_column: str = "",
        padj_threshold: float = 0.05,
        p_value_threshold: float = 0.05,
        base_mean_threshold: float = 0.0,
        log2_fold_change_threshold: float = 0.0,
        top_n: int = 25,
        focus_genes: list[str] | None = None,
        gene_alias_table_path: str = "",
        gene_alias_table_text: str = "",
        gene_alias_query_column: str = "",
        gene_alias_target_column: str = "",
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Interpret one existing differential-expression result table or an RDS result object.

        Use this when the user already has a DEG/result table. `result_path` may be an absolute path, `upload_...`,
        or a reusable `artifact_...` selector. It may also point to a `.rds` DESeq2 result; GLESKit converts it
        internally to a reusable TSV. If the question targets one specific gene, pass it in `focus_genes`
        so the tool can surface direct gene-level metrics. The tool also returns compact `derived_inputs` follow-up
        paths for enrichment, DEG comparison, and ranked GSEA. If the user has raw counts instead of results, use
        `analyze_deseq2_counts` instead. `padj_threshold`, `p_value_threshold`, `base_mean_threshold`, and
        `log2_fold_change_threshold` are result-table filters: pass `log2_fold_change_threshold=0.0` when the
        question does not state an absolute LFC cutoff; do not infer one from a default. Use the returned handoff
        paths for downstream enrichment instead of reopening the full result table. The returned
        `all_significant_genes_path` contains every gene meeting the stated filters; directional handoffs are
        compact ranked follow-up lists and should not replace the all-significant set for an undirected question.
        """

        payload = svc.analyze_rnaseq_result(
            result_path=result_path,
            table_text=table_text,
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
            species=species,
            label=label,
            comparison=comparison,
            gene_column=gene_column,
            log2_fold_change_column=log2_fold_change_column,
            padj_column=padj_column,
            p_value_column=p_value_column,
            base_mean_column=base_mean_column,
            dispersion_column=dispersion_column,
            stat_column=stat_column,
            padj_threshold=padj_threshold,
            p_value_threshold=p_value_threshold,
            base_mean_threshold=base_mean_threshold,
            log2_fold_change_threshold=log2_fold_change_threshold,
            top_n=top_n,
            focus_genes=focus_genes,
            gene_alias_table_path=gene_alias_table_path,
            gene_alias_table_text=gene_alias_table_text,
            gene_alias_query_column=gene_alias_query_column,
            gene_alias_target_column=gene_alias_target_column,
        )
        payload = svc.attach_figure_source_id("analyze_rnaseq_result", payload)
        return _auto_figure_tool_result(
            "analyze_rnaseq_result",
            payload,
            svc,
            download_registry=download_registry,
            http_url_builder=(lambda token: _http_download_url_for_token(token, ctx=ctx)),
        )

    @server.tool()
    def analyze_differential_result(
        modality: str,
        positive_condition: str,
        reference_condition: str,
        result_scope: str,
        result_path: str = "",
        table_text: str = "",
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
        species: str = "human",
        label: str = "",
        identifier_column: str = "",
        effect_column: str = "",
        padj_column: str = "",
        p_value_column: str = "",
        stat_column: str = "",
        significance_mode: str = "padj",
        padj_threshold: float = 0.05,
        p_value_threshold: float = 0.05,
        log2_fold_change_threshold: float = 0.0,
        identifier_mapping: dict[str, list[str]] | None = None,
        identifier_mapping_path: str = "",
        identifier_mapping_text: str = "",
        mapping_identifier_column: str = "",
        mapping_gene_column: str = "",
        background_genes: list[str] | None = None,
        include_enrichment: bool = True,
        include_ranked_gsea: bool = True,
        effect_definition: dict[str, Any] | None = None,
        response_detail: str = "compact",
        save_to_session_id: str = "",
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Analyze a precomputed transcriptomics or proteomics differential-result table.

        This is the canonical gene-level result-table route. Declare `modality`, the exact positive and reference
        conditions defining a positive effect, and whether the supplied table is `full_results` or `filtered_results`.
        GLESKit never infers contrast orientation from free text. Protein identifiers are mapped conservatively to genes;
        ambiguous identifiers remain auditable rather than being expanded or guessed. This tool does not process raw
        mass-spectrometry or raw RNA counts and does not make diagnostic or biomarker claims. Set
        `include_enrichment=false` and/or `include_ranked_gsea=false` to return a large complete table's canonical
        evidence and reusable handoffs before running expensive follow-up analyses separately. Use
        `effect_definition` for an effect's declared formula, especially a linear abundance ratio; GLESKit will not
        infer numerator/denominator biology from a column header. Responses are compact by default.
        """
        payload = svc.analyze_differential_result(
            modality=modality, positive_condition=positive_condition, reference_condition=reference_condition,
            result_scope=result_scope, result_path=result_path, table_text=table_text, sheet_name=sheet_name,
            header_row=header_row, skip_rows=skip_rows, species=species, label=label,
            identifier_column=identifier_column, effect_column=effect_column, padj_column=padj_column,
            p_value_column=p_value_column, stat_column=stat_column, significance_mode=significance_mode,
            padj_threshold=padj_threshold, p_value_threshold=p_value_threshold,
            log2_fold_change_threshold=log2_fold_change_threshold, identifier_mapping=identifier_mapping,
            identifier_mapping_path=identifier_mapping_path, identifier_mapping_text=identifier_mapping_text,
            mapping_identifier_column=mapping_identifier_column, mapping_gene_column=mapping_gene_column,
            background_genes=background_genes, include_enrichment=include_enrichment,
            include_ranked_gsea=include_ranked_gsea,
            effect_definition=effect_definition,
            response_detail=response_detail,
            save_to_session_id=save_to_session_id,
        )
        return svc.attach_figure_source_id("analyze_differential_result", payload)

    @server.tool()
    def preflight_differential_result(
        result_path: str,
        modality: str,
        sheet_name: str = "",
        species: str = "human",
        label: str = "",
        save_to_session_id: str = "",
    ) -> dict[str, Any]:
        """Inspect a differential-result workbook server-side and return a compact routing manifest.

        It reports candidate complete and filtered tables, compatible complete-table recovery for a filtered list,
        and declarations still required before analysis. No source table rows are returned to the model.
        """

        return svc.preflight_differential_result(
            result_path=result_path,
            modality=modality,
            sheet_name=sheet_name,
            species=species,
            label=label,
            save_to_session_id=save_to_session_id,
        )

    @server.tool()
    def integrate_gene_level_results(
        results: list[dict[str, Any]],
        label: str = "",
        pathway_results: list[dict[str, Any]] | None = None,
        response_detail: str = "compact",
        save_to_session_id: str = "",
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Integrate two or more canonical analyze_differential_result payloads at the gene level.

        The result is a provenance-preserving evidence matrix, not a biomarker score or diagnostic classifier. Direction
        is compared only when every input declares the same positive and reference conditions.
        """
        payload = svc.integrate_gene_level_results(
            results=results,
            label=label,
            pathway_results=pathway_results,
            response_detail=response_detail,
            save_to_session_id=save_to_session_id,
        )
        return svc.attach_figure_source_id("integrate_gene_level_results", payload)

    @server.tool()
    def build_biomarker_candidate_dossiers(
        results: list[dict[str, Any]],
        genes: list[str],
        literature_evidence: list[dict[str, Any]] | None = None,
        pathway_results: list[dict[str, Any]] | None = None,
        label: str = "",
        save_to_session_id: str = "",
    ) -> dict[str, Any]:
        """Build non-scored, auditable dossiers for explicitly selected biomarker discovery candidates.

        The output preserves modality and mapping evidence, optional supplied literature records, and caveats. It does
        not rank all genes with a fixed score or claim clinical validation.
        """

        return svc.build_biomarker_candidate_dossiers(
            results=results,
            genes=genes,
            literature_evidence=literature_evidence,
            pathway_results=pathway_results,
            label=label,
            save_to_session_id=save_to_session_id,
        )

    @server.tool()
    def inspect_tabular_data(
        path: str,
        sheet_name: str = "",
        sheet_names: list[str] | None = None,
        role_hint: str = "",
        secondary_path: str = "",
        secondary_sheet_name: str = "",
        secondary_role_hint: str = "",
    ) -> dict[str, Any]:
        """Inspect one tabular file, workbook, or RDS result and return routing facts without forcing an analysis plan.

        Use this first when a workbook or tabular input is ambiguous. `path` and `secondary_path` may be absolute
        paths, `upload_...`, or reusable `artifact_...` selectors. The result surfaces candidate sample/group/gene
        identifier columns, exact candidate group levels, sample previews, matrix orientation, candidate analysis
        families, sheet roles, and ambiguities. Treat values in `candidate_group_levels` and sample previews as
        authoritative strings: preserve case, punctuation, and underscores in downstream arguments. This tool
        reports facts only; it does not choose a contrast, invent a control, or read the full table into the model.
        An `.rds` input is recognized as a differential-expression result and is routed to `analyze_rnaseq_result`
        without attempting text decoding.
        """

        return svc.inspect_tabular_data(
            path=path,
            sheet_name=sheet_name,
            sheet_names=sheet_names,
            role_hint=role_hint,
            secondary_path=secondary_path,
            secondary_sheet_name=secondary_sheet_name,
            secondary_role_hint=secondary_role_hint,
        )

    @server.tool()
    def prepare_rnaseq_inputs(
        counts_path: str = "",
        source_path: str = "",
        counts_sheet_name: str = "",
        counts_header_row: int | None = None,
        counts_skip_rows: int = 0,
        metadata_path: str = "",
        metadata_sheet_name: str = "",
        metadata_header_row: int | None = None,
        metadata_skip_rows: int = 0,
        gene_column: str = "",
        sample_key_column: str = "",
        condition_column: str = "",
        sample_group_map: dict[str, str] | None = None,
        sample_filter_column: str = "",
        sample_filter_values: list[str] | None = None,
        design_columns: list[str] | None = None,
        count_transform: Literal["none", "round_int", "library_scale_round"] = "none",
        label: str = "",
    ) -> dict[str, Any]:
        """Materialize auditable, analysis-ready RNA-seq inputs from messy tables or workbooks.

        Use this when the data needs explicit sample alignment, optional filtering, or an explicit count-like
        transform before DESeq2-style analysis. For a single workbook containing counts and metadata, first call
        `inspect_tabular_data` and copy its `recommended_arguments`: it can identify the count/metadata sheets,
        header rows, gene column, matching `sample_key_column`, and whether normalized values need `round_int`.
        When no metadata table exists, use the complete bounded `matrix_sample_columns` returned by inspection to
        provide an exact `sample_group_map` instead; GLESKit materializes validated metadata and rejects missing or
        extra sample names. Do not provide both a metadata table and `sample_group_map`.
        Omit header rows only when you want that validated workbook inference; explicitly supplied sheets, headers,
        and column names are preserved. Choose `condition_column` and any sample filter from the exact candidate
        labels returned by inspection; the tool never invents a biological contrast. It writes sanitized
        counts/metadata TSVs plus a reusable prepared manifest for downstream `analyze_deseq2_counts`. Pass the
        returned `prepared_manifest_path` to `analyze_deseq2_counts`; do not reconstruct the sanitized paths or
        pass the manifest to a tool that expects a data table.
        """

        return svc.prepare_rnaseq_inputs(
            counts_path=counts_path,
            source_path=source_path,
            counts_sheet_name=counts_sheet_name,
            counts_header_row=counts_header_row,
            counts_skip_rows=counts_skip_rows,
            metadata_path=metadata_path,
            metadata_sheet_name=metadata_sheet_name,
            metadata_header_row=metadata_header_row,
            metadata_skip_rows=metadata_skip_rows,
            gene_column=gene_column,
            sample_key_column=sample_key_column,
            condition_column=condition_column,
            sample_group_map=sample_group_map,
            sample_filter_column=sample_filter_column,
            sample_filter_values=sample_filter_values,
            design_columns=design_columns,
            count_transform=count_transform,
            label=label,
        )

    @server.tool()
    def analyze_deseq2_counts(
        counts_path: str = "",
        prepared_manifest_path: str = "",
        counts_sheet_name: str = "",
        counts_header_row: int = 1,
        counts_skip_rows: int = 0,
        sample_metadata_path: str = "",
        sample_metadata_sheet_name: str = "",
        sample_metadata_header_row: int = 1,
        sample_metadata_skip_rows: int = 0,
        sample_metadata_text: str = "",
        sample_group_map: dict[str, str] | None = None,
        species: str = "human",
        label: str = "",
        comparison: str = "",
        gene_column: str = "",
        sample_column: str = "sample",
        condition_column: str = "condition",
        condition_a: str = "",
        condition_b: str = "",
        design_formula: str = "",
        min_count: int = 10,
        padj_threshold: float = 0.05,
        p_value_threshold: float = 0.05,
        base_mean_threshold: float = 0.0,
        significance_mode: Literal["padj", "p_value"] = "padj",
        log2_fold_change_threshold: float = 0.0,
        top_n: int = 25,
        lfc_shrinkage: Literal["none", "apeglm"] = "none",
        focus_genes: list[str] | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Run DESeq2 on raw counts plus optional sample metadata.

        `counts_path` and `sample_metadata_path` may be absolute paths, `upload_...`, or reusable `artifact_...`
        selectors. Use this for raw counts, not for already-computed DEG tables. If preprocessing was already
        materialized with `prepare_rnaseq_inputs`, pass `prepared_manifest_path` and the tool will reuse the
        sanitized counts/metadata directly. When such a manifest has an intercept-only design but this call supplies
        `condition_column`, `condition_a`, and `condition_b`, GLESKit uses `~ condition_column` automatically; an
        explicit non-intercept `design_formula` always takes precedence. If the question targets one specific gene after the DESeq2 run, pass it
        in `focus_genes` so the tool can surface the gene-level metrics directly from the generated result table.
        Use `significance_mode="p_value"` for raw-p-value questions and `base_mean_threshold` for a post-DESeq2
        baseMean filter. These filters control the returned DEG handoffs, not the count prefilter `min_count`.
        `log2_fold_change_threshold=0.0` means no absolute-LFC filter; pass the question's stated cutoff explicitly
        and never invent one. When `condition_a` and `condition_b` are used, copy exact values from the metadata or
        `inspect_tabular_data` output. Do not translate semantic labels: if the question says `DMSO`, pass
        `condition_b="DMSO"` even when another valid level such as `Untreated` is present. For metadata with more
        than two levels, use explicit pairwise contrasts. When a count matrix has no metadata table, pass one exact
        `sample_group_map` object covering every `matrix_sample_columns` entry returned by inspection; do not build
        `sample_metadata_text` by hand. Do not pass `sample_group_map` alongside file or inline metadata. The response includes compact `derived_inputs` paths;
        pass those paths directly to comparison or enrichment tools.
        """

        payload = svc.analyze_deseq2_counts(
            counts_path=counts_path,
            prepared_manifest_path=prepared_manifest_path,
            counts_sheet_name=counts_sheet_name,
            counts_header_row=counts_header_row,
            counts_skip_rows=counts_skip_rows,
            sample_metadata_path=sample_metadata_path,
            sample_metadata_sheet_name=sample_metadata_sheet_name,
            sample_metadata_header_row=sample_metadata_header_row,
            sample_metadata_skip_rows=sample_metadata_skip_rows,
            sample_metadata_text=sample_metadata_text,
            sample_group_map=sample_group_map,
            species=species,
            label=label,
            comparison=comparison,
            gene_column=gene_column,
            sample_column=sample_column,
            condition_column=condition_column,
            condition_a=condition_a,
            condition_b=condition_b,
            design_formula=design_formula,
            min_count=min_count,
            padj_threshold=padj_threshold,
            p_value_threshold=p_value_threshold,
            base_mean_threshold=base_mean_threshold,
            significance_mode=significance_mode,
            log2_fold_change_threshold=log2_fold_change_threshold,
            top_n=top_n,
            lfc_shrinkage=lfc_shrinkage,
            focus_genes=focus_genes,
        )
        return _auto_figure_tool_result(
            "analyze_deseq2_counts",
            payload,
            svc,
            download_registry=download_registry,
            http_url_builder=(lambda token: _http_download_url_for_token(token, ctx=ctx)),
        )

    @server.tool()
    def analyze_stratified_deseq2(
        counts_path: str,
        sample_metadata_path: str,
        species: str = "human",
        counts_sheet_name: str = "",
        counts_header_row: int = 1,
        sample_metadata_sheet_name: str = "",
        sample_metadata_header_row: int = 1,
        gene_column: str = "",
        sample_key_column: str = "",
        stratum_column: str = "celltype",
        sex_column: str = "sex",
        target_strata: list[str] | None = None,
        reference_strata: list[str] | None = None,
        condition_a: str = "M",
        condition_b: str = "F",
        feature_annotation_path: str = "",
        feature_annotation_sheet_name: str = "",
        feature_annotation_header_row: int = 1,
        feature_annotation_key_column: str = "",
        feature_annotation_group_column: str = "gene_biotype",
        feature_annotation_group_value: str = "protein_coding",
        analysis: Literal[
            "lfc_distribution_test",
            "focus_gene_lfc",
            "significant_gene_count",
            "annotation_group_lfc_test",
        ] = "lfc_distribution_test",
        padj_threshold: float = 0.05,
        focus_genes: list[str] | None = None,
        p_value_threshold: float = 0.05,
        base_mean_threshold: float = 10.0,
        absolute_log2_fold_change_threshold: float = 0.5,
        lfc_shrinkage: Literal["none", "apeglm"] = "apeglm",
        test: Literal["t", "welch"] = "welch",
        label: str = "",
    ) -> dict[str, Any]:
        """Run an explicit stratified DESeq2 contrast with a requested statistical endpoint.

        Use ``lfc_distribution_test`` for a selected group of strata versus a reference group; it runs one contrast
        per stratum and returns the final LFC-distribution t-statistic. Use ``focus_gene_lfc`` for one or more named
        genes, ``significant_gene_count`` for a padj/baseMean/LFC-filtered count, and
        ``annotation_group_lfc_test`` for a t-test between an annotation group (for example, protein-coding genes)
        and all other annotated features.
        The three endpoint modes run one pooled contrast over ``target_strata`` and do not require reference strata.
        Copy exact metadata labels from inspection and pass multi-level groups as a list, never as ``"CD4/CD8"``.
        """
        return svc.analyze_stratified_deseq2(
            counts_path=counts_path,
            sample_metadata_path=sample_metadata_path,
            species=species,
            counts_sheet_name=counts_sheet_name,
            counts_header_row=counts_header_row,
            sample_metadata_sheet_name=sample_metadata_sheet_name,
            sample_metadata_header_row=sample_metadata_header_row,
            gene_column=gene_column,
            sample_key_column=sample_key_column,
            stratum_column=stratum_column,
            sex_column=sex_column,
            target_strata=target_strata,
            reference_strata=reference_strata,
            condition_a=condition_a,
            condition_b=condition_b,
            feature_annotation_path=feature_annotation_path,
            feature_annotation_sheet_name=feature_annotation_sheet_name,
            feature_annotation_header_row=feature_annotation_header_row,
            feature_annotation_key_column=feature_annotation_key_column,
            feature_annotation_group_column=feature_annotation_group_column,
            feature_annotation_group_value=feature_annotation_group_value,
            analysis=analysis,
            padj_threshold=padj_threshold,
            focus_genes=focus_genes,
            p_value_threshold=p_value_threshold,
            base_mean_threshold=base_mean_threshold,
            absolute_log2_fold_change_threshold=absolute_log2_fold_change_threshold,
            lfc_shrinkage=lfc_shrinkage,
            test=test,
            label=label,
        )

    @server.tool()
    def run_expression_pca(
        matrix_path: str = "",
        table_text: str = "",
        row_id_column: str = "",
        delimiter: str = "",
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
        transpose: bool = False,
        analysis_axis: str = "columns",
        n_components: int = 2,
        scale: bool = True,
        top_n_loadings: int = 15,
        force: bool = False,
        label: str = "",
    ) -> dict[str, Any]:
        """Run PCA on a gene-level expression matrix and store the result as a workspace artifact.

        `matrix_path` may be an absolute path, `upload_...`, or a reusable `artifact_...` selector.
        Pass `analysis_axis="columns"` when samples are in matrix columns and `analysis_axis="rows"` when samples are
        in matrix rows. PCA assumes the selected observation axis is the sample axis and the feature axis contains
        genes/features.
        If the selected observation axis looks larger than the feature axis, the tool will fail fast unless force=True.
        The response surfaces `question_ready.pc1_explained_variance_percent` so the caller can finalize directly
        when the question only asks for the first-component variance percentage.
        """

        return svc.run_expression_pca(
            matrix_path=matrix_path,
            table_text=table_text,
            row_id_column=row_id_column,
            delimiter=delimiter,
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
            transpose=transpose,
            analysis_axis=analysis_axis,
            n_components=n_components,
            scale=scale,
            top_n_loadings=top_n_loadings,
            force=force,
            label=label,
        )

    @server.tool()
    def run_expression_hierarchical_clustering(
        matrix_path: str = "",
        table_text: str = "",
        row_id_column: str = "",
        delimiter: str = "",
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
        transpose: bool = False,
        analysis_axis: str = "columns",
        cluster_count: int = 3,
        linkage: str = "average",
        metric: str = "correlation",
        label: str = "",
    ) -> dict[str, Any]:
        """Run hierarchical clustering on one gene-level matrix.

        `matrix_path` may be an absolute path, `upload_...`, or a reusable `artifact_...` selector.
        Pass `analysis_axis="columns"` when samples are in matrix columns. `metric` is the clustering distance;
        prefer `correlation` for gene-expression matrices. The response returns
        `derived_inputs.cluster_labels_path` so downstream tools can reuse the cluster assignments without
        reading hidden tool-output previews.
        """

        return svc.run_expression_hierarchical_clustering(
            matrix_path=matrix_path,
            table_text=table_text,
            row_id_column=row_id_column,
            delimiter=delimiter,
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
            transpose=transpose,
            analysis_axis=analysis_axis,
            cluster_count=cluster_count,
            linkage=linkage,
            metric=metric,
            label=label,
        )

    @server.tool()
    def run_expression_correlation(
        matrix_path: str = "",
        table_text: str = "",
        row_id_column: str = "",
        delimiter: str = "",
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
        transpose: bool = False,
        analysis_axis: str = "columns",
        method: str = "pearson",
        secondary_matrix_path: str = "",
        secondary_table_text: str = "",
        secondary_row_id_column: str = "",
        secondary_delimiter: str = "",
        secondary_sheet_name: str = "",
        secondary_header_row: int = 1,
        secondary_skip_rows: int = 0,
        secondary_transpose: bool = False,
        secondary_analysis_axis: str = "columns",
        secondary_value_column: str = "",
        group_labels: list[str] | None = None,
        group_summary_stat: str = "mean",
        top_n: int = 25,
        label: str = "",
    ) -> dict[str, Any]:
        """Run correlation on one gene-level matrix and store the result as a workspace artifact.

        `matrix_path` and `secondary_matrix_path` may be absolute paths, `upload_...`, or reusable `artifact_...`
        selectors. Pass `analysis_axis="columns"` when samples are in matrix columns.
        """

        return svc.run_expression_correlation(
            matrix_path=matrix_path,
            table_text=table_text,
            row_id_column=row_id_column,
            delimiter=delimiter,
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
            transpose=transpose,
            analysis_axis=analysis_axis,
            method=method,
            secondary_matrix_path=secondary_matrix_path,
            secondary_table_text=secondary_table_text,
            secondary_row_id_column=secondary_row_id_column,
            secondary_delimiter=secondary_delimiter,
            secondary_sheet_name=secondary_sheet_name,
            secondary_header_row=secondary_header_row,
            secondary_skip_rows=secondary_skip_rows,
            secondary_transpose=secondary_transpose,
            secondary_analysis_axis=secondary_analysis_axis,
            secondary_value_column=secondary_value_column,
            group_labels=group_labels,
            group_summary_stat=group_summary_stat,
            top_n=top_n,
            label=label,
        )

    @server.tool()
    def run_expression_consensus_clustering(
        matrix_path: str = "",
        table_text: str = "",
        row_id_column: str = "",
        delimiter: str = "",
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
        transpose: bool = False,
        analysis_axis: str = "columns",
        cluster_count: int = 3,
        iterations: int = 50,
        sample_fraction: float = 0.7,
        feature_fraction: float = 1.0,
        random_seed: int = 0,
        metric: str = "correlation",
        linkage: str = "average",
        label: str = "",
    ) -> dict[str, Any]:
        """Run bootstrap consensus clustering on a gene-level matrix and store the result as a workspace artifact.

        `matrix_path` may be an absolute path, `upload_...`, or a reusable `artifact_...` selector.
        Pass `analysis_axis="columns"` when samples are in matrix columns.
        `metric` is the clustering distance, not an evaluation score. Prefer `correlation` for gene-expression matrices.
        Common aliases such as `pearson` are normalized to correlation distance automatically.
        This tool returns cohort-wide stability summaries across all observations. Use `evaluate_cluster_agreement`
        only when the question is specifically about held-out test agreement after train/test splitting.
        """

        return svc.run_expression_consensus_clustering(
            matrix_path=matrix_path,
            table_text=table_text,
            row_id_column=row_id_column,
            delimiter=delimiter,
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
            transpose=transpose,
            analysis_axis=analysis_axis,
            cluster_count=cluster_count,
            iterations=iterations,
            sample_fraction=sample_fraction,
            feature_fraction=feature_fraction,
            random_seed=random_seed,
            metric=metric,
            linkage=linkage,
            label=label,
        )

    @server.tool()
    def run_log_transform(
        matrix_path: str = "",
        table_text: str = "",
        row_id_column: str = "",
        delimiter: str = "",
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
        transpose: bool = False,
        analysis_axis: str = "columns",
        base: float = 2.0,
        offset: float = 1.0,
        preset: Literal["custom", "log2", "log2p1", "log10", "log10p1", "ln", "lnp1"] = "custom",
        label: str = "",
    ) -> dict[str, Any]:
        """Apply a flexible log transform to a matrix and store the result as a workspace artifact.

        Presets are explicit and limited to: custom, log2, log2p1, log10, log10p1, ln, and lnp1.
        The public result is JSON-safe: it returns a summary, transformed-table metadata, warnings, workspace info,
        artifact references, and `derived_inputs.matrix_path` for reuse in downstream matrix tools, not the in-memory
        matrix object.
        """

        return svc.run_log_transform(
            matrix_path=matrix_path,
            table_text=table_text,
            row_id_column=row_id_column,
            delimiter=delimiter,
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
            transpose=transpose,
            analysis_axis=analysis_axis,
            base=base,
            offset=offset,
            preset=preset,
            label=label,
        )

    @server.tool()
    def run_supervised_expression_model(
        matrix_path: str = "",
        table_text: str = "",
        row_id_column: str = "",
        delimiter: str = "",
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
        transpose: bool = False,
        analysis_axis: str = "columns",
        metadata_path: str = "",
        metadata_table_text: str = "",
        metadata_delimiter: str = "",
        metadata_sheet_name: str = "",
        metadata_header_row: int = 1,
        metadata_skip_rows: int = 0,
        sample_column: str = "sample",
        target_column: str = "",
        task: Literal["classification", "regression"] = "classification",
        model_family: Literal["logistic_regression", "linear_regression", "ridge"] = "logistic_regression",
        split_strategy: Literal["train_test", "stratified_train_test", "group_train_test"] = "train_test",
        group_column: str = "",
        test_size: float = 0.3,
        random_seed: int = 0,
        top_n_features: int = 25,
        label: str = "",
    ) -> dict[str, Any]:
        """Run generic supervised modeling on one gene-level matrix plus one metadata table.

        `matrix_path` and `metadata_path` may be absolute paths, `upload_...`, or reusable `artifact_...` selectors.
        Pass `analysis_axis="columns"` when samples are in matrix columns. Use this for classification, regression,
        and split-aware evaluation.
        """

        return svc.run_supervised_expression_model(
            matrix_path=matrix_path,
            table_text=table_text,
            row_id_column=row_id_column,
            delimiter=delimiter,
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
            transpose=transpose,
            analysis_axis=analysis_axis,
            metadata_path=metadata_path,
            metadata_table_text=metadata_table_text,
            metadata_delimiter=metadata_delimiter,
            metadata_sheet_name=metadata_sheet_name,
            metadata_header_row=metadata_header_row,
            metadata_skip_rows=metadata_skip_rows,
            sample_column=sample_column,
            target_column=target_column,
            task=task,
            model_family=model_family,
            split_strategy=split_strategy,
            group_column=group_column,
            test_size=test_size,
            random_seed=random_seed,
            top_n_features=top_n_features,
            label=label,
        )

    @server.tool()
    def evaluate_cluster_agreement(
        matrix_path: str = "",
        table_text: str = "",
        row_id_column: str = "",
        delimiter: str = "",
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
        transpose: bool = False,
        analysis_axis: str = "columns",
        cluster_count: int = 3,
        iterations: int = 50,
        train_fraction: float = 0.7,
        metric: str = "correlation",
        linkage: str = "average",
        random_seed: int = 0,
        label: str = "",
    ) -> dict[str, Any]:
        """Measure held-out test agreement across train/test splits. Use this when bootstrap consensus alone is not enough.

        `matrix_path` may be an absolute path, `upload_...`, or a reusable `artifact_...` selector.
        Pass `analysis_axis="columns"` when samples are in matrix columns. Use this for held-out 70/30-style agreement
        questions; use `run_expression_consensus_clustering` for cohort-wide bootstrap stability or full-matrix
        consistency summaries.
        `metric` controls the clustering distance used inside the train/test consensus clustering steps.
        Do not pass agreement words such as `accuracy` here; the tool already returns held-out test-only agreement
        count and rate.
        """

        return svc.evaluate_cluster_agreement(
            matrix_path=matrix_path,
            table_text=table_text,
            row_id_column=row_id_column,
            delimiter=delimiter,
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
            transpose=transpose,
            analysis_axis=analysis_axis,
            cluster_count=cluster_count,
            iterations=iterations,
            train_fraction=train_fraction,
            metric=metric,
            linkage=linkage,
            random_seed=random_seed,
            label=label,
        )

    @server.tool()
    def run_expression_statistics(
        analysis: Literal[
            "matrix_annotation_correlation",
            "distribution_summary",
            "group_test",
            "featurewise_group_test",
            "matrix_featurewise_group_test",
            "matrix_group_fold_change_summary",
            "matrix_group_test",
            "matrix_log2_fold_change_distribution",
            "threshold_count",
            "proportion_interval",
            "exact_binomial_test",
            "multiple_testing_summary",
        ] = "distribution_summary",
        source_path: str = "",
        table_text: str = "",
        row_id_column: str = "",
        delimiter: str = "",
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
        transpose: bool = False,
        analysis_axis: str = "columns",
        target_axis: Literal["features", "observations"] = "features",
        summary_stat: Literal["mean", "median"] = "mean",
        metadata_path: str = "",
        metadata_table_text: str = "",
        metadata_delimiter: str = "",
        metadata_sheet_name: str = "",
        metadata_header_row: int = 1,
        metadata_skip_rows: int = 0,
        metadata_key_column: str = "",
        metadata_group_column: str = "",
        metadata_group_value: str = "",
        metadata_group_values: list[str] | None = None,
        sample_group_map: dict[str, str] | None = None,
        group_a: str = "",
        group_b: str = "",
        annotation_path: str = "",
        annotation_table_text: str = "",
        annotation_delimiter: str = "",
        annotation_sheet_name: str = "",
        annotation_header_row: int = 1,
        annotation_skip_rows: int = 0,
        annotation_key_column: str = "",
        annotation_value_column: str = "",
        feature_annotation_path: str = "",
        feature_annotation_table_text: str = "",
        feature_annotation_delimiter: str = "",
        feature_annotation_sheet_name: str = "",
        feature_annotation_header_row: int = 1,
        feature_annotation_skip_rows: int = 0,
        feature_annotation_key_column: str = "",
        feature_annotation_group_column: str = "",
        feature_annotation_group_value: str = "",
        method: Literal["pearson", "spearman"] = "pearson",
        value_column: str = "",
        scope: Literal["all_values", "feature_means", "observation_means", "column"] = "all_values",
        group_column: str = "",
        key_column: str = "",
        pair_column: str = "",
        annotation_group_column: str = "",
        annotation_group_value: str = "",
        test: Literal["t", "welch", "anova", "mannwhitney", "kruskal", "paired_t", "paired_wilcoxon"] = "welch",
        observation_unit: Literal["flattened_values", "featurewise", "pairwise_log2_fold_change"] = "flattened_values",
        value_transform: Literal["none", "log2p1", "log2"] = "none",
        threshold: float = 0.0,
        threshold_operator: Literal["<", "<=", ">", ">="] = "<",
        pseudocount: float = 1.0,
        success_count: int = 0,
        trial_count: int = 0,
        null_probability: float = 0.5,
        alpha: float = 0.05,
        correction_methods: list[str] | None = None,
        missing_value_policy: Literal["error", "omit", "drop_feature"] = "error",
        label: str = "",
    ) -> dict[str, Any]:
        """Run one expression-statistics or RNA/QC calculation.

        `source_path` and `annotation_path` may be absolute paths, `upload_...`, or reusable `artifact_...` selectors.
        For matrix-backed analyses, pass `analysis_axis="columns"` when samples are in matrix columns.
        For subgrouped matrix-vs-annotation correlations, use `annotation_group_column` plus `annotation_group_value`
        to restrict the annotation table before computing the statistic. When a matrix question needs a sample subset,
        also pass `metadata_path`, `metadata_group_column`, and exact `metadata_group_value` or
        `metadata_group_values` values from inspection. For `analysis="matrix_group_test"`, choose the observation
        unit explicitly: `flattened_values` pools every numeric matrix entry, `featurewise` tests each feature's
        group observations and returns feature rows, and `pairwise_log2_fold_change` computes one log2 ratio of
        group means per feature for every selected group pair before the requested group test. The pairwise mode
        requires non-negative values and `pseudocount > 0`; with four selected groups it produces six pairwise
        distributions. Use `sample_group_map` for continuous/qPCR matrices without metadata; labels such as `P_1`
        and `C_11` may be inferred conservatively and are reported in `resolution_notes`. For continuous/qPCR/Ct
        matrices with documented missing measurements, set `missing_value_policy="omit"` to test each feature using
        its finite observations, or `"drop_feature"` to require complete features; `"error"` preserves strict
        validation. Featurewise results expose raw-p-value counts and percentages alongside BH, BY, Bonferroni, and
        all-corrections-intersection counts in `question_ready_counts`; use those scalar fields instead of inferring
        totals from previews. Use
        `analysis="threshold_count"` with `value_column`, `threshold`, and `threshold_operator` for direct counts
        in arbitrary numeric result columns such as `dispersion`. Use `analysis="matrix_log2_fold_change_distribution"`
        for distribution shape/skewness rather than an ANOVA. If inspection reports a blank first identifier column
        or `column_1`, omit `row_id_column`.
        """

        return svc.run_expression_statistics(
            analysis=analysis,
            source_path=source_path,
            table_text=table_text,
            row_id_column=row_id_column,
            delimiter=delimiter,
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
            transpose=transpose,
            analysis_axis=analysis_axis,
            target_axis=target_axis,
            summary_stat=summary_stat,
            metadata_path=metadata_path,
            metadata_table_text=metadata_table_text,
            metadata_delimiter=metadata_delimiter,
            metadata_sheet_name=metadata_sheet_name,
            metadata_header_row=metadata_header_row,
            metadata_skip_rows=metadata_skip_rows,
            metadata_key_column=metadata_key_column,
            metadata_group_column=metadata_group_column,
            metadata_group_value=metadata_group_value,
            metadata_group_values=metadata_group_values,
            sample_group_map=sample_group_map,
            group_a=group_a,
            group_b=group_b,
            annotation_path=annotation_path,
            annotation_table_text=annotation_table_text,
            annotation_delimiter=annotation_delimiter,
            annotation_sheet_name=annotation_sheet_name,
            annotation_header_row=annotation_header_row,
            annotation_skip_rows=annotation_skip_rows,
            annotation_key_column=annotation_key_column,
            annotation_value_column=annotation_value_column,
            feature_annotation_path=feature_annotation_path,
            feature_annotation_table_text=feature_annotation_table_text,
            feature_annotation_delimiter=feature_annotation_delimiter,
            feature_annotation_sheet_name=feature_annotation_sheet_name,
            feature_annotation_header_row=feature_annotation_header_row,
            feature_annotation_skip_rows=feature_annotation_skip_rows,
            feature_annotation_key_column=feature_annotation_key_column,
            feature_annotation_group_column=feature_annotation_group_column,
            feature_annotation_group_value=feature_annotation_group_value,
            method=method,
            value_column=value_column,
            scope=scope,
            group_column=group_column,
            key_column=key_column,
            pair_column=pair_column,
            annotation_group_column=annotation_group_column,
            annotation_group_value=annotation_group_value,
            test=test,
            observation_unit=observation_unit,
            value_transform=value_transform,
            threshold=threshold,
            threshold_operator=threshold_operator,
            pseudocount=pseudocount,
            success_count=success_count,
            trial_count=trial_count,
            null_probability=null_probability,
            alpha=alpha,
            correction_methods=correction_methods,
            missing_value_policy=missing_value_policy,
            label=label,
        )

    @server.tool()
    def analyze_single_cell_rnaseq(
        analysis: Literal["summary", "cell_type_deg_counts", "gene_cell_type_lookup", "pseudobulk"] = "summary",
        h5ad_path: str = "",
        deg_table_path: str = "",
        gene: str = "",
        padj_threshold: float = 0.05,
        gene_column: str = "",
        cell_type_column: str = "",
        padj_column: str = "",
        p_value_column: str = "",
        log2_fold_change_column: str = "",
        sheet_names: list[str] | None = None,
        sample_column: str = "",
        condition_column: str = "",
        cell_type_values: list[str] | None = None,
        layer: str = "",
        use_raw: bool = False,
        obs_preview_limit: int = 10,
        var_preview_limit: int = 10,
        label: str = "",
    ) -> dict[str, Any]:
        """Run one single-cell RNA analysis task at a time.

        `h5ad_path` and `deg_table_path` may be absolute paths, `upload_...`, or reusable `artifact_...` selectors.
        Use one task per call: object summary, cell-type DEG counts, gene lookup across cell types, or pseudobulk
        construction.
        """

        return svc.analyze_single_cell_rnaseq(
            analysis=analysis,
            h5ad_path=h5ad_path,
            deg_table_path=deg_table_path,
            gene=gene,
            padj_threshold=padj_threshold,
            gene_column=gene_column,
            cell_type_column=cell_type_column,
            padj_column=padj_column,
            p_value_column=p_value_column,
            log2_fold_change_column=log2_fold_change_column,
            sheet_names=sheet_names,
            sample_column=sample_column,
            condition_column=condition_column,
            cell_type_values=cell_type_values,
            layer=layer,
            use_raw=use_raw,
            obs_preview_limit=obs_preview_limit,
            var_preview_limit=var_preview_limit,
            label=label,
        )

    @server.tool()
    def cluster_marker_analysis(
        matrix_path: str = "",
        table_text: str = "",
        row_id_column: str = "",
        delimiter: str = "",
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
        transpose: bool = False,
        analysis_axis: str = "columns",
        cluster_labels: Any = None,
        cluster_labels_path: str = "",
        top_n: int = 25,
        test: Literal["t", "welch", "anova", "mannwhitney", "kruskal", "paired_t", "paired_wilcoxon"] = "welch",
        padj_threshold: float = 0.05,
        species: str = "human",
        enrich_top_markers: bool = False,
        enrichment_database: str = "GO_BP",
        enrichment_limit: int = 10,
        label: str = "",
    ) -> dict[str, Any]:
        """Find cluster markers from one expression matrix and optionally enrich the top hits.

        Pass `cluster_labels_path` from an upstream clustering tool when available; this avoids re-reading
        bulky clustering previews and lets the tool consume the saved label assignments directly. When
        enrichment is enabled, prefer answering from `question_ready.cluster_with_most_significant_pathways`
        and `question_ready.top_reactome_pathway_for_max_cluster` if those fields are present.
        """

        return svc.cluster_marker_analysis(
            matrix_path=matrix_path,
            table_text=table_text,
            row_id_column=row_id_column,
            delimiter=delimiter,
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
            transpose=transpose,
            analysis_axis=analysis_axis,
            cluster_labels=cluster_labels,
            cluster_labels_path=cluster_labels_path,
            top_n=top_n,
            test=test,
            padj_threshold=padj_threshold,
            species=species,
            enrich_top_markers=enrich_top_markers,
            enrichment_database=enrichment_database,
            enrichment_limit=enrichment_limit,
            label=label,
        )

    @server.tool()
    def compare_differential_expression_results(
        results: list[dict[str, Any]],
        gene_source: Literal["upregulated", "downregulated", "all_significant"] = "all_significant",
        label: str = "",
    ) -> dict[str, Any]:
        """Compare two or more differential-expression results to quantify shared, unique, and directionally concordant genes.

        Each result spec may be a DEG/result table (`result_path` or inline `table_text`), a DESeq2 handoff `manifest_path`,
        or one of the compact DESeq2 gene-list paths such as `all_significant_genes_path`, `upregulated_genes_path`,
        or `downregulated_genes_path`. Do not pass `ranked_gene_scores_path` here; that path belongs to `ranked_gene_set_gsea`.
        """

        return svc.compare_differential_expression_results(
            results=results,
            gene_source=gene_source,
            label=label,
        )

    @server.tool()
    def run_pipeline(
        source: dict[str, Any],
        steps: list[dict[str, Any]] | None = None,
        label: str = "",
    ) -> dict[str, Any]:
        """Run one linear multi-step GLESKit analysis pipeline per turn and store each stage as a workspace artifact.

        Assemble the full stage list before calling this tool; do not chain a second run_pipeline call for the same request.
        The source kind may be inferred from the supplied fields when omitted, and each step may be provided either as
        {"tool": "...", "params": {...}} or as a one-key compatibility shorthand such as {"gene_summary": {...}}.
        For path-aware sources or step params, pass the original absolute path, `upload_...`, or reusable `artifact_...`
        selector directly.
        """

        return svc.run_pipeline(source=source, steps=steps, label=label)

    @server.tool()
    def enrich_gene_set(
        genes: list[str] | None = None,
        genes_path: str = "",
        genes_text: str = "",
        species: str = "human",
        label: str = "",
        database: str = "GO_BP",
        library: str = "",
        backend: Literal["auto", "clusterprofiler", "enrichr"] = "auto",
        background_genes: list[str] | None = None,
        background_genes_path: str = "",
        simplify_go_similarity: float = 0.0,
        focus_terms: list[str] | None = None,
        response_detail: str = "compact",
        save_to_session_id: str = "",
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Run pathway enrichment for a gene set.

        This tool accepts gene symbols or common gene IDs such as Ensembl/Entrez and normalizes them internally;
        do not pre-resolve each gene with resolve_gene. You may provide genes directly, a newline-delimited
        `genes_text`, or a `genes_path` that points to a compact gene-list file. `background_genes_path` may point
        to a generated tested-gene universe handoff and is validated by the controlled path policy. Human gene sets prefer the local
        YuLab backend and fall back to g:Profiler when needed for the default route. To reproduce an Enrichr/gseapy
        library, pass its exact library name (for example `GO_Biological_Process_2021`, `Reactome_2022`, or
        `KEGG_2019_Mouse`) with
        `backend="enrichr"`; an explicit Enrichr route does not silently fall back to clusterProfiler. Use
        `database="GO_BP"` or `database="REAC"` for generic local GO/Reactome enrichment instead. Prefer a
        `backend="clusterprofiler"` request only when that local method is required: it never silently substitutes
        g:Profiler, and returns a structured diagnostic if the requested local backend fails. With `backend="auto"`,
        an eligible local failure may fall back to g:Profiler and is reported as `fallback_used=true`.
        `genes_path` from a prior `derived_inputs` handoff. For Pseudomonas aeruginosa locus tags such as `PA14_...`,
        pass `database="KEGG"`; GLESKit detects the PA14 reference automatically and records the effective strain.
        When the question asks for a named pathway or GO term,
        pass its exact name or identifier in `focus_terms`; the compact response then includes matching rows in
        `focused_terms` even when the term is outside the top preview. Read the returned `library`, `backend`,
        `top_term`, `top_terms`, and (when requested) `focused_terms` fields before stating an answer so a local
        result is not described as gseapy/Enrichr. A `focus_status="not_found"` result means the requested target
        was absent in that correctly configured enrichment result; report that evidence rather than rerunning an
        equivalent analysis with changed filters. Never read hidden tool-output files to recover omitted terms.
        For explicit graph/network requests, call `list_figure_options` using `figure_source_id` from this result,
        then `generate_figure` with the chosen `figure_type`.
        """

        payload = svc.enrich_gene_set(
            genes=genes,
            genes_path=genes_path,
            genes_text=genes_text,
            species=species,
            label=label,
            database=database,
            library=library,
            backend=backend,
            background_genes=background_genes,
            background_genes_path=background_genes_path,
            simplify_go_similarity=simplify_go_similarity,
            focus_terms=focus_terms,
            response_detail=response_detail,
            save_to_session_id=save_to_session_id,
        )
        payload = svc.attach_figure_source_id("enrich_gene_set", payload)
        return _auto_figure_tool_result(
            "enrich_gene_set",
            payload,
            svc,
            download_registry=download_registry,
            http_url_builder=(lambda token: _http_download_url_for_token(token, ctx=ctx)),
        )

    @server.tool()
    def gene_set_disease_context(
        genes: list[str],
        species: str = "human",
        label: str = "",
        database: str = "DO",
        limit: int = 15,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Run local disease enrichment for a gene set using YuLab disease knowledge bases. This tool normalizes the input genes internally; do not pre-resolve each gene with resolve_gene. For explicit disease graph/network requests, call list_figure_options using figure_source_id from this result, then generate_figure with a matching figure_type (for example disease_cnetplot)."""

        payload = svc.gene_set_disease_context(
            genes=genes,
            species=species,
            label=label,
            database=database,
            limit=limit,
        )
        payload = svc.attach_figure_source_id("gene_set_disease_context", payload)
        return _auto_figure_tool_result(
            "gene_set_disease_context",
            payload,
            svc,
            download_registry=download_registry,
            http_url_builder=(lambda token: _http_download_url_for_token(token, ctx=ctx)),
        )

    @server.tool()
    def ranked_gene_set_gsea(
        gene_scores: dict[str, float] | None = None,
        gene_scores_path: str = "",
        gene_scores_text: str = "",
        species: str = "human",
        label: str = "",
        limit: int = 15,
        database: str = "GO_BP",
        response_detail: str = "compact",
        execution_timeout_seconds: int = 0,
        save_to_session_id: str = "",
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Run local ranked GSEA on a preranked gene signature using the YuLab backend.

        You may provide the ranked signature directly, as a `gene_scores_path` pointing to a two-column
        gene-score table, or as newline-delimited `gene_scores_text`. Gene symbols and common gene IDs such as
        Ensembl/Entrez are accepted and normalized internally.
        """

        payload = svc.ranked_gene_set_gsea(
            gene_scores=gene_scores,
            gene_scores_path=gene_scores_path,
            gene_scores_text=gene_scores_text,
            species=species,
            label=label,
            limit=limit,
            database=database,
            response_detail=response_detail,
            execution_timeout_seconds=execution_timeout_seconds or None,
            save_to_session_id=save_to_session_id,
        )
        payload = svc.attach_figure_source_id("ranked_gene_set_gsea", payload)
        return _auto_figure_tool_result(
            "ranked_gene_set_gsea",
            payload,
            svc,
            download_registry=download_registry,
            http_url_builder=(lambda token: _http_download_url_for_token(token, ctx=ctx)),
        )

    @server.tool()
    def compare_gene_programs(
        programs: dict[str, list[str]],
        species: str = "human",
        label: str = "",
        database: str = "GO_BP",
        limit: int = 10,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Compare enrichment signals across multiple named gene programs.

        Human GO/Reactome requests prefer the local YuLab backend. KEGG, WikiPathways, and non-human comparisons
        fall back to g:Profiler and include shared-term summaries across programs. Each program's genes are
        normalized internally; do not pre-resolve them one by one.
        """

        payload = svc.compare_gene_programs(
            programs=programs,
            species=species,
            label=label,
            database=database,
            limit=limit,
        )
        payload = svc.attach_figure_source_id("compare_gene_programs", payload)
        return _auto_figure_tool_result(
            "compare_gene_programs",
            payload,
            svc,
            download_registry=download_registry,
            http_url_builder=(lambda token: _http_download_url_for_token(token, ctx=ctx)),
        )

    @server.tool()
    def search_papers(query: str, limit: int = 5, open_access_only: bool = False) -> dict[str, Any]:
        """Search Europe PMC for papers. This tool is stateless unless a desktop workspace session is active."""

        return svc.search_papers(query=query, limit=limit, open_access_only=open_access_only)

    @server.tool()
    def gene_disease_associations(gene: str, species: str = "human", limit: int = 10) -> dict[str, Any]:
        """Retrieve disease associations for a gene. This tool is stateless unless a desktop workspace session is active."""

        return svc.gene_disease_associations(gene=gene, species=species, limit=limit)

    @server.tool()
    def investigate_gene_set(
        genes: list[str],
        question: str = "",
        label: str = "",
        species: str = "human",
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Run a multi-provider gene set workflow. This tool normalizes the input genes internally and is usually the best first-pass tool for a pasted gene list. This tool is stateless unless a desktop workspace session is active."""

        payload = svc.investigate_gene_set(genes=genes, question=question, label=label, species=species)
        payload = svc.attach_figure_source_id("investigate_gene_set", payload)
        return _auto_figure_tool_result(
            "investigate_gene_set",
            payload,
            svc,
            download_registry=download_registry,
            http_url_builder=(lambda token: _http_download_url_for_token(token, ctx=ctx)),
        )

    @server.custom_route(f"{download_path_prefix}/{{token}}", methods=["GET"], name="figure_download_http")
    async def figure_download_http(request):  # pragma: no cover - exercised through live HTTP transport
        from starlette.responses import PlainTextResponse, Response

        token = str((request.path_params or {}).get("token") or "").strip()
        entry = download_registry.get(token)
        if entry is None:
            return PlainTextResponse("This figure download link is unavailable or has expired.", status_code=404)

        safe_filename = (entry.filename or "gleskit-figure").replace('"', "")
        disposition = f"attachment; filename*=UTF-8''{quote(safe_filename)}"
        return Response(
            content=entry.data,
            media_type=entry.mime_type or "application/octet-stream",
            headers={
                "Content-Disposition": disposition,
                "Cache-Control": "no-store, max-age=0",
            },
        )

    @server.resource(
        "gleskit://workspace/status",
        name="workspace_status",
        description="Current status of the optional desktop workspace bridge.",
    )
    def workspace_status_resource() -> dict[str, Any]:
        return svc.workspace_status()

    @server.resource(
        "gleskit://reference/status",
        name="reference_status",
        description="Current status of the local curated reference bundle.",
    )
    def reference_status_resource() -> dict[str, Any]:
        return svc.reference_status()

    @server.resource(
        "gleskit://report-templates",
        name="report_templates",
        description="Current authored report templates available to the assistant.",
    )
    def report_templates_resource() -> dict[str, Any]:
        return svc.list_report_templates()

    @server.resource(
        "gleskit://report-templates/{template_id}",
        name="report_template",
        description="One authored report template, including sections and guidance.",
    )
    def report_template_resource(template_id: str) -> dict[str, Any]:
        return svc.get_report_template(template_id)

    @server.resource(
        "gleskit://workspace/session/{session_id}/report",
        name="session_report",
        description="Latest saved markdown report for a saved workspace session.",
        mime_type="text/markdown",
    )
    def session_report_resource(session_id: str) -> str:
        return svc.get_report_markdown(session_id)

    @server.resource(
        "gleskit://figure-download/{token}",
        name="figure_download",
        description="Temporary downloadable figure file generated by GLESKit.",
        mime_type="application/octet-stream",
    )
    def figure_download_resource(token: str) -> bytes:
        entry = download_registry.get(token)
        if entry is None:
            raise FileNotFoundError("This figure download link is unavailable or has expired.")
        return entry.data

    return server


def main(
    transport: str = "stdio",
    *,
    project_root: str | Path | None = None,
    host: str | None = None,
    port: int | None = None,
    streamable_http_path: str | None = None,
) -> int:
    server = build_server(
        project_root=project_root,
        host=host,
        port=port,
        streamable_http_path=streamable_http_path,
        start_local_download_server=(transport == "stdio"),
    )
    server.run(transport=transport)
    return 0
