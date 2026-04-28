#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib


LEVEL_ORDER = ["low", "medium", "high"]
MANAGED_LEVELS = set(LEVEL_ORDER)
SCRIPT_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class BalanceBand:
    rank: int
    min_pct: float
    max_pct: float
    model_ref: str
    thinking: str


@dataclass(frozen=True)
class ManagedProfile:
    band_rank: int | None
    model_ref: str
    thinking: str


@dataclass
class Settings:
    settings_file: Path
    openclaw_bin: str
    openclaw_config_path: Path
    provider: str
    manage_sessions: bool
    active_minutes: int | None
    five_hour_balance_bands: tuple[BalanceBand, ...]
    weekly_balance_percentage_override_condition: float
    weekly_balance_days_left_override_condition: float
    reset_soon_enabled: bool
    reset_soon_window_minutes: float
    reset_soon_min_weekly_pct: float
    reset_soon_min_five_pct: float
    reset_soon_max_upgrade_steps: int
    reset_soon_allow_highest_band: bool
    notifications_enabled: bool
    notifications_message_prefix: str
    notifications_include_reasons: bool
    notifications_destinations: tuple["NotificationDestination", ...]


@dataclass(frozen=True)
class NotificationDestination:
    channel: str
    target: str
    account: str | None
    thread_id: str | None
    silent: bool


@dataclass
class UsageSnapshot:
    five_hour_left: float | None
    week_left: float | None
    five_hour_reset_at: int | None
    week_reset_at: int | None
    five_hour_reset_in_minutes: float | None
    week_reset_in_minutes: float | None


@dataclass
class Decision:
    raw_band: BalanceBand
    target_band: BalanceBand
    target_profile: ManagedProfile
    weekly_override: str
    reasons: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Automatically tune OpenClaw model and thinking profile based on remaining quota."
    )
    parser.add_argument(
        "--settings-file",
        default=str(SCRIPT_DIR / "config.toml"),
        help="Path to the optimizer TOML settings file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and print actions without modifying OpenClaw.",
    )
    parser.add_argument(
        "--test-notification",
        action="store_true",
        help="Send a test notification to configured destinations and exit.",
    )
    return parser.parse_args()


