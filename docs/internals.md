# Внутреннее устройство ultron: модули и сигнатуры

Это карта пакета `ultron/` для того, кто пишет или читает код. Арбитр —
тесты в `tests/`: если текст здесь и тест расходятся, прав тест.
Решения и контракт — в [design-orchestrator.md](design-orchestrator.md) и
[contract.md](contract.md); политика — в `ultron/policy.py`.

Только стандартная библиотека, Python 3.10+. Код и комментарии по-английски,
строки для человека (компактный вид, ошибки, сообщения исполнителю) — по-русски.

## Поток

```
spec.json ──load_spec──▶ Spec
brief.md + inputs ──compose_prompt──▶ prompt
prompt ──Executor.run──▶ Envelope            (agy; в тестах FakeExecutor)
Envelope + git + гейт ──check_all──▶ CheckReport
CheckReport ──retry_message / escalation_prompt──▶ следующий prompt   (цикл в run_task)
Attempt* ──build_result──▶ TaskResult (dict по schemas/result.json)
TaskResult ──compact──▶ ≤ 40 строк для Claude
всё остальное ──TaskState──▶ ~/.ultron/state/<slug>/tasks/<id>/
```

## `ultron/policy.py` — данные, уже написан

`ROLES`, `Role`, `Budget`, `DEFAULT_BUDGET`, `CAPS`, `GIT_BAN`, `READONLY_BAN`,
`GATE_NOTE`, `COMPACT_MAX_LINES`, `GATE_TAIL_LINES`, `GATE_LOG_MAX_BYTES`,
`STATE_ROOT_ENV`, `DEFAULT_STATE_ROOT`, `AGY_TIMEOUT`, `EXECUTOR_STATUSES`,
`VERDICTS`, `COMMON_FIELDS`.

## `ultron/spec.py` — TaskSpec

```python
class SpecError(ValueError): ...

@dataclass(frozen=True) class Scope:        write: tuple[str, ...] = (); forbid: tuple[str, ...] = ()
@dataclass(frozen=True) class InputRef:     task: str; fields: tuple[str, ...] = ("summary",)
@dataclass(frozen=True) class Acceptance:   cmd: str; expect_exit: int = 0
@dataclass(frozen=True) class ExecutorOpts: model: str | None = None; effort: str | None = None; mode: str | None = None
@dataclass(frozen=True) class TaskBudget:   attempts_per_tier: int; tiers: tuple[str, ...]; executor_tokens: int; minutes: int
@dataclass(frozen=True) class Spec:
    id: str; role: str; dir: Path; brief: Path; goal: str
    scope: Scope; inputs: tuple[InputRef, ...]; acceptance: tuple[Acceptance, ...]
    budget: TaskBudget; executor: ExecutorOpts
    @property def role_policy(self) -> Role        # policy.ROLES[self.role]
    @property def writes(self) -> bool             # role_policy.writes and executor.mode != "plan"

def parse_spec(data: dict, base_dir: Path) -> Spec
def load_spec(path: Path) -> Spec                  # json → parse_spec(data, path.parent)
```

Правила `parse_spec`:

- обязательны `id`, `role`, `dir`, `brief`; неизвестный ключ на любом уровне — `SpecError`
  с именем ключа; неизвестная роль — `SpecError` со словом `role`;
- `id` по шаблону `^[A-Za-z0-9._-]+$`;
- `dir` — абсолютный путь к существующему каталогу; `brief` — существующий файл,
  относительный путь считается от `base_dir`;
- `budget.tiers` по умолчанию `ROLES[role].tiers`; `executor.model` заменяет
  первую ступень; `attempts_per_tier`, `executor_tokens`, `minutes` — из
  `DEFAULT_BUDGET`, если не заданы;
- `acceptance[*].expect_exit` по умолчанию 0; `inputs[*].fields` по умолчанию `("summary",)`.

## `ultron/compose.py` — сборка текста для исполнителя

```python
def compose_prompt(spec: Spec, brief_text: str, inputs: Mapping[str, dict]) -> str
def retry_message(report: CheckReport, spec: Spec) -> str
def escalation_prompt(spec: Spec, brief_text: str, inputs: Mapping[str, dict], previous: Sequence[AttemptSummary]) -> str
@dataclass(frozen=True) class AttemptSummary: number: int; model: str; summary: str; violations: tuple[str, ...]; gate_tail: tuple[str, ...]
```

