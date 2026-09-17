from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .common import ToolError

if TYPE_CHECKING:
    from antigona.egress.proxy import EgressProxy


@dataclass(frozen=True)
class WebFetchResult:
    url: str
    enabled: bool
    detail: str
    untrusted: bool = True
    blocked: bool = False


class WebFetchTool:
    def __init__(self, proxy: EgressProxy | None = None) -> None:
        if proxy is None:
            from antigona.egress.proxy import EgressProxy

            self.proxy = EgressProxy()
        else:
            self.proxy = proxy

    def fetch(self, url: str) -> WebFetchResult:
        if not url:
            raise ToolError("url must not be empty")
        from antigona.egress.proxy import EgressUnavailableError

        try:
            raw = self.proxy.fetch(url)
            return WebFetchResult(
                url=url,
                enabled=True,
                detail=raw,
                untrusted=True,
                blocked=False,
            )
        except EgressUnavailableError as exc:
            return WebFetchResult(
                url=url,
                enabled=False,
                detail=f"egress blocked: {exc}",
                untrusted=True,
                blocked=True,
            )


class DisabledWebFetchTool(WebFetchTool):
    """Deprecated stub tool preserved for backward compatibility."""

    def __init__(self) -> None:
        from antigona.egress.proxy import Allowlist, EgressProxy

        disabled_proxy = EgressProxy(
            allowlist=Allowlist(deny_by_default=True),
            deny_by_default=True,
        )
        super().__init__(proxy=disabled_proxy)
