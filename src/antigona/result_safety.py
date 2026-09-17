"""Fail-closed projection of untrusted tool output and execution inputs.

Raw tool data is transient and may contain credentials, stderr, tracebacks, or
arbitrarily large values.  Only the bounded whitelist projection produced here
may cross a durable/public boundary.
"""

from __future__ import annotations

import html
import os
import re
import stat
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .contracts import ToolResult

MAX_RESULT_TEXT = 4096
MAX_FAILURE_REASON = 512
REDACTED = "[REDACTED]"
DIAGNOSTIC_OMITTED = "[diagnostic output omitted]"
_TRUNCATION_MARKER = "...[truncated]"

_SECRET_NAMES = (
    r"api[_-]?key|secret[_-]?access[_-]?key|access[_-]?key|access[_-]?token|"
    r"auth[_-]?token|authorization|client[_-]?secret|private[_-]?key|"
    r"database[_-]?url|db[_-]?url|db[_-]?password|password|passwd|pwd|secret|"
    r"token|credential|cookie|session"
)
_BEARER_TOKEN_RE = re.compile(
    r"""(?ix)
    \bBearer(?:[ \t]+|\\[tnr]+)
    (?!\[REDACTED\])
    (?:
        \"(?:(?!\")[^\r\n])*\"
      | \'(?:(?!\')[^\r\n])*\'
      | [^\s,;}\]]+
    )
    """
)
_SECRET_ASSIGNMENT_RE = re.compile(
    rf"""(?ix)
    (?P<prefix>
        (?<![A-Za-z0-9])
        (?!["']?(?-i:Pwd)["']?\s*:\s*not(?=\s+found(?:\s|$)))
        ["']?(?:{_SECRET_NAMES})["']?
        \s*[:=]\s*
    )
    (?!\[REDACTED\])
    (?:
        "(?:\\.|[^"\\\r\n])*"
      | '(?:\\.|[^'\\\r\n])*'
      | [^\s,;}}\]]+
    )
    """
)
_SECRET_LABEL_RE = re.compile(rf"(?i)\b(?:{_SECRET_NAMES})\b")
_PROVIDER_TOKEN_RE = re.compile(
    r"""(?ix)
    (?<![A-Za-z0-9])
    (?:
        (?:sk|pk|rk|gsk)[-_][A-Za-z0-9][A-Za-z0-9_-]{8,}
      | github_pat_[A-Za-z0-9_]{10,}
      | gh[pousr]_[A-Za-z0-9]{10,}
      | xox[baprs]-[A-Za-z0-9-]{10,}
      | hf_[A-Za-z0-9][A-Za-z0-9_-]{8,}
      | glpat-[A-Za-z0-9_-]{10,}
      | npm_[A-Za-z0-9_-]{10,}
      | pypi-[A-Za-z0-9_-]{10,}
      | AIza[A-Za-z0-9_-]{20,}
      | ya29\.[A-Za-z0-9._-]{10,}
      | dop_v1_[A-Za-z0-9_-]{10,}
      | lin_api_[A-Za-z0-9_-]{10,}
    )
    (?![A-Za-z0-9])
    """
)
_JWT_RE = re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])")
_TELEGRAM_TOKEN_RE = re.compile(r"(?<!\d)\d{6,}:[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")
_AWS_ACCESS_KEY_RE = re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])")
_PRIVATE_KEY_BLOCK_RE = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----.*?"
    r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
    re.DOTALL,
)
_URL_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>\"']+")
_URL_USERINFO_SEPARATOR_RE = re.compile(r"@|%40", re.IGNORECASE)
_TRACEBACK_RE = re.compile(
    r"(?i)(?:\btraceback\b|\bstack trace\b|(?:^|\n)\s*File \"[^\n]+\", line \d+)",
)

