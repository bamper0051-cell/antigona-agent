# Sandbox — профиль изоляции тулов (P0.4)

Тулы Antigona исполняются в Docker-контейнерах с **двухслойной** изоляцией:
1. **gVisor (runsc)** — приоритетный OCI-runtime, изолирует syscall-поверхность ядра.
2. **runc** — fallback, если runsc недоступен на хосте (логируется громким WARN).

## Профиль запуска (fail-closed)

Любой контейнер тула стартует с этим набором (см. `src/antigona/sandbox/runner.py`):

| Флаг | Значение | Защищает от |
|------|----------|-------------|
| `--runtime=runsc` | gVisor kernel isolation | escape через ядро |
| `--network none` | deny-by-default сеть | исходящих соединений |
| `--cpus` / `--memory` | жёсткие лимиты | DoS-потребления ресурсов |
| `--pids-limit` | лимит процессов | fork-бомб |
| read-only root | RO корень | записи в систему контейнера |
| `--user` (non-root UID) | не root внутри | привилегированных эскейпов |
| tmpfs (только workspace) | монтируется только `0750` workspace | чтения/записи вне рабочей папки |
| **НЕТ** `docker.sock` | сокет не пробрасывается | захвата хоста через Docker |

## Включение runsc

На большинстве хостов runsc уже зарегистрирован в Docker:

```bash
docker info --format '{{.Runtimes}}'   # должен содержать runsc
```

Если нет — установить gVisor (официальная инструкция gvisor.dev) и
зарегистрировать runtime в `/etc/docker/daemon.json`:

```json
{ "runtimes": { "runsc": { "path": "/usr/local/bin/runsc", "runtimeArgs": ["--net=none"] } } }
```

После чего `systemctl --user restart docker` (или системный `docker`).

## Fallback на runc

`runtime_registered("runsc")` проверяет наличие рантайма в Docker в момент запуска.
Если runsc недоступен — выбирается `runc`, в лог пишется:

```
WARNING sandbox.runner: runsc unavailable, falling back to runc (weaker isolation)
```

Функциональность тулов сохраняется, но изоляция ядра ослабляется до уровня
обычного Docker (всё ещё fail-closed по сети/лимитам/workspace).

## Тесты

- `tests/sandbox/test_runner.py` — профиль содержит `--runtime=runsc`, `--network none`,
  лимиты, non-root UID, монтирует только workspace, НЕ пробрасывает docker.sock;
  fallback на runc работает и логирует WARN.
- `tests/sandbox/test_escape.py` — «побеги»: сеть выключена (fail), запись вне workspace
  (fail), fork-бомба гасится `--pids-limit`.

> Спавн контейнеров — только с хост-стороны Worker. Проброс docker.sock внутрь
> контейнера тула ЗАПРЕЩЁН. Sysbox/сокет-брокер — тема P4.
