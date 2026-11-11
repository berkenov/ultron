"""Policy tables: roles, model tiers, budgets, caps, prompt templates.

Data only. The decisions behind these values live in
docs/design-orchestrator.md; this file is their machine-readable form.
Changing a value here is a policy change, not an implementation detail.
"""

from __future__ import annotations

from dataclasses import dataclass

# What an executor may claim about its own work (structured output "status").
EXECUTOR_STATUSES = ("done", "partial", "failed", "blocked")

# What the runtime decides about a task (TaskResult "status").
VERDICTS = ("accepted", "rejected", "blocked", "failed")

# Fields every role returns; role schemas add their own on top.
COMMON_FIELDS = ("status", "summary", "evidence", "open_questions", "next_action")


@dataclass(frozen=True)
class Role:
    name: str
    writes: bool
    tiers: tuple[str, ...]  # tiers[0] is the default model
    answer_template: str  # appended to every brief as the ОТВЕТ block

    @property
    def model(self) -> str:
        return self.tiers[0]


ANSWER_EXPLORE = (
    "ОТВЕТ — Ответ строго по схеме. map — только файлы, относящиеся к задаче, до 40. "
    "facts — по каждому утверждению из ЗАДАЧИ: вердикт и якорь путь:строка; "
    "если не найдено — написать, где искал. summary до 5 предложений. "
    "evidence — до 12 якорей с выдержкой не длиннее 3 строк. Сырые логи не вставлять. "
    "Якоря путь:строка проверяет runtime: файл, номер строки и выдержка должны совпадать с кодом дословно; неподтверждённый якорь возвращает задачу."
)
ANSWER_CODE = (
    "ОТВЕТ — Ответ строго по схеме. changed_files — каждый тронутый файл за все попытки. "
    "commands — гейт и его итог одной фразой, без вывода. Что не сделано — в not_done, "
    "а не в summary. patch оставить пустым, если не сказано иное. summary до 5 предложений."
)
ANSWER_VERIFY = (
    "ОТВЕТ — Ответ строго по схеме. В gate — команда, код выхода и счётчики passed/failed/skipped. "
    "В failures — каждое падение с якорем путь:строка и вероятной причиной в коде. "
    "Сырой лог не вставлять. summary до 5 предложений. "
    "Якоря путь:строка проверяет runtime: файл, номер строки и выдержка должны совпадать с кодом дословно; неподтверждённый якорь возвращает задачу."
)
ANSWER_REVIEW = (
    "ОТВЕТ — Ответ строго по схеме. verdicts — по каждому утверждению из ЗАДАЧИ с якорем. "
    "risks — только с привязкой к файлу или «не найдено». Общие соображения без якоря "
    "не засчитываются. summary до 5 предложений. "
    "Якоря путь:строка проверяет runtime: файл, номер строки и выдержка должны совпадать с кодом дословно; неподтверждённый якорь возвращает задачу. "
    "Каждый verdict со значением false или partially и каждый risk обязаны иметь evidence с выдержкой у того же якоря."
)

ROLES: dict[str, Role] = {
    "explore": Role("explore", writes=False, tiers=("gemini-3.8-flash-high", "gemini-4-argon-medium"), answer_template=ANSWER_EXPLORE),
    "code": Role("code", writes=True, tiers=("gemini-4-argon-high", "claude-sonnet-5-5-high"), answer_template=ANSWER_CODE),
    "verify": Role("verify", writes=False, tiers=("gemini-3.8-flash-high", "gemini-4-argon-medium"), answer_template=ANSWER_VERIFY),
    "review": Role("review", writes=False, tiers=("gemini-4-argon-high", "claude-opus-5-5-high"), answer_template=ANSWER_REVIEW),
}


@dataclass(frozen=True)
class Budget:
    attempts_per_tier: int = 2
    executor_tokens: int = 500_000
    minutes: int = 20


DEFAULT_BUDGET = Budget()