_SENSITIVE_COMPONENTS = frozenset(
    {
        ".aws",
        ".gnupg",
        ".ssh",
        "credential",
        "credentials",
        "key",
        "keys",
        "secret",
        "secrets",
        "token",
        "tokens",
        "vault",
        "vaults",
    }
)
_HIDDEN_CREDENTIAL_PREFIXES = (
    ".api-key",
    ".api_key",
    ".apikey",
    ".auth",
    ".credential",
    ".credentials",
    ".netrc",
    ".npmrc",
    ".password",
    ".passwd",
    ".pgpass",
    ".private-key",
    ".private_key",
    ".pypirc",
    ".secret",
    ".secrets",
    ".token",
    ".tokens",
)
_SENSITIVE_SUFFIXES = (
    ".key",
    ".kdbx",
    ".keystore",
    ".netrc",
    ".p12",
    ".pem",
    ".pfx",
    ".pgpass",
)
_SENSITIVE_COMMAND_RE = re.compile(
    r"(?i)(?:^|[^a-z0-9])(?:\.env[a-z0-9_.-]*|secrets?|vaults?|credentials?|"
    r"private[_-]?key|api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|"
    r"/proc/(?:self|\d+)/environ|os\.environ|getenv\s*\()"
)
_SECRET_ASSIGNMENT_COMMAND_RE = re.compile(rf"(?i)\b(?:{_SECRET_NAMES})\s*=")
_NESTED_ENV_DUMP_RE = re.compile(
    r"(?i)(?:^|[;&|]\s*|\s)(?:env|export|printenv|set)(?:\s|[;&|]|$)"
)
_ENV_DUMP_COMMANDS = frozenset({"env", "export", "printenv", "set"})
_TOKEN_SPLIT_RE = re.compile(r"[\s'\"=,;|&(){}\[\]<>]+")


def _bounded(text: str, max_length: int) -> str:
    limit = max(0, max_length)
    if len(text) <= limit:
        return text
    if limit <= len(_TRUNCATION_MARKER):
        return text[:limit]
    return text[: limit - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


def _redact_url_userinfo(match: re.Match[str]) -> str:
    raw = match.group(0)
    core = raw
    trailing = ""
    while core and core[-1] in ".,;:!?)]}":
        trailing = core[-1] + trailing
        core = core[:-1]
    scheme, separator, remainder = core.partition("://")
    if not separator:
        return raw
    authority_end = len(remainder)
    for delimiter in ("/", "?", "#"):
        index = remainder.find(delimiter)
        if index >= 0:
            authority_end = min(authority_end, index)
    authority = remainder[:authority_end]
    separators = list(_URL_USERINFO_SEPARATOR_RE.finditer(authority))
    if not separators:
        return raw
    credential_separator = separators[-1]
    host = authority[credential_separator.end() :]
    if not host:
        return raw
    suffix = remainder[authority_end:]
    return f"{scheme}://{REDACTED}@{host}{suffix}{trailing}"


def _redact_credentials(text: str) -> str:
    redacted = _PRIVATE_KEY_BLOCK_RE.sub(REDACTED, text)
    redacted = _URL_RE.sub(_redact_url_userinfo, redacted)
    redacted = _BEARER_TOKEN_RE.sub(f"Bearer {REDACTED}", redacted)
    redacted = _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group('prefix')}{REDACTED}",
        redacted,
    )
    redacted = _PROVIDER_TOKEN_RE.sub(REDACTED, redacted)
    redacted = _JWT_RE.sub(REDACTED, redacted)
    redacted = _TELEGRAM_TOKEN_RE.sub(REDACTED, redacted)
    return _AWS_ACCESS_KEY_RE.sub(REDACTED, redacted)


def _remove_unsafe_unicode(text: str, *, preserve_newlines: bool = False) -> str:
    return "".join(
        character
        for character in text
        if (preserve_newlines and character in "\n\r\t")
        or unicodedata.category(character) not in {"Cc", "Cf"}
    )



