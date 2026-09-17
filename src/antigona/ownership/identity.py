"""INV-02 — Stable Repository Identity (``repo_uuid``).

A repository is identified by a stable, durable ``repo_uuid`` — NOT by its
filesystem path.  ``repo_uuid`` satisfies the WORKSPACE OWNERSHIP v2 contract
(wave-wo2-20260821_051217, ``02_REQUIREMENTS.md`` INV-02 / FR-1):

  * **Path-independent** — moving / renaming the directory does not change the
    uuid (T-I5); a path is never used as identity.
  * **Stable across restarts** — the uuid is persisted in a workspace-owned
    marker and reused on later invocations (T-I1).
  * **Stable across clones** — a second clone of the same logical repository
    (same normalized git origin URL) resolves to the SAME uuid (T-I2).
  * **Different repos differ** — a different git origin URL yields a different
    uuid (T-I3).
  * **Copy-dir safe** — a directory copy (same origin, different path, marker
    copied along) is *not* silently returned as a second active authority: it
    resolves to the same logical uuid but is explicitly flagged
    ``authoritative=False`` (T-I4).

Identity is minted deterministically from the canonical git origin URL when the
workspace is a git repository (``uuid5`` over a fixed namespace) so every clone
of the same remote converges on one uuid.  For a non-git directory a random
``uuid4`` is minted and persisted instead.  The minted uuid + a bookkeeping
record (git origin, git HEAD, canonical mint path) is persisted under
``<workspace>/.antigona-ownership/repo_uuid.json``.

Copy-dir safety semantic
------------------------
A ``cp -r`` of a workspace carries both ``.git`` and the ownership marker, so a
pure content comparison cannot distinguish a copy from a rename.  We therefore
record the canonical (resolved) path at mint time in the marker.  When the
identity is reused (marker's git origin still matches the current origin) but
the current resolved path differs from the recorded mint path, the directory is
a relocated/duplicated working tree and is flagged ``authoritative=False``.
This is a *local authority hint only* — true mutual exclusion is enforced by the
central authority (phase 3+), which serializes ownership per ``repo_uuid``
(INV-01 / INV-08).  The uuid itself never depends on the path.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: Sub-directory (under the workspace root) that owns the identity marker.
OWNERSHIP_DIR = ".antigona-ownership"
#: Marker filename holding the persisted identity record.
REPO_UUID_MARKER = "repo_uuid.json"

#: Fixed namespace so ``uuid5`` is reproducible across machines/processes.
REPO_UUID_NAMESPACE = uuid.UUID("ed1a6b5c-2f4a-4b7e-9d0c-5f6a8c9e1b3a")

#: Sentinel returned when a workspace has no git identity.
_NO_ORIGIN: str | None = None


class OwnershipIdentityError(Exception):
    """Raised when repository identity cannot be resolved safely (fail-closed)."""


@dataclass(frozen=True)
class RepoIdentity:
    """Stable identity of a logical repository, plus a local authority hint.

    ``repo_uuid`` is the durable identity (never path-derived).  ``authoritative``
    is a LOCAL hint used for copy-dir safety; real ownership is granted only by
    the central authority in later phases.
    """

    repo_uuid: str
    authoritative: bool
    identity_source: str
    git_origin: str | None
    marker_path: Path | None


@dataclass(frozen=True)
class _Marker:
    """Persisted identity record under ``<workspace>/.antigona-ownership``."""

    repo_uuid: str
    git_origin: str | None
    git_head: str | None
    mint_path: str | None
    minted_at: str


def repo_uuid_for_workspace(workspace_root: Path) -> str:
    """Return the stable ``repo_uuid`` for ``workspace_root`` (identity only)."""
    return resolve_repo_identity(workspace_root).repo_uuid


def resolve_repo_identity(workspace_root: Path) -> RepoIdentity:
    """Resolve (and if necessary mint) the stable identity for a workspace.

    Fail-closed (INV-06): if the marker exists but cannot be read it is treated
    as absent and a fresh identity is minted; a corrupt marker is never trusted.
    """
    root = Path(workspace_root).resolve()
    if not root.is_dir():
        raise OwnershipIdentityError(f"workspace root is not a directory: {root}")

    marker_path = root / OWNERSHIP_DIR / REPO_UUID_MARKER
    current_origin = _git_origin(root)
    current_head = _git_head(root)
    marker = _read_marker(marker_path)

    if marker is not None and marker.git_origin == current_origin:
        # Same logical repository continuing (restart / rename / copy).
        repo_uuid = marker.repo_uuid
        source = "persisted"
    else:
        # Fresh mint: deterministic from git origin (clones converge), else random.
        if current_origin is not None:
            repo_uuid = str(uuid.uuid5(REPO_UUID_NAMESPACE, _normalize_origin(current_origin)))
        else:
            repo_uuid = str(uuid.uuid4())
        source = "minted"
        _write_marker(marker_path, repo_uuid, current_origin, current_head, str(root))

    authoritative = _is_authoritative(marker, source, str(root))
    return RepoIdentity(
        repo_uuid=repo_uuid,
        authoritative=authoritative,
        identity_source=source,
        git_origin=current_origin,
        marker_path=marker_path,
    )


def _is_authoritative(marker: _Marker | None, source: str, current_path: str) -> bool:
    """Local copy-dir safety hint.

    A freshly-minted marker belongs to this working tree -> authoritative.  A
    reused marker is authoritative only if the current path is the same one at
    which the marker was originally minted; otherwise the directory is a
    relocated/duplicated tree and is explicitly non-authoritative.
    """
    if source == "minted":
        return True
    if marker is None or marker.mint_path is None:
        return False
    return marker.mint_path == current_path


# ── git helpers ─────────────────────────────────────────────────────────────


def _git_origin(root: Path) -> str | None:
    """Return the canonical origin URL of ``root``, or None.

    The remote *name* is not part of repo identity — only the URL is.  We
    therefore read ``remote.origin.url`` when present and otherwise fall back to
    the first configured remote's URL, so a clone whose remote happens to be
    named ``upstream``/``origin2``/etc. (same URL as another clone's ``origin``)
    still converges to the same canonical origin.
    """
    raw = _git_config(root, "remote.origin.url")
    if raw is None or raw == "":
        raw = _first_remote_url(root)
    if raw is None or raw == "":
        return None
    return _normalize_origin(raw)


def _first_remote_url(root: Path) -> str | None:
    """Return the URL of the first configured remote, or None if none."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "remote"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    for name in proc.stdout.splitlines():
        name = name.strip()
        if not name:
            continue
        url = _git_config(root, f"remote.{name}.url")
        if url:
            return url
    return None


def _git_head(root: Path) -> str | None:
    """Return the current HEAD commit sha of ``root`` (best-effort), or None."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    sha = proc.stdout.strip()
    return sha or None


def _git_config(root: Path, key: str) -> str | None:
    """Run ``git config --get <key>``; return value or None if unset/absent."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "config", "--get", key],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    value = proc.stdout.strip()
    return value or None


#: regex for the scp-like form ``[user@]host:path`` (no ``scheme://``).
_SCP_LIKE_RE = re.compile(r"^(?:(?P<user>[^@/]+)@)?(?P<host>[^:/]+):(?P<path>.+)$")

#: well-known default ports for common git schemes; stripped when present so an
#: explicit default port never splits two otherwise-identical endpoints.
_DEFAULT_PORTS: dict[str, int] = {
    "ssh": 22,
    "https": 443,
    "http": 80,
    "git": 9418,
    "ftp": 21,
}


def _normalize_origin(url: str) -> str:
    """Canonicalize a git remote URL so equivalent remotes compare equal.

    Transport is NOT identity: host + path is.  Every equivalent transport form
    of the same logical remote converges to one canonical ``host[:port]/path``
    string (and therefore one ``repo_uuid``):

      * scp-like ``git@github.com:org/repo.git``  ->  ``github.com/org/repo``
      * ``ssh://git@github.com/org/repo.git``    ->  ``github.com/org/repo``
      * ``git://github.com/org/repo.git``        ->  ``github.com/org/repo``
      * ``https://github.com/org/repo.git``      ->  ``github.com/org/repo``
      * trailing ``/`` and trailing ``.git`` are stripped
      * the ``user@`` part (if any) is dropped — not part of repo identity
      * scheme and host are lower-cased (hosts are case-insensitive); the path
        is preserved as-is (case-sensitive on most hosts)

    Explicit *non-default* ports are kept (they are part of the endpoint);
    default ports (22 for ssh, 443 for https, 9418 for git, ...) are stripped so
    an explicit default never splits identical endpoints.
    """
    url = url.strip()
    if not url:
        return url

    host: str
    port: int | None
    path: str

    if "://" not in url:
        # scp-like form: ``[user@]host:path`` with no scheme.
        m = _SCP_LIKE_RE.match(url)
        if m is not None:
            host = m.group("host").lower()
            port = _DEFAULT_PORTS["ssh"]  # scp-like implies ssh
            path = m.group("path")
            return _canonical_origin(host, port, path)
        # not a recognizable remote -> leave as an opaque anchor
        return url

    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    try:
        port = parts.port  # None if no port; ValueError on a malformed port
    except ValueError:
        port = None
    host = (parts.hostname or "").lower()
    path = parts.path
    return _canonical_origin(host, port, path)


def _canonical_origin(host: str, port: int | None, path: str) -> str:
    """Build the canonical ``host[:port]/path`` key from parsed components."""
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not path.startswith("/"):
        path = "/" + path
    if not host:
        return path or "/"
    if port is not None and _is_default_port(port, host_ports=(22, 443, 9418, 80, 21)):
        port = None
    port_part = f":{port}" if port is not None else ""
    return f"{host}{port_part}{path}"


def _is_default_port(port: int, host_ports: tuple[int, ...]) -> bool:
    """True if ``port`` is a well-known default git port (safe to strip)."""
    return port in host_ports


# ── marker persistence ──────────────────────────────────────────────────────


def _marker_dir(marker_path: Path) -> Path:
    return marker_path.parent


def _read_marker(marker_path: Path) -> _Marker | None:
    """Read the persisted marker; return None if absent or unreadable."""
    if not marker_path.is_file():
        return None
    try:
        data: dict[str, Any] = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # corrupt/unreadable -> treat as absent
        logger.warning(
            "ownership: unreadable identity marker %s (%s); will re-mint", marker_path, exc
        )
        return None
    return _Marker(
        repo_uuid=str(data.get("repo_uuid", "")),
        git_origin=data.get("git_origin"),
        git_head=data.get("git_head"),
        mint_path=data.get("mint_path"),
        minted_at=str(data.get("minted_at", "")),
    )


def _write_marker(
    marker_path: Path,
    repo_uuid: str,
    git_origin: str | None,
    git_head: str | None,
    mint_path: str,
) -> None:
    """Durably persist the identity marker (create the dir atomically enough)."""
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "repo_uuid": repo_uuid,
        "git_origin": git_origin,
        "git_head": git_head,
        "mint_path": mint_path,
        "minted_at": datetime.now(UTC).isoformat(),
    }
    marker_path.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
