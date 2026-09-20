"""Goal → intent/entities extraction (D7/D8) + fail-closed path expectations (D10).

Pure, deterministic, dependency-free parsing of a free-text task goal into a
structured plan the CLI can submit and the Verifier can enforce:

* ``intent`` — dialog | shell | file_write | file_read | file_write_read | multi_file
* ``path`` / ``content`` / ``command`` — literal entities extracted from the goal
  (quoted filenames, ``файл X``, ``Содержимое …: A / B``, ``с …``, shell verbs)
* ``expected_paths`` — every path/filename the goal names, used by the Verifier
  as a fail-closed check: an artifact for a *different* path must never DONE.

Invariant 1 (LOOP ENGINEERING v2.1): literal user constraints (filename, exact
content, requested order) must survive planning → execution → presentation
unchanged. This module is the single extraction layer for the CLI task path;
the Verifier uses ``expected_paths_from_goal`` independently so a wrong-path
read (default ``task_output.txt`` instead of the named file) is rejected.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from .tools.shell_command import strip_shell_tool_prefix

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps task_goal import-light
    from antigona.router.intent_router import IntentDecision

#: Tool marker for a request that asks for NO side effect (conversation, an
#: answer, or an action whose effect cannot be materialized faithfully). Such a
#: task must never write an artifact and must never reach DONE — it is how the
#: P0 "false DONE machine" is kept closed.
ANSWER_ONLY_TOOL = "answer_only"

# ── Intent classification ────────────────────────────────────────────────

_SHELL_VERBS = (
    "ls", "cat", "echo", "pwd", "mkdir", "touch", "rm", "cp", "mv",
    "python", "python3", "pip", "apt", "apt-get", "uname", "whoami", "date",
    "head", "tail", "grep", "wc", "df", "du", "curl", "wget", "git", "sh",
)

_SHELL_CMD_RE = [
    # «Выполни команду cat /etc/passwd» / «выполни команду: …» /
    # «запусти в оболочке команду ls …» / «выполни в оболочке pwd …»
    # The "в оболочке / через оболочку" filler is instruction wording, not part
    # of the command: without it the live goals fell through to the dialogue
    # branch and were written to task_output.txt as their own file body.
    re.compile(
        r"(?:выполни|выполнить|запусти|исполни)\s+"
        r"(?:в\s+оболочке\s+|в\s+шелле\s+|через\s+оболочку\s+)?"
        r"команд[уы]?\s*:?\s*(.+)",
        re.IGNORECASE,
    ),
    # «Выполни echo hello» / «Выполни cat file» / «запусти pwd» /
    # «запусти в оболочке ls»
    re.compile(
        r"(?:выполни|запусти|исполни)\s+"
        r"(?:в\s+оболочке\s+|в\s+шелле\s+|через\s+оболочку\s+)?"
        r"(" + "|".join(_SHELL_VERBS) + r")\s*(.*)",
        re.IGNORECASE,
    ),
    # «shell: echo hello», «bash: ...», «sh: ...», «execute: ...»
    re.compile(r"^(?:shell|bash|sh|execute)\s*:\s*(.+)", re.IGNORECASE),
]

# «Скажи слово один», «привет», «сколько будет 2+2», «какая модель…»
_DIALOG_RE = re.compile(
    r"\b(?:сколько|привет|приветствие|какая\s+модель|какой\s+(?:llm|провайдер)|"
    r"кто\s+ты|что\s+такое|как\s+дела|скажи|ответь|объясни|почему|зачем|"
    r"напиши\s+короткое|поздоровайся)\b|[\?？]",
    re.IGNORECASE,
)

# R1-PARSER-01: a request phrased as a *question* ("можно создать файл?",
# "можешь создать файл X?", "создай файл?") is a capability question, not an
# instruction — it must never open a blind write flow.
_INTERROGATIVE_LEAD_RE = re.compile(
    r"^\s*(?:а\s+)?(?:можно(?:\s+ли)?|мож(?:ешь|ете)|не\s+мог(?:ли)?\s*бы|"
    r"получится\s+ли|стоит\s+ли|could\s+you|can\s+you|would\s+you|"
    r"is\s+it\s+possible)\b",
    re.IGNORECASE,
)
# Presence of any of these means the goal actually carries content to write, so
# a trailing "?" is punctuation, not an interrogative request.
_CONTENT_CLAUSE_RE = re.compile(
    r"с\s+(?:текстом|содержимым|кодом)\s*[:—–-]\s*\S|"
    r"с\s+(?:текстом|содержимым|кодом)\s+[^?\s]|"
    r"with\s+(?:text|content)\s*[:—–-]\s*\S|"
    r"with\s+(?:text|content)\s+[^?\s]|"
    r"содержим(?:ое|ым)\s*[:—–-]\s*\S|"
    r"запиш[иь]\s+(?:туда|сюда|в\s+\S+)\s*[:—–-]?\s*\S|"
    r"ровно\s*[:{]|строго\s*[:{]|:\s*[^?\s]|```",
    re.IGNORECASE,
)
_QUOTED_SPAN_RE = re.compile(r"\"[^\"]*\"|'[^']*'|«[^»]*»")


def _is_interrogative_request(goal: str) -> bool:
    """True when the goal is a question about doing something, not an order."""
    unquoted = _QUOTED_SPAN_RE.sub("", goal)
    has_lead = bool(_INTERROGATIVE_LEAD_RE.search(goal))
    has_tail_q = unquoted.rstrip().endswith(("?", "？"))
    if not (has_lead or has_tail_q):
        return False
    return not _CONTENT_CLAUSE_RE.search(unquoted)

_READ_MARKERS = (
    "прочитай", "прочти", "читай", "прочитать", "покажи содержимое",
    "покажи что внутри", "выведи содержимое", "верни содержимое",
    "что в файле", "что внутри", "read the file", "read file", "read back",
    "show content", "прочитай тот же путь",
)

_WRITE_MARKERS = (
    "создай файл", "создай отчёт", "создай папку", "создай файл с именем",
    "запиши", "записать", "create file", "write file", "создай", "создать",
)

# ── Entity extraction ─────────────────────────────────────────────────────

_QUOTED_RE = re.compile(r"[\"']([^\"']+)[\"']")

# Имя файла: поддерживает пробелы для путей с косой чертой (slash-scoped: path/to a file.ext).
_EXTENSIONS = (
    r"txt|md|json|log|csv|py|sh|yaml|yml|toml|ini|cfg|html|xml"
)
_PATH_EXT_RE = re.compile(
    r"(?<![\w/])("
    r"(?:[A-Za-zА-Яа-яЁё0-9_.\-/]+/[A-Za-zА-Яа-яЁё0-9_.\-]+(?<![.,;:!?])(?:[ \t]+(?!(?:с|со|и|текстом|содержимым|значениями|словом|строго|затем|потом|после|этого|создай|создайте|создать|запиши|запишите|записать|create|write)\b)[A-Za-zА-Яа-яЁё0-9_.\-/]+)*\.(?:" + _EXTENSIONS + r"))"
    r"|"
    r"(?:[A-Za-zА-Яа-яЁё0-9_.\-]+\.(?:" + _EXTENSIONS + r"))"
    r")\b",
    re.IGNORECASE,
)

# «файл literal.txt», «файл /etc/passwd», «файл ../../../etc/passwd»,
# «отчёт report.md», «файл workspace/l15 a b.txt».
# Slash-scoped пути могут содержать пробелы; одиночные токены — без пробелов.
_FILE_NOUN_RE = re.compile(
    r"(?:файл|file|отчёт|report)\s+([^\s,;:]+)", re.IGNORECASE
)

# D4: имя файла СРАЗУ после «создай/создать/create/запиши/сохрани» — победитель для target_path.
# Без этого путь брался из первого кавычечного токена, которым у цели вида
# «Создай exam.json ровно {"system":"antigona",…}» оказывался ключ JSON
# («system») — файл писался не туда, имя терялось.
_CREATE_FILENAME_RE = re.compile(
    r"(?:созда(?:й|йте|ть)|create|запиши|записать|write|сохрани|сохранить|save)\s+"
    r"(?:(?:an?\s+|the\s+)?(?:пустой\s+|empty\s+)?(?:\w+\s+)?(?:файл\s+|file\s+|документ\s+|скрипт\s+|в\s+|в\s+папку\s+|папку\s+))?"
    r"([A-Za-zА-Яа-яЁё0-9_.\-/]+\.(?:" + _EXTENSIONS + r"))\b",
    re.IGNORECASE,
)

# D4: «… ровно {"system":"x","ok":true}» — литеральный JSON-контент.
# Проверяется первым, чтобы служебное «ровно/строго» не попало в содержимое,
# а сам JSON сохранился байт-в-байт.
_CONTENT_EXACT_JSON_RE = re.compile(
    r"(?:ровно|строго|exactly)\s*:?\s*(\{.*\})", re.DOTALL | re.IGNORECASE
)

_STOPWORD_TOKENS = frozenset(
    {
        "с", "и", "в", "по", "на", "для", "the", "a", "an", "with", "in",
        "to", "named", "именем", "exactly", "под", "из", "от", "как",
        "обратно",  # «прочитай этот же файл обратно» — инструкция, не путь
    }
)

_FOLDER_RE = re.compile(r"папк[уа]\s+([^\s,.;:]+)", re.IGNORECASE)

_NAMED_FILE_RE = re.compile(
    r"(?:с\s+именем|именем|named|name(?:d)?\s+as)\s+"
    r"([A-Za-zА-Яа-яЁё0-9_.\-/]+(?:[ \t]+(?!"
    r"(?:с|со|и|текстом|содержимым|значениями|словом|строго|затем|потом|после|этого|не)\b)"
    r"[A-Za-zА-Яа-яЁё0-9_.\-/]+)*\.(?:" + _EXTENSIONS + r"))",
    re.IGNORECASE,
)

_CONTENT_TWO_LINES_RE = re.compile(
    r"(?:(?:точным\s+)?содержим(?:ое|ым)\s+)?(?:должно\s+быть\s+)?(?:ровно\s+)?(?:с\s+)?(?:из\s+)?"
    r"(?:двух\s+строк|двумя\s+строками|две\s+строки)\s*:\s*(.+)",
    re.IGNORECASE | re.DOTALL,
)
_CONTENT_COLON_RE = re.compile(r"содержимое\s*:\s*(.+)", re.IGNORECASE)
_CONTENT_INTO_FILE_RE = re.compile(
    r"(?:запиши|записать|напиши)\s+в\s+(?:файл\s+)?[^\s]+\s+(?:с\s+)?(?:содержимое|содержимым)\s*:?\s*(.+)",
    re.IGNORECASE,
)
# P0-017/T2: «напиши (число 42|текст X) в (файл) answer.txt» -> content=42/X.
_CONTENT_NAPISHI_INTO_RE = re.compile(
    r"\b(?:напиши|запиши|записать)\s+(?:(?:число|слово|значение|текст|строку)\s+)?([^\s.]+)\s+в\s+(?:файл\s+)?[\w./\-]+\.[A-Za-z]{2,4}\b",
    re.IGNORECASE,
)
# P0-040: «создай PATH и запиши {туда|сюда|в него|в неё} CONTENT» — продолжение
# «запиши туда …» несёт литеральное содержимое файла. Якорь — наречие места
# (туда/сюда) либо местоимение созданного файла (в него/в неё), а не «в файл
# <имя>» (эту форму обслуживает _CONTENT_INTO_FILE_RE).
_CONTENT_INTO_IT_RE = re.compile(
    r"\b(?:запиши|записать|запиш[иь]те|напиши|помести|внеси|добав[ья]|впиши)\s+"
    r"(?:туда|сюда|в\s+не(?:го|[её]))\s+"
    r"(.+)",
    re.IGNORECASE | re.DOTALL,
)
# Ведущее служебное слово в хвосте «запиши туда …» («текст», «строку»,
# «значение», «содержимое», «следующее») — инструкция, а не часть литерала.
_CONTENT_INTO_IT_FILLER_RE = re.compile(
    r"^(?:следующ(?:ий|ее)\s+)?(?:текст|строку|значение|содержимое)\s+(?=\S)",
    re.IGNORECASE,
)
# Хвост, который ОПИСЫВАЕТ формат/объём, а не задаёт литеральное содержимое
# («две строки», «одну строку», «текст», «содержимое»). В этом случае тело
# файла готовит drafter — парсер обязан вернуть пусто и не угадывать.
_CONTENT_INTO_IT_DESCRIPTOR_RE = re.compile(
    r"^(?:текст|содержим(?:ое|ым)|что[- ]?нибудь|что[- ]?то|"
    r"(?:\d+|одн[уаой]|дв[еа]|двух|двумя|тр[иёе]х?|тремя|несколько|пар[уаы])\s+"
    r"строк(?:а|и|у|ой)?|строк[ауи]|строчк[ауи]|одной\s+строкой|one\s+line|two\s+lines"
    r")\.?$",
    re.IGNORECASE,
)
_CONTENT_WITH_RE = re.compile(
    # Path token allows slashes/subdirs (evidence/x.txt), slash-scoped spaces
    # (workspace/l15 a b.txt) AND quoted paths with spaces ("evidence/my test file.txt").
    # «содержимым/текстом» and optional «строго:» are instruction words, NOT part of the literal content.
    r"(?:создай|запиши|записать|напиши)\s+(?:в\s+)?(?:файл\s+)?(?:\"[^\"]+\"|'[^']+'|[\w.\-/]+(?:/[\w.\-]+(?:[ \t]+[\w.\-/]+)*)|[\w.\-/]+)\s+(?:в\s+\S+\s+)?с\s+"
    r"(?:(?:текстом|содержимым)\s*(?:строго:)?\s*)?(.+)",
    re.IGNORECASE,
)
# P0-041: English «write/create [file] <path> with [content/text] <content>»
_CONTENT_WITH_EN_RE = re.compile(
    r"(?:write|create)\s+(?:(?:an?\s+|the\s+)?file\s+)?(?:\"[^\"]+\"|'[^']+'|[\w.\-/]+(?:/[\w.\-]+(?:[ \t]+[\w.\-/]+)*)|[\w.\-/]+)\s+"
    r"(?:with\s+(?:(?:the\s+)?(?:exact\s+)?(?:text|content)\s*(?:strictly:|exactly:)?\s*:?\s*|:\s*)|containing\s+)"
    r"(.+)",
    re.IGNORECASE | re.DOTALL,
)

# «с одной строкой: X» / «одной строкой X» — однострочный контейнер-дескриптор:
# это НЕ часть литерального содержимого, а указание на формат. Содержимое = X.
_LINE_COUNT_DESC_RE = re.compile(
    r"^(?:в\s+)?(?:одну\s+строку|одной\s+строкой|одной\s+строки|одна\s+строка|one\s+line)\s*[:.)} -]?\s*",
    re.IGNORECASE,
)
_CONTENT_REPORT_RE = re.compile(
    r"создай\s+отчёт\s+[\w.\-]+\s*:\s*(.+)", re.IGNORECASE
)

# FP-L05g: the cue word that introduces the BODY of a named write. The
# «с текстом/содержимым X» shape was the only one the patterns above knew, so
# the live Telegram request «запиши файл live_regression_probe.txt со словом
# regression» produced content="" → the write degraded to answer_only (task
# 3eff1a86 FAILED, no file anywhere) AND the already-extracted file name was
# dropped (path="stdout"). A named write with an explicit body must never
# degrade; the cue list below covers the natural formulations.
_CONTENT_CUE_WORD = (
    r"(?:слов(?:ом|о|а)\b|текст(?:ом|а|е)?\b|содержим(?:ым|ое|ого)\b|"
    r"надпис(?:ью|ь|и)\b|значени(?:ем|е)\b|строчк(?:ой|у)\b|"
    r"word|text|content)"
)
_CONTENT_CUE_RE = re.compile(
    r"^(?:с\s+|со\s+)?(?:строго\s*:?\s*)?" + _CONTENT_CUE_WORD + r"\s*:?\s*",
    re.IGNORECASE,
)

#: A file name token (quoted, slash-scoped with spaces, or a single word).
_PATH_TOKEN = (
    r"(?:\"[^\"]+\"|'[^']+'|[\w.\-/]+(?:/[\w.\-]+(?:[ \t]+[\w.\-/]+)*)|[\w.\-/]+)"
)

#: «<write verb> [в] [файл] <path> [в <folder>] <tail>» — the body of a named
#: write. Used only as the LAST resort of ``_extract_content``: every shape
#: above (JSON, two-lines, «с текстом X», «туда X») wins first.
_CONTENT_AFTER_NAMED_PATH_RE = re.compile(
    r"(?:созда(?:й|йте|ть)|create|запиш(?:и|ите|ем|ете)|записать|write|"
    r"напиш(?:и|ите)|написать|сохрани(?:ть)?|save)\s+"
    r"(?P<prep>в\s+файл\s+|файл\s+|file\s+|в\s+)?"
    r"(?P<path>" + _PATH_TOKEN + r")\s+"
    r"(?:в\s+\S+\s+)?"
    r"(?P<tail>.+)",
    re.IGNORECASE,
)

_MULTI_VALUES_RE = re.compile(r"со\s+значениями\s+(.+)", re.IGNORECASE)

# B5: «Создай square.py: <описание>. Запусти python square.py 12» —
# составная цель «создать исполняемый файл из описания и запустить его».
# Без отдельного разбора она уезжала в intent="shell" с пустыми path/content:
# планировщик строил `sh -c "<вся русская цель>"`, агент терял имя файла и
# писал текст в дефолтный task_output.txt, а запуск не выполнялся вовсе.
_SCRIPT_EXTENSIONS = ("py", "sh", "js", "rb", "pl", "php", "lua")

_CREATE_SCRIPT_RE = re.compile(
    r"(?:созда(?:й|йте|ть)|create|напиши|write)\s+(?:файл\s+|file\s+|скрипт\s+|script\s+)?"
    r"([A-Za-zА-Яа-яЁё0-9_.\-/]+\.(?:" + "|".join(_SCRIPT_EXTENSIONS) + r"))\b",
    re.IGNORECASE,
)

# «Запусти python square.py 12» / «run python3 ./square.py 12» / «выполни sh a.sh»
_RUN_SCRIPT_RE = re.compile(
    r"(?:запусти(?:те)?|запустить|выполни(?:те)?|выполнить|исполни|run|execute)\s+"
    r"(python3?|sh|bash|node|ruby|perl|php|lua)\s+"
    r"([A-Za-zА-Яа-яЁё0-9_.\-/]+\.(?:" + "|".join(_SCRIPT_EXTENSIONS) + r"))"
    r"((?:\s+[^\s.,;]+)*)",
    re.IGNORECASE,
)

# Описание содержимого: «<file>.py: печатает квадрат argv[1]» / «<file>.py — …».
_SCRIPT_DESCRIPTION_RE_TPL = r"{name}\s*(?::|—|–|-)\s*(.+)"

# L7-1: Сигналы диагностики, исправления ошибок и повторного запуска
_FIX_DIAGNOSE_SIGNALS = re.compile(
    r"(?:"
    r"исправ(?:ь|ьте|ить|ленн\w*)|"
    r"почин(?:и|ите|ить)|"
    r"найди\s+ошибку|"
    r"диагностир(?:уй|уйте|овать|ка)|"
    r"почему\s+(?:результат\s+)?неверн\w*|"
    r"почему\s+не\s+работает|"
    r"ошибк[ауеи]|"
    r"перезапусти(?:те)?|перезапустить|"
    r"запусти\s+(?:снова|опять|еще\s+раз|ещё\s+раз)|"
    r"исправленный\s+вывод|"
    r"\bfix\b|\bdiagnose\b|\bdebug\b|\bcorrect\b|\brerun\b|\brun\s+again\b"
    r")",
    re.IGNORECASE,
)


def _extract_write_fix_run(goal: str) -> dict[str, str] | None:
    """L7-1: «создай <script> [buggy]. запусти, найди ошибку, исправь, перезапусти» → compound plan.

    Возвращает ``{"path", "command", "content", "content_hint", "fix_content", "fix_command"}``
    либо None, если цель не содержит одновременно создание скрипта и сигналы диагностики/исправления/перезапуска.
    """
    if not _FIX_DIAGNOSE_SIGNALS.search(goal):
        return None
    created = _CREATE_SCRIPT_RE.search(goal)
    if not created:
        return None
    path = _normalize_path(created.group(1))
    if not path:
        return None

    # Определение команды запуска (интерпретатор + путь + аргументы)
    command = ""
    for run in _RUN_SCRIPT_RE.finditer(goal):
        run_path = _normalize_path(run.group(2))
        if run_path.rsplit("/", 1)[-1] == path.rsplit("/", 1)[-1]:
            argv = [run.group(1).strip(), run_path]
            argv.extend(part for part in run.group(3).split() if part)
            command = " ".join(argv)
            break

    if not command:
        ext = path.rsplit(".", 1)[-1].casefold()
        interpreter_map = {
            "py": "python",
            "sh": "sh",
            "bash": "bash",
            "js": "node",
            "rb": "ruby",
            "pl": "perl",
            "php": "php",
            "lua": "lua",
        }
        interp = interpreter_map.get(ext, "python")
        command = f"{interp} {path}"

    # Извлечение исходного (ошибочного) кода из текста цели.
    # NB: НЕ использовать bare `[...]` для content — Python-срезы в коде
    # (sum(items[:-1])) содержат квадратные скобки и дают ложный фрагмент.
    # Приоритет: fences → tail-split → brackets-только-если-не-срез.
    content = ""
    if "```" in goal:
        fence_match = re.search(r"```(?:\w+)?\n?([\s\S]+?)```", goal)
        if fence_match:
            content = fence_match.group(1).strip()
    elif not re.search(r"\[[^\[\]]*[:-][^\[\]]*\]", goal):
        # Ни одного Python-среза — квадратные скобки безопасны как обёртка content.
        bracket_match = re.search(r"\[([\s\S]+?)\]", goal)
        if bracket_match:
            content = bracket_match.group(1).strip()
    if not content:
        created_end = created.end()
        tail = goal[created_end:].lstrip()
        # Снять префикс «ровно с кодом:» / «со следующим кодом:» / «так:»
        code_prefix = re.match(
            r"(?:ровно\s+)?(?:с\s+(?:кодом|текстом|содержимым)|кодом|так|такое|вот)\s*:?\s*",
            tail,
            re.IGNORECASE,
        )
        if code_prefix:
            tail = tail[code_prefix.end() :]
        if tail.startswith(":") or tail.startswith("—") or tail.startswith("-"):
            tail = tail[1:].lstrip()
        # Разделитель код/дальнейшие указания: «. Запусти» ИЛИ «Затем/Потом/После запусти».
        run_fix_split = re.search(
            r"(?:\.\s+|[Зз]атем\s+|[Пп]отом\s+|[Пп]осле\s+|,\s*)(?:[Зз]апусти|[Вв]ыполни|[Пп]ойми|[Нн]айди|[Ии]справь|[Дд]иагностируй|[Рр]un|[Ff]ix|[Dd]iagnose)\b",
            tail,
        )
        if run_fix_split:
            candidate_code = tail[: run_fix_split.start()].strip()
            if candidate_code:
                content = candidate_code

    if content:
        content = strip_code_fences(content, path=path)


    description = ""
    fix_match = _FIX_DIAGNOSE_SIGNALS.search(goal)
    if fix_match:
        description = goal[fix_match.start() :].strip()

    return {
        "path": path,
        "command": command,
        "content": content,
        "content_hint": description,
        "fix_content": "",
        "fix_command": command,
    }

# Строка-маркер markdown-фенса в любом месте черновика: ```python / ``` / ```.
_FENCE_LINE_RE = re.compile(r"^\s*```.*$")

# Хвостовой «отчёт» LLM после кода: «(stdout: 144.0 / exit code: 0)», «Вывод:».
# Только в начале строки (допускается ведущая скобка), чтобы не резать
# строковые литералы внутри кода — они почти всегда с отступом или в кавычках.
_REPORT_MARKER_RE = re.compile(
    r"^[(\[]?\s*(?:stdout|exit\s+code|вывод|output)\s*[:=]",
    re.IGNORECASE,
)

# `output: int = 5` — легальная python-аннотация, не отчётный маркер.
_ANNOTATION_RE = re.compile(r"^\w+\s*:\s*[A-Za-z_][\w\[\], .]*\s*=")


def _longest_compilable_prefix(source: str) -> str:
    """Вернуть самый длинный префикс строк, который компилируется как python.

    Только УСЕЧЕНИЕ: середина не переписывается, ничего не дописывается.
    Если компилируется весь текст — он возвращается без изменений
    (идемпотентность). Если не компилируется даже одна строка — возвращается
    исходный текст (fail-safe, лучше не резать, чем срезать всё).
    """
    if not source.strip():
        return source
    try:
        compile(source, "<draft>", "exec")
    except (SyntaxError, ValueError):
        pass
    else:
        return source

    lines = source.split("\n")
    for count in range(len(lines) - 1, 0, -1):
        candidate = "\n".join(lines[:count]).strip("\n")
        if not candidate:
            continue
        try:
            compile(candidate + "\n", "<draft>", "exec")
        except (SyntaxError, ValueError):
            continue
        return candidate + "\n"
    return source


def strip_code_fences(text: str, *, path: str | None = None) -> str:
    """Убрать markdown-ограждение и хвостовой отчёт из черновика исходника.

    Канон B5 запрещает фенсы в записанном файле: `python square.py` на файле
    с ``` падает с SyntaxError. Реальный LLM-черновик приходит грязным не
    только целиком-обёрнутым блоком: фенсы встречаются внутри текста, а после
    кода дописывается секция вида «(stdout: 144.0 / exit code: 0)». Ни то, ни
    другое не является телом файла — stdout производит отдельный run-шаг.

    Чистка детерминированная и идемпотентная: уже чистый python проходит
    насквозь без изменений.

    ``path`` — имя целевого файла. Если это python-скрипт (``.py``), после
    снятия фенсов и отчётных секций результат дополнительно усекается до
    самого длинного КОМПИЛИРУЕМОГО префикса: живой черновик дописывает после
    кода произвольную русскую аннотацию («После выполнения команды `python
    x.py 12`:»), которая не является отчётным маркером, но ломает файл.
    Для обычных text/json/csv/md путей эта логика НЕ применяется.
    """
    value = text or ""
    if not value:
        return value

    lines = value.split("\n")
    cleaned: list[str] = []
    changed = False
    for line in lines:
        if _FENCE_LINE_RE.match(line):
            changed = True
            continue
        if _REPORT_MARKER_RE.match(line) and not _ANNOTATION_RE.match(line):
            changed = True
            break
        cleaned.append(line)

    if changed:
        body = "\n".join(cleaned).strip("\n")
        value = body + "\n" if body else ""

    if path and path.rsplit("/", 1)[-1].casefold().endswith(".py"):
        value = _longest_compilable_prefix(value)
    return value


def _extract_write_run(goal: str) -> dict[str, str] | None:
    """B5: «создай <script>. запусти <interpreter> <script> <args>» → план.

    Возвращает ``{"path", "command", "content"}`` либо None, если цель не
    называет ОДИН и тот же файл и в create-, и в run-части.
    """
    created = _CREATE_SCRIPT_RE.search(goal)
    if not created:
        return None
    path = _normalize_path(created.group(1))
    if not path:
        return None
    for run in _RUN_SCRIPT_RE.finditer(goal):
        run_path = _normalize_path(run.group(2))
        if run_path.rsplit("/", 1)[-1] != path.rsplit("/", 1)[-1]:
            continue
        argv = [run.group(1).strip(), run_path]
        argv.extend(part for part in run.group(3).split() if part)
        description = ""
        desc_match = re.search(
            _SCRIPT_DESCRIPTION_RE_TPL.format(name=re.escape(created.group(1))),
            goal[created.start() :],
            re.IGNORECASE,
        )
        if desc_match:
            description = _cut_sentence(desc_match.group(1))
        return {
            "path": path,
            "command": " ".join(argv),
            "content_hint": description,
        }
    return None

# Обрезать хвост до конца предложения (для content и command).
# Wave 0 (PX-04): the old rule cut every literal after ". <Capital>"
# ("Hello. Мир" -> "Hello"), destroying real content. Cut only when the
# sentence boundary is followed by an instruction/sequence starter, so
# literal multi-sentence content survives ("запиши туда Hello. Мир").
_SENTENCE_END_RE = re.compile(
    r"\s*\.\s+(?:затем|потом|после|дальше|теперь|далее|выполни|запусти|"
    r"создай|запиши|напиши|прочитай|прочти|покажи|удали|проверь|вернись|"
    r"перейди|добавь|продолжи|финальный|финального|итоговый|результат|"
    r"доставь|отправь|верни|убедись|гарантируй|обязан|должен|сообщи|выведи)\b|\.$",
    re.IGNORECASE,
)


_SEQUENCER_RE = re.compile(
    r"\s*(?:,|;)?\s*(?:а\s+)?затем\b|\s*(?:,|;)?\s*потом\b|\s*после\s+этого\b",
    re.IGNORECASE,
)

_SUBSEQUENT_MODIFY_RE = re.compile(
    r"(?:(?:а\s+)?затем\b|потом\b|после\s+этого\b)\s*[:,]?\s*[^.;\n]*?\b"
    r"(?:замени(?:ть|те)?|обнови(?:ть|те)?|добавь(?:те)?|добавить|"
    r"перепиши(?:те)?|переписать|измени(?:ть|те)?|допиши(?:те)?|дописать|"
    r"сотри(?:те)?|стереть|удали(?:ть|те)?|модифицируй(?:те)?|модифицировать)\b",
    re.IGNORECASE,
)

_SUBSEQUENT_ANY_OP_RE = re.compile(
    r"(?:(?:а\s+)?затем\b|потом\b|после\s+этого\b)\s*[:,]?\s*[^.;\n]*?\b"
    r"(?:замени(?:ть|те)?|обнови(?:ть|те)?|добавь(?:те)?|добавить|"
    r"перепиши(?:те)?|переписать|прочитай(?:те)?|прочесть|прочти|"
    r"измени(?:ть|те)?|допиши(?:те)?|дописать|сотри(?:те)?|стереть|удали(?:ть|те)?|"
    r"создай(?:те)?|создать|запиши(?:те)?|записать|модифицируй(?:те)?|модифицировать)\b",
    re.IGNORECASE,
)


def _cut_sentence(text: str) -> str:
    # Обрезать до конца предложения или секвенсора - самый ранний.
    cuts: list[int] = []
    match = _SENTENCE_END_RE.search(text)
    if match:
        cuts.append(match.start())
    seq = _SEQUENCER_RE.search(text)
    if seq:
        cuts.append(seq.start())
    if cuts:
        return text[: min(cuts)].strip()
    return text.strip()


def _normalize_path(raw: str) -> str:
    """Привести извлечённый путь к workspace-relative виду.

    ``/workspace/...`` → относительный; ``./x`` → ``x``. Абсолютные пути вне
    workspace (``/etc/passwd``) и traversal (``../../../etc/passwd``) НЕ
    трогаем — их блокирует серверная политика/песочница (fail-closed).
    """
    value = (raw or "").strip().strip("\"'`")
    if value.endswith("."):
        value = value[:-1].rstrip()
    if value.startswith("/workspace/"):
        value = value[len("/workspace/") :]
    elif value.startswith("workspace/"):
        value = value[len("workspace/") :]
    elif value in ("/workspace", "workspace"):
        return ""
    if value.startswith("./"):
        value = value[2:]
    return value.strip()