`compose_prompt` собирает, в этом порядке, разделяя пустой строкой:

1. `brief_text` без хвостовых пробелов;
2. для каждого `InputRef`: заголовок `ДАНО ИЗ <task>` и под ним `<field>: <значение>` —
   строки как есть, списки и словари как JSON с `ensure_ascii=False`; `inputs`
   — словарь `task_id → TaskResult`; поле ищется сначала в корне результата,
   потом в `role_fields`; отсутствующее поле — строка `<field>: нет`;
3. `ОБЪЁМ — менять только: a, b. Не трогать: c, d.` для пишущей роли (пустой
   `write` → `менять только: любые файлы в рабочем каталоге`); для роли без записи
   или режима `plan` — `ОБЪЁМ — ` + `READONLY_BAN`;
4. `GATE_NOTE`, если роль пишет и `acceptance` непуст;
5. `role_policy.answer_template`;
6. `GIT_BAN` — последняя строка, ровно один раз.

`retry_message` начинается со строки `Приёмка не пройдена.`; дальше по одной
секции на причину: гейт (`Гейт: <cmd> — код выхода <n>, ожидался <m>.` и
`Хвост вывода:` с `GATE_TAIL_LINES` строками), объём (`Нарушения объёма — откати эти файлы:` и дальше каждая строка
`report.scope` дословно, по одной на строку; для роли без записи после них —
`READONLY_BAN`), контракт (`Ответ не
по схеме: <нарушения через ;>`); в конце — `Исправь причину в разрешённых
файлах (<write или «рабочий каталог»>). <forbid> не трогать.` (вторая фраза
только при непустом forbid), `GIT_BAN` и `Ответ строго по схеме: changed_files
— все тронутые файлы за все попытки.`

`escalation_prompt` = `compose_prompt` + блок `ПРЕДЫДУЩИЕ ПОПЫТКИ` с строкой на
попытку: `#<n> (<model>): <summary>; нарушения: <…>` и хвост гейта, если был.

## `ultron/executor.py` — конверт и запуск agy

```python
@dataclass(frozen=True) class Envelope:
    status: str | None; conversation_id: str | None; structured_output: dict | None
    denied_actions: tuple[str, ...]; usage: dict; num_turns: int | None; duration_seconds: float
    raw: dict; noise: tuple[str, ...]
    @classmethod def from_output(cls, text: str) -> "Envelope"
    @property def ok(self) -> bool            # status == "SUCCESS" and not denied_actions and structured_output is not None

class Executor(Protocol):
    def run(self, prompt: str, *, dir: Path, model: str, schema: Path, conversation: str | None = None,
            mode: str | None = None, effort: str | None = None, timeout: str = AGY_TIMEOUT) -> Envelope: ...

class AgyExecutor:
    def __init__(self, binary: str = "agy", grace_seconds: int = 60): ...
    def command(self, prompt, *, dir, model, schema, conversation=None, mode=None, effort=None, timeout=AGY_TIMEOUT) -> list[str]
    def run(...) -> Envelope

def parse_timeout(text: str) -> int         # "20m" → 1200, "90s" → 90, "1h" → 3600, "15" → 15; иначе ValueError
```

`from_output`: последняя строка, начинающаяся с `{`, которая разбирается как JSON
и содержит `conversation_id` или `status`, — конверт; остальные непустые строки —
`noise`. `structured_output` берётся из одноимённого поля, иначе из `response`,
если это JSON-объект. `denied_actions` — кортеж `display_name`. Нет конверта —
`status="NO_OUTPUT"`. `command` начинается с `[binary, "--dangerously-skip-permissions", "-p", prompt, "--add-dir", str(dir), "--model", model, "--output-format", "json", "--json-schema", str(schema), "--print-timeout", timeout]`, дальше при наличии `--conversation`, `--mode`, `--effort`. `run` ловит `TimeoutExpired`
(таймаут = `parse_timeout(timeout) + grace_seconds`) и возвращает `status="TIMEOUT"`; stdout и stderr
читаются раздельно, конверт ищется в stdout, `noise` — непустые строки stdout вне конверта плюс хвост stderr.

## `ultron/check.py` — проверки