def load_toml_file(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def resolve_path(raw: str | None, base_dir: Path, default: Path) -> Path:
    if not raw:
        return default
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return candidate
    return (base_dir / candidate).resolve()


def validate_percentage(name: str, value: float) -> None:
    if not 0 <= value <= 100:
        raise ValueError(f"{name} must be between 0 and 100, got {value}")


def normalize_model_ref(model_ref: str, default_provider: str | None = None) -> str:
    value = model_ref.strip()
    if not value:
        raise ValueError("model reference must not be empty")
    if "/" in value:
        return value
    if not default_provider:
        raise ValueError(f"model reference {value!r} is missing a provider prefix")
    return f"{default_provider}/{value}"


def split_model_ref(model_ref: str) -> tuple[str | None, str]:
    if "/" not in model_ref:
        return None, model_ref
    provider, model = model_ref.split("/", 1)
    return provider or None, model


def profile_from_band(band: BalanceBand) -> ManagedProfile:
    return ManagedProfile(
        band_rank=band.rank,
        model_ref=band.model_ref,
        thinking=band.thinking,
    )


def profile_to_state(profile: ManagedProfile | None) -> dict[str, Any] | None:
    if profile is None:
        return None
    return {
        "band_rank": profile.band_rank,
        "model_ref": profile.model_ref,
        "thinking": profile.thinking,
    }


def profile_from_state(raw: Any) -> ManagedProfile | None:
    if not isinstance(raw, dict):
        return None
    model_ref = raw.get("model_ref")
    thinking = raw.get("thinking")
    if not isinstance(model_ref, str) or not isinstance(thinking, str):
        return None
    band_rank = raw.get("band_rank")
    if band_rank is not None and not isinstance(band_rank, int):
        band_rank = None
    return ManagedProfile(
        band_rank=band_rank,
        model_ref=model_ref.strip(),
        thinking=thinking.strip(),
    )


def profiles_equal(left: ManagedProfile | None, right: ManagedProfile | None) -> bool:
    if left is None or right is None:
        return left is right
    return left.model_ref == right.model_ref and left.thinking == right.thinking


def display_level(level: str | None) -> str:
    if level in MANAGED_LEVELS:
        return level.capitalize()
    return "Unknown"


def display_profile(profile: ManagedProfile | None) -> str:
    if profile is None:
        return "Unknown profile"
    return f"{profile.model_ref} | thinking {display_level(profile.thinking)}"


def display_profile_compact(profile: ManagedProfile | None) -> str:
    if profile is None:
        return "Unknown profile"
    return f"{profile.model_ref} {display_level(profile.thinking)}"


def parse_balance_bands(raw_bands: Any, default_provider: str) -> tuple[BalanceBand, ...]:
    if not isinstance(raw_bands, list) or not raw_bands:
        raise ValueError("five_hour_balance.bands must be a non-empty array of tables")

    parsed: list[BalanceBand] = []
    seen_ranks: set[int] = set()
    for index, entry in enumerate(raw_bands, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"five_hour_balance.bands[{index}] must be a table")

        rank_raw = entry.get("rank", entry.get("range"))
        if not isinstance(rank_raw, int):
            raise ValueError(f"five_hour_balance.bands[{index}].rank must be an integer")
        if rank_raw in seen_ranks:
            raise ValueError(f"duplicate band rank: {rank_raw}")
        seen_ranks.add(rank_raw)

        min_pct = float(entry.get("min_pct"))
        max_pct = float(entry.get("max_pct"))
        validate_percentage(f"five_hour_balance.bands[{index}].min_pct", min_pct)
        validate_percentage(f"five_hour_balance.bands[{index}].max_pct", max_pct)
        if min_pct >= max_pct:
            raise ValueError(
                f"five_hour_balance.bands[{index}] must satisfy min_pct < max_pct, got {min_pct} >= {max_pct}"
            )

        model_ref_raw = entry.get("model")
        thinking_raw = entry.get("thinking", entry.get("mode"))
        if not isinstance(model_ref_raw, str):
            raise ValueError(f"five_hour_balance.bands[{index}].model must be a string")
        if not isinstance(thinking_raw, str):
            raise ValueError(f"five_hour_balance.bands[{index}].thinking must be a string")
        thinking = thinking_raw.strip().lower()
        if thinking not in MANAGED_LEVELS:
            raise ValueError(
                f"five_hour_balance.bands[{index}].thinking must be one of {sorted(MANAGED_LEVELS)}, got {thinking!r}"
            )

        parsed.append(
            BalanceBand(
                rank=rank_raw,
                min_pct=min_pct,
                max_pct=max_pct,
                model_ref=normalize_model_ref(model_ref_raw, default_provider=default_provider),
                thinking=thinking,
            )
        )

    parsed.sort(key=lambda band: band.min_pct)

    tolerance = 1e-9
    if abs(parsed[0].min_pct - 0.0) > tolerance:
        raise ValueError("five_hour_balance.bands must start at 0%")
    if abs(parsed[-1].max_pct - 100.0) > tolerance:
        raise ValueError("five_hour_balance.bands must end at 100%")

    for previous, current in zip(parsed, parsed[1:]):
        if abs(previous.max_pct - current.min_pct) > tolerance:
            raise ValueError(
                "five_hour_balance.bands must be contiguous with no gaps or overlaps "
                f"(got {previous.max_pct} followed by {current.min_pct})"
            )

    return tuple(parsed)


def validate_settings(settings: Settings) -> None:
    percentage_fields = {
        "weekly_balance.percentage_override_condition": settings.weekly_balance_percentage_override_condition,
        "reset_soon.min_weekly_pct": settings.reset_soon_min_weekly_pct,
        "reset_soon.min_five_pct": settings.reset_soon_min_five_pct,
    }
    for name, value in percentage_fields.items():
        validate_percentage(name, value)

    if settings.active_minutes is not None and settings.active_minutes <= 0:
        raise ValueError("behavior.active_minutes must be > 0 when set")
    if settings.weekly_balance_days_left_override_condition < 0:
        raise ValueError("weekly_balance.days_left_override_condition must be >= 0")
    if settings.reset_soon_window_minutes < 0:
        raise ValueError("reset_soon.window_minutes must be >= 0")
    if settings.reset_soon_max_upgrade_steps < 0:
        raise ValueError("reset_soon.max_upgrade_steps must be >= 0")
    if settings.notifications_enabled and not settings.notifications_destinations:
        raise ValueError("notifications.enabled is true but no notifications.destinations are configured")
    if settings.notifications_enabled:
        for destination in settings.notifications_destinations:
            if not destination.channel.strip():
                raise ValueError("notifications.destinations[].channel must not be empty")
            if not destination.target.strip():
                raise ValueError("notifications.destinations[].target must not be empty")


def load_settings(settings_file: Path) -> Settings:
    raw = load_toml_file(settings_file)
    base_dir = settings_file.parent

    openclaw_cfg = raw.get("openclaw", {})
    behavior_cfg = raw.get("behavior", {})
    five_hour_balance_cfg = raw.get("five_hour_balance", {})
    weekly_cfg = raw.get("weekly_balance", raw.get("weekly", {}))
    reset_cfg = raw.get("reset_soon", {})
    notifications_cfg = raw.get("notifications", {})
    raw_destinations = notifications_cfg.get("destinations", [])

    provider = str(openclaw_cfg.get("provider", "openai-codex")).strip()
    if not provider:
        raise ValueError("openclaw.provider must not be empty")

    destinations: list[NotificationDestination] = []
    if isinstance(raw_destinations, list):
        for entry in raw_destinations:
            if not isinstance(entry, dict):
                continue
            destinations.append(
                NotificationDestination(
                    channel=str(entry.get("channel", "")).strip(),
                    target=str(entry.get("target", "")).strip(),
                    account=str(entry["account"]).strip() if entry.get("account") is not None else None,
                    thread_id=str(entry["thread_id"]).strip() if entry.get("thread_id") is not None else None,
                    silent=bool(entry.get("silent", False)),
                )
            )

    settings = Settings(
        settings_file=settings_file,
        openclaw_bin=str(openclaw_cfg.get("bin", str(Path.home() / ".npm-global" / "bin" / "openclaw"))),
        openclaw_config_path=resolve_path(
            openclaw_cfg.get("config_path"),
            base_dir,
            Path.home() / ".openclaw" / "openclaw.json",
        ),
        provider=provider,
        manage_sessions=bool(behavior_cfg.get("manage_sessions", True)),
        active_minutes=behavior_cfg.get("active_minutes"),
        five_hour_balance_bands=parse_balance_bands(
            five_hour_balance_cfg.get("bands"),
            default_provider=provider,
        ),
        weekly_balance_percentage_override_condition=float(
            weekly_cfg.get("percentage_override_condition", 15)
        ),
        weekly_balance_days_left_override_condition=float(
            weekly_cfg.get("days_left_override_condition", 1)
        ),
        reset_soon_enabled=bool(reset_cfg.get("enabled", True)),
        reset_soon_window_minutes=float(reset_cfg.get("window_minutes", 10)),
        reset_soon_min_weekly_pct=float(reset_cfg.get("min_weekly_pct", 45)),
        reset_soon_min_five_pct=float(reset_cfg.get("min_five_pct", 15)),
        reset_soon_max_upgrade_steps=int(reset_cfg.get("max_upgrade_steps", 1)),
        reset_soon_allow_highest_band=bool(
            reset_cfg.get("allow_highest_band", reset_cfg.get("allow_medium_to_high", False))
        ),
        notifications_enabled=bool(notifications_cfg.get("enabled", False)),
        notifications_message_prefix=str(notifications_cfg.get("message_prefix", "ModelCostOptimizer")).strip()
        or "ModelCostOptimizer",
        notifications_include_reasons=bool(notifications_cfg.get("include_reasons", True)),
        notifications_destinations=tuple(destinations),
    )

    if settings.active_minutes is not None:
        settings.active_minutes = int(settings.active_minutes)
    validate_settings(settings)
    return settings


def run_openclaw(settings: Settings, *extra: str, expect_json: bool = False) -> Any:
    cmd = [settings.openclaw_bin, *extra]
    env = os.environ.copy()
    env.setdefault("HOME", str(settings.openclaw_config_path.parent.parent))
    completed = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            f"Command failed ({completed.returncode}): {' '.join(cmd)}\n"
            f"STDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
        )
    if expect_json:
        return json.loads(completed.stdout)
    return completed.stdout