def _first_file_token(goal: str) -> str:
    """Первый не-служебный токен после «файл/отчёт/file/report»."""
    for match in _FILE_NOUN_RE.finditer(goal):
        token = match.group(1).strip()
        if token.casefold() not in _STOPWORD_TOKENS:
            return _normalize_path(token)
    return ""


def _extract_path(goal: str) -> str:
    named = _NAMED_FILE_RE.search(goal)
    if named:
        candidate = _normalize_path(named.group(1))
        if candidate and _is_path_like(candidate):
            return candidate
    # Wave 0 (PX-01): explicit «создай файл X» must win over any quoted token —
    # a JSON string value ("x. Y") was stealing the target path, so the file
    # was written to the wrong name (DONE on a wrong artifact).
    created = _CREATE_FILENAME_RE.search(goal)
    if created:
        candidate = _normalize_path(created.group(1))
        if candidate and _is_path_like(candidate):
            return candidate
    for quoted in _QUOTED_RE.finditer(goal):
        candidate = _normalize_path(quoted.group(1))
        if candidate and _is_path_like(candidate):
            return candidate
    ext_match = _PATH_EXT_RE.search(goal)
    if ext_match:
        candidate = _normalize_path(ext_match.group(1))
        if candidate and _is_path_like(candidate):
            return candidate
    file_token = _first_file_token(goal)
    if file_token and _is_path_like(file_token):
        return file_token
    folder = _FOLDER_RE.search(goal)
    if folder:
        return _normalize_path(folder.group(1))
    abs_re = re.search(r"(?:прочитай|прочти|прочитать|cat|read)\s+(/[^\s,;:.]+(?:\.[^\s,;:.]+)?)", goal, re.IGNORECASE)
    if abs_re:
        return _normalize_path(abs_re.group(1))
    return ""