```python
@dataclass(frozen=True) class GateRun: cmd: str; exit_code: int; expected: int; tail: tuple[str, ...]; log: str
    @property def ok(self) -> bool
@dataclass(frozen=True) class CheckReport:
    contract: tuple[str, ...]; scope: tuple[str, ...]; gate: tuple[GateRun, ...]; anchors: tuple[str, ...]; warnings: tuple[str, ...]
    @property def ok(self) -> bool                      # нет нарушений contract/scope/anchors и все gate.ok
    @property def violations(self) -> tuple[str, ...]   # contract + scope + anchors + «гейт <cmd>: код <n>, ожидался <m>»

def load_role_schema(role: str, schema_dir: Path | None = None) -> dict   # ultron/schemas/roles/<role>.json
def check_contract(env: Envelope, role: Role, schema: dict) -> tuple[list[str], list[str]]
def git_status(dir: Path) -> set[str] | None            # строки `git status --porcelain`; None вне репозитория
def changed_paths(before: set[str], after: set[str]) -> list[str]
def check_scope(changed: Sequence[str], spec: Spec) -> list[str]
def run_gate(spec: Spec) -> list[GateRun]
def check_anchors(so: dict, role: policy.Role, root: Path) -> list[str]
def check_all(env: Envelope, spec: Spec, schema: dict, before: set[str] | None, after: set[str] | None) -> CheckReport
```

- `check_contract`: нарушения — `agy status=<…>` если не `SUCCESS`; `исполнитель без прав: <имена>`;
  `нет structured_output: ответ не по схеме`; `нет обязательных полей: <…>`;
  `status=<…> вне done/partial/failed/blocked`. Предупреждения — превышение
  `CAPS` (`<поле>: <n> при лимите <cap>`), `status=done, но changed_files и patch пусты` для `code`.
- `changed_paths`: записи из `after − before`, путь = строка без первых трёх
  символов; для переименований `R  a -> b` — `b`; сортировка. Байткод не
  считается изменением: записи с `__pycache__/` в пути и файлы `.pyc` отбрасываются
  здесь, и только здесь — выше по стеку фильтров нет.
- `check_scope`: роль без записи или `mode == "plan"` — любое изменение нарушение
  `роль <role> без записи, но изменено: <пути>`; пишущая роль — путь вне `write`
  (пустой `write` = без ограничений) или внутри `forbid` → `вне разрешённого объёма: <путь>`.
  Совпадение — точное или по префиксу каталога (`tests/` покрывает `tests/x.py`).
  Также, если задан `scope.max_diff_lines`, работает `check_diff_size`: при превышении числа
  строк диффа добавляется нарушение `правка больше лимита: ...`. Файлы в этом случае
  целиком возвращаются к baseline.
- `run_gate`: команды по порядку через `subprocess.run(cmd, shell=True, cwd=spec.dir)`,
  объединённый stdout+stderr, обрезка лога до `GATE_LOG_MAX_BYTES`, хвост из
  `GATE_TAIL_LINES` строк; после первого несовпадения кода выхода — стоп.
- `check_anchors`: проверка формата якорей, существования файла, номера строки и совпадения `excerpt` для `evidence` (длиной от 12 символов); плюс наличие evidence для негативных verdict и risk. Нарушения возвращаются списком. Для роли code не выполняется.
- `check_all`: гейт запускается только если контракт и объём чисты и роль пишет;
  для ролей без записи `gate` пуст, если `acceptance` пуст.

## `ultron/state.py` — состояние и журнал

```python
def state_root() -> Path                     # $ULTRON_STATE или ~/.ultron/state
def project_slug(project_dir: Path) -> str   # "/Users/x/dev/ultron" → "-Users-x-dev-ultron"
class TaskState:
    def __init__(self, root: Path, project_dir: Path, task_id: str)
    root, project_dir, task_id: атрибуты как переданы
    dir, spec_path, brief_path, attempts_path, gate_log_path, patch_path, result_path: Path
    def save_spec(self, data: dict) -> None; def load_spec(self) -> dict | None
    def save_brief(self, text: str) -> None
    def append_attempt(self, record: dict) -> None; def load_attempts(self) -> list[dict]
    def save_gate_log(self, text: str) -> None
    def save_patch(self, text: str) -> Path
    def save_result(self, result: dict) -> None; def load_result(self) -> dict | None
    def journal_append(self, record: dict) -> None      # <root>/<slug>/journal.jsonl
def list_tasks(root: Path, project_dir: Path) -> list[str]
```

