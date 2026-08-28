---
name: jira-search
description: >-
  Поиск и чтение задач Jira через локальные CLI-скрипты и сохранённые JSON-отчёты.
  Используй при запросах: покажи CAT2-1234, найди задачи Маши, покажи баги,
  задачи без оценки, задачи в Testing, задачи по промокодам, jira search, jira
  issue, jql.
---

# Jira search

## Назначение

Скилл строит JQL по простому запросу пользователя, запускает локальный CLI, читает свежий JSON из `reports/` и отвечает кратко, без прямой работы с Jira API внутри ответа.

Токен не читать и не показывать. Старые отчёты не перезаписывать и не удалять.

## Команды

Запускать из корня репозитория:

```bash
python3 scripts/cli/get_issue.py CAT2-1234
python3 scripts/cli/search_issues.py --jql "project = CAT2 AND status != Done"
python3 scripts/cli/search_issues.py --jql "project = CAT2 AND assignee = currentUser()" --limit 20 --format json
```

Если текущая директория другая, использовать полный путь:

```bash
python3 scripts/cli/get_issue.py CAT2-1234
python3 scripts/cli/search_issues.py --jql "project = CAT2 AND status = Testing"
```

## Как действовать

1. Определи, это одна задача или поиск.
2. Если пользователь дал ключ вида `CAT2-1234`, запускай `get_issue.py`.
3. Иначе построй JQL и запускай `search_issues.py`.
4. Прочитай сохранённый JSON из `reports/`.
5. Ответь кратко:
   - для одной задачи: ключ, статус, исполнитель, оценка, sprint, 1-2 важных факта;
   - для поиска: сколько найдено, какие главные группы/паттерны, 5-10 первых задач.

## Быстрые шаблоны JQL

- `покажи баги` -> `project = CAT2 AND issuetype = Bug ORDER BY updated DESC`
- `задачи без оценки` -> `project = CAT2 AND ((timeoriginalestimate is EMPTY) OR ("Original Estimate" is EMPTY)) ORDER BY updated DESC`
- `задачи в Testing` -> `project = CAT2 AND status = Testing ORDER BY updated DESC`
- `найди задачи Маши` -> `project = CAT2 AND assignee = "Маша" ORDER BY updated DESC`
- `задачи по промокодам` -> `project = CAT2 AND text ~ "промокод*" ORDER BY updated DESC`

Если формулировка неоднозначна, сначала предложи самый вероятный JQL и явно скажи, что это рабочая гипотеза.

## Что читать в JSON

- Общие поля отчёта: `report_generated_at`, `query_type`, `query`, `total_issues`
- Список задач: `issues`
- Внутри задачи: `key`, `title`, `description`, `status`, `type`, `assignee`, `author`, `dates`, `estimates`, `sprint`, `labels`, `comments`, `changelog`, `links`

## Чего не делать

- Не создавать и не менять задачи
- Не писать комментарии
- Не переходить по статусам
- Не считать метрики команды, если этого не просили отдельно
- Не придумывать JQL как факт, если не уверен в поле или имени человека
