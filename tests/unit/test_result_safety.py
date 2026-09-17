from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

from antigona.contracts import ToolResult
from antigona.core.paths import home_dir
from antigona.result_safety import (
    MAX_RESULT_TEXT,
    is_safe_workspace_path,
    is_sensitive_command,
    is_sensitive_execution,
    is_sensitive_path,
    project_tool_result,
    sanitize_result_text,
)

# Derived from the canonical home helper instead of a hardcoded owner path.
_HOME = str(home_dir())


def test_sanitize_result_text_redacts_common_secret_forms_and_bounds() -> None:
    source = (
        "Authorization: Bearer synthetic-token-value\n"
        "OPENAI_API_KEY=synthetic-api-key\n"
        'password: "synthetic-password"\n'
        + ("x" * (MAX_RESULT_TEXT * 2))
    )

    sanitized = sanitize_result_text(source)

    assert sanitized is not None
    assert len(sanitized) <= MAX_RESULT_TEXT
    assert "synthetic-token-value" not in sanitized
    assert "synthetic-api-key" not in sanitized
    assert "synthetic-password" not in sanitized
    assert sanitized.count("[REDACTED]") >= 3


def test_sanitizer_preserves_pwd_command_not_found_diagnostic() -> None:
    assert sanitize_result_text("/bin/sh: 1: Pwd: not found <diagnostic>") == (
        "/bin/sh: 1: Pwd: not found &lt;diagnostic&gt;"
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("password: hunter2", "password: [REDACTED]"),
        ("pwd=secretvalue", "pwd=[REDACTED]"),
        ("token: tokenvalue", "token: [REDACTED]"),
        (
            "Authorization: " + "Bearer " + "bearer-value",
            "Authorization: [REDACTED] [REDACTED]",
        ),
    ],
)
def test_sanitizer_still_redacts_credentials(source: str, expected: str) -> None:
    assert sanitize_result_text(source) == expected


def test_sensitive_paths_and_commands_fail_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert is_sensitive_path(".env")
    assert is_sensitive_path("config/secrets/token.txt")
    assert is_sensitive_path("reports/keys.json")
    assert is_sensitive_path("config/credentials.backup")
    assert is_sensitive_path(f"{_HOME}/private.txt")
    assert is_sensitive_command(("cat", ".env"))
    assert is_sensitive_command(("printenv",))
    assert is_sensitive_command(("bash", "-lc", "env"))
    assert is_sensitive_command(("bash", "-lc", f"cat {_HOME}/private.txt"))
    assert is_sensitive_command(("bash", "-lc", "API_KEY=synthetic command"))

    assert not is_sensitive_path("reports/count.txt")
    assert not is_sensitive_command(("wc", "-c", "workspace/file.txt"))
    assert is_safe_workspace_path("reports/count.txt", workspace)
    assert not is_safe_workspace_path("../outside.txt", workspace)
    assert not is_safe_workspace_path("credentials/token.txt", workspace)


def test_sanitizer_omits_tracebacks_and_private_key_material() -> None:
    traceback = 'Traceback (most recent call last):\n  File "worker.py", line 1\nboom'
    private_key = (
        "-----BEGIN PRIVATE KEY-----\n"
        "synthetic-private-key-material\n"
        "-----END PRIVATE KEY-----"
    )

    diagnostic = sanitize_result_text(traceback)
    key_result = sanitize_result_text(private_key)

    assert diagnostic == "[diagnostic output omitted]"
    assert key_result is not None
    assert "synthetic-private-key-material" not in key_result
    assert "[REDACTED]" in key_result


