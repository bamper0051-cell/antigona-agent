"""Drift guard tests for systemd unit environment parity.

Ensures that the docker proxy unit reads the exact same environment policy file(s)
as the worker unit, preventing runtime policy divergence (such as
ANTIGONA_SANDBOX_ALLOW_RUNC_FALLBACK or ANTIGONA_WORKSPACE).

B34: the shared file is the ``@ANTIGONA_ENV_FILE@`` token (the installer renders
it to ``<installing-user-home>/antigona.env``) rather than a hardcoded
absolute home path, so the published units stay portable while the parity
invariant (proxy and worker reference the SAME file) is unchanged.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"

# The single token every shipped unit must use for the production env file.
PRODUCTION_ENV_TOKEN = "@ANTIGONA_ENV_FILE@"


def parse_unit_environment_files(unit_path: Path) -> list[str]:
    """Parse EnvironmentFile directives from a systemd unit file.
    
    Returns raw strings declared in EnvironmentFile= lines.
    """
    env_files: list[str] = []
    text = unit_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("EnvironmentFile="):
            _, value = line.split("=", 1)
            env_files.append(value.strip())
    return env_files


def normalize_env_path(raw_path: str) -> str:
    """Normalize systemd EnvironmentFile path by stripping optional '-' prefix and quotes."""
    path = raw_path.strip()
    if path.startswith("-"):
        path = path[1:].strip()
    if (path.startswith('"') and path.endswith('"')) or (path.startswith("'") and path.endswith("'")):
        path = path[1:-1].strip()
    return path


def get_normalized_env_files(unit_path: Path) -> set[str]:
    """Return set of normalized file paths referenced by EnvironmentFile in the unit."""
    raw_list = parse_unit_environment_files(unit_path)
    return {normalize_env_path(p) for p in raw_list}


def test_repo_systemd_units_exist() -> None:
    """Ensure deploy/systemd directory and canonical unit files exist in repo."""
    assert DEPLOY_SYSTEMD_DIR.is_dir(), f"deploy/systemd dir missing at {DEPLOY_SYSTEMD_DIR}"
    proxy_unit = DEPLOY_SYSTEMD_DIR / "antigona-docker-proxy.service"
    worker_unit = DEPLOY_SYSTEMD_DIR / "antigona-worker.service"
    assert proxy_unit.is_file(), f"Missing proxy unit in repo: {proxy_unit}"
    assert worker_unit.is_file(), f"Missing worker unit in repo: {worker_unit}"


def test_docker_proxy_and_worker_declare_production_env_file() -> None:
    """Assert docker-proxy and worker units declare EnvironmentFile with production env."""
    proxy_unit = DEPLOY_SYSTEMD_DIR / "antigona-docker-proxy.service"
    worker_unit = DEPLOY_SYSTEMD_DIR / "antigona-worker.service"

    proxy_env_files = get_normalized_env_files(proxy_unit)
    worker_env_files = get_normalized_env_files(worker_unit)

    target_env = PRODUCTION_ENV_TOKEN
    assert target_env in proxy_env_files, (
        f"antigona-docker-proxy.service must declare EnvironmentFile with {target_env}, "
        f"got: {proxy_env_files}"
    )
    assert target_env in worker_env_files, (
        f"antigona-worker.service must declare EnvironmentFile with {target_env}, "
        f"got: {worker_env_files}"
    )


def test_worker_environment_files_subset_of_proxy_environment_files() -> None:
    """Assert proxy unit EnvironmentFile set contains all of worker unit EnvironmentFile set.
    
    Prevents silent policy/workspace divergence between worker execution and proxy gatekeeping.
    """
    proxy_unit = DEPLOY_SYSTEMD_DIR / "antigona-docker-proxy.service"
    worker_unit = DEPLOY_SYSTEMD_DIR / "antigona-worker.service"

    proxy_env_files = get_normalized_env_files(proxy_unit)
    worker_env_files = get_normalized_env_files(worker_unit)

    missing = worker_env_files - proxy_env_files
    assert not missing, (
        f"Worker EnvironmentFile set must be a subset of proxy EnvironmentFile set. "
        f"Proxy is missing: {missing}"
    )


def test_all_repo_services_environment_files_readable_and_parsed() -> None:
    """Parse all deploy/systemd/*.service files and verify consistent env file references."""
    service_files = list(DEPLOY_SYSTEMD_DIR.glob("*.service"))
    assert len(service_files) >= 7, f"Expected at least 7 service units in repo, found {len(service_files)}"

    for svc in service_files:
        env_files = get_normalized_env_files(svc)
        assert PRODUCTION_ENV_TOKEN in env_files, (
            f"Service {svc.name} does not reference the production env file token "
            f"{PRODUCTION_ENV_TOKEN}"
        )


def test_env_parity_drift_detection_simulation(tmp_path: Path) -> None:
    """Simulate missing EnvironmentFile or dropped policy path and ensure drift is detected."""
    proxy_mock = tmp_path / "mock-proxy.service"
    worker_mock = tmp_path / "mock-worker.service"

    # Worker specifies production env, but proxy misses it
    worker_mock.write_text("[Service]\nEnvironmentFile=-/etc/antigona/antigona.env\n", encoding="utf-8")
    proxy_mock.write_text("[Service]\nEnvironment=HOME=/var/lib/antigona\n", encoding="utf-8")

    worker_envs = get_normalized_env_files(worker_mock)
    proxy_envs = get_normalized_env_files(proxy_mock)

    assert not (worker_envs.issubset(proxy_envs)), "Drift detection should fail when proxy lacks worker envs"
