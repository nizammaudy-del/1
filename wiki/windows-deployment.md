# Windows Deployment Guide

This guide covers running the Headroom proxy persistently on Windows for
Claude Code and Codex, including a workaround for a gap in the built-in
`persistent-service` / `persistent-task` install presets on standard
(non-administrator) Windows accounts.

Validated on: Windows 11 Pro (build 10.0.26200), headroom-ai 0.31.0
(PyPI `win_amd64` wheel), against `main` at `v0.31.0-93-g2c9eb7c5`,
Claude Code CLI, Codex Desktop.

## Prerequisites

- Windows 10/11
- Python 3.10+
- headroom-ai 0.31.0 or later (see [Installing](#installing) below)

## Installing

As of headroom-ai 0.31.0, PyPI publishes a prebuilt `win_amd64` wheel:

```powershell
python -m venv .venv
.venv\Scripts\pip.exe install "headroom-ai[proxy]"
```

No compiler toolchain is required. Earlier versions (< ~0.28, before the
`win_amd64` wheel job landed — see `ci: add Windows wheel build job (win_amd64)`
and `ci(release): publish win_amd64 wheel` in the main branch history) only
shipped an sdist for Windows and required building the Rust extension from
source with the MSVC Build Tools. If `pip install` falls back to a source
build, upgrade to the latest release first.

```powershell
.venv\Scripts\headroom.exe --version
.venv\Scripts\headroom.exe doctor
```

## Persistent proxy: the gap on standard (non-admin) accounts

Headroom ships two OS-native persistent-runtime presets
(see [Persistent Installs](persistent-installs.md)):

| Preset | Mechanism | Windows requirement |
|---|---|---|
| `persistent-service` | `sc.exe create ...` | Elevated (administrator) SCM handle |
| `persistent-task` | `schtasks /Create ... /SC ONSTART` | Administrator rights to register a boot-time task |

Both fail with **Access is denied** on a standard Windows account that is not
running elevated:

```
> headroom install apply --preset persistent-service --providers manual --target claude --target codex
[SC] OpenSCManager FAILED 5:
Access is denied.
Error: Failed to install deployment 'default': Command 'sc.exe create headroom-default ...' returned non-zero exit status 5.

> headroom install apply --preset persistent-task --providers manual --target claude --target codex
ERROR: Access is denied.
Error: Failed to install deployment 'default': Command '['schtasks', '/Create', '/TN', 'headroom-default-startup', ..., '/SC', 'ONSTART', '/F']' returned non-zero exit status 1.
```

Without one of these, the only remaining durable option is `headroom init -g claude`,
which intentionally sets `supervisor_kind = none` and instead relies on a
SessionStart/PreToolUse hook (`headroom init hook ensure`) to lazily start the
proxy on demand. That path has its own failure mode on Windows: see
[Known Issues](#known-issues) below.

### Workaround: AtLogOn Scheduled Task (no elevation required)

A per-user Scheduled Task with an `AtLogOn` trigger (rather than `OnStart`)
can be registered by a standard account without elevation. `scripts/windows/`
in this repo provides a minimal installer that mirrors the shape of the
built-in `persistent-task` preset, but uses `AtLogOn`:

```powershell
scripts\windows\install-scheduled-task.ps1 -PythonExe "C:\path\to\.venv\Scripts\python.exe"
Start-ScheduledTask -TaskName HeadroomProxy
```

This registers a task that:

- Starts the proxy at user logon (`New-ScheduledTaskTrigger -AtLogOn`)
- Restarts up to 999 times, 1 minute apart, on failure
- Runs at `RunLevel Limited` (no UAC elevation)

To remove it:

```powershell
scripts\windows\uninstall-scheduled-task.ps1
```

Once the task is registered and running, routing to Claude Code / Codex is
unchanged — it is still just the static `ANTHROPIC_BASE_URL` / `config.toml`
provider wiring that `headroom init` (or `install apply --providers`) sets up.
Only the *lifecycle* (start/keep-alive) responsibility moves from the hook to
the Scheduled Task. If you use this workaround, remove the SessionStart/
PreToolUse hook entries from `~/.claude/settings.json` so only one thing
supervises the proxy process — see [Known Issues](#known-issues) for why
leaving both in place is unsafe.

## Known Issues

### Hook-based `ensure` can double-start the proxy under slow compression

`headroom init -g claude` installs a SessionStart and PreToolUse hook that
runs `headroom init hook ensure` on every session start and (on Windows) every
Bash/PowerShell tool call. That command's internal readiness check
(`_ensure_profile_running` in `headroom/install/runtime.py`) does:

1. Probe `/readyz` with a 1-second budget.
2. If not ready, acquire a per-profile start lock, probe again.
3. If the recorded PID is alive, wait up to 15s for `/readyz`; if that also
   times out, `SIGTERM` the existing process and start a fresh one.

Claude Code's own hook timeout is also 15 seconds
(`"timeout": 15` in the SessionStart/PreToolUse hook entries). Under a slow or
degraded ONNX/Kompress compression pass — which we observed taking >10s for a
single request on this machine — `/readyz` can be slow enough that this
internal logic (and/or Claude Code's own hook timeout) races the still-healthy
proxy, kills it, and starts a duplicate. Because the duplicate's
`start_detached_agent` writes to the same `runner.pid` file
(`~/.headroom/deploy/<profile>/runner.pid`), the loser of the race can clobber
or delete the PID bookkeeping for the still-good process, leaving Windows with
a proxy that is still bound to the port but no longer tracked — the next
Claude Code launch then hits a hook failure and appears to require manually
killing headroom before Claude Code will start.

This is very likely the Windows manifestation of
[headroomlabs-ai/headroom#822](https://github.com/headroomlabs-ai/headroom/pull/822)
("fix(windows): unwedge compression on degraded ONNX runtimes"), which was
still open (not merged) at the time of writing against `main`
(`v0.31.0-93-g2c9eb7c5`). It does not reproduce via Codex on Windows, because
Codex hooks are currently disabled upstream on Windows
(`headroom init codex` prints "Codex hooks are currently disabled upstream on
Windows; provider routing was still installed."), so Codex never runs this
code path — it only uses the static `config.toml` provider block.

**Until #822 lands**, avoid running both the hook and a supervisor
(Scheduled Task or Windows Service) at the same time — pick one owner for the
proxy's lifecycle. The workaround above assumes the Scheduled Task is the sole
owner and the hooks have been removed from `settings.json`.

### `persistent-service` / `persistent-task` require elevation

See [above](#persistent-proxy-the-gap-on-standard-non-admin-accounts). Not
filed upstream yet; tracked here for now.

## Related guides

- [Persistent Installs](persistent-installs.md)
- [macOS LaunchAgent](macos-deployment.md)
- [CLI Reference](cli.md)
- [Proxy Server](proxy.md)
