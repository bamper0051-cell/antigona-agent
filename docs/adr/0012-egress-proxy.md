# ADR-0012: Smart Egress-Proxy с allowlist и подсистема безопасного сетевого доступа для web-fetch

- **Статус:** Accepted
- **Дата:** 2026-07-26
- **Авторы:** Antigona Team
- **Связано с:** ADR-0001 (Clean-Room Architecture), ADR-0011 (Dual-LLM Quarantine Model)

---

## 1. Контекст и проблема

В шаге P0 инструмент `web.fetch` был реализован как заглушка (`DisabledWebFetchTool`), возвращающая статус `enabled=False` и сообщение от отключении. В шаге P3.3 была внедрена Dual-LLM quarantine-модель (`src/antigona/worker/quarantine.py`), которая санирует untrusted-контент перед передачей в reasoning агента.

Для перехода к фазе P4 (сетевые инструменты и изоляция) необходимо сделать `web.fetch` реально работающим инструментом, сохранив при этом жёсткие инварианты безопасности Antigona:
1. **Предотвращение Server-Side Request Forgery (SSRF) и неконтролируемого сетевого egress.**
2. **Deny-by-default allowlist:** домен запроса должен явно проверяться по списку разрешённых доменов.
3. **Fail-closed транспорт:** при недоступности прокси-сервера или блокировке по allowlist инструмент должен блокировать возврат сырых данных (`enabled=False`), а не делать прямые сетевые вызовы в открытую сеть.
4. **Интеграция с Quarantine (P3.3):** сырой контент, полученный через egress-proxy, должен по-прежнему проходить через `quarantine.sanitize()`.

## 2. Принятое решение

1. **Транспортный слой `EgressProxy` (`src/antigona/egress/proxy.py`):**
   - Выступает в качестве единственной точки выполнения сетевых запросов для `WebFetchTool`.
   - Инкапсулирует `Allowlist` и маршрутизацию сетевых вызовов (через внешнее прокси `proxy_url` или прямой HTTP-запрос при соблюдении allowlist).
   - При любых сетевых сбоях, таймаутах или отказе allowlist выбрасывает `EgressUnavailableError` (подкласс `ToolError`).

2. **Правила `Allowlist` (Deny-by-Default):**
   - Поддерживает точные домены (например, `example.com`) и суффикс-шаблоны (`*.wikipedia.org`).
   - Суффикс-шаблон `*.example.com` сопоставляется с `api.example.com`, а также с базовым `example.com`.
   - Если домен не найден в allowlist, `Allowlist.contains(host)` возвращает `False`, что приводит к выбросу `EgressUnavailableError`.

3. **Обновление `WebFetchTool` (`src/antigona/worker/tools/web_fetch_tool.py`):**
   - `WebFetchTool` принимает инстанс `EgressProxy`.
   - `fetch(url)` выполняет запрос исключительно через `self.proxy.fetch(url)`.
   - В случае успешного запроса возвращает `WebFetchResult(url=url, enabled=True, detail=raw_content, untrusted=True)`.
   - В случае `EgressUnavailableError` возвращает `WebFetchResult(url=url, enabled=False, detail=f"egress blocked: {exc}", untrusted=True)` (fail-closed).
   - Запрещены прямые сетевые вызовы (`requests.get`, `httpx.get`, `socket`, `urlopen`) в коде `web_fetch_tool.py`.

4. **Централизованная конфигурация (`src/antigona/config.py`):**
   - Новые настройки в `Settings`:
     - `egress_enabled: bool` (по умолчанию `True`, env `ANTIGONA_EGRESS_ENABLED`)
     - `egress_deny_by_default: bool` (по умолчанию `True`, env `ANTIGONA_EGRESS_DENY_BY_DEFAULT`)
     - `egress_allowlist: list[str]` (список доменов, env `ANTIGONA_EGRESS_ALLOWLIST`)
     - `egress_proxy_url: str | None` (URL прокси, env `ANTIGONA_EGRESS_PROXY_URL`)
     - `egress_allowlist_file: str | None` (путь к файлу со списком доменов, env `ANTIGONA_EGRESS_ALLOWLIST_FILE`)
     - `egress_timeout_seconds: int` (таймаут запроса в сек, env `ANTIGONA_EGRESS_TIMEOUT`)

5. **Ограничения Clean-Room и Изоляции:**
   - НИКАКИХ заимствований из сторонних проектов (klio/Hermes/OpenClaw).
   - Прокси-транспорт реализуется in-process (через standard library urllib / optional httpx), без создания Docker-контейнеров через `docker.sock`.

---

## 3. Последствия

- **Плюсы:**
  - `web.fetch` работает по-настоящему, но со строго ограниченной поверхностью атаки.
  - Поведение при ошибках сетевого уровня — fail-closed (сырые данные не отдаются).
  - Интеграция с Quarantine (P3.3) сохраняется полностью.
- **Ограничения:**
  - Запросы к доменам вне allowlist блокируются с `enabled=False`.
  - Micro-VM / Firecracker изоляция вынесена в P4.2.
