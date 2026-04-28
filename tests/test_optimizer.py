import importlib.util
import re
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "openclaw-model-cost-optimizer.py"
SPEC = importlib.util.spec_from_file_location("optimizer", MODULE_PATH)
optimizer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = optimizer
SPEC.loader.exec_module(optimizer)


class OptimizerPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = optimizer.Settings(
            settings_file=Path("config.toml"),
            openclaw_bin="openclaw",
            openclaw_config_path=Path("openclaw.json"),
            provider="openai-codex",
            manage_sessions=True,
            active_minutes=1440,
            five_hour_balance_bands=(
                optimizer.BalanceBand(1, 0.0, 30.0, "openai-codex/gpt-5.4-mini", "high"),
                optimizer.BalanceBand(2, 30.0, 60.0, "openai-codex/gpt-5.4", "medium"),
                optimizer.BalanceBand(3, 60.0, 100.0, "openai-codex/gpt-5.4", "high"),
            ),
            weekly_balance_percentage_override_condition=15.0,
            weekly_balance_days_left_override_condition=1.0,
            reset_soon_enabled=True,
            reset_soon_window_minutes=10.0,
            reset_soon_min_weekly_pct=45.0,
            reset_soon_min_five_pct=15.0,
            reset_soon_max_upgrade_steps=1,
            reset_soon_allow_highest_band=False,
            notifications_enabled=True,
            notifications_message_prefix="Test message",
            notifications_include_reasons=True,
            notifications_destinations=(),
        )

    def test_reset_soon_does_not_downgrade_top_band_when_highest_band_is_blocked(self) -> None:
        top_band = self.settings.five_hour_balance_bands[-1]
        upgraded = optimizer.raise_band(
            settings=self.settings,
            base_band=top_band,
            max_steps=1,
            allow_highest_band=False,
        )
        self.assertEqual(upgraded, top_band)

    def test_decision_keeps_band_three_when_reset_is_close(self) -> None:
        usage = optimizer.UsageSnapshot(
            five_hour_left=88.0,
            week_left=98.0,
            five_hour_reset_at=None,
            week_reset_at=None,
            five_hour_reset_in_minutes=7.3,
            week_reset_in_minutes=(6 * 24 * 60) + (19 * 60) + 7,
        )

        decision = optimizer.decide_profile(self.settings, usage)

        self.assertEqual(decision.raw_band.rank, 3)
        self.assertEqual(decision.target_band.rank, 3)
        self.assertEqual(decision.target_profile.thinking, "high")
        self.assertIn("selects band 3", decision.reasons[0])
        self.assertIn("did not change the selected band", decision.reasons[1])

    def test_notification_reason_text_includes_full_decision_chain(self) -> None:
        usage = optimizer.UsageSnapshot(
            five_hour_left=88.0,
            week_left=98.0,
            five_hour_reset_at=None,
            week_reset_at=None,
            five_hour_reset_in_minutes=7.0,
            week_reset_in_minutes=(6 * 24 * 60) + (19 * 60) + 7,
        )
        decision = optimizer.decide_profile(self.settings, usage)

        message = optimizer.format_notification_message(
            settings=self.settings,
            usage=usage,
            decision=decision,
            current_profile=optimizer.ManagedProfile(
                band_rank=3,
                model_ref="openai-codex/gpt-5.4",
                thinking="medium",
            ),
            updated_sessions=[],
            test=False,
        )

        self.assertIn("Reason: 5h balance 88% selects band 3", message)
        self.assertIn("reset-soon bonus did not change the selected band", message)
        self.assertIn("weekly balance 98% is not below the override threshold 15%", message)
        self.assertRegex(message, r"Model change \d{4}-\d{2}-\d{2} \d{2}:\d{2}")


if __name__ == "__main__":
    unittest.main()
