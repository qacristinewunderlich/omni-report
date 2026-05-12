#!/usr/bin/env python3
"""
Gera arquivos data/{SQUAD}-data.json a partir do Jira.

Estratégia de coleta de pontos (em ordem de prioridade):
1. Greenhopper Velocity Chart API — retorna pontos exatos da coluna "Concluído"
2. Sprint Report API — pontos completados por sprint
3. Fallback: soma story_points das issues Done via Agile API

Regra: usar apenas issues concluídas (statusCategory == "done").
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

CANDIDATE_FIELDS = [
    "customfield_10016",
    "story_points",
    "customfield_10028",
    "customfield_10034",
    "customfield_10024",
]


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
) -> dict[str, Any] | None:
    url = f"{base_url}{endpoint}"
    resp = session.get(url, params=params, timeout=60)
    if resp.status_code == 404:
        return None
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


def detect_points_field(
    session: requests.Session,
    site_url: str,
    board_id: int,
) -> str:
    """Consulta a configuração do board para descobrir o campo de estimativa."""
    config = jira_get(session, site_url, f"/rest/agile/1.0/board/{board_id}/configuration")
    if config:
        est = config.get("estimation", {})
        field = est.get("field", {})
        field_id = field.get("fieldId", "")
        if field_id:
            print(f"  [BOARD-CONFIG] Campo de estimativa: {field_id} ({field.get('displayName', '')})")
            return field_id
    return "customfield_10016"


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
        if not payload:
            break
        chunk = payload.get("values", [])
        sprints.extend(chunk)
        if payload.get("isLast", True):
            break
        start_at += payload.get("maxResults", 50)
    return sprints


def get_velocity_chart(
    session: requests.Session,
    site_url: str,
    board_id: int,
) -> dict[int, dict[str, int]] | None:
    """
    Tenta obter dados do Greenhopper Velocity Chart.
    Retorna {sprint_id: {"completed": pts, "committed": pts}} ou None.
    """
    payload = jira_get(
        session,
        site_url,
        "/rest/greenhopper/1.0/rapid/charts/velocity",
        params={"rapidViewId": board_id},
    )
    if not payload:
        return None

    result: dict[int, dict[str, int]] = {}
    completed = payload.get("velocityStatEntries", {})
    for sprint_id_str, entry in completed.items():
        sid = int(sprint_id_str)
        comp = entry.get("completed", {})
        comm = entry.get("estimated", {})
        result[sid] = {
            "completed": int(round(comp.get("value", 0))),
            "committed": int(round(comm.get("value", 0))),
        }
    if result:
        print(f"  [VELOCITY-CHART] Obtidos dados de {len(result)} sprints via Greenhopper")
    return result if result else None


def get_sprint_report_points(
    session: requests.Session,
    site_url: str,
    board_id: int,
    sprint_id: int,
) -> int | None:
    """
    Tenta obter pontos completados via Sprint Report (Greenhopper).
    """
    payload = jira_get(
        session,
        site_url,
        "/rest/greenhopper/1.0/rapid/charts/sprintreport",
        params={"rapidViewId": board_id, "sprintId": sprint_id},
    )
    if not payload:
        return None

    contents = payload.get("contents", {})
    completed_issues = contents.get("completedIssues", [])
    total = 0
    for issue in completed_issues:
        est = issue.get("estimateStatistic", {})
        stat_value = est.get("statFieldValue", {})
        value = stat_value.get("value")
        if value is not None:
            total += int(round(float(value)))
    return total if total > 0 else None


def list_sprint_issues(
    session: requests.Session,
    site_url: str,
    sprint_id: int,
    points_field: str,
) -> list[dict[str, Any]]:
    fields_list = list(set(["assignee", "issuetype", "status", points_field] + CANDIDATE_FIELDS))
    fields = ",".join(fields_list)
    issues: list[dict[str, Any]] = []
    start_at = 0
    while True:
        payload = jira_get(
            session,
            site_url,
            f"/rest/agile/1.0/sprint/{sprint_id}/issue",
            params={"startAt": start_at, "maxResults": 100, "fields": fields},
        )
        if not payload:
            break
        chunk = payload.get("issues", [])
        issues.extend(chunk)
        start_at += payload.get("maxResults", 100)
        if start_at >= payload.get("total", 0):
            break
    return issues


def get_points(fields: dict[str, Any], points_field: str) -> float:
    value = fields.get(points_field)
    if value is None:
        for candidate in CANDIDATE_FIELDS:
            value = fields.get(candidate)
            if value is not None:
                break
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
    points_field_override: str | None,
    max_sprints: int,
) -> dict[str, Any]:
    points_field = points_field_override or detect_points_field(session, site_url, squad.board_id)

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

    velocity_data = get_velocity_chart(session, site_url, squad.board_id)

    sprint_rows: list[dict[str, Any]] = []
    assignee_points_by_sprint: dict[str, list[int]] = defaultdict(list)
    issue_type_counts: dict[str, int] = defaultdict(int)

    previous_points: int | None = None
    total_done_issues = 0
    total_points = 0
    used_velocity_chart = False

    for sprint_idx, sprint in enumerate(sprints):
        sprint_id = sprint["id"]
        issues = list_sprint_issues(session, site_url, sprint_id, points_field)

        done_issues = []
        for issue in issues:
            fields = issue.get("fields", {})
            if is_done(fields):
                done_issues.append(issue)

        # --- Determine sprint points using best available source ---
        sprint_points_from_fields = 0
        for issue in done_issues:
            fields = issue.get("fields", {})
            sprint_points_from_fields += int(round(get_points(fields, points_field)))

        sprint_points = sprint_points_from_fields

        if velocity_data and sprint_id in velocity_data:
            vc_pts = velocity_data[sprint_id]["completed"]
            if vc_pts > 0:
                sprint_points = vc_pts
                used_velocity_chart = True
        elif sprint_points == 0:
            report_pts = get_sprint_report_points(session, site_url, squad.board_id, sprint_id)
            if report_pts and report_pts > 0:
                sprint_points = report_pts
                used_velocity_chart = True

        # --- Distribute points per assignee proportionally ---
        assignee_raw: dict[str, int] = defaultdict(int)
        for issue in done_issues:
            fields = issue.get("fields", {})
            pts = int(round(get_points(fields, points_field)))
            assignee = fields.get("assignee")
            assignee_name = (
                assignee.get("displayName")
                if assignee and assignee.get("displayName")
                else "Não atribuído"
            )
            if pts > 0:
                assignee_raw[assignee_name] += pts
            else:
                assignee_raw[assignee_name] += 0

            issue_type = (fields.get("issuetype") or {}).get("name", "Sem tipo")
            issue_type_counts[issue_type] += 1

        raw_total = sum(assignee_raw.values())
        if sprint_points > 0 and raw_total == 0 and done_issues:
            per_issue = sprint_points / len(done_issues)
            for issue in done_issues:
                fields = issue.get("fields", {})
                assignee = fields.get("assignee")
                aname = (
                    assignee.get("displayName")
                    if assignee and assignee.get("displayName")
                    else "Não atribuído"
                )
                assignee_raw[aname] += int(round(per_issue))

        for assignee_name in list(assignee_raw.keys()):
            while len(assignee_points_by_sprint[assignee_name]) < sprint_idx:
                assignee_points_by_sprint[assignee_name].append(0)
            assignee_points_by_sprint[assignee_name].append(assignee_raw[assignee_name])

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

    if used_velocity_chart:
        print(f"  [INFO] Pontos obtidos via Velocity Chart / Sprint Report do Jira")

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
    points_field_override = os.getenv("JIRA_STORY_POINTS_FIELD", "").strip() or None
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
            points_field_override=points_field_override,
            max_sprints=args.max_sprints,
        )
        output_path = write_dataset(squad.key, payload)
        generated.append(output_path.name)
        print(f"[OK] {output_path} — {payload['meta']['totalPoints']} pts / {payload['meta']['totalIssues']} issues")

    print(f"[DONE] Arquivos gerados: {', '.join(generated)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