def _bounded_json_object(text: str, start: int) -> str | None:
    """Return the single balanced JSON object beginning at ``text[start] == '{'``.

    R1-PARSER-01: ``_CONTENT_EXACT_JSON_RE`` captures ``{ .* }`` greedily, so
    ``ровно {"a":1} а потом ещё {x}`` swallowed the trailing prose into the file
    body. Scan brace depth (string- and escape-aware) to the matching ``}`` and
    accept only if that substring is valid JSON.
    """
    if start < 0 or start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    json.loads(candidate)
                except Exception:
                    return None
                return candidate
    return None


def _extract_content(goal: str) -> str:
    exact_json = _CONTENT_EXACT_JSON_RE.search(goal)
    if exact_json:
        bounded = _bounded_json_object(goal, exact_json.start(1))
        if bounded is not None:
            return bounded
        # Backward-compat: «ровно {…}» whose braces are not valid JSON keeps the
        # historical greedy capture.
        return exact_json.group(1).strip()
    # P0-032: JSON-контент после двоеточия («data.json: {…}») без «ровно».
    json_colon = re.search(r":\s*(\{.*\})", goal, re.DOTALL)
    if json_colon:
        candidate = json_colon.group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except Exception:
            pass
    # P0-032: JSON-структура {...} или [...] в тексте цели
    for m in re.finditer(r"(\{.*\}|\[.*\])", goal, re.DOTALL):
        cand = m.group(1).strip()
        try:
            json.loads(cand)
            return cand
        except Exception:
            pass
    two = _CONTENT_TWO_LINES_RE.search(goal)
    if two:
        tail = two.group(1).strip()
        lines = [line.strip() for line in tail.splitlines() if line.strip()]
        if len(lines) >= 2:
            return f"{lines[0]}\n{lines[1]}"
        if " / " in tail:
            line1, line2 = tail.split(" / ", 1)
            return f"{line1.strip()}\n{_cut_sentence(line2)}"
        m_slash = re.match(r"^(\S[^\n]*?)\s*/\s*(\S.*)$", tail, re.DOTALL)
        if m_slash and len(m_slash.group(1).split()) <= 6:
            return f"{m_slash.group(1).strip()}\n{_cut_sentence(m_slash.group(2))}"
        # P0-031: «ровно с двумя строками: FIRST и SECOND» — два литерала через «и».
        m_i = re.match(r"^(\S[^\n]*?)\s+и\s+(\S.*)$", tail, re.DOTALL)
        if m_i and len(m_i.group(1).split()) <= 4 and len(m_i.group(2).split()) <= 4:
            return f"{m_i.group(1).strip()}\n{_cut_sentence(m_i.group(2)).strip()}"
        return _cut_sentence(tail)
    colon = _CONTENT_COLON_RE.search(goal)
    if colon:
        return _cut_sentence(colon.group(1))
    into_file = _CONTENT_INTO_FILE_RE.search(goal)
    if into_file:
        return _cut_sentence(into_file.group(1))
    napishi = _CONTENT_NAPISHI_INTO_RE.search(goal)
    if napishi:
        return _cut_sentence(napishi.group(1))
    into_it = _CONTENT_INTO_IT_RE.search(goal)
    if into_it:
        cand = _cut_sentence(into_it.group(1)).strip()
        cand = _CONTENT_INTO_IT_FILLER_RE.sub("", cand, count=1).strip()
        if cand and not _CONTENT_INTO_IT_DESCRIPTOR_RE.match(cand):
            return cand
    with_ = _CONTENT_WITH_RE.search(goal)
    if with_:
        content = _cut_sentence(with_.group(1))
        # «с содержимым: X» — срезать ведущие ':' / '—' / пробелы, оставить X.
        content = content.lstrip(":—–- \t")
        # «с одной строкой: X» → содержимое X (дескриптор одной строки не содержимое).
        content = _LINE_COUNT_DESC_RE.sub("", content, count=1)
        # FP-L05g: this branch's cue group is OPTIONAL, so «с надписью SIGN»
        # captured "надписью SIGN" and «с текстом» (no body) captured the cue
        # word itself as the body. Drop a leading cue word; an empty remainder
        # means the text states no body at all (fail closed, no invented file).
        return _strip_leading_content_cue(content)
    with_en = _CONTENT_WITH_EN_RE.search(goal)
    if with_en:
        content = _cut_sentence(with_en.group(1))
        content = content.lstrip(":—–- \t")
        content = _LINE_COUNT_DESC_RE.sub("", content, count=1)
        return _strip_leading_content_cue(content)
    report = _CONTENT_REPORT_RE.search(goal)
    if report:
        return _cut_sentence(report.group(1))
    # FP-L05g (last resort): «<write verb> [в] файл <name> [с] словом|текстом|
    # содержимым|надписью <body>» and «напиши в файл <name> <literal>».
    return _content_after_named_path(goal)


