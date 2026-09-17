from __future__ import annotations

if __name__ == "__main__" and not __package__:
    print(
        "\n❌ Не запускайте этот файл напрямую "
        "(`python cli.py ...`) — так src/antigona/ попадает в sys.path "
        "и конфликтует со стандартным модулем `queue`, "
        "из-за чего импорт валится с неочевидной ошибкой при запуске "
        "chat/tui/panel.\n\n"
        "Используйте штатный вход:\n"
        "  antigona chat|tui|panel|...   (после `uv sync` / `pip install -e .`)\n"
        "  python -m antigona.cli chat  (запуск из корня проекта без установки)\n"
    )
    raise SystemExit(1)

import asyncio
import json
import os
import pathlib
import traceback
import uuid
from typing import Annotated, Any

import httpx
import typer
from rich.console import Console

from antigona import __version__
from antigona.config import load_project_env as _load_project_env
from antigona.core import paths
from antigona.core.gateway_client import GatewayClient as GatewayClient
from antigona.security.auth_service import cli_principal

app = typer.Typer(name="antigona", help="Antigona CLI — Gateway Client", add_completion=False)
console = Console()


def _cli_version() -> str:
    """Return the version exposed by the Antigona package."""
    return __version__


def _version_option(value: bool) -> None:
    if value:
        typer.echo(_cli_version())
        raise typer.Exit()


@app.callback()
def _root_callback(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_option,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = False,
) -> None:
    """Antigona CLI — Gateway Client."""

TERMINAL_STATES = {
    "DONE",
    "FAILED",
    "BLOCKED",
    "CANCELLED",
    "TIMEOUT",
    "POLICY_DENIED",
}


class CLIStateStore:
    def __init__(self, path: str | None = None) -> None:
        if path:
            self.path = pathlib.Path(path)
        else:
            env_path = os.getenv("ANTIGONA_STATE_FILE")
            if env_path:
                self.path = pathlib.Path(env_path)
            else:
                self.path = paths.cli_state_file()

    def _ensure_dir(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"flows": {}, "last_seq": 0}
        try:
            with open(self.path, encoding="utf-8") as f:
                data: dict[str, Any] = json.load(f)
                return data
        except Exception:
            return {"flows": {}, "last_seq": 0}

    def save(self, data: dict[str, Any]) -> None:
        self._ensure_dir()
        temp_file = self.path.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        temp_file.replace(self.path)

    def record_flow(
        self,
        flow_id: str,
        correlation_id: str,
        goal: str,
        status: str = "RECEIVED",
        last_seq: int = 0,
    ) -> None:
        state = self.load()
        flows = state.setdefault("flows", {})
        existing = flows.get(flow_id, {})
        existing.update(
            {
                "flow_id": flow_id,
                "correlation_id": correlation_id,
                "goal": goal,
                "status": status,
                "last_seq": max(existing.get("last_seq", 0), last_seq),
            }
        )
        flows[flow_id] = existing
        state["last_seq"] = max(state.get("last_seq", 0), last_seq)
        self.save(state)

    def update_flow_status(self, flow_id: str, status: str, seq: int = 0) -> None:
        state = self.load()
        flows = state.setdefault("flows", {})
        if flow_id in flows:
            flows[flow_id]["status"] = status
            if seq > 0:
                flows[flow_id]["last_seq"] = max(flows[flow_id].get("last_seq", 0), seq)
        if seq > 0:
            state["last_seq"] = max(state.get("last_seq", 0), seq)
        self.save(state)

    def get_flow(self, flow_id: str) -> dict[str, Any] | None:
        state = self.load()
        flows: dict[str, Any] = state.get("flows", {})
        res: dict[str, Any] | None = flows.get(flow_id)
        return res


def _handle_gateway_error(gateway_url: str, exc: Exception) -> None:
    del gateway_url  # A configured URL can itself contain credentials.
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 401:
            msg = "[bold red]Authentication error (401). Проверьте --token[/bold red]"
        elif status == 409:
            msg = (
                "[bold red]Idempotency conflict (409). "
                "Повторите с уникальным idempotency key.[/bold red]"
            )
        elif 422 <= status < 500:
            msg = f"[bold red]Validation error ({status}); details withheld: [REDACTED][/bold red]"
        elif 500 <= status < 600:
            msg = f"[bold red]Server error ({status}); details withheld: [REDACTED][/bold red]"
        else:
            msg = f"[bold red]HTTP {status}; details withheld: [REDACTED][/bold red]"
        console.print(msg)
    elif isinstance(exc, (ConnectionError, httpx.ConnectError, httpx.ConnectTimeout)):
        console.print(
            "[bold red]Gateway недоступен (connection refused). "
            "Проверьте, что стек запущен.[/bold red]"
        )
    elif isinstance(exc, httpx.TimeoutException):
        console.print("[bold red]Gateway не ответил за таймаут.[/bold red]")
    else:
        console.print(
            "[bold red]Gateway request failed; details withheld: [REDACTED]. "
            "Проверьте, что стек запущен командой ./run.sh[/bold red]"
        )
    raise typer.Exit(code=1)


def _env_token_from_env_file() -> str:
    """ANTIGONA_GATEWAY_TOKEN из .env (cwd, затем owner_dir). Значение не логируется."""
    candidates: list[pathlib.Path] = [paths.runtime_dir() / ".env"]
    try:
        candidates.append(pathlib.Path(paths.owner_dir()) / ".env")
    except Exception:
        pass
    for env_path in candidates:
        try:
            if not env_path.exists():
                continue
            for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("ANTIGONA_GATEWAY_TOKEN="):
                    return line.split("=", 1)[1].strip().strip("\"'")
        except OSError:
            continue
    return ""