Каталоги создаются при первой записи. JSON — `ensure_ascii=False`, по строке на
запись в `.jsonl`. `reset()` удаляет attempts, gate.log, patch и result задачи,
оставляя каталог; `run` вызывает его перед стартом, `retry` — нет.

## `ultron/view.py` — context firewall

```python
def compact(result: dict, max_lines: int = COMPACT_MAX_LINES) -> str
def evidence_text(attempts: Sequence[dict], evidence_id: str) -> str | None
```

Первая строка: `=== <id> | <role> | <status> | attempts <n> | tier <model> | <s> s | in <k> cache <k> out <k>`.
Дальше `summary:`, секции роли из `role_fields` (как в `prototype/worker.py`),
`checks: contract <ok|violated> · scope <ok|violated|skipped> · gate [<exit>] attempt <n>`
(без гейта — `gate —`), `changed_files:` (и `claimed:` только при расхождении), `evidence (n):`
ids с якорями без выдержек, `open_questions`, `next_action`, `violations:` при
не-accepted, последняя строка `state: <dir>`. Длиннее `max_lines` — обрезать и
завершить строкой `… ещё <n> строк: ultron show <id> --raw`. Ни одна строка не
начинается с `{`. `evidence_text` ищет id в `evidence` последней попытки с
`structured_output` и возвращает `<id> @ <where>\n<excerpt>`.

## `ultron/run.py` — цикл

```python
@dataclass class Attempt: number: int; tier_index: int; model: str; conversation: str | None; prompt: str; envelope: Envelope; report: CheckReport; changed: list[str]
def run_task(spec: Spec, *, executor: Executor, state: TaskState, inputs: Mapping[str, dict] | None = None,
             schema_dir: Path | None = None, clock: Callable[[], float] = time.monotonic) -> dict
def build_result(spec: Spec, attempts: Sequence[Attempt], status: str, changed_files: Sequence[str], state: TaskState) -> dict
```

Цикл `run_task` — как в design-orchestrator.md, раздел 4:

0. `inputs is None` → результаты берутся из состояния того же проекта:
   `TaskState(state.root, spec.dir, ref.task).load_result()`; нет результата → `SpecError`
   (`inputs: нет результата задачи <id>`).
1. `state.reset()`; `before = git_status(spec.dir)`; сохранить spec (dict с абсолютными
   `dir` и `brief`, остальное как в файле) и первый prompt в состояние.
2. Попытка: `executor.run(prompt, …)`; `after = git_status`; `report = check_all`;
   `append_attempt` (конверт как есть плюс `_meta`: number, tier, model, violations, warnings, changed);
   `journal_append` тем же составом плюс `task`, `role`, `verdict` попытки.
3. Сначала `executor_status == "blocked"` → `blocked`, стоп — даже если все проверки
   зелёные: исполнитель просит решение, принимать нельзя. Потом `report.ok` → `accepted`, стоп.
   Конверт без `structured_output` со статусом `NO_OUTPUT`/`TIMEOUT` → `failed`, стоп.
   Сумма `usage.total_tokens` > `budget.executor_tokens` или прошло больше
   `budget.minutes` → `failed`, стоп (нарушение `budget: …` в `checks.violations`).
4. Иначе: попыток на ступени меньше `attempts_per_tier` → `retry_message` в ту же
   сессию; есть следующая ступень → `escalation_prompt`, новая сессия
   (`conversation=None`), следующая модель; иначе → `rejected`.
5. `changed_files` в результате — `changed_paths(before, git_status(spec.dir))` после последней попытки.
   `claimed_files` — пути из `changed_files` последнего `structured_output`.
   `role_fields` — последний `structured_output` без `COMMON_FIELDS`. `usage` — суммы.
   `refs.patch` — путь, если последний ответ содержал непустой `patch` (сохраняется через `save_patch`).
6. `save_result`; вернуть dict по `schemas/result.json` (`role` — роль задачи).

## `ultron/cli.py` — интерфейс

```python
def main(argv: Sequence[str] | None = None, executor: Executor | None = None) -> int
```

```
ultron run SPEC [--state ROOT] [--dry-run]
ultron show TASK [--dir DIR] [--state ROOT] [--evidence ID | --gate | --diff | --raw | --brief]
ultron retry TASK --message FILE [--dir DIR] [--state ROOT]
ultron list [--dir DIR] [--state ROOT]
```