def _strip_leading_content_cue(content: str) -> str:
    """Drop a leading cue word («словом», «с текстом», «надписью», …).

    FP-L05g. The cue is instruction, never content: «создай a.txt с надписью
    SIGN» has the body ``SIGN``. Returns "" when nothing but the cue word was
    present — the body is then not derivable and the write must fail closed.
    """
    stripped = _CONTENT_CUE_RE.sub("", content, count=1)
    if stripped == content:
        return content.strip()
    return stripped.lstrip(":—–- \t")


def _content_after_named_path(goal: str) -> str:
    """Return the literal body of a named write, else "" (fail closed).

    FP-L05g. Two shapes are accepted:

    * a cue word introduces the body — «со словом X», «словом X», «с текстом
      X», «текстом X», «с содержимым X», «с надписью X» (the cue word itself is
      instruction, never content);
    * «напиши в файл <name> X» — the body follows the named file directly, with
      no cue word. Here only a bare literal counts (a single token or a quoted
      string): «запиши в файл date.txt текущую дату» describes the body instead
      of stating it and must stay answer_only, exactly as before.

    The pattern is anchored on a write verb AND a name, so a phrase without a
    named file never matches a body here.
    """
    match = _CONTENT_AFTER_NAMED_PATH_RE.search(goal)
    if not match:
        return ""
    prep = (match.group("prep") or "").strip().casefold()
    tail = match.group("tail").strip()
    stripped = _CONTENT_CUE_RE.sub("", tail, count=1)
    if stripped == tail:
        # No cue word: the body must directly follow «… в файл <name> » and be
        # a bare literal, otherwise the write has no derivable content.
        if prep != "в файл":
            return ""
        if not re.fullmatch(r"\"[^\"]+\"|'[^']+'|\S+", tail):
            return ""
    else:
        tail = stripped
    content = _cut_sentence(tail).strip()
    if not content or _CONTENT_INTO_IT_DESCRIPTOR_RE.match(content):
        return ""
    return content