def test_tool_result_projection_never_persists_failed_stderr_or_sensitive_text() -> None:
    raw_failure = ToolResult(
        False,
        "failed",
        data={"output": "Traceback\npassword=synthetic-password"},
        error="Traceback: arbitrary stderr with synthetic-password",
    )
    failed = project_tool_result(raw_failure, path="reports/out.txt", command=("false",))
    serialized = repr(failed)

    assert failed["failure_reason"] == "tool execution failed"
    assert "stdout_preview" not in failed
    assert "synthetic-password" not in serialized
    assert "Traceback" not in serialized

    raw_success = ToolResult(
        True,
        "completed",
        data={"output": "Bearer synthetic-token-value"},
    )
    sensitive = project_tool_result(raw_success, path=".env", command=("cat", ".env"))
    assert sensitive["blocked"] is True
    assert sensitive["text_omitted"] is True
    assert "stdout_preview" not in sensitive


def test_sanitizer_redacts_quoted_bearer_provider_jwt_telegram_aws_and_env_dump() -> None:
    synthetic_values = [
        "synthetic bearer marker with spaces",
        "synthetic escaped bearer marker",
        "hf_" + "SYNTHETIC_PROVIDER_MARKER_123456789",
        "sk-" + "ant-SYNTHETIC_PROVIDER_MARKER_123456789",
        "github_pat_SYNTHETIC_PROVIDER_MARKER_123456789",
        "glpat-" + "SYNTHETIC_PROVIDER_MARKER_123456789",
        "npm_SYNTHETIC_PROVIDER_MARKER_123456789",
        "AIza" + "SYNTHETIC_PROVIDER_MARKER_123456789",
        "eyJzeW50aGV0aWM.c2FtcGxlbWFya2Vy.c2lnbmF0dXJl",
        "123456789:" + "SYNTHETIC_BOT_MARKER_1234567890",
        f"AKIA{'S' * 16}",
        "synthetic-assignment-marker-one",
        "synthetic_assignment_marker_two",
        "synthetic-db-password-marker",
    ]
    source = "\n".join(
        [
            f'Authorization: Bearer "{synthetic_values[0]}"',
            rf'Authorization: Bearer \"{synthetic_values[1]}\"',
            *synthetic_values[2:11],
            f'API_KEY="{synthetic_values[11]}"',
            f"export ACCESS_TOKEN={synthetic_values[12]}",
            f'"dbPassword": "{synthetic_values[13]}"',
        ]
    )

    sanitized = sanitize_result_text(source, max_length=20_000)

    assert sanitized is not None
    assert sanitized.count("[REDACTED]") >= len(synthetic_values)
    for synthetic in synthetic_values:
        assert synthetic not in sanitized


def test_sanitizer_redacts_complete_url_userinfo_with_multiple_and_encoded_at() -> None:
    source = (
        "https://synthetic-user:synthetic-pa@ss@example.invalid/path "
        "https://user%40name:pass%40word@example.invalid/other"
    )

    sanitized = sanitize_result_text(source)

    assert sanitized is not None
    assert "synthetic-user" not in sanitized
    assert "synthetic-pa" not in sanitized
    assert "user%40name" not in sanitized
    assert "pass%40word" not in sanitized
    assert sanitized.count("[REDACTED]@example.invalid") == 2


def test_sanitizer_removes_all_control_and_format_characters_and_escapes_html() -> None:
    source = (
        '<b onclick="synthetic-handler">safe</b>'
        "\x00\x85\u200b\u202e\u2066&#x202e;&#x200b;"
    )

    sanitized = sanitize_result_text(source)

    assert sanitized is not None
    assert "<b" not in sanitized
    assert "</b>" not in sanitized
    assert "&lt;b" in sanitized
    assert "safe" in sanitized
    assert not any(unicodedata.category(char) in {"Cc", "Cf"} for char in sanitized)


