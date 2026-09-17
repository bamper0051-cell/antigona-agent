---
name: python-test-diagnosis
description: Последовательная диагностика Python/pytest-проектов.
version: 1.0.0
metadata:
  antigona:
    requires_tools: [read_file, search_files, run_pytest]
---

# Python Test Diagnosis

## Procedure

1. Прочитай `pyproject.toml`, `pytest.ini` или `setup.cfg`.
2. Определи project Python; `.venv/bin/python` имеет приоритет.
3. Проверь синтаксис изменённых Python-файлов.
4. Запусти тесты непосредственно изменённого модуля.
5. Запусти связанные unit tests.
6. Запусти связанные integration tests.
7. Только затем запускай полный suite.
8. При timeout используй `--durations=20`, деление suite и меньшую область проверки.
9. Не повторяй ту же команду без новой гипотезы.

## Verification

В финальном отчёте перечисли точные команды, exit code, число passed/failed/errors/skipped и непроверенные области.