# Lines the runtime appends to every brief; Claude never types them.
GIT_BAN = "Коммиты не создавать, git не трогать."
READONLY_BAN = "Файлы не менять. git не трогать."
GATE_NOTE = "Гейт прогони сам перед ответом; оркестратор прогонит его ещё раз независимо."

# Size caps: exceeding them is a warning in the compact view, never a rejection.
CAPS = {
    "summary_chars": 700,
    "evidence": 12,
    "open_questions": 5,
    "map": 40,
    "facts": 20,
    "changed_files": 60,
    "commands": 10,
    "failures": 20,
    "warnings": 10,
    "verdicts": 30,
    "risks": 15,
}
COMPACT_MAX_LINES = 40  # the context firewall: what Claude sees per result
GATE_TAIL_LINES = 10  # lines of gate output quoted back to the executor
GATE_LOG_MAX_BYTES = 200_000  # gate.log is truncated beyond this

# Where task state and the journal live: outside any repository.
STATE_ROOT_ENV = "ULTRON_STATE"
DEFAULT_STATE_ROOT = "~/.ultron/state"

# Executor defaults.
AGY_TIMEOUT = "20m"
AGY_TIMEOUT_MAX_MINUTES = 60

# Quick read-only questions (`ultron ask`): no spec, no snapshots, no gate.
ASK_MODEL = "gemini-3.8-flash-high"
ASK_TIMEOUT = "5m"
ASK_MAX_CHARS = 6000  # what Claude sees; the full answer is saved next to the journal
ASK_NOTE = (
    "Ты консультант, только чтение. " + READONLY_BAN + " "
    "Без вступлений, до 40 строк. Если вопрос про код проекта, ответ — список мест, а не рассказ: "
    "на каждое место строка `путь:строка` — дословная выдержка 1–3 строки — одна фраза, зачем оно. "
    "Пути относительно корня проекта, имена функций и классов — в обратных кавычках и дословно. "
    "После списка — вывод в 1–3 предложения. Последней строкой «Не нашёл:» — что искал и не нашёл, или «ничего». "
    "Якоря и имена сверяет программа: несуществующая строка или имя будет помечено. "
    "Если вопрос не про код — краткий ответ со ссылками на источники."
)
ASK_MIN_LINES = 300  # named files shorter than this in total are cheaper for Claude to read itself
NOTE_TARGETS = ("self", "subagent", "flash", "argon")  # who a piece of work went to (`ultron note`)

# Pacing: minimum pause between executor calls, so runs do not look like a flood
# to the subscription's abuse filters. 0 disables.
PACE_SECONDS = 5
PACE_ENV = "ULTRON_PACE_SECONDS"

# Windows `ultron usage` reports: the subscription refreshes every five hours
# under a weekly cap. Google publishes no numbers, so this only counts our side.
USAGE_WINDOWS = (("5 ч", 5 * 3600), ("7 дн", 7 * 86400))

# Learned limits (`ultron budget`). Google publishes no quota numbers, so the
# runtime learns them from its own quota failures; nothing is enforced before
# the first one. See ultron/limits.py.
QUOTA_PATTERN = r"quota|rate.?limit|resource.?exhausted|too many requests|usage limit|limit reached|\b429\b"
QUOTA_BLOCK_HOURS = 5  # first block equals the refresh window; doubles per repeated failure
QUOTA_BLOCK_MAX_HOURS = 7 * 24
LIMIT_SAFETY = 0.8  # work up to this share of the lowest usage a failure was seen at
LIMIT_GROWTH_PER_DAY = {"5 ч": 0.02, "7 дн": 0.01}  # a quiet ceiling is probed upward
LIMIT_GROWTH_MAX = 1.2
LIMIT_FORGET_DAYS = 45  # failures older than this no longer bound anything
SAVING_SHARE = 0.5  # from this share of the learned limit: reading only, cheap model
BRIEF_WARN_CHARS = 4000  # a longer brief means the handoff likely costs more than it saves
