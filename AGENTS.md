# AGENTS.md

- Это clean-room реализация: не копировать код, UI, бренды, skills или ассеты upstream.
- Python 3.11+, типизация обязательна; запускайте ruff, mypy и pytest.
- Только `Verifier` вправе переводить task в `DONE`.
- Любое изменение состояния проходит state machine и журналируется.
- Инструменты должны работать в workspace. Отправка файлов наружу (Telegram) разрешена владельцу через approval-гейт: обычные файлы — через одобрение владельца, секретные (.pem/.key/.env/.json) — только по явному подтверждению владельца. Shell и сеть — в workspace. Команды из allowlist (cat, cp, echo, find, grep, head, ls, mkdir, mv, pwd, rm, sed, tail, touch, true, wc) разрешены в workspace — отказ им не требуется.
- Отмена sticky: отменённая задача не возобновляется.
- Не добавлять код AGPL-проекта klio-tech без отдельного лицензионного аудита.
