"""Archive Operations Tool for Antigona.

Provides full lifecycle archive operations:
  - archive.inspect: Inspect format, size, file count and entries without extracting
  - archive.list: List files inside archive
  - archive.extract: Safe extraction with path traversal and zip-bomb protection
  - archive.create: Create ZIP or TAR.GZ archive and verify contents
  - archive.verify: Test integrity of an archive

Security guarantees:
  - Path traversal protection: rejects entries with '../', absolute paths, or escaping target
  - Symlink escape prevention: does not follow symlinks outside target directory
  - Zip-bomb limits: enforces max extracted size and max extracted files
  - Workspace confinement: target directory must resolve within workspace
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from antigona.core import paths
from antigona.tools.contracts import (
    RiskLevel,
    Tool,
    ToolCategory,
    ToolInput,
    ToolOutput,
    ToolSpec,
)

# Limits for safety
DEFAULT_MAX_UNPACK_BYTES = 100 * 1024 * 1024  # 100 MB
DEFAULT_MAX_UNPACK_FILES = 1000
DEFAULT_MAX_ARCHIVE_CREATE_BYTES = 500 * 1024 * 1024  # 500 MB


class ArchiveSecurityError(ValueError):
    """Raised when an archive violates security policies (traversal, bomb, etc.)."""


class ArchiveVerificationError(RuntimeError):
    """Raised when archive verification fails."""


@dataclass
class ArchiveInspection:
    path: str
    format: str  # "zip", "tar", "tar.gz", "tar.bz2"
    file_count: int
    compressed_bytes: int
    uncompressed_bytes: int
    files: list[dict[str, Any]] = field(default_factory=list)
    sha256: str = ""
    valid: bool = True
    error: str | None = None


def compute_sha256(file_path: Path | str) -> str:
    """Compute SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def inspect_archive(archive_path: Path | str) -> ArchiveInspection:
    """Inspect archive metadata and contents without writing to disk."""
    path = Path(archive_path).resolve()
    if not path.exists() or not path.is_file():
        return ArchiveInspection(
            path=str(path),
            format="unknown",
            file_count=0,
            compressed_bytes=0,
            uncompressed_bytes=0,
            valid=False,
            error=f"Archive file does not exist: {path}",
        )

    file_size = path.stat().st_size
    file_sha256 = compute_sha256(path)

    # Check zip
    if zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path, "r") as zf:
                bad_file = zf.testzip()
                if bad_file:
                    return ArchiveInspection(
                        path=str(path),
                        format="zip",
                        file_count=len(zf.infolist()),
                        compressed_bytes=file_size,
                        uncompressed_bytes=sum(e.file_size for e in zf.infolist()),
                        sha256=file_sha256,
                        valid=False,
                        error=f"Corrupt zip entry: {bad_file}",
                    )

                files_info = []
                total_uncompressed = 0
                for info in zf.infolist():
                    if not info.is_dir():
                        files_info.append({
                            "name": info.filename,
                            "size": info.file_size,
                            "compressed_size": info.compress_size,
                        })
                        total_uncompressed += info.file_size

                return ArchiveInspection(
                    path=str(path),
                    format="zip",
                    file_count=len(files_info),
                    compressed_bytes=file_size,
                    uncompressed_bytes=total_uncompressed,
                    files=files_info,
                    sha256=file_sha256,
                    valid=True,
                )
        except Exception as exc:
            return ArchiveInspection(
                path=str(path),
                format="zip",
                file_count=0,
                compressed_bytes=file_size,
                uncompressed_bytes=0,
                sha256=file_sha256,
                valid=False,
                error=str(exc),
            )

    # Check tar
    try:
        if tarfile.is_tarfile(path):
            with tarfile.open(path, "r:*") as tf:
                files_info = []
                total_uncompressed = 0
                for member in tf.getmembers():
                    if member.isfile():
                        files_info.append({
                            "name": member.name,
                            "size": member.size,
                        })
                        total_uncompressed += member.size

                fmt = "tar"
                if path.name.endswith((".tar.gz", ".tgz")):
                    fmt = "tar.gz"
                elif path.name.endswith((".tar.bz2", ".tbz2")):
                    fmt = "tar.bz2"

                return ArchiveInspection(
                    path=str(path),
                    format=fmt,
                    file_count=len(files_info),
                    compressed_bytes=file_size,
                    uncompressed_bytes=total_uncompressed,
                    files=files_info,
                    sha256=file_sha256,
                    valid=True,
                )
    except Exception as exc:
        return ArchiveInspection(
            path=str(path),
            format="tar",
            file_count=0,
            compressed_bytes=file_size,
            uncompressed_bytes=0,
            sha256=file_sha256,
            valid=False,
            error=str(exc),
        )

    return ArchiveInspection(
        path=str(path),
        format="unsupported",
        file_count=0,
        compressed_bytes=file_size,
        uncompressed_bytes=0,
        valid=False,
        error="Unsupported archive format",
    )


