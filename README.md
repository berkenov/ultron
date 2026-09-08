# ultron

Утилита взаимодействия Claude и Gemini: Claude планирует и принимает, исполнитель
через Antigravity CLI (`agy`) делает, между ними — контракт
`TaskSpec → Executor → TaskResult` и context firewall.

**Этап: фазы 1 и 2 собраны 09.09.2026** — 139 тестов, живые прогоны на учебном
проекте в обоих режимах прошли. Фаза 2 — транзакционное дерево: снимки рабочего
состояния в объекты git, восстановление чужих файлов из baseline самим runtime,
гейт независимо от доклада, чистая эскалация, режим worktree с переносом патча. Решения — [docs/design-orchestrator.md](docs/design-orchestrator.md),
раздел 0; карта модулей — [docs/internals.md](docs/internals.md). Утилита собрала
себя сама: Fable писал карту, тесты и брифы, исполнитель через `agy` — код,
брифы лежат в `docs/briefs/phase1/`. Дальше — замер на реальной задаче по
`~/.claude/skills/ultron/bench-protocol.md`.

## Быстрый старт

```bash
export PATH="$HOME/dev/ultron/bin:$PATH"
ultron run spec.json                          # компактный вид; код выхода 0 = accepted, 2 = иначе
ultron show T-03 --dir /abs/repo --diff       # приёмка по смыслу
ultron show T-03 --dir /abs/repo --evidence e2 | --gate | --brief | --raw
ultron retry T-03 --dir /abs/repo --message fix.md
ultron rollback T-03 --dir /abs/repo --to baseline   # или attempt-N
ultron integrate T-03 --dir /abs/repo                # после конфликта в режиме worktree
ultron clean T-03 --dir /abs/repo                    # удалить worktree задачи
ultron list --dir /abs/repo
ultron run spec.json --dry-run                # показать команду agy, не запускать
```

Пример `spec.json` — в `~/.claude/skills/ultron/SKILL.md`, поля — в
`ultron/schemas/spec.json`. Состояние и журнал: `~/.ultron/state/<slug>/`
(`ULTRON_STATE` меняет корень). Нужен `agy` в PATH и Python 3.10+.

## Что здесь

| Путь | Что это |
|---|---|
| `docs/design-orchestrator.md` | проект: вердикт по схеме «три слоя», архитектура, контракт, цикл runtime, риски, решения, фазы |
| `docs/contract.md` | контракт v2: уровни исполнителей, роли, поля TaskResult, firewall, эскалация, измеренная экономика |
| `docs/SKILL.v2.md` | черновик политики для Claude поверх текущего скилла `gemini-delegate` |
| `docs/examples/briefs/` | брифы по ролям и сообщение возврата из проб 2026-09-09 |
| `docs/examples/toy-project/` | учебный проект, на котором гонялись пробы: гейт `python3 tests/test_service.py && python3 tools/check.py` |
| `ultron/` | пакет: policy, spec, compose, executor, check, state, view, run, cli; схемы в `ultron/schemas/` |
| `tests/` | тесты и учебный проект-фикстура; `python3 -m unittest discover -s tests -t .` |
| `bin/ultron` | точка входа |
| `docs/briefs/phase1/`, `docs/briefs/phase2/` | брифы, по которым исполнитель собрал пакет |

Живой скилл `~/.claude/skills/ultron/SKILL.md` (до 09.09.2026 `gemini-delegate`) не зависит от этой папки.

Python 3.10+, только стандартная библиотека. Тесты: `python3 -m unittest discover -s tests -v`.