def load_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def current_default_level(config: dict[str, Any]) -> str | None:
    level = config.get("agents", {}).get("defaults", {}).get("thinkingDefault")
    return level if isinstance(level, str) else None


def current_default_model_ref(config: dict[str, Any]) -> str | None:
    model_ref = config.get("agents", {}).get("defaults", {}).get("model", {}).get("primary")
    return model_ref if isinstance(model_ref, str) else None


def current_default_profile(config: dict[str, Any]) -> ManagedProfile | None:
    thinking = current_default_level(config)
    model_ref = current_default_model_ref(config)
    if not isinstance(thinking, str) or not isinstance(model_ref, str):
        return None
    return ManagedProfile(
        band_rank=None,
        model_ref=model_ref,
        thinking=thinking,
    )


def compute_minutes_until(reset_at_ms: int | None) -> float | None:
    if reset_at_ms is None:
        return None
    delta_seconds = max(0.0, reset_at_ms / 1000.0 - time.time())
    return delta_seconds / 60.0


def load_usage_snapshot(settings: Settings) -> UsageSnapshot:
    status = run_openclaw(settings, "status", "--json", "--usage", expect_json=True)
    providers = status.get("usage", {}).get("providers", [])
    provider = next((item for item in providers if item.get("provider") == settings.provider), None)
    if provider is None:
        available = ", ".join(sorted(str(item.get("provider")) for item in providers if item.get("provider")))
        raise RuntimeError(
            f"Provider {settings.provider!r} not found in usage snapshot. Available: {available or 'none'}"
        )

    five_hour_left = None
    week_left = None
    five_hour_reset_at = None
    week_reset_at = None

    for window in provider.get("windows", []):
        label = str(window.get("label", "")).strip().lower()
        used_percent = window.get("usedPercent")
        reset_at = window.get("resetAt")
        if not isinstance(used_percent, (int, float)):
            continue
        left = max(0.0, min(100.0, 100.0 - float(used_percent)))
        if label == "5h":
            five_hour_left = left
            five_hour_reset_at = int(reset_at) if isinstance(reset_at, (int, float)) else None
        elif label == "week":
            week_left = left
            week_reset_at = int(reset_at) if isinstance(reset_at, (int, float)) else None

    if five_hour_left is None:
        raise RuntimeError("The 5h usage window is missing from the status snapshot.")

    return UsageSnapshot(
        five_hour_left=five_hour_left,
        week_left=week_left,
        five_hour_reset_at=five_hour_reset_at,
        week_reset_at=week_reset_at,
        five_hour_reset_in_minutes=compute_minutes_until(five_hour_reset_at),
        week_reset_in_minutes=compute_minutes_until(week_reset_at),
    )


