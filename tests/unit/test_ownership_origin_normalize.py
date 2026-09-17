"""DF-WO2-001 — Origin-form normalization (transport is not identity).

Red->Green tests T-I6..T-I10: ALL equivalent transport forms of the SAME
logical remote must converge to ONE repo_uuid.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from antigona.ownership.identity import resolve_repo_identity

#: All transport-equivalent forms of the SAME logical repo (github.com/org/repo).
FORMS = [
    "git@github.com:org/repo.git",            # scp-like
    "ssh://git@github.com/org/repo.git",      # ssh URL
    "https://github.com/org/repo.git",        # https with .git
    "https://github.com/org/repo.git/",       # trailing slash
    "https://github.com/org/repo",            # no .git
    "git://github.com/org/repo.git",          # git protocol
    "ssh://github.com/org/repo.git",          # ssh, no user
    "ssh://git@github.com:22/org/repo.git",   # ssh default port 22
    "https://github.com:443/org/repo.git",    # https default port 443
]

#: A genuinely DIFFERENT repo (must NOT converge with the forms above).
OTHER = "https://github.com/other-owner/other-repo.git"


def _git_init(root: Path, remote_url: str | None, remote_name: str = "origin") -> None:
    root.mkdir()
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    if remote_url is not None:
        subprocess.run(
            ["git", "-C", str(root), "config", f"remote.{remote_name}.url", remote_url],
            check=True,
        )


def _uuid_for(url: str, tmp_path: Path, remote_name: str = "origin") -> str:
    d = tmp_path / f"{remote_name}__{abs(hash(url))}"
    _git_init(d, url, remote_name)
    return resolve_repo_identity(d).repo_uuid


def test_TI6_scp_like_and_ssh_url_converge(tmp_path: Path) -> None:
    assert _uuid_for("git@github.com:org/repo.git", tmp_path) ==            _uuid_for("ssh://git@github.com/org/repo.git", tmp_path)


def test_TI7_https_and_ssh_converge(tmp_path: Path) -> None:
    assert _uuid_for("https://github.com/org/repo.git", tmp_path) ==            _uuid_for("ssh://git@github.com/org/repo.git", tmp_path)


def test_TI8_git_suffix_and_trailing_slash_converge(tmp_path: Path) -> None:
    assert _uuid_for("https://github.com/org/repo.git", tmp_path) ==            _uuid_for("https://github.com/org/repo", tmp_path) ==            _uuid_for("https://github.com/org/repo.git/", tmp_path)


def test_TI8b_all_transport_forms_converge_to_one_uuid(tmp_path: Path) -> None:
    uuids = {_uuid_for(f, tmp_path) for f in FORMS}
    assert len(uuids) == 1, f"expected all forms to converge, got {len(uuids)} uuids"


def test_TI9_renamed_remote_alias_converges(tmp_path: Path) -> None:
    origin_uuid = _uuid_for("https://github.com/org/repo.git", tmp_path, "origin")
    assert origin_uuid == _uuid_for("https://github.com/org/repo.git", tmp_path, "upstream")
    assert origin_uuid == _uuid_for("https://github.com/org/repo.git", tmp_path, "origin2")


def test_TI10_no_origin_uuid4_not_colliding(tmp_path: Path) -> None:
    # non-git dir -> random uuid4
    plain = tmp_path / "no-git"
    plain.mkdir()
    id1 = resolve_repo_identity(plain)
    id2 = resolve_repo_identity(plain)
    assert id1.repo_uuid == id2.repo_uuid          # stable across invocations
    # a git-derived uuid for the canonical repo must differ from the random one
    git_uuid = _uuid_for("https://github.com/org/repo.git", tmp_path)
    assert id1.repo_uuid != git_uuid
    # two distinct non-git dirs get distinct random uuids (no collision)
    plain2 = tmp_path / "no-git-2"
    plain2.mkdir()
    id3 = resolve_repo_identity(plain2)
    assert id3.repo_uuid != id1.repo_uuid


def test_TI10b_different_repo_still_differs(tmp_path: Path) -> None:
    assert _uuid_for("https://github.com/org/repo.git", tmp_path) !=            _uuid_for(OTHER, tmp_path)
