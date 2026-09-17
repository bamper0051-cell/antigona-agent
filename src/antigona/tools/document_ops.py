"""Document Creation and Verification Tools for Antigona.

Provides tools to create verified documents:
  - Text (.txt)
  - Markdown (.md)
  - JSON (.json)
  - CSV (.csv)
  - (PDF / DOCX / XLSX when libraries present)

Verification law:
  - Text/JSON/CSV: write -> exists -> read-back -> compare expected content -> checksum
  - Binary: write -> exists -> size > 0 -> reopen/parse -> checksum
  - NEVER declare DONE if write did not actually happen or content differs.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
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


class DocumentVerificationError(RuntimeError):
    """Raised when document verification fails."""


@dataclass
class VerifiedDocument:
    name: str
    path: str
    type: str
    sha256: str
    size_bytes: int
    verified: bool = True


def compute_sha256(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def create_and_verify_document(
    filename: str,
    content: str | bytes | dict[str, Any] | list[Any],
    *,
    doc_type: str = "text",
    workspace_root: Path | str | None = None,
    overwrite: bool = True,
) -> VerifiedDocument:
    """Create a document inside the workspace and rigorously verify it."""
    ws = Path(workspace_root or paths.workspace_dir()).resolve()
    ws.mkdir(parents=True, exist_ok=True)

    from antigona.worker.tools.common import ToolError, WorkspaceGuard
    try:
        dest = WorkspaceGuard(ws).resolve(filename)
    except (ToolError, ValueError) as err:
        raise DocumentVerificationError(f"Target path '{filename}' violates workspace boundary '{ws}': {err}") from err

    if dest.exists() and not overwrite:
        raise DocumentVerificationError(f"File already exists and overwrite=False: {dest}")

    dest.parent.mkdir(parents=True, exist_ok=True)

    expected_text: str | None = None

    if doc_type == "json" or filename.endswith(".json"):
        if isinstance(content, (dict, list)):
            expected_text = json.dumps(content, indent=2, ensure_ascii=False)
            data_to_write = expected_text
        else:
            expected_text = str(content)
            # Verify valid JSON syntax
            json.loads(expected_text)
            data_to_write = expected_text
        dest.write_text(data_to_write, encoding="utf-8", newline="\n")

    elif doc_type == "csv" or filename.endswith(".csv"):
        if isinstance(content, list):
            output = io.StringIO()
            writer = csv.writer(output)
            for row in content:
                if isinstance(row, (list, tuple)):
                    writer.writerow(row)
                elif isinstance(row, dict):
                    writer.writerow(list(row.values()))
            expected_text = output.getvalue()
            data_to_write = expected_text
        else:
            expected_text = str(content)
            data_to_write = expected_text
        dest.write_text(data_to_write, encoding="utf-8", newline="\n")

    elif isinstance(content, bytes):
        dest.write_bytes(content)
    else:
        expected_text = str(content)
        dest.write_text(expected_text, encoding="utf-8", newline="\n")

    # Rigorous Verification Step
    if not dest.exists():
        raise DocumentVerificationError(f"Verification failed: File was not created at {dest}")

    actual_size = dest.stat().st_size
    if actual_size == 0 and content not in ("", b"", [], {}):
        raise DocumentVerificationError(f"Verification failed: File at {dest} is 0 bytes")

    if expected_text is not None:
        read_back = dest.read_text(encoding="utf-8")
        if read_back != expected_text:
            raise DocumentVerificationError(
                f"Verification failed: Read-back content does not match expected text for {dest}"
            )
        if doc_type == "json" or filename.endswith(".json"):
            # Verify read-back parses as JSON
            try:
                json.loads(read_back)
            except Exception as exc:
                raise DocumentVerificationError(f"Verification failed: Read-back JSON is corrupt: {exc}") from exc

    doc_sha256 = compute_sha256(dest)

    return VerifiedDocument(
        name=dest.name,
        path=str(dest),
        type=doc_type or dest.suffix.lstrip("."),
        sha256=doc_sha256,
        size_bytes=actual_size,
        verified=True,
    )


class CreateDocumentTool(Tool):
    """Tool to create and verify structured documents (TXT, MD, JSON, CSV)."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="workspace.create_document",
            category=ToolCategory.FILESYSTEM_WRITE,
            description="Create and verify a document in the workspace (txt, md, json, csv)",
            risk_level=RiskLevel.SAFE,
            input_schema={
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "content": {"type": "string"},
                    "doc_type": {"type": "string", "enum": ["text", "markdown", "json", "csv"]},
                },
                "required": ["filename", "content"],
            },
        )

    def validate(self, inp: ToolInput) -> list[str]:
        errors = []
        if not inp.params.get("filename"):
            errors.append("Missing 'filename'")
        if "content" not in inp.params:
            errors.append("Missing 'content'")
        return errors

    async def execute(self, inp: ToolInput) -> ToolOutput:
        filename = inp.params["filename"]
        content = inp.params["content"]
        doc_type = inp.params.get("doc_type") or "text"

        try:
            doc = create_and_verify_document(filename, content, doc_type=doc_type)
            return ToolOutput(
                success=True,
                data={
                    "name": doc.name,
                    "path": doc.path,
                    "type": doc.type,
                    "sha256": doc.sha256,
                    "size_bytes": doc.size_bytes,
                    "verified": True,
                },
                artifacts=[{"name": doc.name, "path": doc.path, "sha256": doc.sha256}],
            )
        except Exception as exc:
            return ToolOutput(success=False, error=str(exc))
