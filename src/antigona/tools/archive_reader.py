"""ZIP archive handler — extract and read received archives."""

import logging
import zipfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Allowed extensions inside archives (для безопасности)
_ALLOWED_EXTS = {".txt", ".md", ".json", ".yaml", ".yml", ".xml", ".csv",
                 ".py", ".js", ".ts", ".html", ".css", ".scss",
                 ".sh", ".env.example", ".cfg", ".ini", ".conf",
                 ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
                 ".mp3", ".wav", ".ogg", ".mp4", ".mov",
                 ".pdf", ".docx", ".xlsx", ".pptx",}

_MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB total
_MAX_EXTRACT_SIZE = 10 * 1024 * 1024  # 10 MB per file read
_MAX_FILES_TO_LIST = 50


def get_archive_info(filepath: str | Path) -> dict[str, Any]:
    """Get archive metadata without extracting.

    Returns:
        dict with: filename, size_bytes, file_count, total_size,
                   files (list of {name, size, is_dir})
    """
    path = Path(filepath)
    info: dict[str, Any] = {
        "filename": path.name,
        "size_bytes": path.stat().st_size,
        "file_count": 0,
        "total_size": 0,
        "files": [],
        "is_zip": False,
        "is_tar": False,
        "error": None,
    }

    try:
        if zipfile.is_zipfile(path):
            info["is_zip"] = True
            with zipfile.ZipFile(path, "r") as zf:
                for entry in zf.infolist():
                    if entry.is_dir():
                        continue
                    info["files"].append({
                        "name": entry.filename,
                        "size": entry.file_size,
                        "is_dir": False,
                    })
                    info["file_count"] += 1
                    info["total_size"] += entry.file_size
        else:
            # Try tar
            import tarfile
            if tarfile.is_tarfile(path):
                info["is_tar"] = True
                with tarfile.open(path, "r:*") as tf:
                    for member in tf.getmembers():
                        if member.isfile():
                            info["files"].append({
                                "name": member.name,
                                "size": member.size,
                                "is_dir": False,
                            })
                            info["file_count"] += 1
                            info["total_size"] += member.size
            else:
                info["error"] = "Неподдерживаемый формат архива"

        # Trim file list if too large
        if len(info["files"]) > _MAX_FILES_TO_LIST:
            info["files"] = info["files"][:_MAX_FILES_TO_LIST] + [
                {"name": f"... и ещё {len(info['files']) - _MAX_FILES_TO_LIST} файлов",
                 "size": 0, "is_dir": False}
            ]

    except Exception as e:
        info["error"] = str(e)

    return info


def extract_and_read(filepath: str | Path,
                     max_chars: int = 20000,
                     allowed_exts: set[str] | None = None) -> str:
    """Extract archive and read text contents.

    Returns formatted string with all readable file contents.
    Safe: binary files, large files and blocked extensions are skipped.
    """
    path = Path(filepath)
    allowed = allowed_exts or _ALLOWED_EXTS
    result_parts: list[str] = []
    total_read = 0

    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path, "r") as zf:
                for entry in zf.infolist():
                    if entry.is_dir():
                        continue
                    ext = Path(entry.filename).suffix.lower()
                    if ext not in allowed:
                        continue
                    if entry.file_size > _MAX_EXTRACT_SIZE:
                        result_parts.append(
                            f"📄 {entry.filename} — ⏭️ файл слишком большой "
                            f"({entry.file_size / 1024:.0f} KB)"
                        )
                        continue

                    try:
                        content = zf.read(entry.filename)
                        if isinstance(content, bytes):
                            text = content.decode("utf-8", errors="replace")
                        else:
                            text = content

                        if total_read + len(text) > max_chars:
                            text = text[:max_chars - total_read]
                            result_parts.append(
                                f"📄 {entry.filename} ({entry.file_size} байт):\n"
                                f"```\n{text}\n```\n... (обрезано)"
                            )
                            total_read += len(text)
                            break

                        result_parts.append(
                            f"📄 {entry.filename} ({entry.file_size} байт):\n"
                            f"```\n{text}\n```"
                        )
                        total_read += len(text)
                    except Exception:
                        result_parts.append(
                            f"📄 {entry.filename} — ⚠️ не удалось прочитать"
                        )

        else:
            import tarfile
            if tarfile.is_tarfile(path):
                with tarfile.open(path, "r:*") as tf:
                    for member in tf.getmembers():
                        if not member.isfile():
                            continue
                        ext = Path(member.name).suffix.lower()
                        if ext not in allowed:
                            continue
                        if member.size > _MAX_EXTRACT_SIZE:
                            result_parts.append(
                                f"📄 {member.name} — ⏭️ слишком большой "
                                f"({member.size / 1024:.0f} KB)"
                            )
                            continue

                        try:
                            f = tf.extractfile(member)
                            if f is None:
                                continue
                            content = f.read()
                            if isinstance(content, bytes):
                                text = content.decode("utf-8", errors="replace")
                            else:
                                text = content

                            if total_read + len(text) > max_chars:
                                text = text[:max_chars - total_read]
                                result_parts.append(
                                    f"📄 {member.name} ({member.size} байт):\n"
                                    f"```\n{text}\n```\n... (обрезано)"
                                )
                                total_read += len(text)
                                break

                            result_parts.append(
                                f"📄 {member.name} ({member.size} байт):\n"
                                f"```\n{text}\n```"
                            )
                            total_read += len(text)
                        except Exception:
                            result_parts.append(
                                f"📄 {member.name} — ⚠️ не удалось прочитать"
                            )

    except Exception as e:
        return f"❌ Ошибка распаковки: {e}"

    if not result_parts:
        return "📭 Архив пуст или не содержит читаемых файлов."

    return "\n\n".join(result_parts)


def format_archive_info(info: dict[str, Any]) -> str:
    """Format archive metadata for Telegram."""
    if info.get("error"):
        return f"❌ {info['error']}"

    lines = [
        f"📦 <b>{info['filename']}</b>",
        f"  Размер: {info['size_bytes'] / 1024:.1f} KB",
        f"  Файлов: {info['file_count']}",
        f"  Общий размер: {info['total_size'] / 1024:.1f} KB",
        "",
        "📋 <b>Содержимое:</b>",
    ]

    for f in info["files"][:30]:
        size_str = f"{f['size'] / 1024:.1f} KB" if f['size'] > 0 else "-"
        lines.append(f"  📄 {f['name']} ({size_str})")

    if info["file_count"] > 30:
        lines.append(f"  ... и ещё {info['file_count'] - 30} файлов")

    return "\n".join(lines)