- `run`: печатает компактный вид; код выхода 0 при `accepted`, 2 иначе; `--dry-run`
  печатает команду `agy` (бриф заменён на `<бриф N символов>`) и ничего не запускает.
- `show`: без флагов — компактный вид из `result.json`; `--evidence` — `evidence_text`;
  `--gate` — `gate.log`; `--diff` — `git diff --stat` и diff по `changed_files`; `--raw` —
  `result.json` целиком; `--brief` — отправленный текст. Нет задачи — сообщение и код 1.
- `retry`: читает `spec.json` из состояния (`parse_spec(data, state.dir)`), берёт
  `conversation_id` и модель последней попытки из `attempts.jsonl`, отправляет
  сообщение из файла в ту же сессию как ещё одну попытку (номер продолжает счёт),
  повторяет проверки, перезаписывает `result.json`; печатает компактный вид;
  код выхода как у `run`. Для этого в `run.py` есть
  `continue_task(spec, message, *, executor, state, schema_dir=None) -> dict`.
- `--dir` по умолчанию — текущий каталог; `executor` в `main` — для тестов.

`ultron/__main__.py` вызывает `main()`. `bin/ultron` — шим на `python3 -m ultron`
из каталога репозитория.

---

# Фаза 2: транзакционное дерево

Принцип: **runtime откатывает не «git», а дельту конкретной задачи или
попытки.** Пользовательские незакоммиченные правки, существовавшие до задачи,
неприкосновенны. `git checkout`, `git reset`, `git stash` по пользовательскому
дереву не используются нигде.

## `ultron/snapshot.py` — снимок рабочего состояния

```python
@dataclass(frozen=True) class Snapshot:
    label: str                      # "baseline", "attempt-1", …
    commit: str                     # sha коммита-снимка; ref refs/ultron/<task>/<label>
    tree: str                       # sha дерева
    files: Mapping[str, str]        # путь → sha блоба
    modes: Mapping[str, str]        # путь → режим из ls-tree ("100644", "100755", "120000")

def take_snapshot(repo: Path, task_id: str, label: str) -> Snapshot
def diff_paths(a: Snapshot, b: Snapshot) -> list[str]
def restore_paths(repo: Path, snap: Snapshot, paths: Sequence[str]) -> list[str]
def restore_all(repo: Path, current: Snapshot, target: Snapshot) -> list[str]
def patch_between(repo: Path, a: Snapshot, b: Snapshot) -> str
def list_snapshots(repo: Path, task_id: str) -> list[str]
def drop_snapshots(repo: Path, task_id: str) -> None
```

- `take_snapshot`: временный индекс (`GIT_INDEX_FILE` во временном файле),
  `git add -A .` в него (уважает `.gitignore`; настоящий индекс и дерево не
  меняются), `git write-tree`, `git commit-tree <tree> [-p HEAD, если HEAD есть]
  -m "ultron <task> <label>"` с авторством из окружения `GIT_AUTHOR_NAME=ultron`,
  `GIT_AUTHOR_EMAIL=ultron@local` и теми же committer-переменными (не зависеть
  от `git config` пользователя), `git update-ref refs/ultron/<task>/<label> <commit>`
  (повторный снимок с той же меткой перезаписывает ref). `files`/`modes` — из
  `git ls-tree -r -z <tree>`. Работает в репозитории без коммитов.
- `diff_paths`: пути, где sha различается, есть только в `a` (удалён) или только
  в `b` (добавлен); сортировка; байткод (`__pycache__/` в пути, `.pyc`) отбрасывается
  здесь, и только здесь.
- `restore_paths`: для пути из `snap.files` — записать байты `git cat-file blob <sha>`
  (создать каталоги; бит исполняемости по `modes`); для пути, которого в снимке
  нет, — удалить файл, если существует. Возвращает восстановленные пути.
- `restore_all(repo, current, target)` = `restore_paths(repo, target, diff_paths(current, target))`.
- `patch_between`: `git diff --binary <a.commit> <b.commit>`.
- `list_snapshots`: метки из `git for-each-ref refs/ultron/<task>/`, отсортированы.

## `ultron/check.py` — изменения

