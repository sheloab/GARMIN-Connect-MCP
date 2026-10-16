from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptSpec:
    name: str
    description: str
    template: str


PROMPT_SPECS = [
    PromptSpec(
        name="recovery_check",
        description="Use Garmin recovery data to decide whether today should be easy or hard.",
        template=(
            "Use Garmin tools first. Check recovery, sleep, HRV, body battery, stress, "
            "and training load. Then recommend today's training intensity with clear reasoning."
        ),
    ),
    PromptSpec(
        name="weekly_training_review",
        description="Review recent training volume, sport balance, recovery, and risk.",
        template=(
            "Use Garmin tools first. Review the last 7-28 days of activities, load, sleep, "
            "stress, and recovery. Summarize what improved, what looks risky, and what to do next."
        ),
    ),
    PromptSpec(
        name="activity_analysis",
        description="Analyze one recent workout using its activity details.",
        template=(
            "Use get_recent_activities to identify the workout, then get_activity_detail. "
            "Analyze execution, intensity, pacing, and recovery implications."
        ),
    ),
    PromptSpec(
        name="race_plan_context",
        description="Gather Garmin context before creating a race or training plan.",
        template=(
            "Use Garmin tools first. Pull full snapshot, recent load, fitness, zones, and "
            "personal records. Build recommendations from the athlete's current data."
        ),
    ),
]

PROMPTS_BY_NAME = {prompt.name: prompt for prompt in PROMPT_SPECS}
