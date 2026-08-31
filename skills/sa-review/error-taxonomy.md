# Таксономия ошибок SA review

Категория нужна ревьюеру для группировки, не для Jira-комментария.

## Категории

Использовать эти идентификаторы, не изобретать синонимы без нужды:

```text
scenario-coverage
entry-point-coverage
platform-differences
ecosystem-impact
contract-versioning
contract-level-mixing
null-empty-behavior
edge-cases
decision-traceability
decision-rationale
review-comments-processing
naming-consistency
docs-consistency
docs-relevance
testability
jira-task-quality
implementation-feasibility
```

Ориентир по разделам чек-листа (не взаимно однозначно):

| Раздел чек-листа | Типичные категории |
|------------------|-------------------|
| 1. Схема | `ecosystem-impact` |
| 2. Сценарии и точки входа | `scenario-coverage`, `entry-point-coverage`, `platform-differences` |
| 3. Спецификации и шаблоны | `docs-consistency`, `docs-relevance`, `ecosystem-impact`, `contract-versioning`, `contract-level-mixing` |
| 4. Однозначность / имена | `naming-consistency` |
| 4. Полнота | `null-empty-behavior`, `edge-cases` |
| 4. Решения | `decision-traceability`, `decision-rationale` |
| 4. Непротиворечивость | `docs-consistency` |
| 4. Проверяемость | `testability` |
| 4. Актуальность | `docs-relevance` |
| 4. Выполнимость | `implementation-feasibility` |
| 5. Задачи в Jira | `jira-task-quality` |
| 6. Передача / прошлые замечания | `review-comments-processing` |

Одна проблема — одна основная категория. Дополнительную не плодить.

## Повтор внутри одной задачи

Было замечание в iteration 1 → в iteration 2 оно осталось:

```text
unresolved_previous_review
```

В вердиктах текущей проверки это же может быть `unresolved_previous_comment` — не смешивать с новым `ISSUE` той же сути.
