"""TASK-195: who may start the daily worker, and which of them the gate lets through.

GitHub expressions cannot be run here, so these pin the text the gate step is
built from. A drift in any of them changes which attempt does the day's work.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from fear_ladder import paths

WORKFLOW = paths.PROJECT_ROOT / ".github" / "workflows" / "daily_monitor.yml"


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    loaded = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # YAML 1.1 reads the bare key `on` as the boolean true.
    loaded["on"] = loaded.pop(True, loaded.get("on"))
    return loaded


def _gate(workflow: dict[str, Any]) -> str:
    [step] = [s for s in workflow["jobs"]["monitor"]["steps"] if s.get("id") == "gate"]
    return " ".join(step["run"].split())


def test_an_external_scheduler_can_say_which_attempt_it_is(workflow: dict[str, Any]) -> None:
    attempt = workflow["on"]["workflow_dispatch"]["inputs"]["attempt"]
    assert attempt["type"] == "choice"
    assert attempt["options"] == ["manual", "scheduled", "last"]
    # A person pressing "Run workflow" names nothing, and must still get a run.
    assert attempt["default"] == "manual"


def test_only_a_manual_dispatch_skips_the_gate(workflow: dict[str, Any]) -> None:
    gate = _gate(workflow)
    assert (
        "github.event_name == 'workflow_dispatch' && (inputs.attempt || 'manual') == 'manual'"
        " && '--manual'"
    ) in gate


def test_the_last_attempt_is_either_the_last_schedule_or_an_external_last(
    workflow: dict[str, Any],
) -> None:
    gate = _gate(workflow)
    assert (
        "(github.event.schedule == env.LAST_ATTEMPT_CRON || "
        "(github.event_name == 'workflow_dispatch' && inputs.attempt == 'last'))"
        " && '--last-attempt'"
    ) in gate


def test_the_last_attempt_cron_is_the_latest_schedule(workflow: dict[str, Any]) -> None:
    crons = [entry["cron"] for entry in workflow["on"]["schedule"]]
    last = workflow["env"]["LAST_ATTEMPT_CRON"]
    assert last in crons

    def minutes(cron: str) -> int:
        minute, hour, *_ = cron.split()
        return int(hour) * 60 + int(minute)

    assert minutes(last) == max(minutes(cron) for cron in crons)
