from types import SimpleNamespace

from antigona.verifier_service import _effect_context


def test_target_only_effect_is_not_labeled_as_tool_stdout() -> None:
    task = SimpleNamespace(
        tool_name="workspace.write",
        tool_arguments={"path": "file.txt"},
        steps=[
            SimpleNamespace(
                tool_name="workspace.write",
                input={"path": "file.txt"},
                output={
                    "tool_result": {
                        "ok": True,
                        "status": "completed",
                        "path": "file.txt",
                    }
                },
            )
        ],
    )

    context = _effect_context(
        task,
        artifact_path="file.txt",
        sha256="a" * 64,
        size=8,
    )

    rendered = context.render("artifact")
    assert "file.txt" not in context.recorded_stdout
    assert "Tool stdout (recorded, verbatim):" not in rendered
    assert "file.txt" in rendered
