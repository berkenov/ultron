# ultron: руководство по использованию

`ultron` запускает исполнителя (Gemini или Claude через Antigravity CLI `agy`)
по спецификации задачи, сам проверяет результат гейтом и объёмом изменений,
хранит состояние вне репозитория и отдаёт наверх компактный отчёт. Кто
формулирует задачу и принимает результат по смыслу — человек или Claude, для
утилиты без разницы.

## 1. Установка

Нужны: Python 3.10+, git, `agy` в `PATH` с выполненным входом в Antigravity.

```bash
git clone git@github.com:berkenov/ultron.git ~/dev/ultron
export PATH="$HOME/dev/ultron/bin:$PATH"     # добавить в ~/.zshrc
ultron --help
agy models                                   # список моделей, он меняется
```

Проверка, что исполнитель может выполнять команды и править файлы:

```bash
mkdir -p /tmp/agy-probe && printf 'line one\n' > /tmp/agy-probe/probe.txt
agy --dangerously-skip-permissions -p "В файле probe.txt замени содержимое ровно на строку: DELEGATED OK" \
  --add-dir /tmp/agy-probe --model gemini-3.8-flash-medium --output-format json
head -1 /tmp/agy-probe/probe.txt             # ждём DELEGATED OK
```

Без флага `--dangerously-skip-permissions` исполнитель в неинтерактивном режиме
не выполняет команд и не правит файлы, а в отчёте пишет `SUCCESS`. `ultron`
добавляет флаг сам.

## 2. Задача: spec.json и бриф

Задача — это два файла: `spec.json` (что runtime обязан проверить) и бриф в
прозе (что делать исполнителю). Оба пишет постановщик, обычно Claude.

```json
{
  "id": "T-03",
  "role": "code",
  "goal": "greet здоровается словом hello",
  "dir": "/abs/path/to/repo",
  "brief": "brief-03.md",
  "scope": {"write": ["app/service.py"], "forbid": ["tests/", "tools/"]},
  "acceptance": [{"cmd": "python3 -m unittest -q"}],
  "inputs": [{"task": "T-01", "fields": ["map", "facts"]}],
  "budget": {"attempts_per_tier": 2, "tiers": ["gemini-3.1-pro-high", "claude-sonnet-4-6"], "executor_tokens": 500000, "minutes": 20},
  "executor": {"model": null, "effort": null, "mode": null},
  "workspace": {"mode": "inplace", "seed": []}
}
```

| Поле | Обязательно | Смысл |
|---|---|---|
| `id` | да | имя задачи, оно же каталог состояния: буквы, цифры, `._-` |
| `role` | да | `explore` (карта и факты), `code` (правки), `verify` (прогнать гейт и разобрать падения), `review` (второе мнение); файлы меняет только `code` |
| `dir` | да | абсолютный путь к репозиторию |
| `brief` | да | путь к брифу, относительный считается от файла spec |
| `goal` | нет | одна строка для журнала |
| `scope.write` | нет | что можно менять; пусто у `code` = любые файлы |
| `scope.forbid` | нет | что нельзя менять даже внутри `write`; изменения там runtime откатит сам |
| `acceptance` | нет | команды гейта, по порядку, в `dir`; `expect_exit` по умолчанию 0 |
| `inputs` | нет | поля результатов прошлых задач, которые runtime вставит в бриф текстом |
| `budget` | нет | попыток на ступень, ступени моделей, лимит токенов и минут; по умолчанию из политики роли |
| `executor.model` | нет | заменяет первую ступень; `effort`: low/medium/high; `mode: "plan"` — патч текстом, дерево не трогать |
| `workspace.mode` | нет | `inplace` (в вашем дереве) или `worktree` (в отдельном worktree, см. раздел 6) |

Бриф — семь блоков. Пропущенный блок — место, где исполнитель начнёт угадывать:

```
Роль: code
КОНТЕКСТ    — что за проект, что за модуль, зачем правка
ФАЙЛЫ       — точные пути: что читать, что менять
ЗАДАЧА      — что сделать, в императиве, без «желательно»
НЕ ТРОГАТЬ  — файлы и поведение, которые обязаны остаться как есть
ПРИЁМКА     — команда, которая должна пройти, и что считается успехом
ОТВЕТ       — можно опустить: runtime добавит шаблон ответа роли сам
ОГРАНИЧЕНИЯ — стиль, язык, правила проекта
```