def find_band_by_rank(settings: Settings, rank: int | None) -> BalanceBand | None:
    if rank is None:
        return None
    for band in settings.five_hour_balance_bands:
        if band.rank == rank:
            return band
    return None


def find_band_by_profile(settings: Settings, profile: ManagedProfile | None) -> BalanceBand | None:
    if profile is None:
        return None
    for band in settings.five_hour_balance_bands:
        if band.model_ref == profile.model_ref and band.thinking == profile.thinking:
            return band
    return None


def classify_five_hour_band(settings: Settings, balance_pct: float) -> BalanceBand:
    for band in reversed(settings.five_hour_balance_bands):
        if balance_pct < band.min_pct:
            continue
        if band.max_pct >= 100 and balance_pct <= band.max_pct:
            return band
        if balance_pct < band.max_pct:
            return band
    return settings.five_hour_balance_bands[0]


def decide_five_hour_band(
    settings: Settings,
    usage: UsageSnapshot,
) -> tuple[BalanceBand, list[str]]:
    five_hour_balance = usage.five_hour_left
    if five_hour_balance is None:
        raise RuntimeError("5h balance is unavailable")

    selected = classify_five_hour_band(settings, five_hour_balance)
    reasons = [
        f"5h balance {five_hour_balance:.1f}% selects band {selected.rank} "
        f"({selected.min_pct:.1f}%..{selected.max_pct:.1f}%)"
    ]
    return selected, reasons


