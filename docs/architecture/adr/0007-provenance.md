# ADR-007: Provenance & Unified Paths API

- **Статус:** ACCEPTED (2026-07-31)
- **Связанные:** ADR-001, ADR-004

## Решение
Единый Runtime Provenance Chain и **Unified Paths API** (`src/antigona/core/paths.py`).

Все пути резолвятся ТОЛЬКО через `core/paths.py`. В бизнес-логике запрещено:
- `Path.home()` / `expanduser()` (кроме пользовательского ввода);
- хардкод абсолютных путей проекта;
- самостоятельное вычисление `PROJECT_ROOT`;
- cwd-относительные пути конфигурации/состояния.

Enforcement:
- **CI Architecture Guard** (`scripts/arch_guard.py`, в `.github/workflows/ci.yml`):
  новый `Path.home()`/`expanduser`/`Path("/home/user/...")`/`PROJECT_ROOT=` вне
  `core/paths.py` -> exit 1. Baseline `scripts/arch_baseline.txt` ОБЯЗАН
  сокращаться (сейчас 8: modified-файлы + 2 легитимных expanduser).
- **Runtime Validator** (`startup/validator.py`): C10 (cwd всех процессов =
  `/opt/antigona`) + полные контракты.

## Причины
Forensic-аудит A.1 (Rev2): 5 механизмов резолва путей (hardcoded / Path.home /
`__file__` / cwd / env) — риск при переносе/смене HOME. Рефакторинг 14 чистых
файлов выполнен (ADDR-007), modified-файлы покрыты baseline.

## Рассмотренные альтернативы
1. Оставить 5 механизмов — отклонено (риск расхождения путей).
2. Единый paths.py + CI-запрет новых (выбрано).

## Последствия
- Новый код обязан использовать paths.py.
- Baseline сокращается по мере рефакторинга modified-файлов.
- Пути не изменились (резолв централизован, значения те же).

## Условия изменения
Расширение paths.py (новая каноническая функция пути) — допустимо; изменение
корня/резолва — отдельный ADR + approval.
