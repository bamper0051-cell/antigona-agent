# ADR-003: Startup

- **Статус:** ACCEPTED (2026-07-31)

## Решение
Официальный (canonical) launcher Antigona — **`run.sh`** (`/opt/antigona/run.sh`):
полный env (.env + дефолты + owner-secrets), health-check каждой службы,
Runtime Validator pre/post, `wait`.

Классификация сценариев:
| Сценарий | Статус |
|---|---|
| **run.sh** | **CANONICAL** (живой, health-checked) |
| start_bot.sh / start_gateway.sh | Legacy-риск (частичный env: только .env) |
| start_antigona_stack.sh | **Legacy** (вытеснен run.sh; TEST_MODE=1) |
| bin/antigona-chat.sh | DORMANT (обёртка antigona-cli) |
| deploy/systemd/* | **Legacy-шаблоны** (layout `/opt/antigona`, не установлен) |

## Причины
Forensic-аудит Rev2 §8: run.sh запустил живую связку (PID 1703889);
start_bot.sh (частичный env) запускал зависший бот 1455536.
`gateway/api.py:309` ссылается на устаревший `start_antigona_stack.sh`
(мёртвый fallback токена — регулярка не совпадает с реальным скриптом).

## Рассмотренные альтернативы
1. **start_antigona_stack.sh** — отклонено: nohup, TEST_MODE=1, без health-check.
2. **systemd /opt layout** — отклонено для текущей инсталляции (другой корень).
3. **run.sh (выбрано)** — единый, health-checked, полный env.

## Последствия
- Новые компоненты запускаются только через run.sh.
- start_bot.sh/gateway.sh → либо полный env, либо документированы как
  developer-утилиты; migrate в legacy после approval.
- `gateway/api.py:309` — фикс (источник токена только env).

## Условия изменения
Изменение canonical launcher — отдельный ADR + approval Owner.

## Addendum 2026-09-16: W3 startup gate and the real deployment path (B28)

Независимая adversarial-проверка W3 (envelope M13=1e3c287e) вернула BLOCKED по двум
блокерам, оба подтверждены наблюдением на диске; этот аддендум фиксирует факты и что
именно изменено, а что — нет.

**B28a — гейт не доходил до продакшна.** `ExecStartPre=@ANTIGONA_ROOT@/scripts/startup_gate.sh`
был добавлен только в 7 шаблонов `deploy/systemd/*.service`, а это **18-строчные LEGACY**
шаблоны (см. классификацию выше). Реально работающие 7 юнитов — **58–61-строчные
HARDENED** файлы в `/etc/systemd/system/antigona-*.service` (`User=antigona-svc`,
`Group=antigona-svc`, `ProtectSystem=strict`, `ProtectHome=tmpfs`,
`NoNewPrivileges=yes`, `ReadWritePaths=...`, `BindReadOnlyPaths=...`,
`Environment=PYTHONPATH/ANTIGONA_IMMUTABLE_DEPLOYMENT=1`); в них **не было ни одного
`ExecStartPre`** (`grep -l ExecStartPre /etc/systemd/system/antigona-*.service` → rc=1,
все 7 сервисов активны, PPID 1). Следовательно утверждение «живой стек может стартовать
только против верифицированного C11-манифеста» в продакшне было ложным.

**Механизм, который действительно достигает продакшна:** шиппинговый drop-in
[`deploy/systemd/dropins/10-antigona-startup-gate.conf`](../../../deploy/systemd/dropins/10-antigona-startup-gate.conf),
устанавливаемый `deploy/systemd/install_units.sh --dropins` в
`<DEST_DIR>/<unit>.service.d/10-antigona-startup-gate.conf` для всех 7 юнитов. Drop-in
**сливается** с уже существующим юнитом (включая hardened-юниты в `/etc/systemd/system`)
и не заменяет его, поэтому sandbox и privilege drop сохраняются. Шиппинговый файл
содержит ровно одну секцию `[Service]` с единственной директивой
`ExecStartPre=@ANTIGONA_ROOT@/scripts/startup_gate.sh` и лежит вне глоба `*.service`.

**OWNER-GATED, НЕ сделано:** установка drop-in (и любые `daemon-reload` / restart для
активации) — действие, требующее решения владельца. В этой волне drop-in **не устанавливался**,
`systemctl` / `daemon-reload` / restart **не вызывались**, `/etc` **не изменялся**. Артефакты
и их комментарии явно это фиксируют.

**B28b — installer мог регрессировать живой стек.** `install_units.sh` писал
отрендеренный шаблон безусловно (`printf '%s\n' "$rendered" > "$target_path"`), без
drift-проверки и бэкапа; документированный путь установки (README, `install.sh:251`)
затёр бы hardened-юниты 18-строчными legacy-шаблонами, потеряв sandbox. Теперь
installer fail-closed: для существующего target с отличающимися байтами — hard error
rc=1 с перечислением путей, **до любой записи** (preflight, дерево назначения не
меняется); `--force` требует `--backup-dir` и делает байт-идентичный бэкап каждого
отличающегося target **до** записи, печатая путь бэкапа. Идентичное содержимое
переписывается идемпотентно и `--force` не требует. Появились режимы `--check`
(drift-отчёт, ничего не пишет; rc=1 при DRIFTED) и `--dropins`. Installer не вызывает
`systemctl`, `daemon-reload`, restart, не ходит в сеть и не использует `sudo` — только
пишет файлы под `--dest` / `--backup-dir`. Фальсификатор:
`tests/unit/test_w3_install_units_guard.py` (на 1e3c287: 8 failed / 1 passed; после
фикса: 24 passed вместе с двумя существующими тестами).

**Остаточный риск N1 (закрыт ниже, см. аддендум 2026-09-16 B30/N1).** `scripts/startup_gate.sh` уважает
`ANTIGONA_GATE_PYTHON`, то есть гейт обходится `ANTIGONA_GATE_PYTHON=/bin/true` с
тихим rc=0. В этой волне `scripts/startup_gate.sh` не менялся; переменная известна и
документирована здесь как остаточный риск.

## Addendum 2026-09-16 (b): B30 base-unit selection, drop-in mode and N1 closed

**B30 — installer безусловно тащил базовые шаблоны в список установки.**
`install_units.sh` добавлял все 7 `deploy/systemd/*.service` в write-набор независимо
от содержимого DEST. Следствия на реальном деплое (в DEST — hardened-юниты, 58–61
строка, `User=antigona-svc`, `ProtectSystem=strict`, `ProtectHome=tmpfs`):
`install_units.sh <ROOT> --dest <DEST> --dropins` без `--force` возвращал rc=1
(`refusing to overwrite differing target(s); nothing was written`) и создавал **0 из 7**
drop-in-ов; единственный рабочий путь `--dropins --force --backup-dir <b>` давал 7
drop-in-ов, но **заменял** hardened-юниты 18-строчными legacy-шаблонами, теряя
privilege drop и sandbox. Это прямо противоречит смыслу drop-in-механизма: drop-in
**сливается** с существующим юнитом и не заменяет его.

Что изменено (только выборочная семантика целей; политика fail-closed сохранена):

* базовый юнит попадает в список установки **только если** файла в DEST нет **или** он
  байт-идентичен отрендеренному шаблону;
* вне drop-in-режима поведение прежнее: отличающийся базовый юнит — hard error rc=1 **до
  любой записи** (дерево DEST не меняется), `--force` по-прежнему требует бэкапа в
  `--backup-dir` **до** записи;
* в drop-in-режиме отличающийся (hardened) базовый юнит **пропускается и остаётся
  байт-идентичным**, а drop-in всё равно устанавливается (rc=0); ни один базовый
  `*.service` при этом не перезаписывается;
* `--check` (drift-отчёт, ничего не пишет, rc=1 при DRIFTED) и `--dry-run` продолжают
  обходить полный набор целей и не изменились;
* **новая явная семантика `--dropins-only`** (алиас `--no-base-units`): пишутся только
  `<DEST>/<unit>.service.d/10-antigona-startup-gate.conf` (7 файлов), базовые
  `*.service` не пишутся и не рассматриваются как цели установки вовсе.

Фальсификаторы (`tests/unit/test_w3_install_units_guard.py`):
`test_dropins_over_hardened_dest_succeeds_and_leaves_base_units_untouched` (DEST
предзаполнен 60-строчными hardened-юнитами, `--dropins` без `--force`: rc=0, 7
drop-in-ов, sha256 всех 7 базовых юнитов до == после) и
`test_dropins_only_writes_only_dropins_and_no_base_units`. RED-FIRST: на родительском
HEAD `e8b4eafa` новый тест падает (rc=1, `refusing to overwrite differing target(s)`,
0 drop-in-ов; лог — `RED_FIRST_parent.log` в evidence-каталоге B30_WRITER).

**N1 — тихий обход гейта закрыт.** `ANTIGONA_GATE_PYTHON=/bin/true` давал молчаливый
rc=0 и печатал `immutability contract verified` (измерено на `e8b4eafa`: rc=0, stdout
178 байт, stderr 0 байт) — при том что документация обещает ровно один громкий
override (`ANTIGONA_SKIP_STARTUP_GATE=1`). Теперь `scripts/startup_gate.sh`
fail-closed:

* интерпретатор, чей basename не `python*`/`pypy*` (например `/bin/true`), отвергается
  громким `CRITICAL ... FAIL-CLOSED` баннером в stderr с rc=1;
* интерпретатор, не давший **никакого** вывода валидатора, тоже отвергается: «нет
  доказательства — нет PASS» (реальный валидатор всегда печатает заголовок отчёта).

Фальсификаторы: `test_gate_fail_closed_on_non_python_interpreter`,
`test_gate_fail_closed_on_silent_python_named_interpreter`
(`tests/unit/test_w3_startup_gate_fail_closed.py`). `ANTIGONA_SKIP_STARTUP_GATE=1`
остаётся единственным громким override.

**По-прежнему OWNER-GATED:** установка drop-in и любые `daemon-reload`/restart для
активации. `/etc/systemd/system` в этой волне не изменялся, `systemctl` не вызывался;
проверка installer'а выполняется на throwaway DEST внутри evidence-каталога.

## Operational prerequisite (2026-09-17)

Шиппинговый drop-in
[`deploy/systemd/dropins/10-antigona-startup-gate.conf`](../../../deploy/systemd/dropins/10-antigona-startup-gate.conf)
добавляет в каждый юнит `ExecStartPre=<root>/scripts/startup_gate.sh`. Гейт запускает
`python3 -m antigona.startup.validator --check=manifest`; валидатор разрешает
deployment-envelope из переменной окружения `ANTIGONA_DEPLOYMENT_MANIFEST` либо, по
умолчанию, из `<code-root>/CANDIDATE_DEPLOYMENT_MANIFEST.json`
(`src/antigona/startup/validator.py:181`). Envelope **не публикуется** (см.
`build_public_release_w5v16_b39.sh`, `ALLOWED_REF`: "deployment envelope supplied by the
operator ... never shipped") — его поставляет оператор. Следствие, измеренное
2026-09-17 на публичном клоне: на чистом source-checkout без envelope гейт fail-closed,
завершается с rc=1, и systemd **отменяет** запуск юнита.

Единственный документированный обход — `ANTIGONA_SKIP_STARTUP_GATE=1` (громкий, виден
в journal; измерено: rc=0); либо предоставить envelope и указать на него
`ANTIGONA_DEPLOYMENT_MANIFEST`.

Измеренный вывод (verbatim):

```text
=== Runtime Validator — manifest ===
  [CRITICAL] ❌ contract:C11:deployment_manifest: manifest verification failed: [Errno 2] No such file or directory: '<clone>/CANDIDATE_DEPLOYMENT_MANIFEST.json'
  [WARN    ] ✅ contract:C11:evidence_chain: C11 verdict journalled at sha256:8d957f61...
⛔ КРИТИЧЕСКИЕ НАРУШЕНИЯ — запуск отменён.
CRITICAL: startup gate FAIL-CLOSED - immutability validation (--check=manifest)
```

