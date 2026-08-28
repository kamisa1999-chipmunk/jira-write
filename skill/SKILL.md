---
name: jira-write
description: >-
  Создаёт и изменяет задачи Jira по свободному тексту: одна задача, пакет по
  итогам встречи, обновление полей, связи, комментарии, переходы статуса.
  Всегда preview → подтверждение → apply. Используй при командах: заведи баг,
  создай задачу, заведи задачи по итогам груминга, обнови CAT2-1234, назначь на
  Машу, добавь в спринт, смени приоритет, добавь комментарий, jira write.
---

# Jira write

## Назначение

Свободный текст / заметки встречи → план задач (preview) → **явное подтверждение** → запись в Jira через CLI.

Токен не читать и не показывать. Не удалять задачи. Не применять изменения без подтверждения. Не придумывать обязательные поля, критерии приёмки и технические решения.

## Обязательные источники правил

Перед любым create/batch **прочитай**:

1. `config/issue-creation-rules.md` — паттерны CAT2 (типы, summary, description, labels/components, связи, что спрашивать).
2. `scripts/config/projects/{PROJECT}.json` — маппинг полей, алиасы, defaults.
3. При необходимости — `get_create_metadata.py` (кэш < 8 ч) для обязательных полей типа.

Правила из `issue-creation-rules.md` приоритетнее «интуиции». Алиасы людей/компонентов — из project config. Идентификаторы `customfield_*` **не** хардкодить в скилле.

Если обязательное по правилам или createmeta поле отсутствует → preview с `ready: false`, **не** вызывать `--apply`, задать вопросы только по пробелам (`Q-*` из правил).

## CLI (из корня этого репозитория)

По умолчанию все команды только **preview**. Запись — только с `--apply` после подтверждения.

```bash
python3 scripts/cli/get_create_metadata.py --project CAT2
python3 scripts/cli/create_issue.py --input issue.json
python3 scripts/cli/create_issue.py --input issue.json --apply
python3 scripts/cli/create_issue.py --batch --input batch.json
python3 scripts/cli/update_issue.py CAT2-1234 --input changes.json
python3 scripts/cli/link_issues.py CAT2-1234 CAT2-1235 --relation blocks
python3 scripts/cli/add_comment.py CAT2-1234 --text "..."
```

Перед первым запуском: `pip3 install -r scripts/requirements.txt`.

Подробные схемы JSON: [reference.md](reference.md).

## Workflow: одна задача

1. Прочитать `issue-creation-rules.md` (если ещё не в контексте).
2. Разобрать текст → тип, платформа, поля по правилам (§2–§5 правил).
3. Для каждого заполняемого поля зафиксировать **основание** (`R-…` / defaults / явное слово пользователя).
4. `get_create_metadata.py` (или кэш < 8 ч), сверить required.
5. Собрать `issue.json` только из известных/разрешённых defaults значений.
6. `create_issue.py --input issue.json` → показать preview пользователю **с основаниями** (формат ниже).
7. Спросить только недостающие обязательные или коды `Q-*` из §10 правил.
8. После «да / создай / применяй» → `--apply`. Вернуть ключ и ссылку.

Для Dev/Bug парную `Testing` **не** создавать и не предлагать — её заводит автоматика (`R-GEN-11`). Создавать Testing только по явной просьбе.

## Workflow: задачи по итогам встречи

1. Прочитать заметки / расшифровку + `issue-creation-rules.md`.
2. Выделить **принятые решения** и понятные action items. Не делать задачу из каждой реплики.
3. Декомпозиция по правилам: платформы отдельно, Blocks backend→frontend. Testing в пакет не включать.
4. Предложить список с основаниями полей; отдельно — спорные места (`Q-*`).
5. Финальный список → `create_issue.py --batch --input batch.json` (preview).
6. После подтверждения → `--apply`. Сначала задачи, потом связи. При частичном сбое: создано / не создано / связи / что повторить.

## Формат preview (create)

```text
Preview создания (ready: yes/no)

1) DevelopmentF — [WEB] …
   Основания:
   - issue_type: DevelopmentF ← R-TYPE-FRONT
   - summary: [WEB] … ← R-SUM-DEV, R-DEVF-01
   - components: [Web] ← §4 платформы
   - budget: Не заполнено ← R-FLD-BUDGET / defaults
   - epic: — ← Q-EPIC (не задан)
   (Testing к задаче создастся автоматически — не включаем)

Не закрыто (блокирует apply):
- Q-EPIC: к какой инициативе привязать?

Применить?
```

Без закрытия обязательных полей / `Q-BUG-SEV|ENV|PLAT` для Bug — **не** вызывать `--apply`.

## Workflow: обновление

1. Актуальное состояние + `editmeta` / transitions (делает `update_issue.py`).
2. Показать diff «было → стало».
3. Применить только после подтверждения.

```text
Изменения для CAT2-1234:

- Assignee: Иван → Мария
- Labels: +promo
- Estimate: 8 ч → 16 ч
- Status: Development → Code Review

Применить?
```

Поддержано: summary/description, labels ±, assignee, priority, estimate, sprint, fixVersion, epic/parent, links ±, comment, transition по статусу.

## Правила (кратко)

| Можно | Нельзя |
|-------|--------|
| Preview без записи | `--apply` без явного «да» |
| Defaults только из project config + `R-FLD-*` | Выдумывать severity/env/platform/AC/epic |
| Основания `R-*` в preview | Молча менять status / assignee / release |
| Вопросы только по `Q-*` / required | Массовые правки без отдельного «да» |
| Не включать авто-Testing в пакет | Создавать Testing к Dev/Bug без явной просьбы |
| Частичный отчёт при ошибке пакета | Удалять задачи; печатать токен |

## Типы CAT2 (ориентир → детали в issue-creation-rules)

| Запрос | Тип | Prefix |
|--------|-----|--------|
| баг | `Bug` (+ severity, env, platform) | `[WEB]` / `[iOS]` / … |
| backend / техзадача | `DevelopmentB` | `[Back]` |
| web / iOS / Android / «задача» UI | `DevelopmentF` | `[WEB]` / `[iOS]` / `[Android]` |
| регресс | `Testing` | `Regress Web …` / `Regress Mobile …`; epic `CAT2-3311` |
| автотест (написание) | `DevelopmentB` + label `autotest` | `Написание автотеста…` / `[#id][PLATFORM]…` |
| data-test-id под AT | `DevelopmentF` | `[WEB] Добавить data-test-id…` |
| тесткейсы | `Documentation` + label `testcase` | `Подготовить тест-кейсы для…` |
| QA / testing (вручную) | `Testing` | `Testing [Prefix] …` |
| анализ | `Analysis` | `[SA]` |
| дизайн | `Design` | `[Design]` |

Алиасы людей и компонентов — в project config.

## Чего не делать

- Не ходить в Jira write без скилла / без preview
- Не коммитить `.env`
- Не подставлять выдуманные AC, платформы, эпики, **оценки часов**
- Не игнорировать `issue-creation-rules.md`
- Не создавать unit-тесты ради этой фичи
- Estimate — только из текущего запроса/груминга (`R-GEN-13`)