def _resolve_gateway_token(explicit: str | None) -> str:
    """D9: токен из --token → env → .env. Без токена — понятная ошибка, не 401."""
    if explicit:
        return explicit
    env_tok = os.getenv("ANTIGONA_GATEWAY_TOKEN")
    if env_tok:
        return env_tok
    file_tok = _env_token_from_env_file()
    if file_tok:
        return file_tok
    console.print(
        "[bold red]Error: Gateway token is required "
        "(--token or ANTIGONA_GATEWAY_TOKEN in env/.env)[/bold red]"
    )
    raise typer.Exit(code=1)


def _resolve_gateway_url(explicit: str | None) -> str:
    if explicit:
        return explicit
    return os.getenv("ANTIGONA_GATEWAY_URL") or "http://127.0.0.1:8090"


async def _stream_events_ws(gateway_url: str, token: str, flow_id: str | None = None) -> None:
    client = GatewayClient(gateway_url, token)
    store = CLIStateStore()
    last_seq = 0
    if flow_id:
        f_info = store.get_flow(flow_id)
        if f_info:
            last_seq = f_info.get("last_seq", 0)

    console.print(f"[bold cyan]Subscribing to WS events stream (seq={last_seq})...[/bold cyan]")
    try:
        async for data in client.connect_events(after_seq=last_seq):
            msg_type = data.get("type")
            ev_flow_id = data.get("flow_id", "")
            seq = data.get("seq", 0)

            if flow_id and ev_flow_id != flow_id:
                continue

            if msg_type == "transition":
                from_s = data.get("from_state") or "NONE"
                to_s = data.get("to_state", "UNKNOWN")
                reason = data.get("reason", "")
                actor = data.get("actor", "")
                cid = data.get("correlation_id", "")
                console.print(
                    f"[yellow]Transition:[/yellow] [{ev_flow_id[:8]}] {from_s} ➔ [bold green]{to_s}[/bold green] "
                    f"(actor={actor}, reason={reason}, cid={str(cid)[:8]}, seq={seq})"
                )
                store.update_flow_status(ev_flow_id, to_s, seq=seq)
                if flow_id and ev_flow_id == flow_id and to_s in TERMINAL_STATES:
                    console.print(
                        f"[bold green]Flow {flow_id} completed with status: {to_s}[/bold green]"
                    )
                    break
    except (ConnectionError, httpx.HTTPError, OSError) as exc:
        _handle_gateway_error(gateway_url, exc)
    except Exception as exc:
        console.print("[bold red]WS stream error; details withheld: [REDACTED][/bold red]")
        raise typer.Exit(code=1) from exc


async def _stream_ws(gateway_url: str, token: str, flow_id: str) -> None:
    await _stream_events_ws(gateway_url, token, flow_id)


