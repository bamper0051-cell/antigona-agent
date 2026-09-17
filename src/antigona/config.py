from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from .core import paths
from .storage.engine import UnsupportedDatabaseURL, normalize_db_url


def load_project_env(root: Path | None = None) -> None:
    """Load project ``.env`` values without overriding the caller's environment."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    project_root = root if root is not None else paths.project_root()
    load_dotenv(dotenv_path=project_root / ".env", override=False)


@dataclass(frozen=True)
class Settings:
    database_url: str
    workspace: Path
    dev_tokens: dict[str, str] | None = None
    sandbox_backend: str = "docker"
    sandbox_runtime: str = "auto"
    docker_image: str = "python:3.12-alpine"
    lease_seconds: int = 30
    max_retries: int = 1
    tool_timeout_seconds: int = 10
    llm_timeout_seconds: int = 120
    test_mode: bool = False
    model_primary: str = "openrouter/anthropic/claude-3.5-sonnet"
    model_secondary: str = "openrouter/openai/gpt-4o-mini"
    quarantine_model: str = "none"
    workspace_backend: str = "local"
    workspace_mock: bool = True
    # DF-WO2-003: wire the ownership gate into the live path (default OFF).
    ownership_enabled: bool = False
    ownership_dir: Path | None = None
    ssh_host: str = "localhost"
    ssh_port: int = 22
    ssh_username: str = "root"
    ssh_key_path: str | None = None
    modal_app_name: str = "antigona-workspace"
    modal_environment: str = "dev"
    daytona_api_key: str | None = None
    daytona_region: str = "eu"
    daytona_workspace_id: str | None = None
    egress_enabled: bool = True
    egress_deny_by_default: bool = True
    egress_allowlist: list[str] = field(default_factory=list)
    egress_proxy_url: str | None = None
    egress_allowlist_file: str | None = None
    egress_timeout_seconds: int = 10
    microvm_vcpus: int = 1
    microvm_mem_mib: int = 256
    microvm_timeout_seconds: int = 30
    microvm_kernel_path: str | None = None
    microvm_rootfs_path: str | None = None
    e2b_api_key: str | None = None
    e2b_template: str = "base"
    # ── P4.3 durable layer ────────────────────────────────────────────────
    # New fields are appended so existing positional Settings(...) call sites
    # keep their meaning. ``db_url`` is the async form of ``database_url``
    # unless ANTIGONA_DB_URL overrides it; ``redis_url=None`` disables the
    # broker and the state cache, i.e. behaviour identical to pre-P4.3.
    db_url: str = ""
    redis_url: str | None = None
    redis_state_ttl_seconds: int = 300
    db_connect_retries: int = 5
    db_connect_backoff_seconds: float = 1.0
    cron_enabled: bool = False
    cron_tick_interval_seconds: int = 30
    # ── P5.1 delivery channels ────────────────────────────────────────────
    # Appended for the same reason as the P4.3 block: positional Settings(...)
    # call sites keep their meaning. ``delivery_mock=True`` is the safe CI
    # default (no channel is contacted), an empty ``delivery_enabled_channels``
    # means "no allowlist, every registered channel may be used", and the
    # Telegram credentials fall back to the pre-P5.1 ANTIGONA_TELEGRAM_* names.
    delivery_enabled_channels: list[str] = field(default_factory=list)
    delivery_default_channel: str = "telegram"
    delivery_result_channels: list[str] = field(default_factory=lambda: ["telegram"])
    delivery_mock: bool = True
    delivery_timeout_seconds: int = 10
    delivery_max_attempts: int = 5
    delivery_telegram_bot_token: str | None = None
    delivery_telegram_chat_id: str | None = None
    delivery_discord_webhook: str | None = None
    delivery_discord_token: str | None = None
    delivery_discord_channel_id: str | None = None
    delivery_slack_webhook: str | None = None
    delivery_slack_token: str | None = None
    delivery_slack_channel: str | None = None
    delivery_whatsapp_token: str | None = None
    delivery_whatsapp_phone_id: str | None = None
    delivery_whatsapp_to: str | None = None
    delivery_signal_url: str | None = None
    delivery_signal_from: str | None = None
    delivery_signal_to: str | None = None
    delivery_email_smtp_host: str | None = None
    delivery_email_smtp_port: int = 587
    delivery_email_user: str | None = None
    delivery_email_password: str | None = None
    delivery_email_from: str | None = None
    delivery_email_to: str | None = None
    delivery_email_use_tls: bool = True

    def __post_init__(self) -> None:
        """Reject result fanout configurations that can only fail at runtime."""

        from .delivery.factory import registered_channels

        result_channels = {
            channel.strip().lower() for channel in self.delivery_result_channels if channel.strip()
        }
        if result_channels - registered_channels():
            raise RuntimeError("ANTIGONA_DELIVERY_RESULT_CHANNELS contains an unknown channel")

        enabled_channels = {
            channel.strip().lower() for channel in self.delivery_enabled_channels if channel.strip()
        }
        disabled = result_channels - enabled_channels - {"fake"}
        if enabled_channels and disabled:
            raise RuntimeError("ANTIGONA_DELIVERY_RESULT_CHANNELS contains a disabled channel")

    def async_db_url(self) -> str:
        """Async URL for the durable layer, derived from ``database_url`` if unset."""
        return self.db_url or normalize_db_url(self.database_url)

    @classmethod
    def from_env(cls) -> Settings:
        raw = os.getenv("ANTIGONA_DEV_TOKENS", "")
        tokens = dict(item.split(":", 1) for item in raw.split(",") if ":" in item)
        backend = os.getenv("ANTIGONA_SANDBOX_BACKEND", "docker")
        runtime = os.getenv("ANTIGONA_SANDBOX_RUNTIME", "auto")
        test_mode = os.getenv("ANTIGONA_TEST_MODE") == "1"
        if backend == "inprocess" and not test_mode:
            raise RuntimeError("inprocess sandbox requires ANTIGONA_TEST_MODE=1")
        raw_channels = os.getenv("ANTIGONA_DELIVERY_CHANNELS", "")
        delivery_channels = [
            item.strip().lower() for item in raw_channels.split(",") if item.strip()
        ]
        raw_result_channels = os.getenv("ANTIGONA_DELIVERY_RESULT_CHANNELS", "telegram")
        delivery_result_channels: list[str] = []
        for item in raw_result_channels.split(","):
            clean = item.strip().lower()
            if clean and clean not in delivery_result_channels:
                delivery_result_channels.append(clean)
        raw_allowlist = os.getenv("ANTIGONA_EGRESS_ALLOWLIST", "")
        egress_allowlist = [item.strip() for item in raw_allowlist.split(",") if item.strip()]
        database_url = os.getenv("ANTIGONA_DATABASE_URL", "sqlite:///./antigona.db")
        # ANTIGONA_DB_URL wins; otherwise the legacy sync URL is rewritten onto its
        # async driver. An unknown driver is a configuration error, refused here
        # rather than surfacing as an obscure failure at first connect.
        try:
            db_url = normalize_db_url(os.getenv("ANTIGONA_DB_URL") or database_url)
        except UnsupportedDatabaseURL as exc:
            raise RuntimeError(f"invalid database url: {exc}") from exc
        settings = cls(
            database_url=database_url,
            workspace=Path(os.getenv("ANTIGONA_WORKSPACE", "./workspace")).resolve(),
            dev_tokens=tokens or None,
            sandbox_backend=backend,
            sandbox_runtime=runtime,
            docker_image=os.getenv("ANTIGONA_DOCKER_IMAGE", "python:3.12-alpine"),
            lease_seconds=int(os.getenv("ANTIGONA_LEASE_SECONDS", "30")),
            max_retries=int(os.getenv("ANTIGONA_MAX_RETRIES", "1")),
            tool_timeout_seconds=int(os.getenv("ANTIGONA_TOOL_TIMEOUT", "10")),
            llm_timeout_seconds=int(os.getenv("ANTIGONA_LLM_TIMEOUT", "120")),
            test_mode=test_mode,
            model_primary=os.getenv(
                "ANTIGONA_MODEL_PRIMARY",
                os.getenv("MODEL_PRIMARY", "openrouter/anthropic/claude-3.5-sonnet"),
            ),
            model_secondary=os.getenv(
                "ANTIGONA_MODEL_SECONDARY",
                os.getenv("MODEL_SECONDARY", "openrouter/openai/gpt-4o-mini"),
            ),
            quarantine_model=os.getenv("ANTIGONA_QUARANTINE_MODEL", "none"),
            workspace_backend=os.getenv("ANTIGONA_WORKSPACE_BACKEND", "local"),
            workspace_mock=os.getenv("ANTIGONA_WORKSPACE_MOCK", "1") in ("1", "true", "True"),
            ownership_enabled=os.getenv("ANTIGONA_OWNERSHIP_ENABLED", "0") in ("1", "true", "True"),
            ownership_dir=(
                Path(_dir) if (_dir := os.getenv("ANTIGONA_OWNERSHIP_DIR")) else None
            ),
            ssh_host=os.getenv("ANTIGONA_SSH_HOST", "localhost"),
            ssh_port=int(os.getenv("ANTIGONA_SSH_PORT", "22")),
            ssh_username=os.getenv("ANTIGONA_SSH_USERNAME", "root"),
            ssh_key_path=os.getenv("ANTIGONA_SSH_KEY_PATH"),
            modal_app_name=os.getenv("ANTIGONA_MODAL_APP_NAME", "antigona-workspace"),
            modal_environment=os.getenv("ANTIGONA_MODAL_ENVIRONMENT", "dev"),
            daytona_api_key=os.getenv("ANTIGONA_DAYTONA_API_KEY"),
            daytona_region=os.getenv("ANTIGONA_DAYTONA_REGION", "eu"),
            daytona_workspace_id=os.getenv("ANTIGONA_DAYTONA_WORKSPACE_ID"),
            egress_enabled=os.getenv("ANTIGONA_EGRESS_ENABLED", "1") not in ("0", "false", "False"),
            egress_deny_by_default=os.getenv("ANTIGONA_EGRESS_DENY_BY_DEFAULT", "1")
            not in ("0", "false", "False"),
            egress_allowlist=egress_allowlist,
            egress_proxy_url=os.getenv("ANTIGONA_EGRESS_PROXY_URL") or None,
            egress_allowlist_file=os.getenv("ANTIGONA_EGRESS_ALLOWLIST_FILE") or None,
            egress_timeout_seconds=int(
                os.getenv(
                    "ANTIGONA_EGRESS_TIMEOUT", os.getenv("ANTIGONA_EGRESS_TIMEOUT_SECONDS", "10")
                )
            ),
            microvm_vcpus=int(os.getenv("ANTIGONA_MICROVM_VCPUS", "1")),
            microvm_mem_mib=int(os.getenv("ANTIGONA_MICROVM_MEM_MIB", "256")),
            microvm_timeout_seconds=int(os.getenv("ANTIGONA_MICROVM_TIMEOUT", "30")),
            microvm_kernel_path=os.getenv("ANTIGONA_MICROVM_KERNEL") or None,
            microvm_rootfs_path=os.getenv("ANTIGONA_MICROVM_ROOTFS") or None,
            e2b_api_key=os.getenv("ANTIGONA_E2B_API_KEY") or os.getenv("E2B_API_KEY") or None,
            e2b_template=os.getenv("ANTIGONA_E2B_TEMPLATE", "base"),
            db_url=db_url,
            redis_url=os.getenv("ANTIGONA_REDIS_URL") or None,
            redis_state_ttl_seconds=int(os.getenv("ANTIGONA_REDIS_STATE_TTL", "300")),
            db_connect_retries=int(os.getenv("ANTIGONA_DB_CONNECT_RETRIES", "5")),
            db_connect_backoff_seconds=float(os.getenv("ANTIGONA_DB_CONNECT_BACKOFF", "1.0")),
            cron_enabled=os.getenv("ANTIGONA_CRON_ENABLED", "0") in ("1", "true", "True"),
            cron_tick_interval_seconds=int(os.getenv("ANTIGONA_CRON_TICK_INTERVAL", "30")),
            delivery_enabled_channels=delivery_channels,
            delivery_default_channel=os.getenv("ANTIGONA_DELIVERY_DEFAULT_CHANNEL", "telegram"),
            delivery_result_channels=delivery_result_channels,
            delivery_mock=os.getenv("ANTIGONA_DELIVERY_MOCK", "1") in ("1", "true", "True"),
            delivery_timeout_seconds=int(os.getenv("ANTIGONA_DELIVERY_TIMEOUT", "10")),
            delivery_max_attempts=int(os.getenv("ANTIGONA_DELIVERY_MAX_ATTEMPTS", "5")),
            delivery_telegram_bot_token=(
                os.getenv("ANTIGONA_DELIVERY_TELEGRAM_BOT_TOKEN")
                or os.getenv("ANTIGONA_TELEGRAM_BOT_TOKEN")
                or os.getenv("TELEGRAM_BOT_TOKEN")
                or None
            ),
            delivery_telegram_chat_id=os.getenv("ANTIGONA_DELIVERY_TELEGRAM_CHAT_ID")
            or os.getenv("ANTIGONA_TELEGRAM_CHAT_ID")
            or None,
            delivery_discord_webhook=os.getenv("ANTIGONA_DELIVERY_DISCORD_WEBHOOK") or None,
            delivery_discord_token=os.getenv("ANTIGONA_DELIVERY_DISCORD_TOKEN") or None,
            delivery_discord_channel_id=os.getenv("ANTIGONA_DELIVERY_DISCORD_CHANNEL_ID") or None,
            delivery_slack_webhook=os.getenv("ANTIGONA_DELIVERY_SLACK_WEBHOOK") or None,
            delivery_slack_token=os.getenv("ANTIGONA_DELIVERY_SLACK_TOKEN") or None,
            delivery_slack_channel=os.getenv("ANTIGONA_DELIVERY_SLACK_CHANNEL") or None,
            delivery_whatsapp_token=os.getenv("ANTIGONA_DELIVERY_WHATSAPP_TOKEN") or None,
            delivery_whatsapp_phone_id=os.getenv("ANTIGONA_DELIVERY_WHATSAPP_PHONE_ID") or None,
            delivery_whatsapp_to=os.getenv("ANTIGONA_DELIVERY_WHATSAPP_TO") or None,
            delivery_signal_url=os.getenv("ANTIGONA_DELIVERY_SIGNAL_URL") or None,
            delivery_signal_from=os.getenv("ANTIGONA_DELIVERY_SIGNAL_FROM") or None,
            delivery_signal_to=os.getenv("ANTIGONA_DELIVERY_SIGNAL_TO") or None,
            delivery_email_smtp_host=os.getenv("ANTIGONA_DELIVERY_EMAIL_SMTP_HOST") or None,
            delivery_email_smtp_port=int(os.getenv("ANTIGONA_DELIVERY_EMAIL_SMTP_PORT", "587")),
            delivery_email_user=os.getenv("ANTIGONA_DELIVERY_EMAIL_USER") or None,
            delivery_email_password=os.getenv("ANTIGONA_DELIVERY_EMAIL_PASSWORD") or None,
            delivery_email_from=os.getenv("ANTIGONA_DELIVERY_EMAIL_FROM") or None,
            delivery_email_to=os.getenv("ANTIGONA_DELIVERY_EMAIL_TO") or None,
            delivery_email_use_tls=os.getenv("ANTIGONA_DELIVERY_EMAIL_TLS", "1")
            not in ("0", "false", "False"),
        )
        if settings.model_primary == settings.model_secondary:
            raise RuntimeError("primary and verifier models must differ")
        if (
            settings.quarantine_model != "none"
            and settings.quarantine_model == settings.model_primary
        ):
            raise RuntimeError("primary and quarantine models must differ")
        if settings.delivery_timeout_seconds <= 0:
            raise RuntimeError("ANTIGONA_DELIVERY_TIMEOUT must be positive")
        if settings.delivery_max_attempts <= 0:
            raise RuntimeError("ANTIGONA_DELIVERY_MAX_ATTEMPTS must be positive")
        return settings

    def token_hashes(self) -> dict[str, str]:
        return {
            hashlib.sha256(token.encode()).hexdigest(): owner
            for token, owner in (self.dev_tokens or {}).items()
        }


def generate_dev_token() -> str:
    """Generate a token for env/config injection; generated values are never persisted."""
    return secrets.token_urlsafe(32)