Строки про объём, запрет на git и напоминание про гейт runtime дописывает к
брифу сам. Правила проекта из `CLAUDE.md` и подобных файлов переносите в бриф
текстом: исполнитель их не читает.

## 3. Запуск и чтение отчёта

```bash
ultron run spec-03.json                 # код выхода 0 = accepted, 2 = иначе
ultron run spec-03.json --dry-run       # показать команду agy и ничего не запускать
```

Единственное, что печатается, — компактный отчёт, до 40 строк:

```
=== T-03 | code | accepted | attempts 1 | tier gemini-3.1-pro-high | 33 s | in 21.5k cache 68.8k out 3.3k
summary: …                               ← что сделано, словами исполнителя
changed_files (1):                       ← что заявил исполнитель
  mod app/service.py · …
commands (1):                            ← что он сам запускал, одной фразой
  [0] python3 -m unittest -q · …
checks: contract ok · scope ok · gate [0] attempt 1
changed_files: app/service.py            ← правда из git; claimed: … появится при расхождении
restored: tests/x.py                     ← что runtime вернул из исходного состояния, если было
evidence (2): e1 app/service.py:4; …     ← якоря, выдержки по запросу
open_questions (1): …                    ← развилки, которые исполнитель не вправе решать
next_action: …
failure: execution/gate                  ← только когда статус не accepted
integration: applied                     ← только в режиме worktree
state: ~/.ultron/state/<проект>/tasks/T-03
```

Статусы:

| Статус | Значит | Что делать |
|---|---|---|
| `accepted` | объём и гейт зелёные по независимому прогону runtime, отчёт по схеме получен или после переспроса помечен `missing` | прочитать дифф: `ultron show T-03 --diff`; смысл проверяет человек или Claude |
| `rejected` | попытки и ступени исчерпаны, что-то красное | строка `failure` и `show --gate`; чинить бриф или вернуть с конкретикой через `retry` |
| `blocked` | исполнитель просит решение | ответить на `open_questions` через `retry --message` |
| `failed` | инфраструктура: нет ответа, таймаут, бюджет, посев worktree | смотреть `failure`, править `budget`, `seed` или окружение |

## 4. Команды

```bash
ultron show T-03 --dir /abs/repo                 # отчёт ещё раз
ultron show T-03 --dir /abs/repo --diff          # git diff --stat и дифф тронутых файлов
ultron show T-03 --dir /abs/repo --gate          # полный вывод последнего гейта
ultron show T-03 --dir /abs/repo --evidence e1   # одна выдержка целиком
ultron show T-03 --dir /abs/repo --brief         # текст, который реально ушёл исполнителю
ultron show T-03 --dir /abs/repo --raw           # весь result.json
ultron retry T-03 --dir /abs/repo --message fix.md   # ещё попытка в той же сессии с вашим текстом
ultron rollback T-03 --dir /abs/repo --to baseline   # вернуть дерево к снимку; или --to attempt-2
ultron integrate T-03 --dir /abs/repo            # повторить перенос патча после конфликта (worktree)
ultron clean T-03 --dir /abs/repo                # удалить worktree задачи
ultron list --dir /abs/repo                      # задачи проекта
```

`--dir` по умолчанию — текущий каталог; `--state` меняет корень состояния,
как и переменная `ULTRON_STATE` (по умолчанию `~/.ultron/state`).

## 5. Как runtime проверяет и что делает сам

- **Снимок до задачи.** Всё рабочее состояние, включая незакоммиченные правки,
  сохраняется в объект git (`refs/ultron/<задача>/baseline`). Ваш индекс и дерево
  не меняются. `git checkout`, `git reset`, `git stash` runtime не использует никогда.
- **Объём.** После каждой попытки — снимок и разница с baseline по содержимому.
  Файл вне `write` или внутри `forbid` runtime сам возвращает к вашей версии из
  baseline и сообщает исполнителю. Роли без записи не могут менять ничего.
- **Гейт.** Команды `acceptance` runtime прогоняет сам, независимо от того,
  что доложил исполнитель. Исполнителя тоже просят прогнать гейт до ответа.
- **Доклад отдельно от кода.** Если код и гейт в порядке, а отчёт не по схеме,
  runtime один раз переспрашивает только отчёт; не дождавшись, принимает код и
  помечает `contract missing`.
- **Повторы.** Красный гейт — возврат в ту же сессию с хвостом вывода, до
  `attempts_per_tier` раз. Дальше — следующая ступень модели в новой сессии:
  дерево возвращается к baseline, дифф прошлой попытки лежит в состоянии и
  упоминается в брифе.