def raise_band(
    settings: Settings,
    base_band: BalanceBand,
    max_steps: int,
    allow_highest_band: bool,
) -> BalanceBand:
    if max_steps <= 0:
        return base_band

    bands = settings.five_hour_balance_bands
    start_index = bands.index(base_band)
    highest_allowed_index = len(bands) - 1
    if not allow_highest_band and len(bands) > 1:
        highest_allowed_index = len(bands) - 2

    # The reset-soon bonus is allowed to upgrade or no-op, but never downgrade
    # a band that was already selected by the core policy.
    highest_allowed_index = max(start_index, highest_allowed_index)
    target_index = min(start_index + max_steps, highest_allowed_index)

    return bands[target_index]


def apply_reset_soon_bonus(
    settings: Settings,
    usage: UsageSnapshot,
    base_band: BalanceBand,
) -> tuple[BalanceBand, list[str]]:
    reasons: list[str] = []
    reset_minutes = usage.five_hour_reset_in_minutes
    week_left = usage.week_left
    five_left = usage.five_hour_left

    if not settings.reset_soon_enabled:
        reasons.append("reset-soon bonus is disabled")
        return base_band, reasons

    if reset_minutes is None:
        reasons.append("5h reset time is unavailable")
        return base_band, reasons

    if reset_minutes > settings.reset_soon_window_minutes:
        reasons.append(
            f"5h reset is not close enough yet ({reset_minutes:.1f}m > {settings.reset_soon_window_minutes:.1f}m)"
        )
        return base_band, reasons

    if week_left is not None and week_left < settings.reset_soon_min_weekly_pct:
        reasons.append(f"weekly balance {week_left:.1f}% is below the reset-soon safety floor")
        return base_band, reasons

    if five_left is not None and five_left < settings.reset_soon_min_five_pct:
        reasons.append(f"5h balance {five_left:.1f}% is below the reset-soon safety floor")
        return base_band, reasons

    upgraded = raise_band(
        settings=settings,
        base_band=base_band,
        max_steps=settings.reset_soon_max_upgrade_steps,
        allow_highest_band=settings.reset_soon_allow_highest_band,
    )
    if upgraded == base_band:
        reasons.append("reset-soon bonus did not change the selected band")
        return base_band, reasons

    reasons.append(
        f"5h reset is close ({reset_minutes:.1f}m), so the policy temporarily upgrades "
        f"band {base_band.rank} to band {upgraded.rank}"
    )
    return upgraded, reasons


def apply_weekly_override(
    settings: Settings,
    usage: UsageSnapshot,
) -> tuple[bool, list[str]]:
    weekly_balance = usage.week_left
    weekly_reset_minutes = usage.week_reset_in_minutes
    reasons: list[str] = []

    if weekly_balance is None:
        reasons.append("weekly balance is unavailable, so the weekly override is skipped")
        return False, reasons
    if weekly_reset_minutes is None:
        reasons.append("weekly reset time is unavailable, so the weekly override is skipped")
        return False, reasons

    weekly_days_left = weekly_reset_minutes / (60.0 * 24.0)
    if weekly_balance >= settings.weekly_balance_percentage_override_condition:
        reasons.append(
            f"weekly balance {weekly_balance:.1f}% is not below the override threshold "
            f"{settings.weekly_balance_percentage_override_condition:.1f}%"
        )
        return False, reasons
    if weekly_days_left <= settings.weekly_balance_days_left_override_condition:
        reasons.append(
            f"weekly reset is too close ({weekly_days_left:.2f}d <= "
            f"{settings.weekly_balance_days_left_override_condition:.2f}d)"
        )
        return False, reasons

    reasons.append(
        f"weekly balance {weekly_balance:.1f}% is below "
        f"{settings.weekly_balance_percentage_override_condition:.1f}% and "
        f"{weekly_days_left:.2f} days remain, so the weekly override forces band 1"
    )
    return True, reasons


