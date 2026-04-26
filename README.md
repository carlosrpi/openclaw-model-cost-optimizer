# OpenClaw Model Cost Optimizer

This project automatically adjusts the OpenClaw LLM model and the `thinking` level (`low`, `medium`, `high`) based on the remaining Codex quota.

It is intentionally kept outside the OpenClaw installation so it is easy to:

- back up separately
- move to another machine
- version independently
- refine without mixing watchdog files into OpenClaw state folders

## Current Scope

The current implementation is intentionally narrow:

- it is designed for OpenClaw setups using `openai-codex` OAuth-based
- it expects OpenClaw usage snapshots to expose the `openai-codex` provider

The installer checks for this and stops with a clear message if the machine does not look like an `openai-codex` OAuth-based setup.

## What It Is

The watchdog is an external automation made of:

- a `systemd --user` timer
- a `systemd --user` one-shot service
- a Python script
- a local TOML policy file
- a small local state file
- an installer script that renders machine-specific files
- optional outbound notifications sent through OpenClaw

OpenClaw does not run this code internally. The watchdog simply uses official OpenClaw commands and RPC methods from the outside.

## End-To-End Flow

The runtime architecture is:

1. A `systemd --user` timer triggers the optimizer periodically.
2. The timer starts a `systemd --user` one-shot service.
3. That service runs `python openclaw-model-cost-optimizer.py`.
4. The Python script reads the local `config.toml`, reads its local state file, asks OpenClaw for the current usage snapshot, computes the target model/thinking profile, and only then applies changes if needed.
5. OpenClaw itself remains external to this project. The optimizer does not run inside OpenClaw and does not patch OpenClaw source code.

Inside the Python script, the logical flow is:

1. Read `openclaw status --json --usage` and extract:
   - `5h_balance`
   - `5h_reset_in_minutes`
   - `weekly_balance`
   - `weekly_reset_in_minutes`
2. Select the base band from `five_hour_balance.bands` using only `5h_balance`.
3. Evaluate `reset_soon`:
   - if the 5-hour reset is close enough
   - and the configured safety conditions are satisfied
   - then temporarily raise the selected band by the configured number of steps
4. Evaluate the weekly override at the very end:
   - if `weekly_balance` is below `weekly_balance.percentage_override_condition`
   - and the weekly reset is still more than `weekly_balance.days_left_override_condition` days away
   - then force band `1`
5. Convert the final band into a target `model + thinking` profile.
6. Compare that target profile with the current OpenClaw defaults and the optimizer's remembered state.
7. If a change is needed, apply it through official OpenClaw interfaces:
   - `openclaw models set ...`
   - `openclaw config set agents.defaults.thinkingDefault ...`
   - `openclaw gateway call sessions.patch ...` for managed sessions
8. If notifications are enabled and the managed profile changed, send the message through OpenClaw.
9. Save the optimizer state file so the next run knows what profile it last applied.

In short, the timer/service layer is only the scheduler. The real decision logic lives in the Python script, and the Python script treats OpenClaw as an external system that it reads from and updates through supported commands.

## Repository Layout

- `openclaw-model-cost-optimizer.py`
  Main script. It reads usage, applies policy, updates OpenClaw, and writes local watchdog state.

- `config.example.toml`
  Template for the local policy file. The installer renders this into `config.toml`.

- `openclaw-model-cost-optimizer.service.template`
  Template for the local `systemd --user` service file. The installer renders this into `openclaw-model-cost-optimizer.service`.

- `openclaw-model-cost-optimizer.timer`
  Timer unit committed as-is. It triggers the service every 10 minutes.

- `install.sh`
  Setup script. It validates prerequisites, renders local files, installs the user service/timer, and optionally starts them.

- `README.md`
  This documentation.

Generated locally by the installer:

- `config.toml`
- `openclaw-model-cost-optimizer.service`
- `openclaw-model-cost-optimizer.json`

## Installation

Clone the repository into the directory where you want the watchdog to live, then run the installer from that directory.

Example:

```bash
git clone https://github.com/carlosrpi/openclaw-model-cost-optimizer.git ~/openclawModelCostOptimizer
cd ~/openclawModelCostOptimizer
chmod +x install.sh
./install.sh
```

What the installer does:

1. Checks that `python3` exists and is version 3.11 or newer.
2. Checks that `systemctl` exists and then uses `systemctl --user`.
3. Finds the OpenClaw binary.
4. Checks that OpenClaw config exists.
5. Verifies the machine looks like an `openai-codex` setup.
6. Renders `config.toml` from `config.example.toml`.
7. Renders `openclaw-model-cost-optimizer.service` from the service template.
8. Installs user-level `systemd` symlinks.
9. Reloads `systemd --user`.
10. Enables and starts the timer by default.