def _extract_shell_command(goal: str) -> str:
    for pattern in _SHELL_CMD_RE:
        match = pattern.search(goal)
        if not match:
            continue
        if len(match.groups()) > 1 and match.group(2):
            command = f"{match.group(1)} {match.group(2)}".strip()
        else:
            command = match.group(1).strip()
        command = command.strip("\"'`")
        command = strip_shell_tool_prefix(command)
        # «…и верни вывод» / «…и покажи результат» — часть инструкции, не команды
        command = re.split(r"\s+и\s+(?:верни|покажи|выведи)\b", command, maxsplit=1)[0]
        command = _cut_sentence(command)
        if command:
            return command
    return ""


def _extract_per_file_contents(goal: str) -> dict[str, str]:
    """Extract {file: content} for "file1 с текстом X и file2 с текстом Y"."""
    out: dict[str, str] = {}
    for m in _PATH_EXT_RE.finditer(goal):
        path = m.group(1)
        tail = goal[m.end():]
        c = re.match(r"\s+с\s+(?:текстом|содержимым)\s+(.+)", tail, re.IGNORECASE)
        if not c:
            continue
        content = c.group(1).strip()
        nxt = re.search(
            r"\s+и\s+[^\s,;]+\.(?:txt|md|json|log|csv|py|sh|yaml|yml|toml|ini|cfg|html|xml)\s+с\s+",
            content, re.IGNORECASE,
        )
        if nxt:
            content = content[:nxt.start()].strip()
        out[path] = _cut_sentence(content)
    return out


def _extract_multi_file(goal: str) -> dict[str, object] | None:
    """T30/L10: «Создай папку X. … три файла: a.txt, b.txt со значениями A, B …
    Создай summary.txt со строками <filename>: <content>» → compound shell plan."""
    folder_match = _FOLDER_RE.search(goal)
    files = list(
        dict.fromkeys(
            m.group(1)
            for m in _PATH_EXT_RE.finditer(goal)
            if m.group(1).casefold() != "summary.txt"
        )
    )
    if len(files) < 2:
        return None
    if folder_match:
        folder = _normalize_path(folder_match.group(1))
    else:
        first = files[0]
        folder = _normalize_path(first.rsplit("/", 1)[0]) if "/" in first else ""
    pairs: list[tuple[str, str]] = []
    values_match = _MULTI_VALUES_RE.search(goal)
    if values_match:
        values = [
            v.strip()
            for v in re.split(r"[,;]|\s+и\s+", _cut_sentence(values_match.group(1)))
            if v.strip()
        ]
        for name, value in zip(files, values, strict=False):
            pairs.append((name, value))
    elif not folder_match:
        per_file = _extract_per_file_contents(goal)
        for name in files:
            if name in per_file:
                pairs.append((name, per_file[name]))
    return {"folder": folder, "files": files, "pairs": pairs}



def _build_multi_file_command(data: dict[str, object]) -> tuple[str, str, str]:
    """Вернуть (command, path, content) для compound shell-задачи."""
    folder = str(data.get("folder") or "")
    files = list(cast(list[str], data.get("files") or []))
    pairs = list(cast(list[tuple[str, str]], data.get("pairs") or []))
    if pairs:
        parts: list[str] = []
        if folder:
            parts.append(f"mkdir -p {folder}")
        for name, value in pairs:
            safe = value.replace("'", "'\\''")
            target = name if "/" in name else (f"{folder}/{name}" if folder else name)
            parts.append(f"printf '{safe}\\n' > {target}")
        summary_target = f"{folder}/summary.txt" if folder else "summary.txt"
        summary_lines = "\\n".join(f"{name}: {value}" for name, value in pairs)
        parts.append(f"printf '{summary_lines}\\n' > {summary_target}")
        parts.append(f"cat {summary_target}")
        return (
            " && ".join(parts),
            summary_target,
            "\n".join(f"{name}: {value}" for name, value in pairs) + "\n",
        )
    # Без явных значений: создать структуру и показать список файлов.
    parts = []
    if folder:
        parts.append(f"mkdir -p {folder}")
    for name in files:
        target = name if "/" in name else (f"{folder}/{name}" if folder else name)
        parts.append(f"touch {target}")
    summary_folder = folder if folder else "."
    parts.append(f"find {summary_folder} -type f | sort")
    return (" && ".join(parts), "stdout", "")