def decide_profile(
    settings: Settings,
    usage: UsageSnapshot,
) -> Decision:
    raw_band, raw_reasons = decide_five_hour_band(settings, usage)
    reset_band, reset_reasons = apply_reset_soon_bonus(settings, usage, raw_band)
    weekly_override_applied, weekly_reasons = apply_weekly_override(settings, usage)
    final_band = settings.five_hour_balance_bands[0] if weekly_override_applied else reset_band

    reasons = [*raw_reasons, *reset_reasons, *weekly_reasons]
    return Decision(
        raw_band=raw_band,
        target_band=final_band,
        target_profile=profile_from_band(final_band),
        weekly_override="range_1" if weekly_override_applied else "none",
        reasons=reasons,
    )


def patch_default_profile(
    settings: Settings,
    target_profile: ManagedProfile,
    current_profile: ManagedProfile | None,
    dry_run: bool,
) -> bool:
    current_model_ref = current_profile.model_ref if current_profile is not None else None
    current_thinking = current_profile.thinking if current_profile is not None else None

    model_changed = current_model_ref != target_profile.model_ref
    thinking_changed = current_thinking != target_profile.thinking
    if not model_changed and not thinking_changed:
        return False

    if dry_run:
        return True

    if model_changed:
        run_openclaw(settings, "models", "set", target_profile.model_ref)
    if thinking_changed:
        run_openclaw(
            settings,
            "config",
            "set",
            "agents.defaults.thinkingDefault",
            json.dumps(target_profile.thinking),
            "--strict-json",
        )
    return True


def load_sessions(settings: Settings) -> list[dict[str, Any]]:
    command = ["sessions", "--json"]
    if settings.active_minutes is not None:
        command.extend(["--active", str(settings.active_minutes)])
    payload = run_openclaw(settings, *command, expect_json=True)
    return payload.get("sessions", [])


def session_model_ref(
    settings: Settings,
    session: dict[str, Any],
    fallback_model_ref: str | None,
) -> str | None:
    model = session.get("model")
    provider = session.get("modelProvider") or session.get("providerOverride")
    if isinstance(model, str):
        model_text = model.strip()
        if not model_text:
            return fallback_model_ref
        if "/" in model_text:
            return model_text
        if isinstance(provider, str) and provider.strip():
            return f"{provider.strip()}/{model_text}"
        if fallback_model_ref is not None:
            fallback_provider, _ = split_model_ref(fallback_model_ref)
            if fallback_provider:
                return f"{fallback_provider}/{model_text}"
        return normalize_model_ref(model_text, default_provider=settings.provider)
    return fallback_model_ref


def resolve_session_profile(
    settings: Settings,
    session: dict[str, Any],
    default_profile: ManagedProfile | None,
) -> ManagedProfile | None:
    fallback_model_ref = default_profile.model_ref if default_profile is not None else current_default_model_ref({})
    model_ref = session_model_ref(settings, session, fallback_model_ref)
    thinking = session.get("thinkingLevel")
    if not isinstance(thinking, str) and default_profile is not None:
        thinking = default_profile.thinking
    if not isinstance(model_ref, str) or not isinstance(thinking, str):
        return None
    return ManagedProfile(
        band_rank=None,
        model_ref=model_ref,
        thinking=thinking,
    )


def session_matches(settings: Settings, session: dict[str, Any]) -> bool:
    if session.get("kind") != "direct":
        return False
    model = session.get("model")
    if isinstance(model, str) and "/" in model:
        provider, _ = split_model_ref(model)
        if provider and provider != settings.provider:
            return False
    provider = session.get("modelProvider") or session.get("providerOverride")
    if isinstance(provider, str) and provider.strip() and provider.strip() != settings.provider:
        return False
    return True


def patch_session_profile(settings: Settings, key: str, target_profile: ManagedProfile) -> None:
    run_openclaw(
        settings,
        "gateway",
        "call",
        "sessions.patch",
        "--json",
        "--params",
        json.dumps(
            {
                "key": key,
                "model": target_profile.model_ref,
                "thinkingLevel": target_profile.thinking,
            }
        ),
    )


def should_send_notification(
    settings: Settings,
    *,
    default_changed: bool,
    updated_sessions: list[str],
) -> bool:
    if not settings.notifications_enabled:
        return False
    return default_changed or bool(updated_sessions)


