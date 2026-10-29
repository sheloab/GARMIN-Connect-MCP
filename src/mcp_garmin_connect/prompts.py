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
    PromptSpec(
        name="health_review",
        description="Whole-body health review from every Garmin health signal.",
        template=(
            "Use Garmin tools first: get_health_summary, get_sleep_stats, get_cardio_respiratory, "
            "get_hrv_trend, get_health_snapshots, get_ecg_readings, get_blood_pressure, "
            "get_glucose, get_body_metrics and get_wellness_trends. Explain what is normal, what "
            "changed, and what to watch, in plain language. Garmin data is not a diagnosis: "
            "flag anything that warrants a doctor (e.g. an AFib ECG result, high blood pressure) "
            "without alarming."
        ),
    ),
    PromptSpec(
        name="running_coach_program",
        description="Professional-coach running plan: asks your goal, recommends a duration "
        "from your history, then builds an evidence-based plan with air-quality timing.",
        template=(
            "Act as an experienced, evidence-based running coach. Step 1: ask the athlete "
            "their goal race (10K, half marathon or marathon), and optionally goal time, race "
            "date and running days per week. Step 2: call build_running_plan with that goal "
            "and weeks=0; tell the athlete the recommended plan length and why (from their "
            "own weekly volume and longest recent run), list the options (2 weeks, 1, 2, 3 "
            "or 4 months) with what each means, and ask them to choose. Step 3: call "
            "build_running_plan with the chosen weeks, then get_running_air_quality, and "
            "check get_recovery, get_hrv_trend, get_sleep_stats, get_running_tolerance and "
            "get_advanced_performance. Step 4: present the plan week by week (sessions, "
            "distances, paces and HR zones), the best times of day to run from the "
            "air-quality data, the daily adjustment rules, and the cited evidence. Never "
            "raise a long run more than 10% above the longest run of the last 30 days. Say "
            "clearly that this is not medical advice."
        ),
    ),
]

PROMPTS_BY_NAME = {prompt.name: prompt for prompt in PROMPT_SPECS}