@dataclass(frozen=True)
class GoalPlan:
    """Структурированный план, извлечённый из текста цели."""

    intent: str
    path: str = ""
    content: str = ""
    command: str = ""
    read_after_write: bool = False
    expected_paths: tuple[str, ...] = ()
    note: str = ""
    incomplete_sequence: bool = False
    # B5: составное «создай скрипт из описания → запусти его». ``command``
    # хранит строку запуска, ``content_hint`` — описание, из которого LLM
    # генерирует исходник (это НЕ содержимое файла).
    run_after_write: bool = False
    content_hint: str = ""
    # L7-1: составное «создай → запусти → исправь → перезапусти».
    fix_after_run: bool = False
    fix_content: str = ""
    fix_command: str = ""
    planned_steps: tuple[str, ...] = ()

    @property
    def steps(self) -> tuple[str, ...]:
        if self.planned_steps:
            return self.planned_steps
        if self.intent == "file_write_fix_run":
            return (
                "workspace.write_text",
                "sandbox.shell",
                "workspace.write_text",
                "sandbox.shell",
            )
        if self.intent == "file_write_run":
            return ("workspace.write_text", "sandbox.shell")
        if self.intent == "file_write_read" or self.read_after_write:
            return ("workspace.write_text", "workspace.read_text")
        return (self.tool_name,)

    @property
    def tool_name(self) -> str:
        if self.intent in ("shell", "multi_file"):
            return "sandbox.shell"
        if self.intent == "file_read":
            return "workspace.read_text"
        return "workspace.write_text"



# ── Canonical free-text request resolution ───────────────────────────────
#
# P0 (live defect 2026-09-18): ``POST /tasks`` hard-coded
# ``tool_name="workspace.write_text"`` + ``path="task_output.txt"`` for EVERY
# free-text message, so any request — including "запусти в оболочке команду ls"
# and plain conversation — became a write of its own text, and the verifier
# finalized DONE on that non-empty artifact. The single resolution below is the
# only place a free-text request may be turned into an executable contract, and
# it never fabricates a file body out of the request text.

#: Shell lookalike first tokens a verb-less ASCII invocation may use.
_BARE_COMMAND_VERBS = frozenset(_SHELL_VERBS)
_ENGLISH_STOPWORDS = frozenset(
    {"a", "an", "the", "is", "are", "was", "were", "who", "what", "why", "how",
     "where", "me", "my", "you", "your", "and", "or", "to", "of", "in", "on",
     "it", "this", "that", "please", "do", "does", "can", "could"}
)


def _bare_shell_command(goal: str) -> str:
    """A verb-less ASCII invocation («ls -la /tmp», «pwd») is a command.

    Deliberately conservative: any Cyrillic text, a question mark, an unknown
    first token or an English stopword token means this is NOT a shell command
    (mirrors the router's Step 20b heuristic).
    """
    if not goal or "?" in goal or re.search(r"[А-Яа-яЁё]", goal):
        return ""
    try:
        import shlex

        argv = shlex.split(goal)
    except ValueError:
        return ""
    if not argv or argv[0].rsplit("/", 1)[-1].casefold() not in _BARE_COMMAND_VERBS:
        return ""
    for token in argv[1:]:
        if token.startswith("-"):
            continue
        if token.casefold().strip("\"'") in _ENGLISH_STOPWORDS:
            return ""
    return goal


def _route_intent(goal: str) -> IntentDecision | None:
    """Route the free text through the canonical IntentRouter (never fatal)."""
    try:
        from antigona.router.intent_router import IntentRouter

        return IntentRouter().route(goal)
    except Exception:  # pragma: no cover - router must never break planning
        return None


@dataclass(frozen=True)
class FreeTextRequest:
    """Canonical (free text → executable contract) resolution.

    ``answer_only`` is the fail-closed verdict: the request asks for no side
    effect (a conversation/answer), or the requested effect cannot be
    materialized faithfully from the text alone (an unresolvable shell command,
    a write with no derivable content). Such a request must never be turned
    into a file write of its own text, must never produce an artifact and must
    never reach DONE.
    """

    intent: str
    tool_name: str
    path: str
    content: str | None
    command: tuple[str, ...]
    answer_only: bool
    requires_approval: bool
    reason: str


#: Canonical intents whose requested effect IS a workspace write. A write
#: execution is the correct implementation for them — the file body may
#: legitimately have been drafted by the model ("создай файл X о …").
WRITE_EFFECT_INTENTS = frozenset(
    {
        "file_write",
        "file_write_read",
        "file_write_run",
        "file_write_fix_run",
        "task.file_write",
        "task.file_edit",
        "task.multi_file",
    }
)

#: Canonical intents whose effect is produced by a DIFFERENT, typed tool (MCP,
#: email, TTS, file-send). ``resolve_free_text_request`` reports them as
#: effect-free only because it cannot carry their typed parameters — a
#: limitation of this parser, NOT proof that the request has no effect. A
#: consumer must therefore never treat their ``answer_only`` verdict as "this
#: request performs nothing".
TYPED_EFFECT_INTENTS = frozenset(
    {"task.mcp", "task.email", "task.tts", "task.file_send"}
)

#: Reasons for which ``resolve_free_text_request`` PROVES that no side effect
#: can be materialized: an ACTION was requested but its command could not be
#: resolved (``task.shell``), or a read was requested with no named file
#: (``task.file_read``). These come from the goal parser's own action intents.
#:
#: Deliberately NOT in this set: the generic bucket-4 fallthrough reason
#: ``no_side_effect_requested:*``. It is produced for conversations, questions
#: AND for typed-parameter requests (mcp/email/TTS), so it cannot distinguish
#: "this request has no effect" from "this endpoint cannot express it" — a
#: consumer that treats it as proof would reject every MCP/e-mail/TTS task and
#: every write task whose goal the text parser reads as a plain sentence.
NO_EFFECT_REASONS = frozenset(
    {"shell_command_not_resolvable", "read_without_named_path"}
)

#: The unscoped target of the REMOVED free-text write path. A workspace write
#: to this exact target is the degraded "store the request (or the model's
#: answer) in a file" shape — it can never be an explicit contract, because a
#: caller with a real target names it. Used as the last discriminator by the
#: verifier's write-for-a-no-write-plan rule.
LEGACY_DEFAULT_TARGET = "task_output.txt"

#: Names that MEAN "write a file in the workspace". The TaskFlow contract uses
#: ``workspace.write_text``; ``workspace.write`` is the capability-registry id
#: (``tools/capability_registry.py``) that direct writers of ``CreateTask`` also
#: use. The orchestrator executes both as one write and the verifier must judge
#: both as one write — an alias must never slip past the self-write guard
#: (FP-L05d).
WRITE_TOOL_NAMES = ("workspace.write_text", "workspace.write")


def canonical_tool_name(tool_name: str) -> str:
    """Return the canonical name of ``tool_name`` (aliases collapsed).

    Args:
        tool_name: A tool name from a flow/step/plan.

    Returns:
        ``workspace.write_text`` for every write alias, the name unchanged
        otherwise.
    """
    return "workspace.write_text" if tool_name in WRITE_TOOL_NAMES else tool_name


def _answer_only(intent: str, reason: str, *, requires_approval: bool = False) -> FreeTextRequest:
    return FreeTextRequest(
        intent=intent,
        tool_name=ANSWER_ONLY_TOOL,
        path="stdout",
        content="",
        command=(),
        answer_only=True,
        requires_approval=requires_approval,
        reason=reason,
    )


#: Characters only a real interpreter can honour. Splitting such a string with
#: ``shlex.split`` yields the metacharacters as LITERAL argv tokens: the
#: compound ``multi_file`` plan (``mkdir -p demo && touch demo/a.txt …``) became
#: ``('mkdir', '-p', 'demo', '&&', 'touch', …)`` and the sandbox tried to run a
#: program literally named ``&&`` — the requested files were never created. The
#: previous planner wrapped these commands in ``sh -c``; deleting it (FP-L05)
#: regressed every compound shell goal.
#:
#: FP-L03d (live defect): operators are not the only interpreter-only syntax.
#: The WORD-EXPANSION constructs — glob ``*``/``?``, bracket ``[ ]``, brace
#: ``{ }`` and tilde ``~`` — are equally meaningless to ``shlex.split``: it
#: returned ``('ls', '/workspace/*.txt')``, so the sandbox ran ``ls`` with a
#: literal argument and answered ``No such file or directory`` even though the
#: ``*.txt`` files existed. Every one of them must reach a real interpreter.
_SHELL_METACHAR_RE = re.compile(r"[&|;<>$`\\\n*?\[\]{}~]")


def container_workspace_path() -> str:
    """Return the container path the workspace is mounted at (single source).

    The sandbox mounts the workspace directory at ``SandboxProfile.mount_target``
    and runs every command with that directory as its workdir, so this string is
    the only absolute path a sandbox command can legitimately name.
    """
    from antigona.sandbox.runner import SandboxProfile

    return SandboxProfile.mount_target


