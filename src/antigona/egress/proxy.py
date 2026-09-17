from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from antigona.worker.tools.common import ToolError

if TYPE_CHECKING:
    from antigona.config import Settings


class EgressUnavailableError(ToolError):
    """Raised when egress proxy is unavailable, unreachable, or blocks a request."""

    pass


@dataclass
class Allowlist:
    exact_domains: set[str] = field(default_factory=set)
    suffix_domains: set[str] = field(default_factory=set)
    deny_by_default: bool = True

    @classmethod
    def from_list(cls, entries: list[str], deny_by_default: bool = True) -> Allowlist:
        exact: set[str] = set()
        suffix: set[str] = set()
        for entry in entries:
            cleaned = entry.strip().lower()
            if not cleaned:
                continue
            if cleaned.startswith("*."):
                domain_part = cleaned[2:]
                if domain_part:
                    suffix.add(domain_part)
            else:
                exact.add(cleaned)
        return cls(exact_domains=exact, suffix_domains=suffix, deny_by_default=deny_by_default)

    @classmethod
    def from_file(cls, path: Path | str, deny_by_default: bool = True) -> Allowlist:
        file_path = Path(path)
        if not file_path.exists():
            return cls(deny_by_default=deny_by_default)
        lines = file_path.read_text(encoding="utf-8").splitlines()
        entries = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
        return cls.from_list(entries, deny_by_default=deny_by_default)

    def contains(self, host: str) -> bool:
        if not host:
            return False
        cleaned_host = host.strip().lower()
        if ":" in cleaned_host and not cleaned_host.endswith("]"):
            cleaned_host = cleaned_host.split(":")[0]

        if cleaned_host in self.exact_domains:
            return True

        for suf in self.suffix_domains:
            if cleaned_host == suf or cleaned_host.endswith("." + suf):
                return True

        return False


class EgressProxy:
    def __init__(
        self,
        allowlist: Allowlist | None = None,
        proxy_url: str | None = None,
        timeout_seconds: int = 10,
        deny_by_default: bool = True,
    ) -> None:
        self.allowlist = allowlist if allowlist is not None else Allowlist(deny_by_default=deny_by_default)
        self.proxy_url = proxy_url
        self.timeout_seconds = timeout_seconds
        self.deny_by_default = deny_by_default

    @classmethod
    def from_settings(cls, settings: Settings) -> EgressProxy:
        if not settings.egress_enabled:
            return cls(
                allowlist=Allowlist(deny_by_default=True),
                proxy_url=settings.egress_proxy_url,
                timeout_seconds=settings.egress_timeout_seconds,
                deny_by_default=True,
            )

        if settings.egress_allowlist_file:
            allowlist = Allowlist.from_file(
                settings.egress_allowlist_file,
                deny_by_default=settings.egress_deny_by_default,
            )
            if settings.egress_allowlist:
                other = Allowlist.from_list(
                    settings.egress_allowlist,
                    deny_by_default=settings.egress_deny_by_default,
                )
                allowlist.exact_domains.update(other.exact_domains)
                allowlist.suffix_domains.update(other.suffix_domains)
        else:
            allowlist = Allowlist.from_list(
                settings.egress_allowlist,
                deny_by_default=settings.egress_deny_by_default,
            )

        return cls(
            allowlist=allowlist,
            proxy_url=settings.egress_proxy_url,
            timeout_seconds=settings.egress_timeout_seconds,
            deny_by_default=settings.egress_deny_by_default,
        )

    def fetch(self, url: str) -> str:
        if not url:
            raise EgressUnavailableError("url must not be empty")

        try:
            parsed = urllib.parse.urlparse(url)
        except Exception as exc:
            raise EgressUnavailableError(f"invalid url: {url}") from exc

        if parsed.scheme not in ("http", "https"):
            raise EgressUnavailableError(f"unsupported URL scheme: {parsed.scheme}")

        host = parsed.hostname
        if not host:
            raise EgressUnavailableError(f"invalid url host: {url}")

        if not self.allowlist.contains(host):
            raise EgressUnavailableError(f"domain not in allowlist: {host}")

        if self.proxy_url:
            try:
                import httpx
            except ImportError as exc:
                raise EgressUnavailableError("httpx is required when proxy_url is configured") from exc

            try:
                with httpx.Client(proxy=self.proxy_url, timeout=self.timeout_seconds) as client:
                    resp = client.get(url)
                    resp.raise_for_status()
                    return resp.text
            except (TimeoutError, OSError, httpx.HTTPError) as exc:
                raise EgressUnavailableError(f"egress proxy request failed: {exc}") from exc
        else:
            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "Antigona-EgressProxy/1.0"},
                )
                with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
                    content: bytes = response.read()
                    return content.decode("utf-8", errors="replace")
            except (TimeoutError, OSError, urllib.error.URLError) as exc:
                raise EgressUnavailableError(f"egress proxy direct request failed: {exc}") from exc