def test_sanitizer_redacts_complete_stream_before_its_length_cap() -> None:
    provider_token = "hf_" + "SYNTHETIC_BOUNDARY_MARKER_123456789"
    source = ("x" * 67) + provider_token

    sanitized = sanitize_result_text(source, max_length=72)

    assert sanitized is not None
    assert len(sanitized) <= 72
    assert provider_token not in sanitized
    assert "hf_" not in sanitized


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "/tmp/output.txt",
        "../output.txt",
        "nested/../output.txt",
        ".",
        "nested/./output.txt",
        ".envrc",
        "config/.environment",
        ".token",
        "config/.credentials",
        r"\\server\share\output.txt",
        "//server/share/output.txt",
        "C:relative.txt",
        r"C:\absolute\output.txt",
        "~/output.txt",
    ],
)
def test_sensitive_path_rejects_every_lexical_escape_and_hidden_credential(
    unsafe_path: str,
) -> None:
    assert is_sensitive_path(unsafe_path)


def test_sensitive_execution_includes_content_and_live_symlink_namespace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "linked").symlink_to(outside, target_is_directory=True)

    assert is_sensitive_execution(
        "reports/output.txt",
        ("printf", "safe"),
        content="API_KEY=synthetic-content-marker",
        workspace_root=workspace,
    )
    assert is_sensitive_execution(
        "linked/output.txt",
        ("printf", "safe"),
        workspace_root=workspace,
    )
    assert not is_safe_workspace_path("linked/output.txt", workspace)


def test_success_projection_whitelists_output_and_drops_stderr_traceback_and_args() -> None:
    marker = "SYNTHETIC_EXCEPTION_MARKER"
    raw = ToolResult(
        True,
        "completed",
        data={
            "output": "safe result",
            "stderr": marker,
            "traceback": marker,
            "args": [marker],
        },
    )

    projected = project_tool_result(raw, path="reports/out.txt", command=("printf", "safe"))

    assert projected["stdout_preview"] == "safe result"
    assert marker not in repr(projected)
    assert set(projected) == {"ok", "status", "blocked", "text_omitted", "stdout_preview"}


def test_sanitizer_preserves_newlines_only_when_requested() -> None:
    source = "line 1\nline 2\r\nline 3\ttabbed\x00\x85"
    assert sanitize_result_text(source) == "line 1line 2line 3tabbed"
    assert sanitize_result_text(source, preserve_newlines=True) == "line 1\nline 2\nline 3\ttabbed"


def test_project_tool_result_preserves_newlines_for_read_tool() -> None:
    read_raw = ToolResult(
        True,
        "completed",
        data={
            "output": "ANTIGONA FILE TEST\n12345\n",
            "content": "ANTIGONA FILE TEST\n12345\n",
            "path": "/workspace/notes.txt",
            "tool_name": "workspace.read_text",
        },
    )
    projected_read = project_tool_result(read_raw, path="notes.txt", command=None)
    assert projected_read["stdout_preview"] == "ANTIGONA FILE TEST\n12345\n"

    shell_raw = ToolResult(
        True,
        "completed",
        data={
            "output": "edge-tts installed\n",
        },
    )
    projected_shell = project_tool_result(shell_raw, path=None, command=("pip", "install", "edge-tts"))
    assert projected_shell["stdout_preview"] == "edge-tts installed"


def test_classify_tool_failure_surfaces_permission_error() -> None:
    from antigona.result_safety import classify_tool_failure

    res = ToolResult(
        False,
        "failed",
        error="sandbox blocked: PermissionError: [Errno 13] Permission denied: 'exam/probe/docker_write.txt'",
    )
    classified = classify_tool_failure(res)
    assert "PermissionError" in classified
    assert "Permission denied" in classified
    assert classified != "tool execution blocked by sandbox"


def test_classify_tool_failure_extracts_traceback_exception() -> None:
    from antigona.result_safety import classify_tool_failure

    tb = (
        "Traceback (most recent call last):\n"
        '  File "<string>", line 2, in <module>\n'
        "PermissionError: [Errno 13] Permission denied: 'nested/target.txt'"
    )
    res = ToolResult(False, "failed", error=tb)
    classified = classify_tool_failure(res)
    assert "PermissionError: [Errno 13] Permission denied: 'nested/target.txt'" in classified
    assert "Traceback" not in classified

