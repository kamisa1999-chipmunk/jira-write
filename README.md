# jira-write

Cursor-скилл и Python CLI для создания и изменения задач в Jira: свободный текст → preview → явное подтверждение → запись.

Это отдельный репозиторий **без** заметок про команду. Правила и маппинг полей заточены под проект **CAT2** (Goldapple) — для другого проекта скопируй `scripts/config/projects/CAT2.json` и поправь поля.

## Что внутри

```text
skill/                         # скилл для Cursor
  SKILL.md
  reference.md
config/issue-creation-rules.md # типы, prefix, поля, вопросы Q-*
scripts/                       # CLI и клиент Jira
  config/projects/CAT2.json    # customfield’ы, алиасы
  cli/create_issue.py
  cli/update_issue.py
  …
```

Не входит: карточки людей, 1:1, токены, отчёты, Confluence/GitLab.

В `CAT2.json` есть короткие алиасы Jira-логинов («маша» → `kafanova_m`) — это не HR-заметки, без них фразы вроде «назначь на Машу» не сработают. Свой список можно вычистить или заменить.

## Как подключить скилл в Cursor

```bash
git clone https://github.com/kamisa1999-chipmunk/jira-write.git
ln -s "$(pwd)/jira-write/skill" ~/.cursor/skills/jira-write
```

Если папка `~/.cursor/skills/jira-write` уже есть — сначала переименуй или удали её (это должен быть симлинк на этот `skill/`).

Открой любой проект в Cursor и скажи: «заведи задачу …» / `jira write`.

## Настройка Jira

```bash
cd jira-write
python3 -m pip install -r scripts/requirements.txt
cp scripts/.env.example scripts/.env
# впиши JIRA_PAT (или логин/пароль)
```

Все команды по умолчанию только **preview**. Запись — с `--apply` после подтверждения.

```bash
python3 scripts/cli/get_create_metadata.py --project CAT2
python3 scripts/cli/create_issue.py --input issue.json
python3 scripts/cli/create_issue.py --input issue.json --apply
```

Токен в чат и в git не класть.
