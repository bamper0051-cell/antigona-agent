"""Tests for the Skill/Plugin Loader (antigona/skills/plugin_loader.py).

Covers:
  - Manifest parsing
  - Skill registration, loading, listing, unloading
  - Isolation: broken skill doesn't crash system
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from antigona.skills.plugin_loader import (
    SkillRegistry,
    _parse_manifest,
)


class TestManifestParsing:
    """Tests for _parse_manifest."""

    def test_parse_valid_manifest(self) -> None:
        """Parse a valid SKILL.md manifest."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "test.skill"
            skill_dir.mkdir()
            manifest = skill_dir / "SKILL.md"
            manifest.write_text(
                "---\n"
                "name: test-skill\n"
                "version: 1.2.0\n"
                "description: A test skill\n"
                "tools_required: bash, python\n"
                "permissions: filesystem_read, filesystem_write\n"
                "---\n"
                "# Test Skill\n"
                "This is a test."
            )

            parsed = _parse_manifest(manifest)
            assert parsed is not None
            assert parsed.name == "test-skill"
            assert parsed.version == "1.2.0"
            assert parsed.description == "A test skill"
            assert parsed.tools_required == ["bash", "python"]
            assert parsed.permissions == ["filesystem_read", "filesystem_write"]

    def test_parse_missing_file(self) -> None:
        """Missing SKILL.md should return None."""
        result = _parse_manifest(Path("/tmp/nonexistent_skill/SKILL.md"))
        assert result is None

    def test_parse_no_frontmatter(self) -> None:
        """SKILL.md without frontmatter returns None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = Path(tmpdir) / "SKILL.md"
            manifest.write_text("No frontmatter here")
            result = _parse_manifest(manifest)
            assert result is None

    def test_parse_minimal(self) -> None:
        """Minimal manifest with just a name."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "minimal.skill"
            skill_dir.mkdir()
            manifest = skill_dir / "SKILL.md"
            manifest.write_text("---\nname: minimal\n---\n")

            parsed = _parse_manifest(manifest)
            assert parsed is not None
            assert parsed.name == "minimal"
            assert parsed.version == "0.1.0"  # default


class TestSkillRegistry:
    """Tests for SkillRegistry."""

    def test_register_skill(self) -> None:
        """Register a valid .skill/ directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_root = Path(tmpdir)
            skill_dir = skills_root / "my-skill.skill"
            skill_dir.mkdir()
            manifest = skill_dir / "SKILL.md"
            manifest.write_text(
                "---\nname: my-skill\nversion: 1.0.0\ndescription: My skill\n---\n"
            )

            registry = SkillRegistry(skills_root=str(skills_root))
            skill = registry.register(skill_dir)
            assert skill is not None
            assert skill.manifest.name == "my-skill"
            assert not skill.loaded

    def test_register_twice_fails(self) -> None:
        """Registering the same skill twice returns None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_root = Path(tmpdir)
            skill_dir = skills_root / "dup.skill"
            skill_dir.mkdir()
            manifest = skill_dir / "SKILL.md"
            manifest.write_text("---\nname: dup\n---\n")

            registry = SkillRegistry(skills_root=str(skills_root))
            first = registry.register(skill_dir)
            second = registry.register(skill_dir)
            assert first is not None
            assert second is None  # already registered

    def test_load_skill(self) -> None:
        """Loading a registered skill sets loaded=True."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_root = Path(tmpdir)
            skill_dir = skills_root / "loadable.skill"
            skill_dir.mkdir()
            manifest = skill_dir / "SKILL.md"
            manifest.write_text("---\nname: loadable\n---\n")

            registry = SkillRegistry(skills_root=str(skills_root))
            registry.register(skill_dir)
            loaded = registry.load("loadable")
            assert loaded is not None
            assert loaded.loaded

    def test_load_not_registered(self) -> None:
        """Loading a non-registered skill returns None."""
        registry = SkillRegistry(skills_root="/tmp")
        result = registry.load("nonexistent")
        assert result is None

    def test_list_skills(self) -> None:
        """list_skills returns registered skills."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_root = Path(tmpdir)
            skill_dir = skills_root / "listed.skill"
            skill_dir.mkdir()
            manifest = skill_dir / "SKILL.md"
            manifest.write_text("---\nname: listed\n---\n")

            registry = SkillRegistry(skills_root=str(skills_root))
            assert len(registry.list_skills()) == 0
            registry.register(skill_dir)
            assert len(registry.list_skills()) == 1

    def test_loader_root_follows_the_unified_paths_api(self) -> None:
        """The loader must resolve its root the same way the brain does.

        ``AntigonaBrain._command_plugins`` resolves plugin directories under
        ``paths.owner_dir()``. If the loader resolves its own root differently,
        load and list disagree — and ``disable()`` writes its record outside the
        directory the brain is reading from.
        """
        from unittest import mock

        from antigona.plugins import PluginLoader, PluginRegistry

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "owner"
            (root / "plugins").mkdir(parents=True)
            with mock.patch("antigona.core.paths.owner_dir", return_value=root):
                loader = PluginLoader(PluginRegistry())
                loader.disable("demo")
                assert loader.disabled_names() == {"demo"}
                assert (root / "plugins" / ".disabled.json").is_file()

    def test_unload_skill(self) -> None:
        """Unloading a skill resets loaded state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_root = Path(tmpdir)
            skill_dir = skills_root / "unloadable.skill"
            skill_dir.mkdir()
            manifest = skill_dir / "SKILL.md"
            manifest.write_text("---\nname: unloadable\n---\n")

            registry = SkillRegistry(skills_root=str(skills_root))
            registry.register(skill_dir)
            registry.load("unloadable")
            assert registry.get("unloadable") is not None
            assert registry.get("unloadable").loaded

            result = registry.unload("unloadable")
            assert result
            assert not registry.get("unloadable").loaded

    def test_unregister_skill(self) -> None:
        """Unregister removes skill completely."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_root = Path(tmpdir)
            skill_dir = skills_root / "removable.skill"
            skill_dir.mkdir()
            manifest = skill_dir / "SKILL.md"
            manifest.write_text("---\nname: removable\n---\n")

            registry = SkillRegistry(skills_root=str(skills_root))
            registry.register(skill_dir)
            assert registry.count == 1

            registry.unregister("removable")
            assert registry.count == 0

    def test_discover(self) -> None:
        """Discover finds .skill/ directories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_root = Path(tmpdir)
            (skills_root / "alpha.skill").mkdir()
            (skills_root / "beta.skill").mkdir()

            registry = SkillRegistry(skills_root=str(skills_root))
            discovered = registry.discover()
            names = {d.name for d in discovered}
            assert "alpha.skill" in names
            assert "beta.skill" in names

    def test_broken_skill_isolation(self) -> None:
        """A broken skill should not crash the registry."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_root = Path(tmpdir)
            skill_dir = skills_root / "broken.skill"
            skill_dir.mkdir()
            # No SKILL.md — register should return None gracefully
            registry = SkillRegistry(skills_root=str(skills_root))
            result = registry.register(skill_dir)
            assert result is None
            # Registry should still be usable
            assert registry.count == 0
            assert registry.list_skills() == []

    def test_count_property(self) -> None:
        """Count property returns correct number."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_root = Path(tmpdir)
            registry = SkillRegistry(skills_root=str(skills_root))
            assert registry.count == 0
            for i in range(3):
                sd = skills_root / f"skill{i}.skill"
                sd.mkdir()
                (sd / "SKILL.md").write_text(f"---\nname: skill{i}\n---\n")
                registry.register(sd)
            assert registry.count == 3