def format_count(value: int, singular: str, plural: str) -> str:
    return f"{value} {singular if value == 1 else plural}"


def format_percentage(value: float | None) -> str:
    if value is None:
        return "unknown"
    return f"{value:.0f}%"


def format_duration_minutes(total_minutes: float | None) -> str:
    if total_minutes is None:
        return "unknown time"

    remaining = int(round(max(0.0, total_minutes)))
    days = remaining // (24 * 60)
    remaining -= days * 24 * 60
    hours = remaining // 60
    remaining -= hours * 60
    minutes = remaining

    parts: list[str] = []
    if days:
        parts.append(format_count(days, "day", "days"))
    if hours:
        parts.append(format_count(hours, "hour", "hours"))
    if minutes or not parts:
        parts.append(format_count(minutes, "minute", "minutes"))
    return " ".join(parts)


def normalize_reason_text(reason: str) -> str:
    text = reason.replace("5h balance", "5h balance")
    text = text.replace("weekly balance", "weekly balance")
    return re.sub(r"(\d+)\.0%", r"\1%", text)


def build_reason_text(decision: Decision) -> str:
    if not decision.reasons:
        return "No reason available"
    return "; ".join(normalize_reason_text(reason) for reason in decision.reasons)


def format_change_timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")


def format_notification_message(
    settings: Settings,
    usage: UsageSnapshot,
    decision: Decision,
    current_profile: ManagedProfile | None,
    updated_sessions: list[str],
    *,
    test: bool,
) -> str:
    previous_text = display_profile_compact(current_profile)
    target_text = display_profile_compact(decision.target_profile)
    five_text = format_percentage(usage.five_hour_left)
    week_text = format_percentage(usage.week_left)
    five_reset_text = format_duration_minutes(usage.five_hour_reset_in_minutes)
    week_reset_text = format_duration_minutes(usage.week_reset_in_minutes)
    separator = "." * 49
    if test:
        header_lines = [
            separator,
            f"{settings.notifications_message_prefix} test notification",
            f"Target: {target_text}",
        ]
    else:
        if not profiles_equal(current_profile, decision.target_profile):
            header_lines = [
                separator,
                f"Model change {format_change_timestamp()}",
                f"From: {previous_text}",
                f"To:   {target_text}",
            ]
        else:
            header_lines = [
                separator,
                "Managed sessions updated",
                f"Target: {target_text}",
                f"Count:  {len(updated_sessions)}",
            ]
    quota_lines = (
        f"5h balance at {five_text} will be reset in {five_reset_text}\n"
        f"Weekly balance at {week_text} will be reset in {week_reset_text}"
    )
    if settings.notifications_include_reasons and decision.reasons:
        reason_text = build_reason_text(decision)
        return (
            "\n".join(header_lines)
            + f"\nReason: {reason_text}\n{separator}\n{quota_lines}\n{separator}"
        )
    return "\n".join(header_lines) + f"\n{separator}\n{quota_lines}\n{separator}"


def send_notification_message(
    settings: Settings,
    message: str,
    *,
    dry_run: bool,
) -> list[str]:
    delivered: list[str] = []
    for destination in settings.notifications_destinations:
        command = [
            "message",
            "send",
            "--channel",
            destination.channel,
            "--target",
            destination.target,
            "--message",
            message,
        ]
        if destination.account:
            command.extend(["--account", destination.account])
        if destination.thread_id:
            command.extend(["--thread-id", destination.thread_id])
        if destination.silent:
            command.append("--silent")
        if dry_run:
            command.append("--dry-run")
        run_openclaw(settings, *command)
        delivered.append(f"{destination.channel}:{destination.target}")
    return delivered


def reconcile_sessions(
    settings: Settings,
    target_profile: ManagedProfile,
    default_profile: ManagedProfile | None,
    dry_run: bool,
) -> list[str]:
    if not settings.manage_sessions:
        return []

    sessions = load_sessions(settings)

    updated: list[str] = []

    for session in sessions:
        if not session_matches(settings, session):
            continue
        key = session.get("key")
        if not isinstance(key, str) or not key:
            continue

        current_profile = resolve_session_profile(settings, session, default_profile)
        if profiles_equal(current_profile, target_profile):
            continue

        if not dry_run:
            patch_session_profile(settings, key, target_profile)
        updated.append(key)

    return updated


