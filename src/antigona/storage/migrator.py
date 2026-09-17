"""Alembic entry points (``antigona db upgrade``).

Alembic is the *only* sanctioned way to evolve a Postgres deployment; the SQLite
fallback keeps using :func:`antigona.storage.engine.create_all` (ADR-0014). The
``alembic`` package is imported lazily so importing :mod:`antigona.storage` never
drags migration tooling into the worker/gateway hot path.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .engine import mask_db_url, normalize_db_url

if TYPE_CHECKING:  # pragma: no cover - typing only
    from alembic.config import Config

#: Directory holding ``alembic.ini`` and the ``migrations/`` script tree.
PACKAGE_DIR = Path(__file__).resolve().parent
MIGRATIONS_DIR = PACKAGE_DIR / "migrations"
ALEMBIC_INI = PACKAGE_DIR / "alembic.ini"


def alembic_config(db_url: str) -> Config:
    """Build an Alembic ``Config`` pinned to this package's migration tree."""
    from alembic.config import Config

    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    # ConfigParser interpolation would eat a literal '%' in a password.
    config.set_main_option("sqlalchemy.url", normalize_db_url(db_url).replace("%", "%%"))
    return config


def head_revision() -> str | None:
    """Return the newest revision id in the script tree (no database contact)."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory(str(MIGRATIONS_DIR))
    revision: str | None = script.get_current_head()
    return revision


def upgrade_to_head(db_url: str) -> str:
    """Run ``alembic upgrade head`` against ``db_url``; return the masked URL."""
    from alembic import command

    command.upgrade(alembic_config(db_url), "head")
    return mask_db_url(normalize_db_url(db_url))
