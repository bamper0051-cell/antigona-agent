# Integration checklist

- [ ] Скопирован пакет `antigona.tools`.
- [ ] Registry создаётся один раз на startup.
- [ ] Model tool schemas берутся из `CapabilitySnapshot`.
- [ ] Все вызовы проходят через `ToolExecutor`.
- [ ] История `ToolCallRecord` сохраняется в durable operation.
- [ ] `ToolEvent` публикуется в operation event bus.
- [ ] Telegram presenter показывает started/finished в одном progress message.
- [ ] Owner verification заполняется только доверенным channel middleware.
- [ ] OTP/TOTP выставляет `otp_verified` только для одной операции/действия и с TTL.
- [ ] Terminal не получает shell-строку; только argv.
- [ ] Workspace path проверяется до каждого файлового действия.
- [ ] Есть iteration budget и stop condition.
- [ ] Добавлены eval-сценарии выбора инструмента и обработки timeout.
