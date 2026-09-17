"""Context References — @file, @folder, @url expansion.

Позволяет пользователю ссылаться на файлы, папки и URL-адреса
прямо в тексте сообщения через синтаксис @file:path, @folder:path, @url:url.

Безопасность:
  - Заблокированы чувствительные файлы: .env, ~/.ssh/*, id_rsa, id_ed25519
  - Заблокированы бинарные файлы (проверка по расширению и MIME-сигнатуре)
  - Заблокированы пути за пределами workspace

Поддерживается range для файлов: @file:path/to/file.py:10-30 (строки 10-30)
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Any

from antigona.core import paths

logger = logging.getLogger(__name__)

# ─── Blocked paths / patterns ─────────────────────────────────────────────────

_BLOCKED_PATH_PATTERNS: list[str] = [
    ".env",
    ".env.local",
    ".env.production",
    ".ssh",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    ".pem",
    ".key",
    ".cert",
    "known_hosts",
    "authorized_keys",
    "config",
    ".git/config",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".docker/config.json",
]

_BLOCKED_EXTENSIONS: set[str] = {
    ".pyc",
    ".pyo",
    ".so",
    ".dll",
    ".dylib",
    ".exe",
    ".bin",
    ".o",
    ".obj",
    ".lib",
    ".class",
    ".jar",
    ".war",
    ".zip",
    ".tar",
    ".gz",
    ".7z",
    ".rar",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".ico",
    ".mp3",
    ".mp4",
    ".avi",
    ".mov",
    ".wav",
    ".flac",
    ".ogg",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".db",
    ".sqlite",
    ".sqlite3",
}

_WORKSPACE_ROOTS: list[Path] = [
    paths.project_root(),
]

# Directories that don't start with "." but should still never be scanned or
# read into an LLM prompt — dependency trees, bytecode caches, coverage/build
# artifacts. Dotdirs (.venv, .git, .mypy_cache, ...) are already skipped by
# the leading-dot check in resolve_folder().
_IGNORED_DIR_NAMES: frozenset[str] = frozenset({
    "node_modules",
    "__pycache__",
})

# Max characters of file content injected into a prompt via @file:. Keeps a
# single reference from blowing the token budget (e.g. @file:antigona_all.log).
_MAX_FILE_CHARS: int = 8_000


def is_path_blocked(path: Path) -> bool:
    """Check if a path should be blocked for security reasons.

    Blocks:
    - Paths matching blocked patterns (sensitive files)
    - Binary file extensions
    - Paths outside workspace roots
    """
    resolved = path.resolve()

    # Check workspace containment
    in_workspace = any(
        resolved.is_relative_to(root.resolve())
        for root in _WORKSPACE_ROOTS
    )
    if not in_workspace:
        return True

    # Check blocked patterns
    for pattern in _BLOCKED_PATH_PATTERNS:
        if pattern in resolved.parts:
            return True
        if resolved.name == pattern:
            return True
        if pattern in str(resolved):
            return True

    # Check blocked extensions
    ext = resolved.suffix.lower()
    if ext in _BLOCKED_EXTENSIONS:
        return True

    return False


def is_binary(path: Path) -> bool:
    """Check if a file appears to be binary using content sniffing."""
    if not path.exists():
        return False
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
        if not chunk:
            return False
        # Check for null bytes — strong indicator of binary content
        if b"\x00" in chunk:
            return True
        # MIME type check
        mime_type, _ = mimetypes.guess_type(str(path))
        if mime_type and mime_type.startswith(("image/", "video/", "audio/", "application/")):
            return True
        return False
    except OSError:
        return False


# ─── Reference expansion ─────────────────────────────────────────────────────


def resolve_file(path: str, line_range: str = "") -> str:
    """Read a file and return its contents, respecting line range.

    Args:
        path: Path to the file.
        line_range: Optional range specification like "10-30" or "10" (single line).

    Returns:
        File contents as text with line numbers.

    Raises:
        ValueError: If the path is blocked or file is binary.
        FileNotFoundError: If the file doesn't exist.
    """
    file_path = Path(path).expanduser().resolve()

    if is_path_blocked(file_path):
        raise ValueError(f"Доступ запрещён: {path} (чувствительный файл)")

    if not file_path.exists():
        raise FileNotFoundError(f"Файл не найден: {path}")

    if is_binary(file_path):
        raise ValueError(f"Бинарный файл: {path} (содержимое не отображается)")

    # Read the file
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (UnicodeDecodeError, OSError) as exc:
        raise ValueError(f"Не удалось прочитать файл: {path} — {exc}") from exc

    # Apply line range
    if line_range:
        parts = line_range.split("-")
        try:
            start = int(parts[0])
            end = int(parts[1]) if len(parts) > 1 else start
        except (ValueError, IndexError):
            raise ValueError(f"Неверный range: {line_range!r}. Используй формат: 10-30") from None

        if start < 1:
            start = 1
        if end > len(lines):
            end = len(lines)

        selected = lines[start - 1 : end]
    else:
        selected = lines
        start = 1

    # Build output with line numbers
    result_parts: list[str] = []
    result_parts.append(f"📄 {file_path}")
    if line_range:
        result_parts.append(f"   Строки {start}-{start + len(selected) - 1}")
    result_parts.append("")

    # Token-budget guard: stop adding lines once the char cap is hit instead
    # of dumping an entire (possibly multi-MB) file into the LLM prompt.
    body_chars = 0
    truncated_at: int | None = None
    for i, line in enumerate(selected, start=start):
        rendered = f"{i:>4}| {line.rstrip()}"
        if body_chars + len(rendered) > _MAX_FILE_CHARS:
            truncated_at = i
            break
        result_parts.append(rendered)
        body_chars += len(rendered) + 1

    if truncated_at is not None:
        last_line = start + len(selected) - 1
        result_parts.append(
            f"... (обрезано на строке {truncated_at} из {last_line} — "
            f"файл слишком большой; используй @file:{path}:START-END "
            "для конкретного диапазона)"
        )

    return "\n".join(result_parts)


def resolve_folder(path: str, max_depth: int = 3) -> str:
    """Get a directory tree listing.

    Args:
        path: Path to the directory.
        max_depth: Maximum depth for tree display.

    Returns:
        Formatted directory tree.

    Raises:
        ValueError: If the path is blocked.
        NotADirectoryError: If the path is not a directory.
    """
    folder_path = Path(path).expanduser().resolve()

    if is_path_blocked(folder_path):
        raise ValueError(f"Доступ запрещён: {path}")

    if not folder_path.exists():
        raise FileNotFoundError(f"Путь не найден: {path}")

    if not folder_path.is_dir():
        raise NotADirectoryError(f"Не директория: {path}")

    lines: list[str] = []
    lines.append(f"📁 {folder_path}")
    lines.append("")

    def walk(dir_path: Path, depth: int = 0) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(dir_path.iterdir(), key=lambda x: (not x.is_dir(), x.name))
        except PermissionError:
            lines.append(f"{'  ' * depth}  ⚠️ Permission denied")
            return

        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.is_dir() and (
                entry.name in _IGNORED_DIR_NAMES
                or entry.name.endswith(".egg-info")
            ):
                continue
            prefix = "  " * (depth + 1)
            if entry.is_dir():
                lines.append(f"{prefix}📁 {entry.name}/")
                walk(entry, depth + 1)
            else:
                try:
                    size = entry.stat().st_size
                    size_str = _format_size(size)
                    lines.append(f"{prefix}📄 {entry.name} ({size_str})")
                except OSError:
                    lines.append(f"{prefix}📄 {entry.name}")

    walk(folder_path)
    return "\n".join(lines)


def resolve_url(url: str) -> str:
    """Fetch a URL and return its contents.

    Uses httpx for HTTP requests. Returns the text content.

    Args:
        url: The URL to fetch.

    Returns:
        Text content from the URL.

    Raises:
        ValueError: If the URL is invalid or request fails.
    """
    import httpx

    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()

        content_type = resp.headers.get("content-type", "")
        if "text" not in content_type and "json" not in content_type:
            raise ValueError(f"Неподдерживаемый тип контента: {content_type}")

        text = resp.text
        if len(text) > 100_000:
            text = text[:100_000] + "\n\n... (truncated at 100K chars)"

        # Detect JSON for pretty-printing
        if "json" in content_type:
            import json as _json

            try:
                parsed = _json.loads(text)
                text = _json.dumps(parsed, indent=2, ensure_ascii=False)
            except _json.JSONDecodeError:
                pass

        return (
            f"🌐 {url}\n"
            f"   Status: {resp.status_code}\n"
            f"   Content-Type: {content_type}\n"
            f"   Size: {_format_size(len(text.encode()))}\n"
            f"\n"
            f"{text}"
        )

    except httpx.HTTPError as exc:
        raise ValueError(f"Ошибка HTTP при запросе {url}: {exc}") from exc
    except Exception as exc:
        raise ValueError(f"Не удалось загрузить {url}: {exc}") from exc


# ─── Text expansion ──────────────────────────────────────────────────────────


def expand_refs(text: str) -> str:
    """Find and expand @file:, @folder:, @url: references in text.

    Replaces each reference with the expanded content inline.

    Syntax:
      @file:path/to/file.py
      @file:path/to/file.py:10-30  (line range)
      @folder:path/to/dir
      @url:https://example.com

    Args:
        text: Input text containing @refs.

    Returns:
        Text with @refs expanded inline.
    """
    import re

    result = text

    # @file:path or @file:path:range
    def _replace_file(m: Any) -> str:
        spec = m.group(1)
        parts = spec.split(":")
        file_path = parts[0]
        line_range = ":".join(parts[1:]) if len(parts) > 1 else ""
        try:
            content = resolve_file(file_path, line_range)
            return f"\n\n>>> @file:{spec}\n{content}\n<<<\n\n"
        except (ValueError, FileNotFoundError, OSError) as exc:
            return f"\n\n>>> @file:{spec}\n⚠️ {exc}\n<<<\n\n"

    result = re.sub(r"@file:(\S+)", _replace_file, result)

    # @folder:path
    def _replace_folder(m: Any) -> str:
        path = m.group(1)
        try:
            content = resolve_folder(path)
            return f"\n\n>>> @folder:{path}\n{content}\n<<<\n\n"
        except (ValueError, FileNotFoundError, NotADirectoryError, OSError) as exc:
            return f"\n\n>>> @folder:{path}\n⚠️ {exc}\n<<<\n\n"

    result = re.sub(r"@folder:(\S+)", _replace_folder, result)

    # @url:url
    def _replace_url(m: Any) -> str:
        url = m.group(1)
        try:
            content = resolve_url(url)
            return f"\n\n>>> @url:{url}\n{content}\n<<<\n\n"
        except ValueError as exc:
            return f"\n\n>>> @url:{url}\n⚠️ {exc}\n<<<\n\n"

    result = re.sub(r"@url:(\S+)", _replace_url, result)

    return result


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _format_size(size: int) -> str:
    """Format a file size in human-readable form."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"