Installer options:

```bash
./install.sh --help
```

Supported options:

- `--force-config`
  Re-render `config.toml` even if it already exists.

- `--no-enable`
  Install files but do not enable or start the timer.

- `--openclaw-bin PATH`
  Override the OpenClaw binary path.

- `--openclaw-config PATH`
  Override the OpenClaw config path.

## Configuration File

The policy lives in `config.toml`.

TOML was chosen because it is:

- easy to read
- easy to comment
- easier to maintain than hardcoded thresholds in Python
- built into modern Python via `tomllib`

The installer generates `config.toml` from `config.example.toml`, replacing the machine-specific paths.

Example rendered config:

```toml
[openclaw]
bin = "/home/example/.npm-global/bin/openclaw"
config_path = "/home/example/.openclaw/openclaw.json"
provider = "openai-codex"

[files]
state_path = "/home/example/openclawModelCostOptimizer/openclaw-model-cost-optimizer.json"

[behavior]
manage_sessions = true
active_minutes = 1440

[five_hour_balance]
[[five_hour_balance.bands]]
rank = 1
min_pct = 0
max_pct = 30
model = "openai-codex/gpt-5.4-mini"
thinking = "high"

[[five_hour_balance.bands]]
rank = 2
min_pct = 30
max_pct = 60
model = "openai-codex/gpt-5.4"
thinking = "medium"

[[five_hour_balance.bands]]
rank = 3
min_pct = 60
max_pct = 100
model = "openai-codex/gpt-5.4"
thinking = "high"

[weekly_balance]
percentage_override_condition = 15
days_left_override_condition = 1

[reset_soon]
enabled = true
window_minutes = 10
min_weekly_pct = 45
min_five_pct = 15
max_upgrade_steps = 1
allow_highest_band = false

[notifications]
enabled = false
message_prefix = "ModelCostOptimizer"
include_reasons = true

[[notifications.destinations]]
channel = "telegram"
target = "123456789"
account = "default"
thread_id = "ops-room"
silent = false
```

Important behavior that is not obvious from the example alone:

- `openclaw.provider` decides which provider block is read from `openclaw status --json --usage`.
- The same `openclaw.provider` value is also used to normalize bare model names and to ignore sessions that belong to a different provider.
- `behavior.manage_sessions = false` keeps the optimizer at the global-default level only and skips `sessions.patch`.
- `behavior.active_minutes` is forwarded to `openclaw sessions --active ...` so only recently active sessions are considered for automatic reconciliation.
- `files.state_path` stores both the last applied managed profile and the per-session bookkeeping used to avoid overwriting manual-looking session changes.

## Current Policy

The current decision policy has three layers, applied in this order:

1. Choose a base band from `5h_balance`.
2. Optionally raise that band with `reset_soon`.
3. Optionally force band `1` with the weekly override.

### Base 5-hour selection

The base decision comes from `five_hour_balance.bands`.

Each band defines:

- `min_pct`
- `max_pct`
- `model`
- `thinking`

`model` can be written either as a full `provider/model` reference or as a bare model name. If the provider prefix is omitted, the script prepends `openclaw.provider`.

The script looks at `5h_balance` and picks the band whose configured range contains that percentage.

This supports any number of bands, as long as:

- the first band starts at `0`
- the last band ends at `100`
- bands are contiguous, with no gaps or overlaps

Band `1` is treated as the lowest band. Higher rank numbers mean more permissive bands.

### Reset-soon adjustment

After the base band is selected, `reset_soon` may raise it temporarily.

That happens only if all configured safety conditions pass:

- the feature is enabled
- the 5-hour reset is within `reset_soon.window_minutes`
- `weekly_balance` is at least `reset_soon.min_weekly_pct`
- `5h_balance` is at least `reset_soon.min_five_pct`

If those checks pass, the script raises the selected band by up to `reset_soon.max_upgrade_steps`.

If `allow_highest_band = false`, the reset-soon adjustment cannot reach the top configured band.

### Weekly final override

The weekly override runs last, after the base selection and after `reset_soon`.

If both conditions are true:

- `weekly_balance < weekly_balance.percentage_override_condition`
- the weekly reset is still more than `weekly_balance.days_left_override_condition` days away

then the script forces the final result to band `1`.

With the default example config, this means:

- if weekly balance drops below `15%`
- and there is still more than `1 day` left before the weekly reset

then the optimizer ignores the previously selected band and uses band `1`.

## Notifications