@app.command()
def run(
    goal: Annotated[
        str | None, typer.Argument(help="Task goal text (или -g/--goal)")
    ] = None,
    goal_opt: Annotated[
        str | None, typer.Option("--goal", "-g", help="Task goal text")
    ] = None,
    path: Annotated[str | None, typer.Option("--path", "-p", help="Target path")] = None,
    content: Annotated[
        str | None, typer.Option("--content", "-c", help="File content")
    ] = None,
    tool: Annotated[str | None, typer.Option("--tool", "-t", help="Tool name")] = None,
    command: Annotated[
        list[str] | None, typer.Option("--command", help="Command for shell tool")
    ] = None,
    gateway_url: Annotated[
        str | None, typer.Option("--gateway", help="Gateway URL")
    ] = None,
    token: Annotated[str | None, typer.Option("--token", help="Bearer token")] = None,
    attach_stream: Annotated[
        bool, typer.Option("--attach/--no-attach", help="Attach live WS stream")
    ] = True,
) -> None:
    """Run a task: positional goal or -g, intent/path/content extracted from
    the goal text (D7/D8); dialogue intents get a conversation reply instead
    of a write flow."""
    goal_text = (goal or goal_opt or "").strip()
    if not goal_text:
        console.print(
            "[bold red]Error: цель задачи обязательна "
            "(позиционный аргумент или -g/--goal)[/bold red]"
        )
        raise typer.Exit(code=2)
    url = _resolve_gateway_url(gateway_url)
    tok = _resolve_gateway_token(token)
    client = GatewayClient(url, tok)
    store = CLIStateStore()
    cid = str(uuid.uuid4())

    async def _run() -> None:
        # D7/D8: извлечение path/content/intent из текста цели, когда явные
        # аргументы (--tool/--path/--content/--command) не переданы.
        plan = None
        if tool is None and path is None and content is None and command is None:
            from antigona.task_goal import parse_goal

            plan = parse_goal(goal_text)

        if plan is not None and plan.intent == "dialog":
            console.print(
                "[bold blue]Dialogue intent detected — routing to conversation...[/bold blue]"
            )
            try:
                res = await client.send_dialogue_turn(
                    goal_text, session_id="cli-run", channel="cli", user_id=cli_principal()
                )
            except Exception as exc:
                _handle_gateway_error(url, exc)
            console.print((res.get("reply") or "").strip() or "(пустой ответ)")
            return

        effective_tool = tool or (plan.tool_name if plan else "workspace.write_text")
        effective_path = path or (plan.path if plan and plan.path else "task_output.txt")
        effective_content = content if content is not None else (plan.content if plan else "")
        effective_command = list(command) if command else []
        # B5: составное «создай скрипт → запусти его» НЕ превращается в
        # `sh -c "<цель>"`: файл пишется по названному пути, а запуск уходит
        # отдельным шагом (run_command).
        run_command: list[str] = []
        fix_command: list[str] = []
        fix_after_run = False
        fix_content = ""
        if plan is not None and plan.intent == "file_write_fix_run" and plan.command:
            from antigona.task_goal import strip_code_fences

            if plan.content:
                effective_content = strip_code_fences(plan.content, path=plan.path or effective_path)
            run_command = plan.command.split()
            fix_command = (plan.fix_command or plan.command).split()
            fix_after_run = True
            if plan.fix_content:
                fix_content = strip_code_fences(plan.fix_content, path=plan.path or effective_path)
            else:
                from antigona.conversation.dialogue_engine import DialogueEngine

                engine = DialogueEngine()
                fix_prompt = (
                    f"Исправь ошибку в коде файла {effective_path} согласно требованию:\n"
                    f"{plan.content_hint or goal_text}\n\n"
                    f"Исходный код:\n{effective_content or ''}\n\n"
                    f"Выведи только исправленный код."
                )
                try:
                    with_status = getattr(engine, "draft_file_content_result", None)
                    if with_status is not None:
                        draft = await with_status(fix_prompt, session_id="cli-run")
                        drafted_fix = draft.content
                    else:
                        drafted_fix = await engine.draft_file_content(fix_prompt, session_id="cli-run")
                    if not drafted_fix:
                        if with_status is not None:
                            draft = await with_status(goal_text, session_id="cli-run")
                            drafted_fix = draft.content
                        else:
                            drafted_fix = await engine.draft_file_content(goal_text, session_id="cli-run")
                    if drafted_fix is not None:
                        fix_content = strip_code_fences(drafted_fix, path=effective_path)
                    else:
                        fix_content = ""
                except Exception as exc:
                    console.print(
                        f"[bold yellow]Warning: CLI fix draft failed: {exc}[/bold yellow]"
                    )
                    fix_content = ""
                finally:
                    await engine.close()

            if not fix_content:
                console.print(
                    "[bold red]Error: не удалось сформировать исправленный код для шага исправления "
                    "(LLM недоступен или вернул пустой ответ)[/bold red]"
                )
                raise typer.Exit(code=1)
        elif plan is not None and plan.intent == "file_write_run" and plan.command:
            run_command = plan.command.split()
        elif not effective_command and plan is not None and plan.command:
            effective_command = ["sh", "-c", plan.command]
        read_after_write = bool(plan and plan.read_after_write)
        run_after_write = bool(run_command)

        # BUG ANT-003: stdout-only shell (echo/cat) не должен читать файл.
        if (
            effective_tool == "sandbox.shell"
            and path is None
            and not effective_content.strip()
            and effective_path in ("", "task_output.txt")
        ):
            effective_path = "stdout"

        console.print("[bold blue]Creating flow on Gateway...[/bold blue]")
        try:
            res = await client.create_flow(
                goal=goal_text,
                path=effective_path,
                content=effective_content,
                tool_name=effective_tool,
                command=effective_command,
                read_after_write=read_after_write,
                run_after_write=run_after_write,
                run_command=run_command,
                fix_after_run=fix_after_run,
                fix_content=fix_content,
                fix_command=fix_command,
                correlation_id=cid,
                idempotency_key=f"cli-{uuid.uuid4().hex[:12]}",
            )
        except Exception as exc:
            _handle_gateway_error(url, exc)

        flow_id = str(res["id"])
        flow_cid = str(res.get("correlation_id", cid))
        status = str(res.get("status", "RECEIVED"))
        console.print(
            f"[bold green]Flow Created:[/bold green] ID={flow_id}, CorrelationID={flow_cid}, Status={status}"
        )

        store.record_flow(flow_id, flow_cid, goal_text, status)

        if attach_stream:
            await _stream_events_ws(url, tok, flow_id)

        # P0-030 read delivery: read-задача не завершена для пользователя, пока
        # содержимое файла не доставлено в stdout. Flow может быть DONE, а
        # пользователь так и не увидел прочитанное — это не успех.
        if plan is not None and plan.intent == "file_read":
            try:
                result = await client.get_result(flow_id)
                payload = (result.stdout_preview or result.safe_result_text or "").strip()
                if payload:
                    console.print(payload)
            except Exception as exc:  # noqa: BLE001 - доставка результата не должна ронять команду
                _handle_gateway_error(url, exc)

    asyncio.run(_run())


@app.command()
def attach(
    flow_id: Annotated[str, typer.Argument(help="Flow ID to attach")],
    gateway_url: Annotated[
        str | None, typer.Option("--gateway", help="Gateway URL")
    ] = None,
    token: Annotated[str | None, typer.Option("--token", help="Bearer token")] = None,
) -> None:
    url = _resolve_gateway_url(gateway_url)
    tok = _resolve_gateway_token(token)
    asyncio.run(_stream_events_ws(url, tok, flow_id))


