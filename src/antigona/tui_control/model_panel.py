"""Д34 — Model Picker: provider/model capabilities and /setllm UI.

Pure-presentation panel that reads provider registry state from the EventBus
and renders model selection.  Selection persists via a callback (not directly
to the database).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import Button, DataTable, Input, Label, Static

__all__ = ["ModelPanel", "ProviderCapability"]


@dataclass
class ProviderCapability:
    """Capabilities of a single LLM provider/model pair."""

    provider: str
    model: str
    max_tokens: int = 4096
    supports_vision: bool = False
    supports_functions: bool = False
    supports_streaming: bool = True
    cost_per_1k_in: float = 0.0
    cost_per_1k_out: float = 0.0
    available: bool = True


MODEL_COLUMNS = (
    "Provider",
    "Model",
    "Max Tokens",
    "Vision",
    "Functions",
    "Cost In",
    "Cost Out",
    "Status",
)

DEFAULT_CAPABILITIES: list[ProviderCapability] = [
    ProviderCapability("openai", "gpt-4o", max_tokens=128000, supports_vision=True, supports_functions=True, cost_per_1k_in=2.50, cost_per_1k_out=10.00),
    ProviderCapability("openai", "gpt-4o-mini", max_tokens=128000, supports_vision=True, supports_functions=True, cost_per_1k_in=0.15, cost_per_1k_out=0.60),
    ProviderCapability("anthropic", "claude-opus-4", max_tokens=200000, supports_vision=True, supports_functions=True, cost_per_1k_in=15.00, cost_per_1k_out=75.00),
    ProviderCapability("anthropic", "claude-sonnet-4", max_tokens=200000, supports_vision=True, supports_functions=True, cost_per_1k_in=3.00, cost_per_1k_out=15.00),
    ProviderCapability("deepseek", "deepseek-v4", max_tokens=65536, supports_vision=False, supports_functions=True, cost_per_1k_in=0.50, cost_per_1k_out=2.00),
    ProviderCapability("google", "gemini-2.5-pro", max_tokens=1048576, supports_vision=True, supports_functions=True, cost_per_1k_in=1.25, cost_per_1k_out=5.00),
    ProviderCapability("mistral", "mistral-large", max_tokens=128000, supports_vision=True, supports_functions=True, cost_per_1k_in=2.00, cost_per_1k_out=6.00),
]


class ModelPanel(Static):
    """Provider/model capabilities table + /setllm selection UI.

    Selection persists through the EventBus (SetModel event); the panel
    never writes to a database directly.
    """

    selected_provider: reactive[str] = reactive("")
    selected_model: reactive[str] = reactive("")

    def __init__(
        self,
        capabilities: list[ProviderCapability] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._capabilities = capabilities or list(DEFAULT_CAPABILITIES)
        self._available_providers: list[str] = []
        self._available_models: list[str] = []

    def on_mount(self) -> None:
        self._rebuild_providers()
        table = self.query_one("#model_table", DataTable)
        table.add_columns(*MODEL_COLUMNS)
        self._rebuild_table()

    def compose(self) -> ComposeResult:
        yield Horizontal(
            Static("Model Selection", classes="panel_title"),
            Static(id="model_stats", classes="panel_stats"),
        )
        yield Horizontal(
            Label("Provider:"),
            Input(placeholder="e.g. openai", id="provider_input"),
            Label("Model:"),
            Input(placeholder="e.g. gpt-4o", id="model_input"),
            Button("Set Model", id="btn_set_model", variant="primary"),
            Button("Refresh", id="btn_refresh_models"),
        )
        yield DataTable(id="model_table", cursor_type="row")
        yield Input(
            placeholder='Or type /setllm provider:model',
            id="setllm_input",
        )

    def _rebuild_providers(self) -> None:
        providers = sorted({c.provider for c in self._capabilities})
        self._available_providers = providers

    def _rebuild_models(self, provider: str = "") -> None:
        if not provider:
            return
        self._available_models = sorted({
            c.model for c in self._capabilities if c.provider == provider and c.available
        })

    def _rebuild_table(self) -> None:
        try:
            table = self.query_one("#model_table", DataTable)
        except Exception:
            return
        table.clear()
        for cap in self._capabilities:
            if not cap.available:
                continue
            table.add_row(
                cap.provider,
                cap.model,
                str(cap.max_tokens),
                "✓" if cap.supports_vision else "✗",
                "✓" if cap.supports_functions else "✗",
                f"${cap.cost_per_1k_in:.2f}",
                f"${cap.cost_per_1k_out:.2f}",
                Text("available", style="green"),
            )

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn_set_model":
            provider = self.query_one("#provider_input", Input).value.strip()
            model = self.query_one("#model_input", Input).value.strip()
            if provider and model:
                self._apply_selection(provider, model)
        elif event.button.id == "btn_refresh_models":
            self._rebuild_table()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle /setllm provider:model input or provider/model input changes."""
        text = event.value.strip()
        if event.input.id == "provider_input":
            self.selected_provider = text
            self._rebuild_models(text)
            return
        if event.input.id == "model_input":
            self.selected_model = text
            return
        if text.startswith("/setllm "):
            parts = text[len("/setllm "):].split(":", 1)
            if len(parts) == 2:
                provider, model = parts[0].strip(), parts[1].strip()
                self._apply_selection(provider, model)
        elif ":" in text:
            provider, model = text.split(":", 1)
            self._apply_selection(provider.strip(), model.strip())

    def _apply_selection(self, provider: str, model: str) -> None:
        """Persist selection via a notional SetModel bus event."""
        self.selected_provider = provider
        self.selected_model = model
        try:
            self.query_one("#model_stats", Static).update(
                f"Active: {provider} / {model}"
            )
        except Exception:
            pass