def _normalize_container_workspace_paths(raw: str) -> str:
    """Rewrite sandbox-container absolute workspace paths to relative ones.

    The task-creation gate (``result_safety.is_sensitive_path``) rejects EVERY
    absolute path form, and the canonical rule for sandbox commands is the
    relative form (``SandboxProfile.workdir`` *is* the mount target). Inside the
    container ``/workspace/a.txt`` and ``a.txt`` therefore name the same file —
    but only the relative form can be submitted at all: with the absolute form
    the flow was never created and the owner got «Не удалось отправить задачу»
    instead of output (live FP-L03d).

    Only that one container mount root is rewritten, and only as a whole path
    token: ``/etc/passwd`` stays absolute and is still refused by the gate,
    ``/workspacefoo`` is a different path and is left untouched.
    """
    prefix = re.escape(container_workspace_path())
    nested = re.sub(rf"(?<![\w./-]){prefix}/", "", raw)
    return re.sub(rf"(?<![\w./-]){prefix}(?![\w./-])", ".", nested)


def shell_argv(raw: str) -> tuple[str, ...]:
    """Return the argv for a shell command string (the single place that decides it).

    A command carrying shell metacharacters (``&&``, ``||``, ``|``, ``;``,
    redirects, command substitution, a newline) or word-expansion constructs
    (``*``, ``?``, ``[ ]``, ``{ }``, ``~``) must go through a real interpreter:
    ``("sh", "-c", raw)``. Everything else is split into argv tokens. An
    unparseable string also falls back to ``sh -c`` (fail-safe: the sandbox
    still gets a runnable command).

    The container's own workspace mount is rewritten into its relative form
    first (``/workspace/a.txt`` → ``a.txt``), because the sandbox workdir IS
    that mount and the creation gate refuses absolute path forms.

    Args:
        raw: The command as written by the user or built by the goal parser.

    Returns:
        The argv tuple, empty for an empty command.
    """
    text = _normalize_container_workspace_paths((raw or "").strip())
    if not text:
        return ()
    if _SHELL_METACHAR_RE.search(text):
        return ("sh", "-c", text)
    try:
        import shlex

        argv = tuple(shlex.split(text))
    except ValueError:
        return ("sh", "-c", text)
    return argv or ("sh", "-c", text)


def resolve_free_text_request(
    text: str, *, decision: IntentDecision | None = None
) -> FreeTextRequest:
    """Resolve a free-text request into tool/path/content/command (canonical).

    The single resolver used by ``POST /tasks``, the transport planner and the
    verifier's false-DONE guard. Intent comes from ``parse_goal`` (the dialogue
    path's own parser) AND the canonical ``IntentRouter``; entities come from
    ``parse_goal``. Nothing here ever falls back to "write the request text".

    Args:
        text: The raw owner message.
        decision: Optional already-computed router decision (avoids re-routing).

    Returns:
        A :class:`FreeTextRequest`. ``answer_only=True`` means: create no
        effect, write no file, expect no DONE.
    """
    goal = (text or "").strip()
    route = decision if decision is not None else _route_intent(goal)
    intent = str(getattr(route, "intent", "") or "")
    router_approval = bool(getattr(route, "requires_approval", False))

    plan = parse_goal(goal)
    lower = goal.casefold()

    # 1. Shell: the parser's compound plan or the router's task.shell verdict.
    if plan.intent in ("shell", "multi_file") or intent == "task.shell":
        raw_command = plan.command or (goal if intent == "task.shell" else "")
        if raw_command and not plan.command:
            raw_command = _bare_shell_command(raw_command)
        command: tuple[str, ...] = ()
        if raw_command:
            command = shell_argv(raw_command)
        if not command:
            # An action verb with no resolvable command must not degrade into a
            # write of the request text (the live false-DONE path).
            return _answer_only(
                "task.shell", "shell_command_not_resolvable", requires_approval=True
            )
        return FreeTextRequest(
            intent="shell" if plan.intent != "multi_file" else "multi_file",
            tool_name="sandbox.shell",
            path=plan.path if plan.intent == "multi_file" else "stdout",
            content=None,
            command=command,
            answer_only=False,
            requires_approval=True,
            reason="shell_plan",
        )

    # 2. File read: an explicitly named file only.
    router_path = str((getattr(route, "entities", {}) or {}).get("path") or "")
    if plan.intent == "file_read" or intent == "task.file_read":
        path = plan.path or router_path
        if not path:
            return _answer_only("task.file_read", "read_without_named_path")
        return FreeTextRequest(
            intent="file_read",
            tool_name="workspace.read_text",
            path=path,
            content=None,
            command=(),
            answer_only=False,
            requires_approval=router_approval,
            reason="read_plan",
        )

    # 3. File write: only to a NAMED path and only with the requested content.
    write_intents = WRITE_EFFECT_INTENTS
    if plan.intent in write_intents or intent in write_intents:
        path = plan.path or router_path
        content = plan.content or ""
        if not path:
            # A blind write with no named target is exactly how the defect
            # recorded every message in task_output.txt.
            return _answer_only("file_write", "write_without_named_path")
        if not content.strip():
            if re.search(r"\b(?:пустой|empty)\s+(?:файл|file)\b", lower):
                content = ""  # an explicitly empty file IS the requested effect
            else:
                return _answer_only(
                    "file_write", "write_content_not_derivable", requires_approval=True
                )
        return FreeTextRequest(
            intent="file_write",
            tool_name="workspace.write_text",
            path=path,
            content=content,
            command=(),
            answer_only=False,
            requires_approval=router_approval,
            reason="write_plan",
        )

    # 4. Everything else asks for no side effect: a conversation, an answer, a
    # question, a command result — or an intent this endpoint cannot execute
    # (mcp/tts/email need typed params). Fail closed: no artifact, no DONE.
    if not goal:
        return _answer_only("conversation.noise", "empty_message")
    return _answer_only(intent or "conversation", f"no_side_effect_requested:{intent}")


def resolve_submit_contract(
    goal: str,
    *,
    tool_name: str | None = None,
    path: str | None = None,
    content: str | None = None,
    command: tuple[str, ...] | list[str] = (),
) -> FreeTextRequest:
    """Resolve a STRUCTURED submit whose caller did not name a tool (FP-L05d).

    The one rule for every submit surface that accepts a goal plus optional
    ``tool_name``/``path``/``content`` (``POST /flows``, the Telegram
    ``post_flow`` helper, the planner adapter):

    * an EXPLICIT ``tool_name`` is a contract — returned verbatim;
    * an explicit BODY the request itself does not supply (a non-empty
      ``content`` that is not the goal text) is an explicit draft contract —
      also returned verbatim, as ``workspace.write_text``;
    * anything else is FREE TEXT: the tool/target/body are resolved by
      :func:`resolve_free_text_request` — the same canonical resolver
      ``POST /tasks`` and the brain use. It never falls back to "write the
      request text".

    Args:
        goal: The submitted goal (the free-text request).
        tool_name: The tool the caller named, if any.
        path: The target the caller named, if any.
        content: The body the caller supplied, if any.
        command: The argv the caller supplied, if any.

    Returns:
        The resolved :class:`FreeTextRequest` for this submit.
    """
    explicit_command = tuple(command or ())
    body = (content or "").strip()
    if tool_name is not None:
        return FreeTextRequest(
            intent="explicit_tool",
            tool_name=tool_name,
            path=path or LEGACY_DEFAULT_TARGET,
            content=content if content is not None else "",
            command=explicit_command,
            answer_only=tool_name == ANSWER_ONLY_TOOL,
            requires_approval=False,
            reason="explicit_tool_contract",
        )
    if body and body != (goal or "").strip():
        # A caller-supplied body the request never mentioned: the caller owns
        # this draft (tests/HITL submit path+content on purpose).
        return FreeTextRequest(
            intent="explicit_draft",
            tool_name="workspace.write_text",
            path=path or LEGACY_DEFAULT_TARGET,
            content=content or "",
            command=explicit_command,
            answer_only=False,
            requires_approval=False,
            reason="explicit_draft_contract",
        )
    request = resolve_free_text_request(goal)
    if request.answer_only:
        return request
    if request.command and not explicit_command:
        return request
    if explicit_command and not request.command:
        return FreeTextRequest(
            intent=request.intent,
            tool_name=request.tool_name,
            path=request.path,
            content=request.content,
            command=explicit_command,
            answer_only=request.answer_only,
            requires_approval=request.requires_approval,
            reason=request.reason,
        )
    return request