@app.command()
def cancel(
    flow_id: Annotated[str, typer.Argument(help="Flow ID to cancel")],
    gateway_url: Annotated[
        str | None, typer.Option("--gateway", help="Gateway URL")
    ] = None,
    token: Annotated[str | None, typer.Option("--token", help="Bearer token")] = None,
) -> None:
    url = _resolve_gateway_url(gateway_url)
    tok = _resolve_gateway_token(token)
    client = GatewayClient(url, tok)

    async def _cancel() -> None:
        try:
            res = await client.cancel_flow(flow_id)
            console.print(f"[bold yellow]Flow {flow_id} status:[/bold yellow] {res.get('status')}")
        except Exception as exc:
            _handle_gateway_error(url, exc)

    asyncio.run(_cancel())


@app.command()
def approve(
    approval_id: Annotated[str, typer.Argument(help="Approval ID")],
    yes: Annotated[
        bool, typer.Option("--yes/--no", help="Approve (--yes) or reject (--no)")
    ] = True,
    deny: Annotated[
        bool, typer.Option("--deny", help="Deny approval instead of approving")
    ] = False,
    gateway_url: Annotated[
        str | None, typer.Option("--gateway", help="Gateway URL")
    ] = None,
    token: Annotated[str | None, typer.Option("--token", help="Bearer token")] = None,
) -> None:
    url = _resolve_gateway_url(gateway_url)
    tok = _resolve_gateway_token(token)
    client = GatewayClient(url, tok)
    approve_val = False if deny else yes

    async def _approve() -> None:
        try:
            res = await client.decide_approval(approval_id, approve=approve_val)
            decision = res.decision
            console.print(
                f"[bold green]Approval {approval_id} decided:[/bold green] decision={decision}"
            )
        except Exception as exc:
            _handle_gateway_error(url, exc)

    asyncio.run(_approve())


# --- Skills CLI Subcommands ---

skills_app = typer.Typer(name="skills", help="Manage Antigona skills")
app.add_typer(skills_app, name="skills")


@skills_app.command("validate")
def validate_skill(
    file_path: Annotated[str, typer.Argument(help="Path to .askill file")],
) -> None:
    from pathlib import Path

    from antigona.skills.canonical import verify_footer
    from antigona.skills.errors import SkillFormatError, SkillIntegrityError
    from antigona.skills.format import parse_card

    path = Path(file_path)
    if not path.exists():
        console.print(f"[bold red]File not found:[/bold red] {path}")
        raise typer.Exit(1)

    card_bytes = path.read_bytes()
    try:
        digest, bytes_len = verify_footer(card_bytes)
        card = parse_card(card_bytes)
    except (SkillFormatError, SkillIntegrityError, ValueError) as exc:
        console.print("[bold red]Validation failed; details withheld: [REDACTED][/bold red]")
        raise typer.Exit(1) from exc

    console.print("[bold green]Skill card is VALID (ASKILL/1)[/bold green]")
    console.print(f"ID: {card.skill_id}")
    console.print(f"Slug: {card.slug} (v{card.version})")
    console.print(f"Trust: {card.trust.value}, Risk: {card.risk.value}")
    console.print(f"Steps: {len(card.plan)}")
    console.print(f"SHA256: {digest} ({bytes_len} bytes)")


@skills_app.command("list")
def list_skills(
    gateway_url: Annotated[
        str, typer.Option("--gateway", help="Gateway URL")
    ] = "http://127.0.0.1:8090",
    token: Annotated[str, typer.Option("--token", help="Bearer token")] = "gateway-token",
) -> None:
    async def _list() -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{gateway_url.rstrip('/')}/skills",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            skills: list[dict[str, Any]] = resp.json()
            for s in skills:
                console.print(
                    f"[{s['id']}] [bold]{s['slug']}[/bold] v{s['version']} "
                    f"status=[yellow]{s['status']}[/yellow] trust={s['trust']} risk={s['risk_ceiling']}"
                )

    asyncio.run(_list())