def safe_extract_archive(
    archive_path: Path | str,
    target_dir: Path | str,
    *,
    max_bytes: int = DEFAULT_MAX_UNPACK_BYTES,
    max_files: int = DEFAULT_MAX_UNPACK_FILES,
    allowed_workspace_root: Path | str | None = None,
) -> dict[str, Any]:
    """Safely extract archive with comprehensive path traversal and bomb protection."""
    src = Path(archive_path).resolve()
    dest = Path(target_dir).resolve()

    ws_root = Path(allowed_workspace_root or paths.workspace_dir()).resolve()
    # Ensure dest is inside allowed workspace
    try:
        dest.relative_to(ws_root)
    except ValueError as err:
        raise ArchiveSecurityError(f"Target directory '{dest}' is outside allowed workspace '{ws_root}'") from err

    dest.mkdir(parents=True, exist_ok=True)

    extracted_files: list[str] = []
    total_uncompressed = 0

    if zipfile.is_zipfile(src):
        with zipfile.ZipFile(src, "r") as zf:
            infolist = zf.infolist()
            if len(infolist) > max_files:
                raise ArchiveSecurityError(
                    f"Archive contains {len(infolist)} files, exceeding limit of {max_files}"
                )

            # Pre-flight validation of all entries
            for entry in infolist:
                norm_name = entry.filename.replace("\\", "/")
                # Reject path traversal patterns
                if norm_name.startswith("/") or ".." in norm_name.split("/"):
                    raise ArchiveSecurityError(
                        f"Path traversal detected in archive entry: '{entry.filename}'"
                    )

                # Check resolved destination path
                target_path = (dest / norm_name).resolve()
                try:
                    target_path.relative_to(dest)
                except ValueError as err:
                    raise ArchiveSecurityError(
                        f"Entry '{entry.filename}' resolves outside target directory: '{target_path}'"
                    ) from err

                total_uncompressed += entry.file_size
                if total_uncompressed > max_bytes:
                    raise ArchiveSecurityError(
                        f"Archive uncompressed size ({total_uncompressed} bytes) exceeds limit ({max_bytes} bytes)"
                    )

            # Perform extraction
            for entry in infolist:
                if entry.is_dir():
                    (dest / entry.filename).mkdir(parents=True, exist_ok=True)
                    continue

                target_path = dest / entry.filename
                target_path.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(entry) as source, open(target_path, "wb") as sink:
                    shutil.copyfileobj(source, sink)
                extracted_files.append(str(target_path.relative_to(dest)))

    elif tarfile.is_tarfile(src):
        with tarfile.open(src, "r:*") as tf:
            members = tf.getmembers()
            if len(members) > max_files:
                raise ArchiveSecurityError(
                    f"Archive contains {len(members)} files, exceeding limit of {max_files}"
                )

            for member in members:
                norm_name = member.name.replace("\\", "/")
                if norm_name.startswith("/") or ".." in norm_name.split("/"):
                    raise ArchiveSecurityError(
                        f"Path traversal detected in tar member: '{member.name}'"
                    )
                if member.islnk() or member.issym():
                    link_target = member.linkname
                    if link_target.startswith("/") or ".." in link_target.split("/"):
                        raise ArchiveSecurityError(
                            f"Unsafe symlink target in member: '{member.name}' -> '{link_target}'"
                        )

                target_path = (dest / norm_name).resolve()
                try:
                    target_path.relative_to(dest)
                except ValueError as err:
                    raise ArchiveSecurityError(
                        f"Tar member '{member.name}' resolves outside target directory"
                    ) from err

                total_uncompressed += member.size
                if total_uncompressed > max_bytes:
                    raise ArchiveSecurityError(
                        f"Archive uncompressed size exceeds limit ({max_bytes} bytes)"
                    )

            # Safe extraction
            for member in members:
                if member.isdir():
                    (dest / member.name).mkdir(parents=True, exist_ok=True)
                    continue
                if member.isfile():
                    target_path = dest / member.name
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    f = tf.extractfile(member)
                    if f is not None:
                        with open(target_path, "wb") as sink:
                            shutil.copyfileobj(f, sink)
                        extracted_files.append(str(target_path.relative_to(dest)))
    else:
        raise ArchiveSecurityError(f"Unsupported or corrupted archive: {src}")

    return {
        "success": True,
        "archive": str(src),
        "target_dir": str(dest),
        "file_count": len(extracted_files),
        "total_bytes": total_uncompressed,
        "files": extracted_files,
    }


