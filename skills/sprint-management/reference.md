# Sprint management — reference

## Agile API (Jira Server 9.12)

### Create

`POST /rest/agile/1.0/sprint`

```json
{
  "name": "4. CAT2 10.08.26 - 21.08.26",
  "originBoardId": 359,
  "startDate": "2026-08-10T10:00:00.000+05:00",
  "endDate": "2026-08-21T22:00:00.000+05:00",
  "goal": "",
  "autoStartStop": true,
  "incompleteIssuesDestinationId": -1
}
```

### Update

`PUT /rest/agile/1.0/sprint/{id}` — на этой Jira в теле нужен `state` (и обычно name/dates/goal).

```json
{
  "name": "4. CAT2 10.08.26 - 21.08.26",
  "state": "future",
  "startDate": "2026-08-10T10:00:00.000+05:00",
  "endDate": "2026-08-21T22:00:00.000+05:00",
  "goal": "1. …\n\nПодробнее: https://confluence…",
  "autoStartStop": true,
  "incompleteIssuesDestinationId": 1234
}
```

- `incompleteIssuesDestinationId: -1` → backlog  
- положительный id → следующий спринт  
- `autoStartStop: true` → SprintAutoStart/Stop по датам (отдельная Automation не нужна)

### Дашборд

```text
{JIRA_URL}/secure/RapidBoard.jspa?rapidView={boardId}&view=planning&sprint={sprintId}
```

## Confluence API

`POST /rest/api/content` — страница типа `page`, `ancestors: [{id: parent}]`, body `storage`.

Родитель CAT2 спринтов: `823380099`, space `BIZ`.

Auth: только `CONFLUENCE_PAT` (или basic) — **не** `JIRA_PAT`.

## Конфиг

`scripts/config/sprint_management.yaml`:

- `confluence.parent_page_id`
- `schedule.*` (час старта/финиша, `+05:00`)
- `auto_start_stop`
- `incomplete_destination.backlog_id: -1`

## Пример preview (текст)

```text
Preview создания спринта (ready: yes)

Название: 4. CAT2 10.08.26 - 21.08.26
Даты: 10.08.26 — 21.08.26
Доска: CAT2 / board_id=359
Предыдущий спринт: CAT2 27.07.26 - 07.08.26 (id=5873, перенос сейчас: backlog)
Родитель Confluence: https://confluence.goldapple.ru/pages/viewpage.action?pageId=823380099
Страница: 4. CAT2 10.08.26 - 21.08.26
Проект целей: из запроса пользователя и/или локального `features/`, если он есть

Цели:
  1. Стартовать разработку сертификатов качества на PDP (BE∥FE).
  2. Подготовить СА / груминг Chanel этапа 1 (без старта кода).
  …

Правило переноса незавершённых:
  новый спринт → backlog (следующий спринт ещё не создан)
  механизм: Agile field incompleteIssuesDestinationId …

Автозапуск/завершение:
  Agile field autoStartStop=true …

Будет применено:
  - autoStartStop=true …
  - изменить incompleteIssuesDestinationId предыдущего с backlog на id нового

Блокеры:
  (нет)

Подтверди цели … и создание.
```

## Пример успешного результата

```text
Спринт создан: 4. CAT2 10.08.26 - 21.08.26
Jira: https://jira01.goldapple.ru/secure/RapidBoard.jspa?rapidView=359&view=planning&sprint=…
Confluence: https://confluence.goldapple.ru/pages/viewpage.action?pageId=…

Настройки:
- автоматический запуск: настроен;
- автоматическое завершение: настроено;
- незавершённые задачи: backlog (−1) — нужен следующий спринт.

Цели:
1. …
```

## Дубликат (как у 27.07–07.08)

Если спринт/страница уже есть — preview `ready: no`, apply запрещён. Для проверки: `--verify <sprintId>`.
