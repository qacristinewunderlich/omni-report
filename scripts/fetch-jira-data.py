#!/usr/bin/env python3
"""
Gera arquivos data/{SQUAD}-data.json a partir do Jira.

Regra principal:
- Usar apenas issues concluídas (statusCategory == "done"), equivalente à coluna
  "Concluído" do relatório de velocity do Jira.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from requests.auth import HTTPBasicAuth


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "squads.json"
DATA_DIR = ROOT / "data"


@dataclass
class Squad:
    key: str
    name: str
    board_id: int
    project_key: str


def load_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Variável obrigatória ausente: {name}")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gerar dados de velocity por squad")
    parser.add_argument(
        "--squad",
        help="Key da squad (ex: SAL). Se omitido, gera para todas as squads.",
    )
    parser.add_argument(
        "--max-sprints",
        type=int,
        default=30,
        help="Quantidade máxima de sprints fechadas por squad (default: 30).",
    )
    return parser.parse_args()


def load_squads() -> list[Squad]:
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    squads: list[Squad] = []
    for item in raw:
        squads.append(
            Squad(
                key=item["key"],
                name=item["name"],
                board_id=int(item["boardId"]),
                project_key=item.get("projectKey", item["key"]),
            )
        )
    return squads


def jira_get(
    session: requests.Session,
    base_url: str,
    endpoint: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{base_url}{endpoint}"
    resp = session.get(url, params=params, timeout=45)
    resp.raise_for_status()
    return resp.json()


def sprint_number(name: str) -> int:
    digits = "".join(ch if ch.isdigit() else " " for ch in (name or ""))
    numbers = [int(x) for x in digits.split() if x.isdigit()]
    return numbers[-1] if numbers else 0


def parse_date(value: str | None) -> str | None:
    if not value:
        return None
    return value[:10]


def growth(current: int, previous: int | None) -> float | None:
    if previous in (None, 0):
        return None
    return round(((current - previous) / previous) * 100, 1)


def list_closed_sprints(
    session: requests.Session,
    site_url: str,
    board_id: int,
) -> list[dict[str, Any]]:
    sprints: list[dict[str, Any]] = []
    start_at = 0
    while True:
        payload = jira_get(
            session,
            site_url,
            f"/rest/agile/1.0/board/{board_id}/sprint",
            params={"state": "closed", "startAt": start_at, "maxResults": 50},
        )
        chunk = payload.get("values", [])
        sprints.extend(chunk)
        if payload.get("isLast", True):
            break
        start_at += payload.get("maxResults", 50)
    return sprints


def list_sprint_issues(
    session: requests.Session,
    site_url: str,
    sprint_id: int,
    points_field: str,
) -> list[dict[str, Any]]:
    fields = f"assignee,issuetype,status,{points_field},customfield_10016"
    issues: list[dict[str, Any]] = []
    start_at = 0
    while True:
        payload = jira_get(
            session,
            site_url,
            f"/rest/agile/1.0/sprint/{sprint_id}/issue",
            params={"startAt": start_at, "maxResults": 100, "fields": fields},
        )
        chunk = payload.get("issues", [])
        issues.extend(chunk)
        start_at += payload.get("maxResults", 100)
        if start_at >= payload.get("total", 0):
            break
    return issues


def get_points(fields: dict[str, Any], points_field: str) -> float:
    value = fields.get(points_field)
    if value is None:
        value = fields.get("customfield_10016")
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def is_done(fields: dict[str, Any]) -> bool:
    status = fields.get("status") or {}
    category = status.get("statusCategory") or {}
    return category.get("key") == "done"


def build_squad_dataset(
    session: requests.Session,
    site_url: str,
    squad: Squad,
    points_field: str,
    max_sprints: int,
) -> dict[str, Any]:
    sprints = list_closed_sprints(session, site_url, squad.board_id)
    sprints = sorted(
        sprints,
        key=lambda s: (
            parse_date(s.get("startDate")) or "",
            sprint_number(s.get("name", "")),
        ),
    )
    if max_sprints > 0:
        sprints = sprints[-max_sprints:]

    sprint_rows: list[dict[str, Any]] = []
    assignee_points_by_sprint: dict[str, list[int]] = defaultdict(list)
    issue_type_counts: dict[str, int] = defaultdict(int)

    previous_points: int | None = None
    total_done_issues = 0
    total_points = 0

    for sprint_idx, sprint in enumerate(sprints):
        sprint_id = sprint["id"]
        issues = list_sprint_issues(session, site_url, sprint_id, points_field)

        done_issues = []
        for issue in issues:
            fields = issue.get("fields", {})
            if is_done(fields):
                done_issues.append(issue)

        sprint_points = 0
        for issue in done_issues:
            fields = issue.get("fields", {})
            pts = int(round(get_points(fields, points_field)))
            sprint_points += pts

            assignee = fields.get("assignee")
            assignee_name = (
                assignee.get("displayName")
                if assignee and assignee.get("displayName")
                else "Não atribuído"
            )
            while len(assignee_points_by_sprint[assignee_name]) <= sprint_idx:
                assignee_points_by_sprint[assignee_name].append(0)
            assignee_points_by_sprint[assignee_name][sprint_idx] += pts

            issue_type = (fields.get("issuetype") or {}).get("name", "Sem tipo")
            issue_type_counts[issue_type] += 1

        for assignee_name in list(assignee_points_by_sprint.keys()):
            while len(assignee_points_by_sprint[assignee_name]) <= sprint_idx:
                assignee_points_by_sprint[assignee_name].append(0)

        done_count = len(done_issues)
        total_done_issues += done_count
        total_points += sprint_points

        sprint_rows.append(
            {
                "id": sprint_number(sprint.get("name", "")) or sprint_id,
                "name": sprint.get("name", f"Sprint {sprint_id}"),
                "start": parse_date(sprint.get("startDate")),
                "end": parse_date(sprint.get("endDate")),
                "completedAt": parse_date(sprint.get("completeDate")),
                "points": sprint_points,
                "issues": done_count,
                "growth": growth(sprint_points, previous_points),
            }
        )
        previous_points = sprint_points

    assignees = []
    for assignee_name, arr in assignee_points_by_sprint.items():
        total_assignee_points = sum(arr)
        active = len([x for x in arr if x > 0])
        assignees.append(
            {
                "name": assignee_name,
                "totalPoints": total_assignee_points,
                "sprintsActive": active,
                "avgPerSprint": round(total_assignee_points / active, 1) if active else 0,
                "teamPct": round((total_assignee_points / total_points) * 100, 1)
                if total_points
                else 0.0,
                "pointsBySprint": arr,
            }
        )
    assignees.sort(key=lambda a: a["totalPoints"], reverse=True)

    issue_types = [
        {"label": k, "count": v}
        for k, v in sorted(issue_type_counts.items(), key=lambda item: item[1], reverse=True)
    ]

    return {
        "meta": {
            "squadKey": squad.key,
            "squadName": squad.name,
            "boardId": squad.board_id,
            "projectKey": squad.project_key,
            "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "sourceMetric": "Concluído (statusCategory = done)",
            "totalSprints": len(sprint_rows),
            "totalIssues": total_done_issues,
            "totalPoints": total_points,
            "velocityAvg": round(total_points / len(sprint_rows)) if sprint_rows else 0,
        },
        "sprints": sprint_rows,
        "assignees": assignees,
        "issueTypes": issue_types,
    }


def write_dataset(squad_key: str, payload: dict[str, Any]) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    output_path = DATA_DIR / f"{squad_key}-data.json"
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_path


def main() -> int:
    args = parse_args()

    jira_site = load_env("JIRA_SITE_URL")
    jira_email = load_env("JIRA_EMAIL")
    jira_token = load_env("JIRA_API_TOKEN")
    points_field = os.getenv("JIRA_STORY_POINTS_FIELD", "customfield_10016").strip()
    squads = load_squads()

    if args.squad:
        selected = [s for s in squads if s.key.upper() == args.squad.upper()]
        if not selected:
            raise RuntimeError(f"Squad não encontrada em config/squads.json: {args.squad}")
        squads = selected

    session = requests.Session()
    session.auth = HTTPBasicAuth(jira_email, jira_token)
    session.headers.update({"Accept": "application/json"})

    generated = []
    for squad in squads:
        print(f"[INFO] Gerando dados: {squad.key} (board {squad.board_id})")
        payload = build_squad_dataset(
            session=session,
            site_url=jira_site.rstrip("/"),
            squad=squad,
            points_field=points_field,
            max_sprints=args.max_sprints,
        )
        output_path = write_dataset(squad.key, payload)
        generated.append(output_path.name)
        print(f"[OK] {output_path}")

    print(f"[DONE] Arquivos gerados: {', '.join(generated)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
