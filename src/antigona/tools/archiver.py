"""Archive creation — zip and tar.gz utilities.

Functions:
  - create_zip: create .zip archive from a list of files
  - create_targz: create .tar.gz archive from a list of files
  - include/exclude patterns for filtering
  - Overwrite protection, size limit (500 MB)
"""

from __future__ import annotations

import fnmatch
import os
import tarfile
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_MAX_ARCHIVE_SIZE_BYTES = 500 * 1024 * 1024  # 500 MB


class ArchiveError(Exception):
    """Base exception for archive operations."""


class ArchiveTooLargeError(ArchiveError):
    """Raised when the estimated archive size exceeds the limit."""


class ArchiveExistsError(ArchiveError):
    """Raised when the output file already exists and overwrite is disabled."""


@dataclass
class ArchiveResult:
    """Result of an archive creation operation."""

    path: str
    file_count: int
    total_bytes: int
    archive_bytes: int
    success: bool = True
    error: str | None = None


def _filter_files(
    files: Sequence[str | Path],
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
) -> list[str]:
    """Filter a list of file paths using include/exclude glob patterns.

    Args:
        files: List of file paths.
        include_patterns: Only include files matching these globs.
        exclude_patterns: Exclude files matching these globs.

    Returns:
        Filtered list of string paths.
    """
    result: list[str] = []
    for f in files:
        f_str = str(f)

        # If include patterns are set, file must match at least one
        if include_patterns:
            matched = any(fnmatch.fnmatch(f_str, pat) for pat in include_patterns)
            if not matched:
                continue

        # If exclude patterns are set, file must not match any
        if exclude_patterns:
            excluded = any(fnmatch.fnmatch(f_str, pat) for pat in exclude_patterns)
            if excluded:
                continue

        result.append(f_str)
    return result


def _validate_files_exist(files: Sequence[str]) -> list[str]:
    """Check that all files exist and return list of valid paths."""
    valid: list[str] = []
    for f in files:
        if os.path.exists(f):
            valid.append(f)
    return valid


def _estimate_total_size(files: Sequence[str]) -> int:
    """Estimate total uncompressed size of files."""
    total = 0
    for f in files:
        try:
            total += os.path.getsize(f)
        except OSError:
            pass
    return total


def create_zip(
    files: Sequence[str | Path],
    output_path: str | Path,
    *,
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    overwrite: bool = False,
) -> ArchiveResult:
    """Create a .zip archive from a list of files.

    Args:
        files: List of file or directory paths to include.
        output_path: Output .zip file path.
        include_patterns: Optional glob filter to include only matching files.
        exclude_patterns: Optional glob filter to exclude matching files.
        overwrite: If True, overwrite existing file. Default False.

    Returns:
        ArchiveResult with path, counts, and size info.

    Raises:
        ArchiveTooLargeError: if estimated size exceeds 500 MB.
        ArchiveExistsError: if output exists and overwrite is False.
    """
    output = Path(output_path)
    if output.suffix != ".zip":
        output = output.with_suffix(".zip")

    if output.exists() and not overwrite:
        raise ArchiveExistsError(
            f"Output file '{output}' already exists. Use overwrite=True to replace."
        )

    # Filter
    filtered = _filter_files(files, include_patterns, exclude_patterns)
    valid_files = _validate_files_exist(filtered)

    if not valid_files:
        return ArchiveResult(
            path=str(output),
            file_count=0,
            total_bytes=0,
            archive_bytes=0,
            success=True,
            error=None,
        )

    total_size = _estimate_total_size(valid_files)
    if total_size > _MAX_ARCHIVE_SIZE_BYTES:
        raise ArchiveTooLargeError(
            f"Estimated archive size ({total_size / 1024 / 1024:.1f} MB) "
            f"exceeds the {_MAX_ARCHIVE_SIZE_BYTES / 1024 / 1024:.0f} MB limit."
        )

    # Create parent dirs
    output.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in valid_files:
            f_path = Path(f)
            if f_path.is_dir():
                for dirpath, _dirnames, filenames in os.walk(f):
                    for fn in filenames:
                        full = os.path.join(dirpath, fn)
                        arcname = os.path.relpath(full, start=os.path.commonpath(valid_files))
                        zf.write(full, arcname)
            else:
                arcname = f_path.name
                if len(valid_files) > 1:
                    # Use relative path for multiple files
                    arcname = os.path.relpath(
                        str(f_path),
                        start=os.path.commonpath(valid_files),
                    )
                zf.write(str(f_path), arcname)

    archive_bytes = output.stat().st_size
    return ArchiveResult(
        path=str(output),
        file_count=len(valid_files),
        total_bytes=total_size,
        archive_bytes=archive_bytes,
        success=True,
    )


def create_targz(
    files: Sequence[str | Path],
    output_path: str | Path,
    *,
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    overwrite: bool = False,
) -> ArchiveResult:
    """Create a .tar.gz archive from a list of files.

    Args:
        files: List of file or directory paths to include.
        output_path: Output .tar.gz file path.
        include_patterns: Optional glob filter to include only matching files.
        exclude_patterns: Optional glob filter to exclude matching files.
        overwrite: If True, overwrite existing file. Default False.

    Returns:
        ArchiveResult with path, counts, and size info.

    Raises:
        ArchiveTooLargeError: if estimated size exceeds 500 MB.
        ArchiveExistsError: if output exists and overwrite is False.
    """
    output = Path(output_path)
    if not output.name.endswith((".tar.gz", ".tgz")):
        output = output.with_suffix(".tar.gz")

    if output.exists() and not overwrite:
        raise ArchiveExistsError(
            f"Output file '{output}' already exists. Use overwrite=True to replace."
        )

    # Filter
    filtered = _filter_files(files, include_patterns, exclude_patterns)
    valid_files = _validate_files_exist(filtered)

    if not valid_files:
        return ArchiveResult(
            path=str(output),
            file_count=0,
            total_bytes=0,
            archive_bytes=0,
            success=True,
        )

    total_size = _estimate_total_size(valid_files)
    if total_size > _MAX_ARCHIVE_SIZE_BYTES:
        raise ArchiveTooLargeError(
            f"Estimated archive size ({total_size / 1024 / 1024:.1f} MB) "
            f"exceeds the {_MAX_ARCHIVE_SIZE_BYTES / 1024 / 1024:.0f} MB limit."
        )

    # Create parent dirs
    output.parent.mkdir(parents=True, exist_ok=True)

    with tarfile.open(output, "w:gz") as tf:
        for f in valid_files:
            f_path = Path(f)
            if f_path.is_dir():
                for dirpath, _dirnames, filenames in os.walk(f):
                    for fn in filenames:
                        full = os.path.join(dirpath, fn)
                        arcname = os.path.relpath(full, start=os.path.commonpath(valid_files))
                        tf.add(full, arcname)
            else:
                arcname = f_path.name
                if len(valid_files) > 1:
                    arcname = os.path.relpath(
                        str(f_path),
                        start=os.path.commonpath(valid_files),
                    )
                tf.add(str(f_path), arcname)

    archive_bytes = output.stat().st_size
    return ArchiveResult(
        path=str(output),
        file_count=len(valid_files),
        total_bytes=total_size,
        archive_bytes=archive_bytes,
        success=True,
    )