def build_summary(
    usage: UsageSnapshot,
    current_profile: ManagedProfile | None,
    decision: Decision,
    default_changed: bool,
    updated_sessions: list[str],
    notification_sent_to: list[str],
    dry_run: bool,
) -> dict[str, Any]:
    return {
        "dry_run": dry_run,
        "5h_balance": usage.five_hour_left,
        "5h_reset_in_minutes": usage.five_hour_reset_in_minutes,
        "weekly_balance": usage.week_left,
        "weekly_reset_in_minutes": usage.week_reset_in_minutes,
        "current_default_profile": profile_to_state(current_profile),
        "raw_5h_band_rank": decision.raw_band.rank,
        "target_band_rank": decision.target_band.rank,
        "target_model": decision.target_profile.model_ref,
        "target_thinking": decision.target_profile.thinking,
        "weekly_override": decision.weekly_override,
        "reasons": decision.reasons,
        "default_changed": default_changed,
        "updated_sessions": updated_sessions,
        "notification_sent_to": notification_sent_to,
    }


def print_summary(summary: dict[str, Any]) -> None:
    five = summary["5h_balance"]
    week = summary["weekly_balance"]
    five_reset = summary["5h_reset_in_minutes"]
    week_text = "unknown" if week is None else f"{week:.1f}%"
    five_reset_text = "unknown" if five_reset is None else f"{five_reset:.1f}m"
    action = "would set" if summary["dry_run"] else "set"
    print(
        f"5h balance={five:.1f}% | 5h reset in={five_reset_text} | weekly balance={week_text} | "
        f"{action} band={summary['target_band_rank']} model={summary['target_model']} "
        f"thinking={summary['target_thinking']} | weekly override={summary['weekly_override']} | "
        f"reasons={'; '.join(summary['reasons'])}"
    )
    if summary["default_changed"]:
        print("Default model/thinking profile updated.")
    if summary["updated_sessions"]:
        print("Updated sessions:")
        for key in summary["updated_sessions"]:
            print(f"  - {key}")
    if summary["notification_sent_to"]:
        print("Notifications sent to:")
        for destination in summary["notification_sent_to"]:
            print(f"  - {destination}")


def main() -> int:
    args = parse_args()
    settings_file = Path(args.settings_file).expanduser().resolve()
    settings = load_settings(settings_file)

    config = load_json_file(settings.openclaw_config_path, default={})
    current_profile = current_default_profile(config)

    usage = load_usage_snapshot(settings)
    decision = decide_profile(settings, usage)

    if args.test_notification:
        if not settings.notifications_enabled:
            raise RuntimeError("Notifications are disabled in config.toml.")
        message = format_notification_message(
            settings,
            usage,
            decision,
            current_profile,
            updated_sessions=[],
            test=True,
        )
        delivered = send_notification_message(settings, message, dry_run=args.dry_run)
        summary = build_summary(
            usage=usage,
            current_profile=current_profile,
            decision=decision,
            default_changed=False,
            updated_sessions=[],
            notification_sent_to=delivered,
            dry_run=args.dry_run,
        )
        print_summary(summary)
        return 0

    default_changed = patch_default_profile(settings, decision.target_profile, current_profile, args.dry_run)
    updated_sessions = reconcile_sessions(
        settings,
        decision.target_profile,
        current_profile,
        args.dry_run,
    )
    notification_sent_to: list[str] = []
    if should_send_notification(
        settings,
        default_changed=default_changed,
        updated_sessions=updated_sessions,
    ):
        message = format_notification_message(
            settings,
            usage,
            decision,
            current_profile,
            updated_sessions=updated_sessions,
            test=False,
        )
        notification_sent_to = send_notification_message(settings, message, dry_run=args.dry_run)

    summary = build_summary(
        usage=usage,
        current_profile=current_profile,
        decision=decision,
        default_changed=default_changed,
        updated_sessions=updated_sessions,
        notification_sent_to=notification_sent_to,
        dry_run=args.dry_run,
    )
    print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
