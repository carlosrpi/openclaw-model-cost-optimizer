# Changelog

## Unreleased

- Fixed a reset-soon edge case that could incorrectly demote the highest selected 5-hour band when `allow_highest_band = false`.
- Expanded notification reasons so they include the full decision chain instead of only the first reason line.
- Simplified session management so each run directly enforces the rule-selected profile on OpenClaw defaults and matching sessions.
- Removed the optimizer state file and the manual-override preservation heuristic that depended on it.
- Removed the optimizer's own `--json` output mode while keeping the internal OpenClaw JSON API calls it relies on.
- Updated installer, uninstall, and documentation to reflect the stateless design.

## 0.1.0 - 2026-04-26

- First public release of OpenClaw Model Cost Optimizer.
- Added quota-based model and thinking-profile selection using configurable 5-hour bands.
- Added reset-soon promotion logic and weekly low-balance override logic.
- Added optional reconciliation for recent direct sessions that still appear to be under automatic control.
- Added optional OpenClaw-delivered notifications for managed profile changes.
- Added installer-driven setup for the local config file and `systemd --user` service/timer files.
- Added `uninstall.sh` to disable the timer, remove installed unit wiring, and optionally purge local runtime files.