@skills_app.command("show")
def show_skill(
    skill_id: Annotated[str, typer.Argument(help="Skill ID")],
    gateway_url: Annotated[
        str, typer.Option("--gateway", help="Gateway URL")
    ] = "http://127.0.0.1:8090",
    token: Annotated[str, typer.Option("--token", help="Bearer token")] = "gateway-token",
) -> None:
    async def _show() -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{gateway_url.rstrip('/')}/skills/{skill_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            s: dict[str, Any] = resp.json()
            console.print(f"[bold green]Skill Detail:[/bold green] {s['id']}")
            console.print(f"Slug: {s['slug']} (v{s['version']})")
            console.print(f"Status: {s['status']}")
            console.print(f"Trust: {s['trust']} | Risk Ceiling: {s['risk_ceiling']}")
            if s.get("body"):
                console.print("\n[bold]Card Body:[/bold]\n" + str(s["body"]))

    asyncio.run(_show())


@skills_app.command("promote")
def promote_skill(
    skill_id: Annotated[str, typer.Argument(help="Skill ID to promote")],
    revision: Annotated[int, typer.Option("--revision", "-r", help="Current revision")] = 0,
    gateway_url: Annotated[
        str, typer.Option("--gateway", help="Gateway URL")
    ] = "http://127.0.0.1:8090",
    token: Annotated[str, typer.Option("--token", help="Bearer token")] = "gateway-token",
) -> None:
    async def _promote() -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{gateway_url.rstrip('/')}/skills/{skill_id}/promote",
                json={"revision": revision, "correlation_id": "cli-promote"},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            res: dict[str, Any] = resp.json()
            console.print(
                f"[bold green]Skill Promoted:[/bold green] status={res.get('status', 'ACTIVE')}"
            )

    asyncio.run(_promote())


@skills_app.command("capture")
def capture_skill(
    flow_id: Annotated[str, typer.Argument(help="Completed task flow ID")],
    gateway_url: Annotated[
        str, typer.Option("--gateway", help="Gateway URL")
    ] = "http://127.0.0.1:8090",
    token: Annotated[str, typer.Option("--token", help="Bearer token")] = "gateway-token",
    state_root: Annotated[
        str, typer.Option("--state-root", help="State root directory")
    ] = "./state",
) -> None:
    """Capture навыка — ТОЛЬКО через Gateway (Step 3: ядро владеет БД)."""
    async def _capture() -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{gateway_url.rstrip('/')}/skills/capture",
                json={"flow_id": flow_id, "state_root": state_root},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            res: dict[str, Any] = resp.json()
            console.print(
                f"[bold green]Captured Skill:[/bold green] ID={res.get('id')}, "
                f"Slug={res.get('slug')}, Status={res.get('status')}"
            )

    asyncio.run(_capture())


@skills_app.command("deprecate")
def deprecate_skill(
    skill_id: Annotated[str, typer.Argument(help="Skill ID to deprecate")],
    gateway_url: Annotated[
        str, typer.Option("--gateway", help="Gateway URL")
    ] = "http://127.0.0.1:8090",
    token: Annotated[str, typer.Option("--token", help="Bearer token")] = "gateway-token",
) -> None:
    """Deprecate навыка — ТОЛЬКО через Gateway (Step 3: ядро владеет БД)."""
    async def _deprecate() -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{gateway_url.rstrip('/')}/skills/{skill_id}/deprecate",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            res: dict[str, Any] = resp.json()
            console.print(
                f"[bold yellow]Skill Deprecated:[/bold yellow] ID={res.get('id')}, "
                f"Status={res.get('status')}"
            )

    asyncio.run(_deprecate())


# ── Cron CLI Subcommands ─────────────────────────────────────────────

cron_app = typer.Typer(name="cron", help="Manage cron schedules")
app.add_typer(cron_app)

# GatewayClient cron methods are defined inline in each command.


@cron_app.command("create")
def cron_create(
    name: Annotated[str, typer.Option("--name", "-n", help="Schedule name")],
    cron_expression: Annotated[str, typer.Option("--cron", "-c", help="5-field cron expression")],
    goal: Annotated[str, typer.Option("--goal", "-g", help="Task goal")],
    target_path: Annotated[str, typer.Option("--path", "-p", help="Target path")] = ".",
    content: Annotated[str, typer.Option("--content", help="Task content")] = "",
    tool: Annotated[str, typer.Option("--tool", "-t", help="Tool name")] = "workspace.write_text",
    gateway_url: Annotated[
        str, typer.Option("--gateway", help="Gateway URL")
    ] = "http://127.0.0.1:8090",
    token: Annotated[str, typer.Option("--token", help="Bearer token")] = "gateway-token",
) -> None:
    async def _create() -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{gateway_url.rstrip('/')}/schedules",
                json={
                    "name": name,
                    "cron_expression": cron_expression,
                    "goal": goal,
                    "target_path": target_path,
                    "content": content,
                    "tool_name": tool,
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 422:
                console.print(f"[bold red]Validation error:[/bold red] {resp.text}")
                raise typer.Exit(1)
            resp.raise_for_status()
            s: dict[str, Any] = resp.json()
            console.print(
                f"[bold green]Schedule created:[/bold green] ID={s['id']}, name={s['name']}, next_run={s.get('next_run_at')}"
            )

    asyncio.run(_create())


@cron_app.command("list")
def cron_list(
    gateway_url: Annotated[
        str, typer.Option("--gateway", help="Gateway URL")
    ] = "http://127.0.0.1:8090",
    token: Annotated[str, typer.Option("--token", help="Bearer token")] = "gateway-token",
) -> None:
    async def _list() -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{gateway_url.rstrip('/')}/schedules",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            schedules: list[dict[str, Any]] = resp.json()
            if not schedules:
                console.print("[yellow]No schedules found.[/yellow]")
                return
            for s in schedules:
                cancelled = "[red]CANCELLED[/red]" if s.get("cancelled") else ""
                disabled = (
                    "[yellow]DISABLED[/yellow]"
                    if not s.get("enabled") and not s.get("cancelled")
                    else ""
                )
                flags = " ".join(filter(None, [cancelled, disabled]))
                console.print(
                    f"[{s['id']}] [bold]{s['name']}[/bold] "
                    f"cron={s['cron_expression']} next_run={s.get('next_run_at', '?')} {flags}"
                )

    asyncio.run(_list())


@cron_app.command("show")
def cron_show(
    schedule_id: Annotated[str, typer.Argument(help="Schedule ID")],
    gateway_url: Annotated[
        str, typer.Option("--gateway", help="Gateway URL")
    ] = "http://127.0.0.1:8090",
    token: Annotated[str, typer.Option("--token", help="Bearer token")] = "gateway-token",
) -> None:
    async def _show() -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{gateway_url.rstrip('/')}/schedules/{schedule_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 404:
                console.print(f"[bold red]Schedule not found:[/bold red] {schedule_id}")
                raise typer.Exit(1)
            resp.raise_for_status()
            s: dict[str, Any] = resp.json()
            console.print(f"[bold green]Schedule:[/bold green] {s['id']}")
            console.print(f"Name: {s['name']}")
            console.print(f"Cron: {s['cron_expression']}")
            console.print(f"Goal: {s['goal']}")
            console.print(f"Target path: {s['target_path']}")
            console.print(f"Tool: {s['tool_name']}")
            console.print(f"Enabled: {s['enabled']}")
            console.print(f"Cancelled: {s['cancelled']}")
            console.print(f"Next run: {s.get('next_run_at', 'N/A')}")
            console.print(f"Last run: {s.get('last_run_at', 'N/A')}")
            console.print(f"Created: {s['created_at']}")

    asyncio.run(_show())


@cron_app.command("jobs")
def cron_jobs(
    schedule_id: Annotated[str, typer.Argument(help="Schedule ID")],
    gateway_url: Annotated[
        str, typer.Option("--gateway", help="Gateway URL")
    ] = "http://127.0.0.1:8090",
    token: Annotated[str, typer.Option("--token", help="Bearer token")] = "gateway-token",
) -> None:
    async def _jobs() -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{gateway_url.rstrip('/')}/schedules/{schedule_id}/jobs",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 404:
                console.print(f"[bold red]Schedule not found:[/bold red] {schedule_id}")
                raise typer.Exit(1)
            resp.raise_for_status()
            flows: list[dict[str, Any]] = resp.json()
            if not flows:
                console.print("[yellow]No task flows created by this schedule yet.[/yellow]")
                return
            for f in flows:
                console.print(
                    f"[{f['id']}] goal={f['goal']} status={f['status']} created={f.get('created_at', '?')}"
                )

    asyncio.run(_jobs())


@cron_app.command("cancel")
def cron_cancel(
    schedule_id: Annotated[str, typer.Argument(help="Schedule ID to cancel")],
    gateway_url: Annotated[
        str, typer.Option("--gateway", help="Gateway URL")
    ] = "http://127.0.0.1:8090",
    token: Annotated[str, typer.Option("--token", help="Bearer token")] = "gateway-token",
) -> None:
    async def _cancel() -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{gateway_url.rstrip('/')}/schedules/{schedule_id}/cancel",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 404:
                console.print(f"[bold red]Schedule not found:[/bold red] {schedule_id}")
                raise typer.Exit(1)
            resp.raise_for_status()
            s: dict[str, Any] = resp.json()
            console.print(
                f"[bold yellow]Schedule cancelled:[/bold yellow] ID={s['id']}, name={s['name']}"
            )

    asyncio.run(_cancel())


@cron_app.command("tick")
def cron_tick(
    gateway_url: Annotated[
        str, typer.Option("--gateway", help="Gateway URL")
    ] = "http://127.0.0.1:8090",
    token: Annotated[str, typer.Option("--token", help="Bearer token")] = "gateway-token",
) -> None:
    async def _tick() -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{gateway_url.rstrip('/')}/schedules/tick",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            console.print(
                f"[bold green]Tick executed:[/bold green] tasks_created={result.get('tasks_created', 0)}"
            )

    asyncio.run(_tick())


# ── Replay CLI Command ───────────────────────────────────────────────


@app.command("replay")
def replay(
    flow_id: Annotated[str, typer.Argument(help="Flow ID to replay")],
    timeline: Annotated[bool, typer.Option("--timeline", help="Flat chronological view")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Raw JSON, no formatting")] = False,
    actor: Annotated[
        str | None, typer.Option("--actor", help="Filter transitions by actor")
    ] = None,
    entity_type: Annotated[
        str | None, typer.Option("--entity-type", help="Filter by entity type (task|step)")
    ] = None,
    from_dt: Annotated[
        str | None, typer.Option("--from", help="Filter transitions from this ISO timestamp")
    ] = None,
    to_dt: Annotated[
        str | None, typer.Option("--to", help="Filter transitions up to this ISO timestamp")
    ] = None,
    gateway_url: Annotated[
        str | None, typer.Option("--gateway", help="Gateway URL")
    ] = None,
    token: Annotated[str | None, typer.Option("--token", help="Bearer token")] = None,
) -> None:
    url = _resolve_gateway_url(gateway_url)
    tok = _resolve_gateway_token(token)
    client = GatewayClient(url, tok)

    async def _replay() -> None:
        try:
            if timeline:
                payload = await client.get_replay_timeline(flow_id)
            else:
                payload = await client.get_replay(flow_id, actor, entity_type, from_dt, to_dt)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                console.print(f"[bold red]Flow not found:[/bold red] {flow_id}")
                raise typer.Exit(1) from exc
            console.print("[bold red]Replay failed; details withheld: [REDACTED][/bold red]")
            raise typer.Exit(1) from exc

        if json_output:
            # Plain stdout for piping: `antigona replay <id> --json > replay.json`
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return

        if timeline:
            entries = payload.get("entries", [])
            console.print(f"[bold]Timeline for {flow_id}:[/bold]")
            for e in entries:
                e_type = e.get("type", "?")
                ts = e.get("timestamp", "?")
                desc = e.get("description", "")
                style = {"transition": "cyan", "rejected": "yellow", "artifact": "green"}.get(
                    e_type, "white"
                )
                console.print(f"  {ts}  [{style}][{e_type}][/]  {desc}")
        else:
            # Re-render trajectory via local ReplayEngine for Rich formatting
            # Fallback to raw JSON output if local rendering unavailable
            console.print(f"[bold]Flow Replay: {flow_id}[/bold]")
            console.print(f"  Goal: {payload.get('goal', '?')}")
            status = payload.get("status", "?")
            console.print(f"  Status: {status}   Revision: {payload.get('revision', '?')}")
            console.print(
                f"  Owner: {payload.get('owner_id', '?')}   Path: {payload.get('target_path', '?')}"
            )
            console.print(
                f"  Created: {payload.get('created_at', '?')}   Updated: {payload.get('updated_at', '?')}"
            )
            console.print()

            transitions = payload.get("transitions", [])
            if transitions:
                console.print("[bold]State Transitions:[/bold]")
                for tr in transitions:
                    is_rejected = tr.get("from_state") == tr.get("to_state")
                    style = "yellow" if is_rejected else ""
                    console.print(
                        f"  [{tr.get('id')}] {tr.get('from_state') or 'NONE'} -> "
                        f"{tr.get('to_state')} | actor={tr.get('actor')} | "
                        f"reason: {tr.get('reason', '')}",
                        style=style,
                    )
            else:
                console.print("[dim](No transitions recorded)[/dim]")

            steps = payload.get("steps", [])
            if steps:
                console.print()
                console.print("[bold]Flow Steps:[/bold]")
                for step in steps:
                    console.print(
                        f"  Step #{step.get('index')} ({step.get('title')}) "
                        f"[{step.get('status')}] (retries={step.get('retries')})"
                    )
                    console.print(f"    Input: {step.get('input')}")
                    if step.get("output") is not None:
                        console.print(f"    Output: {step.get('output')}")

            artifacts = payload.get("artifacts", [])
            if artifacts:
                console.print()
                console.print("[bold]Artifacts:[/bold]")
                for a in artifacts:
                    mark = "✅" if a.get("verified") else "❌"
                    console.print(
                        f"  Artifact {a.get('id', '?')}: {a.get('path')} "
                        f"(sha256={str(a.get('sha256', ''))[:12]}..., "
                        f"size={a.get('size', 0)}b) {mark}"
                    )

    asyncio.run(_replay())


# ── Durable storage (P4.3) ───────────────────────────────────────────

db_app = typer.Typer(name="db", help="Durable storage: Alembic migrations")
app.add_typer(db_app, name="db")


@db_app.command("upgrade")
def db_upgrade(
    db_url: Annotated[str, typer.Option("--db-url", help="Async DB URL; default from env")] = "",
) -> None:
    """Run ``alembic upgrade head`` against the configured durable storage."""
    from .config import Settings
    from .storage.migrator import head_revision, upgrade_to_head

    target = db_url or Settings.from_env().async_db_url()
    masked = upgrade_to_head(target)
    console.print(f"[green]upgraded[/green] {masked} → revision {head_revision()}")


# ── TUI CLI Command ──────────────────────────────────────────────────


@app.command("tui")
def tui(
    gateway_url: Annotated[
        str, typer.Option("--gateway", help="Gateway URL")
    ] = "http://127.0.0.1:8090",
    token: Annotated[str, typer.Option("--token", help="Bearer token")] = "gateway-token",
    refresh: Annotated[
        float, typer.Option("--refresh", help="Auto-refresh interval, seconds")
    ] = 5.0,
) -> None:
    """Launch the Textual TUI client (Flows / Live / Approvals / Replay)."""
    try:
        from .tui import AntigonaApp
    except ModuleNotFoundError:
        typer.echo(
            "\u274c The 'tui' command requires the optional 'textual' package.\n"
            "   Install it with:  pip install 'antigona[tui]'\n"
            "   Or use the Living CLI:  antigona chat",
            err=True,
        )
        raise typer.Exit(code=1) from None

    AntigonaApp(gateway_url=gateway_url, token=token, refresh_interval=refresh).run()


@app.command("panel")
def console_panel(
    gateway_url: Annotated[
        str | None, typer.Option("--gateway", help="Gateway URL")
    ] = None,
    token: Annotated[
        str | None,
        typer.Option(
            "--token",
            help="Bearer token (--token may be visible through shell history and process argv)",
        ),
    ] = None,
    refresh: Annotated[
        float, typer.Option("--refresh", help="Refresh/animation interval, seconds")
    ] = 1.0,
    session_id: Annotated[
        str | None,
        typer.Option("--session-id", "-s", help="Session ID for chat history persistence"),
    ] = None,
) -> None:
    """Launch the unified animated Antigona Console (Chat + Activity + Flows + Approvals).

    Live chat with the agent (orchestrator/executor roles), real-time monitor of
    what the agent is doing, approvals, and an animated status bar — all over the
    canonical Gateway surface.
    """
    try:
        from antigona.tui_console import AntigonaConsole
    except ModuleNotFoundError:
        typer.echo(
            "\u274c The 'panel' command requires the optional 'textual' package.\n"
            "   Install it with:  pip install 'antigona[tui]'\n"
            "   Or use the Living CLI:  antigona chat",
            err=True,
        )
        raise typer.Exit(code=1) from None

    env_url = os.getenv("ANTIGONA_GATEWAY_URL")
    url = gateway_url or env_url or "http://127.0.0.1:8090"
    env_tok = os.getenv("ANTIGONA_GATEWAY_TOKEN")
    if not env_tok:
        env_path = pathlib.Path(".env")
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("ANTIGONA_GATEWAY_TOKEN="):
                    env_tok = line.split("=", 1)[1].strip().strip("\"'")
                    break
    tok = token or env_tok or ""
    if not tok:
        console_print = typer.echo
        console_print("[bold red]Error: Gateway token is required (--token or ANTIGONA_GATEWAY_TOKEN)[/bold red]")
        raise typer.Exit(code=1)
    sess = session_id or os.getenv("ANTIGONA_SESSION_ID") or "cli-session"
    AntigonaConsole(
        gateway_url=url,
        token=tok,
        refresh_interval=refresh,
        session_id=sess,
    ).run()


# ── Chat CLI Command ─────────────────────────────────────────────────


@app.command("chat")
def chat(
    gateway_url: Annotated[str | None, typer.Option("--gateway", help="Gateway URL")] = None,
    token: Annotated[
        str | None,
        typer.Option(
            "--token",
            help="Bearer token (--token may be visible through shell history and process argv)",
        ),
    ] = None,
    no_anim: Annotated[bool, typer.Option("--no-anim", help="Disable status animations")] = False,
    session_id: Annotated[
        str | None,
        typer.Option("--session-id", "-s", help="Session ID for chat history persistence"),
    ] = None,
) -> None:
    """Launch interactive task-oriented chat session with Gateway."""
    from antigona.cli_ui.chat import ChatController
    from antigona.cli_ui.renderer import CliRenderer
    from antigona.core.gateway_client import GatewayClient as CoreGatewayClient

    env_url = os.getenv("ANTIGONA_GATEWAY_URL")
    url = gateway_url or env_url or "http://127.0.0.1:8090"
    env_tok = os.getenv("ANTIGONA_GATEWAY_TOKEN")
    if not env_tok:
        # Fallback: try reading from .env file directly
        env_path = pathlib.Path(".env")
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("ANTIGONA_GATEWAY_TOKEN="):
                    env_tok = line.split("=", 1)[1].strip().strip("\"'")
                    break
    tok = token or env_tok
    if not tok:
        console.print(
            "[bold red]Error: Gateway token is required (--token or ANTIGONA_GATEWAY_TOKEN)[/bold red]"
        )
        raise typer.Exit(code=1)

    sess = session_id or os.getenv("ANTIGONA_SESSION_ID") or "cli-session"

    client = CoreGatewayClient(base_url=url, token=tok)
    renderer = CliRenderer()
    controller = ChatController(
        gateway=client,
        renderer=renderer,
        conversation_id=sess,
        enable_animations=not no_anim,
        gateway_url=url,
    )

    try:
        asyncio.run(_run_chat_with_layout(controller))
    except (KeyboardInterrupt, EOFError):
        pass
    except Exception as exc:
        # Honest diagnostics: exception *type* only in the console (messages
        # may carry secrets); full traceback goes to stderr.
        console.print(
            f"[bold red]Chat session failed ({type(exc).__name__}); details withheld: [REDACTED][/bold red]"
        )
        traceback.print_exc()
        raise typer.Exit(code=1) from exc


async def _run_chat_with_layout(controller: Any) -> None:
    """Drive ChatController's interactive loop through the new AntigonaLayout UI.

    Mirrors ChatController.run_interactive_loop's setup/teardown (panel init,
    monitor, cleanup) but renders via AntigonaLayout instead of the plain
    standard input loop, and routes input through the same handle_input().
    """
    from antigona.cli_ui.command_menu import merge_catalog
    from antigona.cli_ui.layout import AntigonaLayout, PickerKeyBridge
    from antigona.cli_ui.layout_renderer import LayoutRendererAdapter

    adapter = LayoutRendererAdapter()
    controller.renderer = adapter

    bridge = PickerKeyBridge()
    controller.key_bridge = bridge

    await controller.render_initial_panel()
    await controller.start_monitor()

    try:
        gateway_commands: list[dict[str, object]] | None = await controller.gateway.list_commands()
    except Exception:
        gateway_commands = None
    catalog = merge_catalog(gateway_commands)

    layout = AntigonaLayout(
        state=controller.state,
        renderer=adapter,
        on_input=controller.handle_input,
        catalog=catalog,
        key_bridge=bridge,
    )
    adapter.attach(layout)
    # /theme switches the live layout's colour skin through the controller.
    controller.theme_applier = layout.apply_theme
    controller.theme_custom_applier = layout.apply_custom_theme
    # /shell reads the live PIN gate result, not a snapshot at construction
    # time — owner_mode flips to True only after AntigonaLayout.run() below
    # completes _request_owner_pin(), and back to relevant-false forever if
    # it never succeeds.
    controller.owner_mode_check = lambda: layout.owner_mode
    try:
        await layout.run()
    finally:
        controller.renderer.release()
        await controller.close()


# ── Portrait Calibration (development utility) ───────────────────────────────


@app.command("portrait-calibrate")
def portrait_calibrate(
    profile: Annotated[
        str,
        typer.Option("--profile", "-p", help="Portrait profile to calibrate (full/large/medium/compact/mini)"),
    ] = "full",
) -> None:
    """Interactive eye-anchor calibration for the living portrait.

    Development utility — NOT for production use.

    Shows the MASTER PORTRAIT with L/R eye markers overlaid.
    Use arrow keys to move anchors, S to save, Q to quit.
    """
    from antigona.cli_ui.portrait_calibrate import run_calibration

    run_calibration(profile_name=profile)


@app.command("portrait-debug")
def portrait_debug(
    profile: Annotated[
        str,
        typer.Option("--profile", "-p", help="Portrait profile to debug (full/large/medium/compact/mini)"),
    ] = "full",
) -> None:
    """Cycle through visual portrait states for developer testing / preview.

    Development utility — NOT for production use.
    Visibly labels outputs as SIMULATED DEBUG STATES.
    """
    from antigona.cli_ui.portrait_calibrate import run_debug

    run_debug(profile_name=profile)


def main() -> None:
    _load_project_env()
    app()


if __name__ == "__main__":
    main()