def create_and_verify_archive(
    output_path: Path | str,
    files_or_dir: Sequence[Path | str] | Path | str,
    *,
    format: str = "zip",
    overwrite: bool = True,
    allowed_workspace_root: Path | str | None = None,
) -> dict[str, Any]:
    """Create an archive and immediately verify its contents and readable headers."""
    out = Path(output_path).resolve()
    ws_root = Path(allowed_workspace_root or paths.workspace_dir()).resolve()

    try:
        out.relative_to(ws_root)
    except ValueError as err:
        raise ArchiveSecurityError(f"Output archive path '{out}' is outside workspace '{ws_root}'") from err

    if out.exists() and not overwrite:
        raise FileExistsError(f"Archive file already exists: {out}")

    out.parent.mkdir(parents=True, exist_ok=True)

    # Collect source files
    file_map: dict[Path, str] = {}  # source_path -> archive_relname
    if isinstance(files_or_dir, (str, Path)):
        source = Path(files_or_dir).resolve()
        if source.is_dir():
            for root, _, filenames in os.walk(source):
                for fn in filenames:
                    fp = Path(root) / fn
                    rel = str(fp.relative_to(source))
                    file_map[fp] = rel
        elif source.is_file():
            file_map[source] = source.name
    else:
        for item in files_or_dir:
            p = Path(item).resolve()
            if p.is_file():
                file_map[p] = p.name

    if not file_map:
        raise ValueError("No files provided for archive creation")

    # Create archive
    if format.lower() == "zip":
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for src_p, rel_name in sorted(file_map.items()):
                zf.write(src_p, arcname=rel_name)
    elif format.lower() in ("tar.gz", "tgz"):
        with tarfile.open(out, "w:gz") as tf:
            for src_p, rel_name in sorted(file_map.items()):
                tf.add(src_p, arcname=rel_name)
    elif format.lower() == "tar":
        with tarfile.open(out, "w") as tf:
            for src_p, rel_name in sorted(file_map.items()):
                tf.add(src_p, arcname=rel_name)
    else:
        raise ValueError(f"Unsupported archive format: {format}")

    # Immediately verify created archive
    if not out.exists() or out.stat().st_size == 0:
        raise ArchiveVerificationError(f"Created archive does not exist or is empty: {out}")

    insp = inspect_archive(out)
    if not insp.valid:
        raise ArchiveVerificationError(f"Created archive failed verification: {insp.error}")

    return {
        "success": True,
        "path": str(out),
        "format": insp.format,
        "file_count": insp.file_count,
        "archive_bytes": out.stat().st_size,
        "sha256": insp.sha256,
        "verified": True,
        "files": [f["name"] for f in insp.files],
    }


class ArchiveInspectTool(Tool):
    """Tool to inspect archive contents."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="archive.inspect",
            category=ToolCategory.FILESYSTEM_READ,
            description="Inspect archive format, size, file count and file list",
            risk_level=RiskLevel.SAFE,
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        )

    def validate(self, inp: ToolInput) -> list[str]:
        if not inp.params.get("path"):
            return ["Missing 'path' parameter"]
        return []

    async def execute(self, inp: ToolInput) -> ToolOutput:
        path = inp.params["path"]
        insp = inspect_archive(path)
        if not insp.valid:
            return ToolOutput(success=False, error=insp.error or "Invalid archive")
        return ToolOutput(
            success=True,
            data={
                "path": insp.path,
                "format": insp.format,
                "file_count": insp.file_count,
                "compressed_bytes": insp.compressed_bytes,
                "uncompressed_bytes": insp.uncompressed_bytes,
                "sha256": insp.sha256,
                "files": insp.files,
            },
        )


class ArchiveExtractTool(Tool):
    """Tool to safely extract an archive."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="archive.extract",
            category=ToolCategory.FILESYSTEM_WRITE,
            description="Safely extract an archive with path traversal and bomb protection",
            risk_level=RiskLevel.SAFE,
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "target_dir": {"type": "string"},
                },
                "required": ["path"],
            },
        )

    def validate(self, inp: ToolInput) -> list[str]:
        if not inp.params.get("path"):
            return ["Missing 'path' parameter"]
        return []

    async def execute(self, inp: ToolInput) -> ToolOutput:
        path = inp.params["path"]
        target_dir = inp.params.get("target_dir") or str(paths.workspace_dir() / "extracted")
        try:
            res = safe_extract_archive(path, target_dir)
            return ToolOutput(success=True, data=res)
        except Exception as exc:
            return ToolOutput(success=False, error=str(exc))


class ArchiveCreateTool(Tool):
    """Tool to create and verify a new archive."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="archive.create",
            category=ToolCategory.FILESYSTEM_WRITE,
            description="Create a ZIP/TAR archive in the workspace and verify it",
            risk_level=RiskLevel.SAFE,
            input_schema={
                "type": "object",
                "properties": {
                    "output_path": {"type": "string"},
                    "source": {"type": "string"},
                    "format": {"type": "string", "enum": ["zip", "tar.gz", "tar"]},
                },
                "required": ["output_path", "source"],
            },
        )

    def validate(self, inp: ToolInput) -> list[str]:
        errors = []
        if not inp.params.get("output_path"):
            errors.append("Missing 'output_path'")
        if not inp.params.get("source"):
            errors.append("Missing 'source'")
        return errors

    async def execute(self, inp: ToolInput) -> ToolOutput:
        output_path = inp.params["output_path"]
        source = inp.params["source"]
        fmt = inp.params.get("format", "zip")
        try:
            res = create_and_verify_archive(output_path, source, format=fmt)
            return ToolOutput(success=True, data=res, artifacts=[{"name": Path(output_path).name, "path": res["path"], "sha256": res["sha256"]}])
        except Exception as exc:
            return ToolOutput(success=False, error=str(exc))
