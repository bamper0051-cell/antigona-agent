"""Plugin/Skill loader — load skills from .skill/ directories.

Each .skill/ directory contains:
  - SKILL.md      — manifest (name, version, description, tools_required, permissions)
  - scripts/      — executable scripts
  - tools/        — tool definitions (Python files)

SkillRegistry provides register, load, list, and unload operations.
Broken skills are isolated — they never crash the system.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from antigona.core import paths

logger = logging.getLogger(__name__)


# ── Manifest ──────────────────────────────────────────────────────────────


@dataclass
class SkillManifest:
    """Skill manifest parsed from SKILL.md."""

    name: str
    version: str = "0.1.0"
    description: str = ""
    tools_required: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    author: str = ""
    dependencies: list[str] = field(default_factory=list)


def _parse_manifest(path: Path) -> SkillManifest | None:
    """Parse SKILL.md frontmatter into a SkillManifest.

    Expects YAML-like frontmatter between ``---`` delimiters.
    """
    if not path.exists():
        logger.warning("Manifest not found at %s", path)
        return None

    try:
        text = path.read_text(encoding="utf-8")

        # Extract frontmatter between --- markers
        if not text.startswith("---"):
            logger.warning("Manifest %s missing frontmatter '---'", path)
            return None

        end_idx = text.find("---", 3)
        if end_idx == -1:
            logger.warning("Manifest %s missing closing '---'", path)
            return None

        frontmatter = text[3:end_idx].strip()

        manifest = SkillManifest(name=path.parent.name)
        for line in frontmatter.splitlines():
            line = line.strip()
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()

            if key == "name":
                manifest.name = value
            elif key == "version":
                manifest.version = value
            elif key == "description":
                manifest.description = value
            elif key == "tools_required":
                manifest.tools_required = [v.strip() for v in value.split(",") if v.strip()]
            elif key == "permissions":
                manifest.permissions = [v.strip() for v in value.split(",") if v.strip()]
            elif key == "author":
                manifest.author = value
            elif key == "dependencies":
                manifest.dependencies = [v.strip() for v in value.split(",") if v.strip()]

        return manifest
    except OSError as exc:
        logger.error("Failed to read manifest %s: %s", path, exc)
        return None


# ── Skill representation ─────────────────────────────────────────────────


@dataclass
class Skill:
    """Representation of a loaded skill/plugin.

    Attributes:
        manifest: The parsed skill manifest.
        skill_dir: Path to the .skill/ directory.
        loaded: Whether the skill is currently loaded.
        error: Error message if loading failed (None if successful).
    """

    manifest: SkillManifest
    skill_dir: Path
    loaded: bool = False
    error: str | None = None
    _tools: dict[str, Any] = field(default_factory=dict)
    _scripts: list[Path] = field(default_factory=list)


# ── Skill Registry ────────────────────────────────────────────────────────


class SkillRegistry:
    """Registry for loading and managing .skill/ plugins.

    Thread-safe for read operations (list, get). Write operations
    (register, load, unload) should be serialised.
    """

    def __init__(self, skills_root: str | Path | None = None) -> None:
        # R1-PORTABILITY-01: default resolves through the canonical path API
        # (owner_dir()/skills), never a hardcoded /var/lib/antigona.
        self._skills_root = Path(skills_root) if skills_root else paths.skills_dir()
        self._skills_root.mkdir(parents=True, exist_ok=True)
        self._skills: dict[str, Skill] = {}

    def discover(self) -> list[Path]:
        """Discover all .skill/ directories under skills_root.

        Returns:
            List of paths to .skill/ directories.
        """
        discovered: list[Path] = []
        if not self._skills_root.is_dir():
            return discovered

        for entry in self._skills_root.iterdir():
            if entry.is_dir() and entry.suffix == ".skill":
                discovered.append(entry)
            elif entry.is_dir() and entry.name.endswith(".skill"):
                discovered.append(entry)

        return sorted(discovered)

    def register(self, skill_dir: str | Path) -> Skill | None:
        """Register a .skill/ directory.

        Parses SKILL.md, validates structure, and registers the skill
        without loading its tools/scripts.

        Args:
            skill_dir: Path to the .skill/ directory.

        Returns:
            The Skill object if registration succeeded, None on error.
        """
        skill_path = Path(skill_dir).resolve()
        if not skill_path.is_dir():
            logger.error("Skill directory not found: %s", skill_path)
            return None

        manifest_path = skill_path / "SKILL.md"
        manifest = _parse_manifest(manifest_path)
        if manifest is None:
            return None

        if manifest.name in self._skills:
            logger.warning("Skill '%s' already registered. Use unload first.", manifest.name)
            return None

        skill = Skill(
            manifest=manifest,
            skill_dir=skill_path,
            loaded=False,
        )
        self._skills[manifest.name] = skill
        logger.info("Registered skill '%s' from %s", manifest.name, skill_path)
        return skill

    def load(self, name: str) -> Skill | None:
        """Load a previously registered skill.

        Discovers scripts/ and tools/, imports tools modules.

        Args:
            name: The skill name.

        Returns:
            The loaded Skill, or None if not found / error.
        """
        skill = self._skills.get(name)
        if skill is None:
            logger.error("Skill '%s' not registered. Call register() first.", name)
            return None

        if skill.loaded:
            return skill

        # Discover scripts/
        scripts_dir = skill.skill_dir / "scripts"
        if scripts_dir.is_dir():
            for entry in sorted(scripts_dir.iterdir()):
                if entry.is_file() and os.access(entry, os.X_OK):
                    skill._scripts.append(entry)

        # Discover and import tools/
        tools_dir = skill.skill_dir / "tools"
        if tools_dir.is_dir():
            for entry in sorted(tools_dir.iterdir()):
                if entry.suffix == ".py" and entry.name != "__init__.py":
                    try:
                        tool_name = f"_skill_{name}_{entry.stem}"
                        spec = importlib.util.spec_from_file_location(tool_name, entry)
                        if spec and spec.loader:
                            mod = importlib.util.module_from_spec(spec)
                            # Isolate: use a separate namespace
                            spec.loader.exec_module(mod)
                            skill._tools[entry.stem] = mod
                    except Exception as exc:
                        logger.error(
                            "Failed to load tool '%s' in skill '%s': %s",
                            entry.name, name, exc,
                        )
                        # Isolate: continue loading other tools
                        continue

        skill.loaded = True
        logger.info(
            "Loaded skill '%s': %d scripts, %d tools",
            name,
            len(skill._scripts),
            len(skill._tools),
        )
        return skill

    def unload(self, name: str) -> bool:
        """Unload a skill (mark as not loaded, clear cached tools/scripts).

        Args:
            name: The skill name.

        Returns:
            True if successfullly unloaded, False if not found.
        """
        skill = self._skills.get(name)
        if skill is None:
            return False

        skill.loaded = False
        skill._scripts.clear()
        skill._tools.clear()
        logger.info("Unloaded skill '%s'", name)
        return True

    def unregister(self, name: str) -> bool:
        """Completely remove a skill from the registry.

        Implicitly unloads it first.

        Args:
            name: The skill name.

        Returns:
            True if removed, False if not found.
        """
        if name not in self._skills:
            return False
        self.unload(name)
        del self._skills[name]
        logger.info("Unregistered skill '%s'", name)
        return True

    def get(self, name: str) -> Skill | None:
        """Get a skill by name."""
        return self._skills.get(name)

    def list_skills(self) -> list[Skill]:
        """List all registered skills."""
        return list(self._skills.values())

    def reload(self, name: str) -> Skill | None:
        """Reload a skill: unload, re-register from disk, then load.

        Args:
            name: The skill name.
        """
        skill = self._skills.get(name)
        if skill is None:
            return None

        skill_dir = skill.skill_dir
        self.unregister(name)
        registered = self.register(skill_dir)
        if registered:
            return self.load(registered.manifest.name)
        return None

    @property
    def count(self) -> int:
        """Number of registered skills."""
        return len(self._skills)
