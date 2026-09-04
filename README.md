# jira-write

Публичные Cursor/ZCode-скиллы и Python CLI для Jira (проект CAT2 / Goldapple). Заметки про людей сюда не входят.

Репозиторий специально **не переименовывали**: старая ссылка работает.

https://github.com/kamisa1999-chipmunk/jira-write

## Скиллы

Каждый скилл — папка с `SKILL.md`. Подключение в Cursor — симлинк в `~/.cursor/skills/`.

| Папка | Зачем |
|-------|--------|
| `skills/jira-write` | создать / обновить задачу (preview → подтверждение → apply) |
| `skills/jira-search` | одна задача или поиск по JQL |
| `skills/jira-history` | changelog и хронология; опционально MR (`--with-git`) |
| `skills/sprint-review` | снимок активного спринта |
| `skills/sprint-results` | черновик итогов спринта для Mattermost |
| `skills/testing-monitor` | очередь To Test и ёмкость теста |
| `skills/sprint-management` | новый спринт + страница Confluence |
| `skills/jira-employee-analysis` | факты по сотруднику из Jira (без ОС) |
| `skills/sa-review` | ревью SA: самопроверка и техническое ревью по очереди, без карточек людей |

Путь `skill/` оставлен как ярлык на `skills/jira-write`, чтобы старая инструкция `ln -s …/skill` не ломалась.

```bash
git clone https://github.com/kamisa1999-chipmunk/jira-write.git
cd jira-write

ln -s "$(pwd)/skills/jira-write" ~/.cursor/skills/jira-write
ln -s "$(pwd)/skills/jira-search" ~/.cursor/skills/jira-search
ln -s "$(pwd)/skills/jira-history" ~/.cursor/skills/jira-history
ln -s "$(pwd)/skills/sprint-review" ~/.cursor/skills/sprint-review
ln -s "$(pwd)/skills/sprint-results" ~/.cursor/skills/sprint-results
ln -s "$(pwd)/skills/testing-monitor" ~/.cursor/skills/testing-monitor
ln -s "$(pwd)/skills/sprint-management" ~/.cursor/skills/sprint-management
ln -s "$(pwd)/skills/jira-employee-analysis" ~/.cursor/skills/jira-employee-analysis
ln -s "$(pwd)/skills/sa-review" ~/.cursor/skills/sa-review
```

Если открыть этот репозиторий как workspace в Cursor, скиллы подхватятся из `.cursor/skills/`.

## Установка в ZCode

Репозиторий упакован как ZCode-плагин (`.zcode-plugin/plugin.json` + `marketplace.json`).

**Плагином (рекомендуется):**

1. Settings → Plugin Management → вкладка Discover → **`+`** → добавить `https://github.com/kamisa1999-chipmunk/jira-write` как marketplace.
2. На карточке плагина `jira-write` нажать **Get** — все скиллы появятся как `jira-write:<имя-скилла>`.
3. Код при такой установке живёт в кэше плагина и затирается при обновлении, поэтому `.env` держи вне репозитория и укажи его через переменную окружения:

```bash
export JIRA_WRITE_ENV_FILE=~/.config/jira-write/.env
```

**Симлинками (как в Cursor):**

```bash
ln -s "$(pwd)/skills/jira-write" ~/.zcode/skills/jira-write
# …и остальные скиллы из skills/ по аналогии
```

Скиллы подхватятся в новой сессии ZCode.

## Настройка

```bash
python3 -m pip install -r scripts/requirements.txt
cp scripts/.env.example scripts/.env
```

В `.env` нужен `JIRA_PAT` (или логин/пароль). Для страниц спринта — отдельно `CONFLUENCE_PAT`. Для истории с MR — отдельно `GITLAB_PAT` / `GITHUB_PAT`. Токены в git и в чат не класть.

Порядок поиска `.env`: путь из `JIRA_WRITE_ENV_FILE` → `scripts/.env` → переменные окружения.

Отчёты пишутся в `reports/` (в git не входят).

Правила создания задач CAT2: `config/issue-creation-rules.md`. Поля и алиасы: `scripts/config/projects/CAT2.json`.

Для другого Jira-проекта скопируй `CAT2.json` и поправь поля (пример второго проекта — `scripts/config/projects/FCTS.json`). `sprint-management` заточен под CAT2 (даты, Confluence parent id).

## CLI (из корня репозитория)

Запись в Jira — только с `--apply` после подтверждения.

```bash
python3 scripts/cli/create_issue.py --input issue.json
python3 scripts/cli/get_issue.py CAT2-1234
python3 scripts/cli/search_issues.py --jql "project = CAT2 AND status = Testing"
python3 scripts/cli/get_issue_history.py CAT2-1234
python3 scripts/cli/get_sprint_snapshot.py
python3 scripts/cli/get_sprint_capacity.py
python3 scripts/cli/get_testing_monitor.py
python3 scripts/cli/get_employee_analysis.py --employee маша --months 2
python3 scripts/cli/manage_sprint.py --start 27.07.26 --end 07.08.26 --goal "…"
```