`git_status` и `changed_paths` удаляются: единственный источник «что изменилось» —
`diff_paths` по снимкам. Новая сигнатура:

```python
def check_all(env: Envelope, spec: Spec, schema: dict, changed: Sequence[str]) -> CheckReport
```

Гейт запускается всегда, когда `spec.acceptance` непуст, независимо от
контракта и объёма: код проверяется отдельно от доклада.

## `ultron/state.py` — добавления

```python
def attempt_patch_path(self, number: int) -> Path        # <dir>/attempt-<n>.diff
def save_attempt_patch(self, number: int, text: str) -> Path
```

`reset()` удаляет и `attempt-*.diff`.

## `ultron/compose.py` — изменения

```python
def retry_message(report: CheckReport, spec: Spec, restored: Sequence[str] = ()) -> str
def report_only_message(spec: Spec) -> str
def escalation_prompt(spec, brief_text, inputs, previous, previous_patch: Path | None = None) -> str
```

- `restored` непуст → секция `Файлы вне объёма восстановлены оркестратором из
  исходного состояния: <пути через запятую>. Не трогай их.` вместо просьбы откатить.
- `report_only_message`: `Код принят: гейт и объём в порядке. Отчёт не разобран.
  Пришли только отчёт строго по схеме, файлы не меняй.` + `GIT_BAN`.
- `previous_patch` задан → строка `Дерево возвращено к исходному состоянию;
  дифф предыдущей попытки лежит в <path>.` внутри блока `ПРЕДЫДУЩИЕ ПОПЫТКИ`.

## `ultron/run.py` — транзакционный цикл

```
0. inputs (как раньше); state.reset(); drop_snapshots(repo, spec.id)
1. baseline = take_snapshot(repo, spec.id, "baseline")
2. попытка n:
     env = executor.run(...)
     snap = take_snapshot(repo, spec.id, f"attempt-{n}")
     changed = diff_paths(baseline, snap)
     violations = check_scope(changed, spec)
     restored = []
     if violations: restored = restore_paths(repo, baseline, <пути из нарушений>)
                    snap = take_snapshot(repo, spec.id, f"attempt-{n}")   # после восстановления
                    changed = diff_paths(baseline, snap)
     report = check_all(env, spec, schema, changed)        # объём уже чист; гейт — по факту
     append_attempt: конверт + _meta{number, tier, model, violations, warnings,
                     changed, restored, snapshot{label, commit, tree}}
     journal_append: то же + task, role, verdict попытки
3. вердикт:
     executor_status == "blocked"                     → blocked        failure decision/blocked
     конверт NO_OUTPUT/TIMEOUT                         → failed         failure infra/no_output | infra/timeout
     бюджет токенов / минут                            → failed         failure infra/budget_tokens | infra/budget_minutes
     гейт ok, объём ok, контракт ok                    → accepted       failure none
     гейт ok, объём ok, контракт нарушен:
        report_retries == 0 → report_only_message в ту же сессию, report_retries = 1, не считается в attempts_per_tier
        иначе               → accepted, checks.contract = "missing", summary = "Отчёт исполнителя не получен; код принят по гейту и объёму.", executor_status = None
     иначе (гейт красный или объём нарушен и гейт красный):
        попыток на ступени < attempts_per_tier → retry_message(report, spec, restored) в ту же сессию
        есть следующая ступень → restore_all(repo, snap, baseline); patch = patch_between(baseline, snap)
                                 → save_attempt_patch(n, patch); escalation_prompt(..., previous_patch=path);
                                 новая сессия, следующая модель
        иначе → rejected     failure execution/gate | execution/scope | execution/contract (по последней причине)
4. результат: changed_files = diff_paths(baseline, take_snapshot(repo, spec.id, "final"));
   checks.scope ∈ {ok, restored, violated, skipped}; checks.restored = пути, восстановленные за все попытки;
   failure = {"domain": none|infra|execution|decision|semantic, "kind": …};
   refs.snapshots = {"baseline": sha, "attempts": [sha, …]}
```

`continue_task` (retry --message от Claude): та же baseline (ref из git), новая
попытка с номером далее по счёту, `_meta.kind = "semantic"`; при отклонении
`failure = semantic/<kind>`.

## `ultron/cli.py` — добавления

```
ultron rollback TASK --to baseline|attempt-N [--dir DIR] [--state ROOT]
```