The watchdog can optionally send a notification when the managed model/thinking profile changes.

This is useful if you want to know when the agent is no longer running on `high`, so you can apply more manual verification to the answers.

Notifications are sent through OpenClaw itself:

- `openclaw message send --channel ... --target ... --message ...`

That keeps the watchdog external to OpenClaw while still reusing the channels that OpenClaw already knows how to deliver to.

Each notification destination can also set:

- `account` to pick a non-default OpenClaw messaging account
- `thread_id` to post into a specific thread when the channel supports it
- `silent = true` to request a quiet delivery

Recommended initial notification policy:

- notify every time the managed profile changes

Example notification config:

```toml
[notifications]
enabled = true
message_prefix = "ModelCostOptimizer"
include_reasons = true

[[notifications.destinations]]
channel = "telegram"
target = "123456789"
account = "default"
thread_id = "ops-room"
silent = false
```

Example message:

```text
Managed profile changed from openai-codex/gpt-5.4 | thinking High to openai-codex/gpt-5.4-mini | thinking High
Reason: 5h balance 28% selects band 1 (0%..30%)
-----------------------------------------------------
5h balance at 28% will be reset in 1 hour 58 minutes
Weekly balance at 61% will be reset in 4 days 7 hours 12 minutes
```

## Why It Also Updates Sessions

Changing only `agents.defaults.thinkingDefault` is not always enough.

If a session already has a stored `thinkingLevel` or a stored model selection, that session may continue using its own stored value. Because of that, the watchdog does three things:

1. Update the global OpenClaw default model.
2. Update the global OpenClaw default thinking level.
3. Patch matching direct sessions that appear to still be under automatic control.

If a session looks manually overridden, the watchdog skips it instead of blindly overwriting it.

More specifically, the current implementation only considers:

- direct sessions
- sessions whose provider matches `openclaw.provider`
- sessions whose current profile still matches either the last profile previously managed for that session or the previous global default profile

That heuristic is the current definition of "still under automatic control".

## What The Watchdog Changes

The watchdog does not manually edit OpenClaw files. It uses official OpenClaw interfaces:

- `openclaw status --json --usage`
- `openclaw models set ...`
- `openclaw config set agents.defaults.thinkingDefault ...`
- `openclaw gateway call sessions.patch ...`
- `openclaw message send ...` for optional notifications

Those operations end up changing OpenClaw state such as:

- `~/.openclaw/openclaw.json`
- `~/.openclaw/agents/main/sessions/sessions.json`

The only file written directly by the watchdog itself is:

- `openclaw-model-cost-optimizer.json`

## Does OpenClaw Need To Know This Exists?

No special OpenClaw integration is required.

OpenClaw does not need a plugin, hook, or extra registration step for this watchdog to work. OpenClaw only needs to keep exposing the official capabilities that the watchdog already uses:

- `openclaw status --json --usage`
- `openclaw models set`
- `openclaw config set`
- `openclaw gateway call sessions.patch`

So OpenClaw works normally even if this project does not exist.

From OpenClaw's point of view, the watchdog is just an external actor that sometimes changes:

- the global default model
- the global `thinkingDefault`
- selected session-level `model` / `thinkingLevel` values

## Useful Commands

Run one manual pass:

```bash
python3 ./openclaw-model-cost-optimizer.py --settings-file ./config.toml --json
```

Run a simulation without changing anything:

```bash
python3 ./openclaw-model-cost-optimizer.py --settings-file ./config.toml --dry-run --json
```

Send a test notification without waiting for a quota-driven level change:

```bash
python3 ./openclaw-model-cost-optimizer.py --settings-file ./config.toml --test-notification
```

Check timer status:

```bash
systemctl --user status openclaw-model-cost-optimizer.timer
```

Check the latest service logs:

```bash
journalctl --user -u openclaw-model-cost-optimizer.service -n 50 --no-pager
```

Trigger an immediate run:

```bash
systemctl --user start openclaw-model-cost-optimizer.service
```

Reload systemd after editing unit files:

```bash
systemctl --user daemon-reload
systemctl --user restart openclaw-model-cost-optimizer.timer
```

## Notes About Code Quality

The current version includes a few defensive checks aimed at making the project safer to reuse:

- config values are validated before use
- percentage thresholds must stay within `0..100`
- 5-hour bands must be contiguous and cover `0..100`
- invalid reset-soon settings fail early with a readable error
- the installer validates prerequisites before wiring the timer

## Future Refinements

Likely next improvements:

- add an uninstall script
- refine manual-vs-automatic session detection
- support more nuanced reset logic
- add optional metrics/history output
