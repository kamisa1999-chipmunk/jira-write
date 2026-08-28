"""Sprint and board lookups via Jira Agile API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from jira_client import JiraClient
from jira_client.exceptions import JiraApiError


def find_board_id(
    client: JiraClient,
    project_key: str,
    board_id: Optional[str] = None,
) -> int:
    """Return board id from config override or first Scrum board for the project."""
    if board_id:
        return int(board_id)

    data = client.get(
        "/rest/agile/1.0/board",
        params={"projectKeyOrId": project_key, "maxResults": 50},
    )
    boards = data.get("values", [])
    if not boards:
        raise JiraApiError(
            404,
            f"Не найдена Scrum/Kanban-доска для проекта {project_key}. "
            "Укажи JIRA_BOARD_ID в .env вручную.",
        )

    scrum_boards = [b for b in boards if b.get("type") == "scrum"]
    board = scrum_boards[0] if scrum_boards else boards[0]
    return int(board["id"])


def list_sprints(
    client: JiraClient,
    board_id: int,
    *,
    state: str = "active,future",
    max_results: int = 50,
) -> List[Dict[str, Any]]:
    """Return sprints for a board filtered by Agile state string."""
    data = client.get(
        f"/rest/agile/1.0/board/{board_id}/sprint",
        params={"state": state, "maxResults": max_results},
    )
    return list(data.get("values") or [])


def get_active_sprint(client: JiraClient, board_id: int) -> Dict[str, Any]:
    """Return the current active sprint on the board (short Agile payload).

    If several sprints are active (old one not closed), pick the one whose
    window contains today, else the latest ``startDate``.
    """
    sprints = list_sprints(client, board_id, state="active", max_results=10)
    if not sprints:
        raise JiraApiError(
            404,
            f"Активный спринт не найден на доске {board_id}",
        )
    if len(sprints) == 1:
        return sprints[0]

    now = datetime.now().astimezone()

    def _parse(value: Optional[str]):
        if not value:
            return None
        cleaned = value.replace("Z", "+00:00")
        if len(cleaned) >= 5 and cleaned[-5] in "+-" and cleaned[-3] != ":":
            cleaned = f"{cleaned[:-2]}:{cleaned[-2:]}"
        try:
            return datetime.fromisoformat(cleaned)
        except ValueError:
            return None

    in_window = []
    for sprint in sprints:
        start = _parse(sprint.get("startDate"))
        end = _parse(sprint.get("endDate"))
        if start and end and start <= now <= end:
            in_window.append(sprint)
    pool = in_window or sprints
    return max(pool, key=lambda item: str(item.get("startDate") or ""))


def get_future_sprints(client: JiraClient, board_id: int) -> List[Dict[str, Any]]:
    """Future sprints sorted by startDate (missing dates last)."""
    sprints = list_sprints(client, board_id, state="future", max_results=50)

    def sort_key(item: Dict[str, Any]) -> str:
        return str(item.get("startDate") or "9999")

    return sorted(sprints, key=sort_key)


def get_nearest_future_sprint(
    client: JiraClient,
    board_id: int,
) -> Optional[Dict[str, Any]]:
    """Nearest future sprint by startDate, or None if board has none."""
    future = get_future_sprints(client, board_id)
    return future[0] if future else None


def get_sprint_details(client: JiraClient, sprint_id: int) -> Dict[str, Any]:
    return client.get(f"/rest/agile/1.0/sprint/{sprint_id}")


def create_sprint(
    client: JiraClient,
    *,
    name: str,
    board_id: int,
    start_date: str,
    end_date: str,
    goal: str = "",
    auto_start_stop: bool = True,
    incomplete_issues_destination_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Create a future sprint on the board via Agile API."""
    body: Dict[str, Any] = {
        "name": name,
        "originBoardId": board_id,
        "startDate": start_date,
        "endDate": end_date,
        "autoStartStop": auto_start_stop,
    }
    if goal:
        body["goal"] = goal
    if incomplete_issues_destination_id is not None:
        body["incompleteIssuesDestinationId"] = incomplete_issues_destination_id
    return client.post("/rest/agile/1.0/sprint", json_body=body)


def update_sprint(
    client: JiraClient,
    sprint_id: int,
    *,
    name: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    goal: Optional[str] = None,
    state: Optional[str] = None,
    auto_start_stop: Optional[bool] = None,
    incomplete_issues_destination_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Update a sprint via Agile PUT.

    Jira Server 9.x требует `state` в теле. Если не передан — читаем текущий.
    Поле переноса незавершённых: ``incompleteIssuesDestinationId``
    (−1 = backlog, id спринта = следующий спринт).
    """
    current = get_sprint_details(client, sprint_id)
    body: Dict[str, Any] = {
        "name": name if name is not None else current.get("name"),
        "state": state if state is not None else current.get("state"),
        "startDate": start_date
        if start_date is not None
        else current.get("startDate"),
        "endDate": end_date if end_date is not None else current.get("endDate"),
        "goal": goal if goal is not None else (current.get("goal") or ""),
    }
    # Drop null dates (undated utility sprints).
    if not body.get("startDate"):
        body.pop("startDate", None)
    if not body.get("endDate"):
        body.pop("endDate", None)

    if auto_start_stop is not None:
        body["autoStartStop"] = auto_start_stop
    elif "autoStartStop" in current:
        body["autoStartStop"] = current.get("autoStartStop")

    if incomplete_issues_destination_id is not None:
        body["incompleteIssuesDestinationId"] = incomplete_issues_destination_id
    elif current.get("incompleteIssuesDestinationId") is not None:
        body["incompleteIssuesDestinationId"] = current.get(
            "incompleteIssuesDestinationId"
        )

    return client.put(f"/rest/agile/1.0/sprint/{sprint_id}", json_body=body)


def list_all_sprints(
    client: JiraClient,
    board_id: int,
    *,
    state: str = "active,future,closed",
) -> List[Dict[str, Any]]:
    """All sprints for a board (paginated)."""
    return list(
        client.paginate(
            f"/rest/agile/1.0/board/{board_id}/sprint",
            results_key="values",
            max_results=50,
            params={"state": state},
        )
    )


def sprint_board_url(jira_base_url: str, board_id: int, sprint_id: int) -> str:
    """RapidBoard URL filtered to a sprint."""
    base = jira_base_url.rstrip("/")
    return (
        f"{base}/secure/RapidBoard.jspa"
        f"?rapidView={board_id}&view=planning&sprint={sprint_id}"
    )