def sanitize_result_text(
    value: object | None,
    *,
    max_length: int = MAX_RESULT_TEXT,
    escape_html: bool = True,
    remove_controls: bool = True,
    preserve_newlines: bool = False,
) -> str | None:
    """Redact the complete value, remove controls, optionally escape HTML, then bound it.

    ``escape_html=False`` yields the security projection only (redaction,
    control-character removal, traceback omission) — no HTML escaping. Safety
    gates that must detect *secret leakage* (not rendering concerns) compare
    against this projection, so a plain quote in user text (``"`` -> ``&quot;``
    under escaping) is not mistaken for a sensitive change.

    ``remove_controls=False`` additionally keeps control characters (e.g.
    newlines) in the projection: a line break is not a secret, so task-creation
    gates must not treat it as a sensitive change either.

    ``preserve_newlines=True`` keeps newline/tab whitespace (\\n, \\t) while
    removing all other control characters; carriage returns are normalized
    to \\n upstream (``str(value).replace(...)``), so \\r is never preserved.
    """

    if value is None:
        return None
    text = html.unescape(str(value).replace("\r\n", "\n").replace("\r", "\n"))
    if _TRACEBACK_RE.search(text):
        return _bounded(DIAGNOSTIC_OMITTED, max_length)
    text = _redact_credentials(text)
    if remove_controls:
        text = _remove_unsafe_unicode(text, preserve_newlines=preserve_newlines)
    if escape_html:
        text = html.escape(text, quote=True)
    return _bounded(text, max_length)


def is_usable_result_text(value: object | None) -> bool:
    """Require meaningful text, not whitespace, diagnostics, or only redactions."""

    if value is None:
        return False
    text = html.unescape(str(value)).strip()
    if not text or text.casefold() == DIAGNOSTIC_OMITTED.casefold():
        return False
    residual = text.replace(REDACTED, "").replace(_TRUNCATION_MARKER, "")
    residual = _SECRET_LABEL_RE.sub("", residual)
    residual = re.sub(r"(?i)\bBearer\b", "", residual)
    residual = re.sub(r"[\s:=,;.'\"`~!@#$%^&*(){}\[\]<>/?\\|+_-]+", "", residual)
    return bool(residual)


def _normalized_path(path: str | Path) -> str:
    return str(path).strip().replace("\\", "/")


def _path_parts(raw: str) -> tuple[str, ...]:
    return tuple(component.casefold() for component in raw.split("/"))


