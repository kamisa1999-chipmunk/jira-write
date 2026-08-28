# Jira write — reference

## Правила создания задач

Файл: `config/issue-creation-rules.md`

Читать **перед** сборкой preview. Там: типы, prefix summary, шаблоны description, components/labels, связи, таблица полей, коды вопросов `Q-*`, id правил `R-*` для оснований в preview.

## Project config

Файл: `scripts/config/projects/{PROJECT}.json`

| Ключ | Назначение |
|------|------------|
| `fields` | Логическое имя → Jira field id (`budget` → `customfield_…`) |
| `defaults` | Разрешённые значения по умолчанию (только явно настроенные) |
| `issue_type_aliases` | «баг» → `Bug` |
| `people_aliases` | «маша» → `kafanova_m` |
| `component_aliases` | «backend» → `Backend` |
| `priority_aliases` | «высокий» → `High` |
| `link_relation_aliases` | «блокирует» → `Blocks` |

Для нового проекта скопируй `CAT2.json`, поправь `fields` по выводу:

```bash
python3 scripts/cli/get_create_metadata.py --project NEW --full
```

## create — один issue

```json
{
  "project": "CAT2",
  "issue_type": "Bug",
  "summary": "[WEB] Не применяется фильтр по акции на листинге",
  "description": "*Описание:*\n...\n\n*Окружение:*\nКонтур: Prod\nСтраны: все\n\n*Предусловия:*\n# ...\n\n*Шаги воспроизведения:*\n# ...\n\n*Ожидаемый результат:*\n...\n\n*Фактический результат:*\n...",
  "assignee": "маша",
  "priority": "высокий",
  "components": ["Web"],
  "epic": "CAT2-1234",
  "parent": null,
  "sprint": "active",
  "estimate_hours": 4,
  "severity": "Major",
  "env": "Prod",
  "platform": "Web",
  "budget": "Не заполнено",
  "links": [
    {"target": "CAT2-1234", "relation": "relates"}
  ]
}
```

Preview:

```json
{
  "operation": "create",
  "mode": "preview",
  "project": "CAT2",
  "issue_type": "Bug",
  "fields": {},
  "links": [],
  "missing_required_fields": [],
  "warnings": [],
  "ready": true
}
```

`ready: false` → не вызывать `--apply`, сначала закрыть `missing_required_fields` / `unresolved`.

## create — пакет (итоги встречи)

```json
{
  "issues": [
    {
      "local_id": "backend",
      "project": "CAT2",
      "issue_type": "DevelopmentB",
      "summary": "[Back] Доработать API фильтра акций",
      "description": "...",
      "components": ["Backend"],
      "epic": "CAT2-1000"
    },
    {
      "local_id": "web",
      "project": "CAT2",
      "issue_type": "DevelopmentF",
      "summary": "[WEB] Применять фильтр по акции на листинге",
      "description": "...",
      "components": ["Web"],
      "epic": "CAT2-1000"
    }
  ],
  "links": [
    {"from": "backend", "to": "web", "relation": "blocks"}
  ]
}
```

Testing к Dev/Bug создаётся автоматикой — в batch не включать (`R-GEN-11`).

После `--apply`: сначала все `issues`, затем `links` (local_id резолвятся в ключи).

## update

```json
{
  "summary": "Новое название",
  "description": "Новое описание",
  "assignee": "кафанова",
  "priority": "High",
  "labels_add": ["promo"],
  "labels_remove": ["old"],
  "estimate_hours": 16,
  "sprint": "active",
  "epic": "CAT2-1000",
  "status": "Code Review",
  "comment": "Уточнение после груминга",
  "links_add": [{"target": "CAT2-2000", "relation": "blocks"}],
  "fields": {
    "budget": "Технический долг"
  }
}
```

`status` / `transition` — имя перехода или целевого статуса из `available_transitions` preview.

## link / comment

```bash
python3 scripts/cli/link_issues.py CAT2-1 CAT2-2 --relation blocks
python3 scripts/cli/link_issues.py CAT2-1 CAT2-2 --relation blocks --apply

python3 scripts/cli/add_comment.py CAT2-1 --text "Нужен ретест на Preprod"
python3 scripts/cli/add_comment.py CAT2-1 --text "…" --apply
```

## Обязательные поля CAT2 (на момент настройки)

| Тип | Кроме project / type / summary |
|-----|--------------------------------|
| Почти все | `budget` (`Бюджет`) — default «Не заполнено» в config |
| Bug / sBug | + `severity`, `env`, `platform` — **без default, спрашивать** |
| Sub-task (`s*`) | + `parent` |

Estimate: через `timetracking` (`8h`). Кастомные «Оценка …» часто недоступны в editmeta.

Sprint: после create/update — Agile `POST /sprint/{id}/issue`, не raw customfield.

## Описание задачи

Актуальные шаблоны (Bug / Dev / Analysis / Testing) — в `config/issue-creation-rules.md` §6.

Краткий шаблон Development:

```text
h2. Контекст
…

h2. Что сделать
* …

h2. Критерии приёмки
* …   (только если явно были на встрече / в запросе)

h2. Ссылки
* …
```

Не добавлять AC «от себя».