- **Стоп.** `blocked` от исполнителя, таймаут, лимит токенов или минут — сразу стоп.

## 6. Режим worktree

```json
"workspace": {"mode": "worktree", "seed": ["ln -s ../../../node_modules node_modules"]}
```

Исполнитель работает в отдельном git worktree, созданном от baseline: ваше
дерево не меняется, пока задача не принята; результат переносится патчем
(`integration: applied`). Конфликт с тем, что вы правили во время задачи, —
`integration: conflict`: патч сохранён, дерево не тронуто, после разбора —
`ultron integrate`. Провал — worktree остаётся для диагностики, `ultron clean`
удаляет. Worktree лежат в `~/.ultron/state/<проект>/worktrees/<задача>`.

В worktree нет того, чего нет в git: `node_modules`, `.dart_tool`, `vendor`,
`.env`. Без них гейт не запустится, поэтому в `seed` — команды или симлинки:
`flutter pub get`, `composer install`, симлинк на общий `node_modules`. Ошибка
посева — `failed`, `failure: infra/seed`, исполнитель не вызывается.

Когда включать: есть незакоммиченная работа, которую нельзя рисковать задеть;
несколько задач параллельно; чужой репозиторий.

## 7. Роли, модели, ступени

| Роль | По умолчанию | Ступень выше | Меняет файлы |
|---|---|---|---|
| `explore` | gemini-3.8-flash-high | gemini-3.1-pro-high | нет |
| `code` | gemini-3.1-pro-high | claude-sonnet-4-6 | да |
| `verify` | gemini-3.8-flash-high | gemini-3.1-pro-high | нет |
| `review` | gemini-3.1-pro-high | claude-opus-4-6-thinking | нет |

Переопределить: `executor.model` (первая ступень) или `budget.tiers` (весь
список). Claude-модели через `agy` расходуют квоту Antigravity, а не Claude Code.
Политика целиком — `ultron/policy.py`.

## 8. Цепочки задач

`inputs` подставляют поля результата прошлой задачи в бриф под заголовком
`ДАНО ИЗ T-01`: например, `map` и `facts` от `explore` в бриф для `code`.
Постановщик только ссылается, ничего не перепечатывает. Задача из `inputs`
должна быть уже выполнена в том же проекте, иначе запуск не начнётся.

## 9. Состояние, журнал, замер

```
~/.ultron/state/<проект>/
├── journal.jsonl              строка на попытку: задача, роль, модель, вердикт, токены
├── worktrees/<задача>/        только в режиме worktree
└── tasks/<задача>/
    ├── spec.json  brief.md    что ушло исполнителю
    ├── attempts.jsonl         конверты agy и проверки по каждой попытке
    ├── gate.log               последний прогон гейта целиком
    ├── attempt-N.diff         дифф попытки перед эскалацией
    ├── patch.diff             патч в режиме plan или при конфликте
    └── result.json            итог
```

Журнал совместим с замером из скилла:

```bash
python3 ~/.claude/skills/ultron/bench.py sum <session> --agy-log ~/.ultron/state/<проект>/journal.jsonl
```

## 10. Работа с Claude

Скилл `ultron` в Claude Code включается словами «используй гемини», «через
ультрон» или «не пиши сам». Дальше Claude пишет spec и бриф, запускает
`ultron run`, читает компактный отчёт и дифф, принимает или возвращает через
`retry`. Если удобнее запускать самому: Claude выдаёт команду, вы запускаете,
он проверяет через `show`.

## 11. Неполадки

| Симптом | Причина | Что делать |
|---|---|---|
| `contract violated`, «исполнитель без прав» | `agy` не смог выполнить команду или правку | проба из раздела 1; перезапустить сессию, где запускается `ultron` |
| гейт красный в worktree, в вашем дереве зелёный | нет зависимостей вне git | `workspace.seed` |
| `integration: conflict` | вы правили те же файлы во время задачи | разобрать конфликт, `ultron integrate` |
| `failure: infra/timeout` | исполнитель не уложился в `--print-timeout` | увеличить `budget.minutes`, разрезать задачу |
| `failure: infra/budget_tokens` | исполнитель жжёт контекст | явный список файлов в `ФАЙЛЫ`, меньше объём, дешевле роль |
| `contract missing` при `accepted` | исполнитель не смог оформить отчёт | код принят по гейту; `show --diff` |
| «исполнитель угадывал», сделано не то | бриф неполон | переписать бриф, новая задача; `retry` только для точечных замечаний |
