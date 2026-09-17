"""Regression coverage for owner-visible paths at the Telegram boundary."""

import pytest

from antigona.durable.operation_models import OperationState
from antigona.events.event_types import FinalResponseReady
from antigona.presentation.presenter import OperationPresenter


def _render(text: str) -> str:
    event = FinalResponseReady(text=text, terminal_state="SUCCEEDED")
    return "".join(
        OperationPresenter._render_final_chunks(event, OperationState.SUCCEEDED)
    )


def test_final_response_keeps_benign_workspace_path_visible() -> None:
    rendered = _render("/opt/antigona-home/.antigona")

    assert "/opt/antigona-home/.antigona" in rendered
    assert "<pre>…</pre>" not in rendered


@pytest.mark.parametrize(
    "sensitive_path",
    [
        "/opt/antigona-home/.antigona/.env",
        "/opt/antigona-home/.antigona/secrets/provider.json",
        "/opt/antigona-home/.antigona/vault/credentials.json",
        "/opt/antigona-home/.antigona/private.key",
    ],
)
def test_final_response_redacts_credential_bearing_paths(sensitive_path: str) -> None:
    rendered = _render(sensitive_path)

    assert sensitive_path not in rendered
    assert "…" in rendered