def requires_exact_write_read_contract(goal: str, *, content: str | None = None, tool_name: str = "workspace.write_text") -> bool:
    """Return True for exact-content file writes that must expose write→read evidence.

    T07-style owner requests demand exact file content (often multi-line).  A
    plain write artifact is not enough evidence for the final report; the flow
    must read back the same file and report the read tool/result.
    """

    if tool_name != "workspace.write_text":
        return False
    text = (goal or "").casefold()
    if not text:
        return False
    exact_markers = (
        "ровно",
        "exact",
        "exactly",
        "точн",
        "двумя строк",
        "две строк",
        "two lines",
    )
    write_markers = ("создай", "создать", "запиши", "write", "create")
    content_markers = ("содержим", "content", "текст")
    has_explicit_content = bool((content or "").strip()) or bool(_extract_content(goal).strip())
    return (
        has_explicit_content
        and any(marker in text for marker in write_markers)
        and any(marker in text for marker in content_markers)
        and any(marker in text for marker in exact_markers)
    )

def parse_goal(goal: str) -> GoalPlan:
    """Классифицировать цель и извлечь entities (D7/D8).

    Приоритет: multi_file (папка + ≥2 файла) → shell → dialog → read+write →
    read → write → dialog (fail-safe: никогда не создаём write-флоу «вслепую»).
    """
    text = (goal or "").strip()
    if not text:
        return GoalPlan(intent="dialog", note="empty goal")
    lower = text.casefold()

    multi = _extract_multi_file(text)
    if multi:
        command, path, content = _build_multi_file_command(multi)
        return GoalPlan(
            intent="multi_file",
            path=path,
            content=content,
            command=command,
            expected_paths=tuple(expected_paths_from_goal(text)),
            note="compound shell plan (folder + files + summary)",
        )

    # L7-1: составная диагностическая цель «создай скрипт → запусти → исправь → перезапусти».
    # Проверяется ДО простого write_run и ДО shell.
    write_fix_run = _extract_write_fix_run(text)
    if write_fix_run:
        return GoalPlan(
            intent="file_write_fix_run",
            path=write_fix_run["path"],
            command=write_fix_run["command"],
            content=write_fix_run["content"],
            content_hint=write_fix_run["content_hint"],
            fix_content=write_fix_run.get("fix_content", ""),
            fix_command=write_fix_run.get("fix_command", write_fix_run["command"]),
            run_after_write=True,
            fix_after_run=True,
            expected_paths=tuple(expected_paths_from_goal(text)),
            planned_steps=(
                "workspace.write_text",
                "sandbox.shell",
                "workspace.write_text",
                "sandbox.shell",
            ),
            note="compound write→run→fix→run plan",
        )

    # B5: «создай <script> из описания → запусти его» проверяется ДО shell,
    # иначе «Запусти python square.py 12» выигрывает как обычная shell-цель,
    # имя файла и описание теряются, и файл не создаётся вообще.
    write_run = _extract_write_run(text)
    if write_run:
        return GoalPlan(
            intent="file_write_run",
            path=write_run["path"],
            command=write_run["command"],
            content_hint=write_run["content_hint"],
            run_after_write=True,
            expected_paths=tuple(expected_paths_from_goal(text)),
            note="compound write→run plan",
        )

    shell_command = _extract_shell_command(text)
    if shell_command:
        return GoalPlan(
            intent="shell",
            command=shell_command,
            expected_paths=tuple(expected_paths_from_goal(text)),
        )

    has_dialog = bool(_DIALOG_RE.search(text))
    has_read = any(marker in lower for marker in _READ_MARKERS)
    has_write = any(marker in lower for marker in _WRITE_MARKERS)
    # P0-017 / P0-041 canonical: «напиши» / English "write" / "create" сам по себе НЕ файловая
    # запись — нужен явный файловый объект (имя с расширением, «в файл», «файл», "file", "empty file").
    # «Напиши число 42» / "write a poem" остаётся диалогом;
    # «Напиши 42 в answer.txt» / "write hello.txt with content HELLO" — file_write.
    if not has_write:
        has_file_target = bool(
            _PATH_EXT_RE.search(text)
            or "в файл" in lower
            or " файл" in lower
            or "файле" in lower
            or " file" in lower
            or "file " in lower
            or "empty file" in lower
            or "пустой файл" in lower
        )
        if "напиши" in lower and has_file_target:
            has_write = True
        elif re.search(r"\b(?:write|create)\b", lower) and has_file_target:
            has_write = True

    if has_dialog and not (has_read or has_write):
        return GoalPlan(intent="dialog", expected_paths=())

    # R1-PARSER-01: never open a blind write flow for an interrogative request.
    if has_write and _is_interrogative_request(text):
        return GoalPlan(
            intent="dialog",
            expected_paths=(),
            note="interrogative_request",
        )

    path = _extract_path(text)
    content = _extract_content(text)

    if has_read and has_write:
        if _SUBSEQUENT_MODIFY_RE.search(text):
            return GoalPlan(
                intent="file_write_read",
                path=path,
                content=content,
                read_after_write=True,
                expected_paths=tuple(expected_paths_from_goal(text)),
                note="incomplete_sequence",
                incomplete_sequence=True,
            )
        return GoalPlan(
            intent="file_write_read",
            path=path,
            content=content,
            read_after_write=True,
            expected_paths=tuple(expected_paths_from_goal(text)),
        )
    if has_read:
        if _SUBSEQUENT_ANY_OP_RE.search(text):
            return GoalPlan(
                intent="file_read",
                path=path,
                expected_paths=tuple(expected_paths_from_goal(text)),
                note="incomplete_sequence",
                incomplete_sequence=True,
            )
        return GoalPlan(
            intent="file_read",
            path=path,
            expected_paths=tuple(expected_paths_from_goal(text)),
        )
    if has_write:
        if _SUBSEQUENT_ANY_OP_RE.search(text):
            return GoalPlan(
                intent="file_write",
                path=path,
                content=content,
                expected_paths=tuple(expected_paths_from_goal(text)),
                note="incomplete_sequence",
                incomplete_sequence=True,
            )
        return GoalPlan(
            intent="file_write",
            path=path,
            content=content,
            read_after_write=requires_exact_write_read_contract(text, content=content),
            expected_paths=tuple(expected_paths_from_goal(text)),
        )
    return GoalPlan(intent="dialog", note="no task markers; treated as dialogue")


def _is_path_like(value: str) -> bool:
    """Отфильтровать служебные слова — путь должен выглядеть как путь.

    A dot alone (e.g. ``Version 1.2 released``) is NOT a file path; the value
    must carry a recognised file extension or a path separator. This keeps
    quoted content from being misread as an expected path (BAM-6 regression:
    "Version 1.2" inside the goal's content must not become a required file).
    """
    if "/" in value or "\\" in value:
        return True
    if "." in value:
        tail = value.rsplit("/", 1)[-1].rsplit(".", 1)[-1].strip().lower()
        return bool(tail) and not any(ch.isspace() for ch in tail)
    return False


def expected_paths_from_goal(goal: str) -> list[str]:
    """Все имена путей/файлов, упомянутые в цели (для fail-closed верификации).

    Возвращает нормализованные относительные формы. Если цель не называет
    ни одного пути — пустой список (проверка пропускается).
    """
    found: list[str] = []
    for match in _QUOTED_RE.finditer(goal):
        candidate = _normalize_path(match.group(1))
        if candidate and _is_path_like(candidate) and candidate not in found:
            found.append(candidate)
    named_spans: list[tuple[int, int]] = []
    for match in _NAMED_FILE_RE.finditer(goal):
        named_spans.append(match.span(1))
        candidate = _normalize_path(match.group(1))
        if candidate and candidate not in found:
            found.append(candidate)
    for match in _PATH_EXT_RE.finditer(goal):
        if any(start <= match.start(1) and match.end(1) <= end for start, end in named_spans):
            continue
        candidate = _normalize_path(match.group(1))
        if candidate and candidate not in found:
            found.append(candidate)
    for match in _FILE_NOUN_RE.finditer(goal):
        token = match.group(1).strip()
        if token.casefold() in _STOPWORD_TOKENS:
            continue
        candidate = _normalize_path(token)
        if candidate and _is_path_like(candidate) and candidate not in found:
            found.append(candidate)
    # absolute paths (/etc/passwd, ../../../etc/passwd) after read/cat markers
    for m in re.finditer(r"(?:прочитай|прочти|прочитать|cat|read)\s+(/[^\s,;:.]+(?:\.[^\s,;:.]+)?)", goal, re.IGNORECASE):
        candidate = _normalize_path(m.group(1))
        if candidate and candidate not in found:
            found.append(candidate)
    return found


__all__ = [
    "ANSWER_ONLY_TOOL",
    "LEGACY_DEFAULT_TARGET",
    "NO_EFFECT_REASONS",
    "TYPED_EFFECT_INTENTS",
    "WRITE_EFFECT_INTENTS",
    "FreeTextRequest",
    "GoalPlan",
    "expected_paths_from_goal",
    "parse_goal",
    "requires_exact_write_read_contract",
    "WRITE_TOOL_NAMES",
    "canonical_tool_name",
    "resolve_free_text_request",
    "resolve_submit_contract",
    "shell_argv",
    "strip_code_fences",
]