Восстанавливает дерево к снимку: `restore_all(repo, take_snapshot(repo, task, "current"), target)`,
печатает восстановленные пути, код 0; нет снимка — сообщение и код 1.

## `ultron/view.py` — добавления

После `checks:` — строка `restored: <пути>` если есть; при не-accepted — строка
`failure: <domain>/<kind>`.

## Схемы

`schemas/result.json`: `checks.scope` enum + `restored`; `checks.restored`;
`failure`; `refs.snapshots`. `schemas/spec.json`: без изменений в этой части.

## Режим worktree (изоляция задачи)

Задача работает не в дереве пользователя, а в отдельном git worktree, созданном
от baseline-снимка, поэтому дерево пользователя не меняется до тех пор, пока
задача не принята. Откат при провале — удаление worktree.

### `spec.json`

```json
"workspace": {"mode": "worktree", "seed": ["ln -s ../../node_modules node_modules", "flutter pub get"]}
```

`mode` ∈ `inplace` (по умолчанию) | `worktree`; `seed` — команды, которые
выполняются в новом worktree до исполнителя (зависимости, симлинки). В `spec.py`:
`@dataclass(frozen=True) class Workspace: mode: str = "inplace"; seed: tuple[str, ...] = ()`,
поле `Spec.workspace`; неизвестный `mode` — `SpecError`.

### `ultron/workspace.py`

```python
class WorkspaceError(RuntimeError): ...
def worktree_root(state_root: Path, project_dir: Path) -> Path      # <state_root>/<slug>/worktrees
def worktree_path(state_root: Path, project_dir: Path, task_id: str) -> Path   # …/worktrees/<task_id>
def create_worktree(repo: Path, path: Path, commit: str) -> Path   # существующий — удалить; git worktree prune; git worktree add --detach <path> <commit>
def seed(path: Path, commands: Sequence[str]) -> list[GateRun]     # по порядку, shell=True, cwd=path; первый ненулевой код → WorkspaceError с командой, кодом и хвостом
def remove_worktree(repo: Path, path: Path) -> None                # git worktree remove --force <path> (отсутствие — не ошибка); git worktree prune
def integrate(repo: Path, patch: str) -> str                       # "" → "empty"; git apply --check (патч через stdin) не прошёл → "conflict"; иначе git apply → "applied"
```

### `ultron/check.py`

`run_gate(spec, cwd: Path | None = None)` и `check_all(..., cwd: Path | None = None)`:
гейт выполняется в `cwd`, если задан, иначе в `spec.dir`.

### `ultron/run.py`

В режиме `worktree`:

1. `baseline = take_snapshot(spec.dir, …)` — в дереве пользователя, со всеми
   незакоммиченными правками.
2. `work = create_worktree(spec.dir, worktree_path(state.root, spec.dir, spec.id), baseline.commit)`;
   `seed(work, spec.workspace.seed)`; ошибка посева → `failed`, `failure = infra/seed`,
   исполнитель не вызывается.
3. Исполнитель, снимки, восстановление и гейт работают в `work`
   (`executor.run(dir=work)`, `take_snapshot(work, …)`, `check_all(..., cwd=work)`).
   Объекты и refs общие с основным репозиторием.
4. `accepted` → `patch = patch_between(work, baseline, final)`; `integrate(spec.dir, patch)`:
   `applied` → `remove_worktree`; `conflict` → патч сохраняется через
   `state.save_patch`, worktree остаётся; `empty` → worktree удаляется.
   `refs.integration` ∈ `applied | conflict | empty`; в режиме `inplace` — `null`.
5. Любой другой вердикт → worktree остаётся для диагностики; `refs.worktree = <path>`.
6. `continue_task` в этом режиме работает в существующем worktree; его отсутствие — `WorkspaceError`.

### `ultron/cli.py`

```
ultron integrate TASK [--dir DIR] [--state ROOT]   # повторить integrate сохранённым патчем; печатает итог; 0 = applied
ultron clean TASK [--dir DIR] [--state ROOT]       # remove_worktree; снимки в git остаются
```

### `ultron/view.py`

При `refs.integration` не `null` — строка `integration: <итог>`; при `conflict` —
` · патч: <refs.patch>`. При `refs.worktree` — строка `worktree: <path>`.

### Схема результата

`refs.worktree: string | null`, `refs.integration: applied | conflict | empty | null`.
