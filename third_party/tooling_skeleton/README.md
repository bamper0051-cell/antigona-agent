# Antigona Tooling Skeleton

Минимальный рабочий каркас инструментального ядра Антигоны.

Внутри:

- единый `ToolRegistry`;
- контракты `ToolSpec`, `ToolCall`, `ToolResult`;
- `ToolPolicyEngine` с уровнями риска, owner/OTP-проверкой и защитой от повторов;
- `ToolExecutor` с timeout, нормализацией ошибок и событиями;
- `CapabilitySnapshot` — модели показываются только доступные инструменты;
- базовый `AgentRunner` с лимитом итераций;
- инструменты `read_file`, `search_files`, `terminal`, `run_pytest`;
- два базовых `SKILL.md`;
- тесты.

## Установка

```bash
cd antigona_tooling_skeleton
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
```

## Быстрый запуск

```bash
python examples/demo_agent_loop.py
```

## Встраивание в Антигону

1. Скопировать `src/antigona/tools/` в основной проект.
2. При старте приложения вызвать `build_default_registry()`.
3. На каждую durable operation создавать `ToolContext`.
4. Передавать модели `CapabilitySnapshot.schemas_for_model`, а не полный реестр.
5. Все tool calls проводить только через `ToolExecutor.execute()`.
6. События executor связать с `OperationEventBus` и единым Telegram presenter.
7. Для `SYSTEM_CHANGE` и `EXTERNAL_IRREVERSIBLE` выставлять `otp_verified=True` только после реальной проверки OTP/TOTP.

## Важная граница

Этот каркас не разрешает модели напрямую вызывать Python-функции. Модель формирует структурированный `ToolCall`, policy принимает решение, executor выполняет handler и возвращает нормализованный `ToolResult`.