def _workspace_namespace_is_safe(relative_path: str, workspace_root: Path) -> bool:
    if os.name == "nt":
        # Windows: os.open has no dir_fd / O_DIRECTORY / O_NOFOLLOW / O_CLOEXEC.
        # Fall back to a best-effort lexical check: resolved target must stay
        # inside the workspace root (no symlink escape).
        try:
            candidate = (workspace_root / relative_path).resolve()
            root_resolved = workspace_root.resolve()
            return root_resolved == candidate or root_resolved in candidate.parents
        except (OSError, RuntimeError, ValueError):
            return False
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    opened: list[int] = []
    try:
        directory = os.open(workspace_root, flags | os.O_DIRECTORY)
        opened.append(directory)
        parts = relative_path.split("/")
        for index, component in enumerate(parts):
            try:
                metadata = os.stat(component, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                return True
            if stat.S_ISLNK(metadata.st_mode):
                return False
            if index == len(parts) - 1:
                return True
            if not stat.S_ISDIR(metadata.st_mode):
                return False
            next_directory = os.open(component, flags | os.O_DIRECTORY, dir_fd=directory)
            opened.append(next_directory)
            directory = next_directory
        return True
    except (OSError, RuntimeError, ValueError):
        return False
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


def is_sensitive_path(
    path: str | Path | None,
    *,
    workspace_root: Path | None = None,
) -> bool:
    """Fail closed for lexical escapes, credential names, and symlink namespaces."""

    if path is None:
        return False
    raw = _normalized_path(path)
    if not raw or "\x00" in raw or raw.startswith(("/", "~")):
        return True
    if re.match(r"^[a-z]:", raw, re.IGNORECASE):
        return True
    parts = _path_parts(raw)
    if any(component in {"", ".", ".."} for component in parts):
        return True
    for component in parts:
        if component.startswith(".env"):
            return True
        if component in _SENSITIVE_COMPONENTS:
            return True
        if component.startswith(_HIDDEN_CREDENTIAL_PREFIXES):
            return True
        stem = component.rsplit(".", 1)[0]
        tokens = frozenset(filter(None, re.split(r"[^a-z0-9]+", stem)))
        if tokens.intersection(_SENSITIVE_COMPONENTS):
            return True
        if component in {"id_dsa", "id_ecdsa", "id_ed25519", "id_rsa"}:
            return True
        if component.endswith(_SENSITIVE_SUFFIXES):
            return True
    if workspace_root is not None and not _workspace_namespace_is_safe(raw, workspace_root):
        return True
    return False


def _command_candidates(argv: list[str]) -> list[str]:
    candidates = list(argv)
    for argument in argv:
        candidates.extend(part for part in _TOKEN_SPLIT_RE.split(argument) if part)
    return candidates


def is_sensitive_command(
    command: Sequence[str] | str | None,
    *,
    workspace_root: Path | None = None,
) -> bool:
    """Reject credential operations and every unsafe path form in command text."""

    if command is None:
        return False
    argv = [command] if isinstance(command, str) else [str(part) for part in command]
    if not argv:
        return False
    executable = Path(argv[0]).name.casefold()
    joined = " ".join(argv)
    if executable in _ENV_DUMP_COMMANDS:
        return True
    if _SENSITIVE_COMMAND_RE.search(joined):
        return True
    if _SECRET_ASSIGNMENT_COMMAND_RE.search(joined):
        return True
    if _NESTED_ENV_DUMP_RE.search(joined):
        return True
    for token in _command_candidates(argv):
        candidate = token.strip("'\"=,;()[]{}")
        if candidate and is_sensitive_path(candidate, workspace_root=workspace_root):
            return True
    return False


def _contains_sensitive_content(content: object | None, workspace_root: Path | None) -> bool:
    if content is None:
        return False
    raw = str(content)
    if not raw:
        return False
    canonical = html.unescape(raw)
    if _redact_credentials(canonical) != canonical:
        return True
    if _SENSITIVE_COMMAND_RE.search(canonical) or _SECRET_ASSIGNMENT_COMMAND_RE.search(
        canonical
    ):
        return True
    return False


def is_sensitive_execution(
    path: str | Path | None,
    command: Sequence[str] | str | None,
    *,
    content: object | None = None,
    workspace_root: Path | None = None,
) -> bool:
    """Classify all execution inputs; existing two-argument callers remain valid."""

    return (
        is_sensitive_path(path, workspace_root=workspace_root)
        or is_sensitive_command(command, workspace_root=workspace_root)
        or _contains_sensitive_content(content, workspace_root)
    )


def is_safe_workspace_path(path: str | Path, workspace_root: Path) -> bool:
    """Validate lexical containment and reject every existing symlink component."""

    return not is_sensitive_path(path, workspace_root=workspace_root)


def classify_tool_failure(result: ToolResult) -> str:
    """Map an untrusted tool error to a safe, useful reason."""

    if result.status == "cancelled":
        return "tool execution cancelled"
    err = (result.error or "").strip()
    if not err:
        return "tool execution failed"

    # Extract meaningful diagnostic line if a multiline traceback was supplied
    clean_err = err
    if "traceback" in err.casefold() or "\n" in err:
        lines = [line.strip() for line in err.splitlines() if line.strip()]
        for line in reversed(lines):
            if any(k in line for k in ("Error:", "Exception:", "Permission denied", "denied", "forbidden", "unsafe", "blocked")):
                clean_err = line
                break
        else:
            clean_err = lines[-1] if lines else err

    lowered = clean_err.casefold()
    if any(k in lowered for k in (".env", ".key", ".pem", "password=", "secret=", "bearer ", "synthetic-password")):
        return "tool execution failed"
    if "execution_unknown" in lowered:
        return clean_err or "EXECUTION_UNKNOWN: tool execution state unconfirmed"
    if "timeout" in lowered or "timed out" in lowered:
        return "tool execution timed out"
    if "quota" in lowered:
        return "workspace quota exceeded"
    if "sandbox" in lowered or "unsafe" in lowered or "blocked" in lowered or "permission" in lowered or "denied" in lowered:
        if "traceback" in lowered or "stack trace" in lowered:
            return "tool execution blocked by sandbox"
        return clean_err[:200]
    if not any(k in lowered for k in ("traceback", "\n  file \"", "stack trace", "secret")):
        return clean_err[:200]
    return "tool execution failed"


def project_tool_result(
    result: ToolResult,
    *,
    path: str | Path | None,
    command: Sequence[str] | str | None,
    content: object | None = None,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    """Build the only ToolResult shape allowed in durable JSON columns."""

    sensitive = is_sensitive_execution(
        path,
        command,
        content=content,
        workspace_root=workspace_root,
    )
    projection: dict[str, Any] = {
        "ok": bool(result.ok) and not sensitive,
        "status": result.status,
        "blocked": sensitive,
        "text_omitted": sensitive or not result.ok,
    }
    if sensitive:
        projection["failure_reason"] = "result withheld by safety policy"
        return projection
    if not result.ok:
        projection["failure_reason"] = classify_tool_failure(result)
        return projection

    is_read_tool = (
        isinstance(result.data, dict)
        and "content" in result.data
        and result.data.get("tool_name") == "workspace.read_text"
    )
    output = result.data.get("output") if isinstance(result.data, dict) else None
    preview = sanitize_result_text(output, preserve_newlines=is_read_tool)
    if preview is not None:
        projection["stdout_preview"] = preview
        projection["text_omitted"] = False
    if isinstance(result.data, dict):
        for field in ("path", "tool_name", "creator_tool"):
            val = result.data.get(field)
            if isinstance(val, str) and val:
                sv = sanitize_result_text(val, escape_html=False)
                if sv is not None:
                    projection[field] = sv
    return projection


#: Raw exception / stack wording is NEVER a user-facing failure reason: it
#: leaks internals and is useless to the owner.
_EXCEPTION_TEXT_RE = re.compile(
    r"(?i)\b(?:[A-Za-z_][A-Za-z0-9_]*\.)?[A-Za-z_][A-Za-z0-9_]*"
    r"(?:Error|Exception|Fail(?:ed|ure))\b"
    r"|traceback|stack trace|file \"|raise "
)

#: Fail-closed security internals that must never reach the owner chat verbatim.
_INTERNAL_REASON_MARKERS: tuple[str, ...] = (
    "fencing token",
    "workspace fence",
    "ownership fence",
    "ownership",
    "fail-closed",
    "fail closed",
    "denied_stale_fence",
    "stale fence",
    "deny-all",
    "write permit",
    "owner lease",
)

#: Neutral replacement shown when the real reason is internal security wording.
_INTERNAL_REASON_REPLACEMENT = "операция отклонена защитой рабочей области"


def public_failure_reason(
    value: object | None,
    *,
    max_length: int = MAX_FAILURE_REASON,
) -> str:
    """Bounded, sanitized, actionable failure reason safe for the owner chat.

    Honest (the real reason is surfaced, never fabricated success) but never
    leaks raw exception/stack text or internal ownership/fencing wording:

    * raw exception / traceback text -> ``""`` (caller keeps its generic text);
    * internal ownership/fencing wording -> a neutral boundary message;
    * anything else -> the redacted, bounded reason.
    """
    if value is None:
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    if _EXCEPTION_TEXT_RE.search(raw):
        return ""
    reason = sanitize_failure_reason(raw)
    if not reason:
        return ""
    lowered = reason.casefold()
    if any(marker in lowered for marker in _INTERNAL_REASON_MARKERS):
        return _INTERNAL_REASON_REPLACEMENT
    return reason[:max_length]


def sanitize_failure_reason(value: object | None) -> str | None:
    """Bound a terminal reason while refusing traceback/exception dumps."""

    if value is None:
        return None
    raw = str(value).strip()
    lowered = raw.casefold()
    if not raw:
        return None
    if "traceback" in lowered or "\n  file \"" in lowered or "stack trace" in lowered:
        return "flow failed"
    return sanitize_result_text(raw, max_length=MAX_FAILURE_REASON)
