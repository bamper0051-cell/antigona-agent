# Third-party strategy

## P0: clean-room

P0 реализован с нуля только по публично описанным архитектурным идеям. Мы не копируем
upstream-код, UI, бренды, prompts, skills, тесты или ассеты Hermes Agent, OpenClaw,
Qwen Code, Minara CLI/skills либо klio-tech engine.

Известные лицензии архитектурных ориентиров: Hermes Agent и OpenClaw — MIT, Qwen Code —
Apache-2.0, Minara CLI/skills — MIT. klio-tech engine — AGPL-3.0; его код не используется.
Эти сведения не заменяют юридическую консультацию.

## Перед будущим копированием или заимствованием

Обязателен отдельный аудит: точная версия и provenance, LICENSE/NOTICE, совместимость
лицензии, история файлов, авторские заголовки, требования attribution/source disclosure,
товарные знаки и письменное решение maintainers. До завершения аудита допускаются только
независимая реализация идей и обычные package dependencies из `pyproject.toml`.
