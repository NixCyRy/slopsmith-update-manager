# Slopsmith Plugin: Update Manager

A plugin for [Slopsmith](https://github.com/byrongamatos/slopsmith) that installs, updates, and uninstalls other plugins from a single in-app screen. Works with both the Docker and [slopsmith-desktop](https://github.com/byrongamatos/slopsmith-desktop) deployments. Uses GitHub downloads directly — no `git` CLI inside the container, no terminal required.

<img width="997" height="999" alt="image" src="https://github.com/user-attachments/assets/57f68aca-7362-4f94-ab9e-73e269a6ad1e" />
<img width="994" height="1239" alt="updateman2" src="https://github.com/user-attachments/assets/4c1457c0-5bd2-48c7-982f-7cff33406119" />

## Features

- **Browse the registry** — parses the "Available Plugins" table from slopsmith's README and lists every plugin with a one-click Install button
- **Filter** — search by name, description, or directory
- **Plugin update detection** — compares each installed plugin's commit SHA against its GitHub default branch (works without the `git` binary)
- **One-click plugin update** — re-downloads the latest source via GitHub zip and replaces the plugin directory atomically
- **Update all** — sequentially updates every plugin that's behind
- **Uninstall** — removes a plugin directory
- **Exclusion list** — flag plugins to skip during update checks and "update all" — persisted to the config directory so it survives restarts
- **In-place restart** — Docker: re-execs the uvicorn process via `os.execv` without touching Docker, preserving PID 1 and container lifetime. Desktop: delegates to the desktop app's built-in restart API
- **Self-update** — the Update Manager can update itself; new files are staged and applied on the next restart

## What's New

### v1.6.0
- **Desktop compatibility** — works with [slopsmith-desktop](https://github.com/byrongamatos/slopsmith-desktop): Docker-specific UI (the `docker compose restart` snippet) is hidden inside the desktop app, restart delegates to `slopsmithDesktop.plugins.restart()`, and staged self-updates are flushed before the desktop-managed restart
- **Removed: Slopsmith core tracking** — the in-app core update feature has been removed; updates to slopsmith itself are applied by rebuilding the container in the usual way

### v1.5.0
- **Self-update** — the Update Manager can now update itself. Clicking Update stages the new version to disk, then a banner prompts you to restart to apply it.

### v1.4.0
- **Rename** — plugin id and install path are now `update_manager` (was `plugin_manager`). API routes moved from `/api/plugins/plugin_manager/*` to `/api/plugins/update_manager/*`.

## Requirements

- Outbound HTTPS from the slopsmith host to `github.com`, `api.github.com`, `raw.githubusercontent.com`, `codeload.github.com`
- No additional Python dependencies — stdlib only (`urllib`, `zipfile`)
- **Docker**: a readable `/proc/self/cmdline` (standard on Linux) — used by in-place restart
- **Desktop**: `CONFIG_DIR` env var set to a writable path by the desktop app (e.g. `~/.config/slopsmith-desktop`)

## Installation

```bash
cd /path/to/slopsmith/plugins
git clone https://github.com/masc0t/slopsmith-update-manager.git update_manager
docker compose restart
```

After this one-time bootstrap, further plugins and the manager itself can be installed and updated through the UI.

## How It Works

1. Open **Update Manager** in the nav — the Updates tab checks every installed plugin against GitHub
2. For each plugin, the installed commit SHA is read from either a `.slopsmith-installed.json` marker (plugins installed through this tool) or from `.git/config` and `.git/HEAD` (plugins cloned manually on the host)
3. Clicking **Update** downloads the repo zip and atomically swaps the plugin directory
4. When updates are applied, a banner appears — click **Restart now** to reload the new code. On Docker this re-execs uvicorn in-place; on desktop it calls the desktop app's restart API

## API

All endpoints are namespaced under `/api/plugins/update_manager/`:

| Method | Path                        | Description                                  |
|--------|-----------------------------|----------------------------------------------|
| GET    | `/config`                   | Returns `{is_desktop}` — detected via `SLOPSMITH_DESKTOP` env var |
| GET    | `/registry`                 | Parses slopsmith's README and returns the plugin list |
| GET    | `/updates`                  | Compares installed plugins against GitHub    |
| POST   | `/install`                  | Body `{url, dirname}` — installs from a GitHub repo |
| POST   | `/update/{plugin_id}`       | Re-downloads latest source and swaps; for `update_manager` itself, stages files and returns `{pending_restart: true}` |
| POST   | `/uninstall/{plugin_id}`    | Removes the plugin directory                 |
| GET    | `/exclusions`               | Returns the current exclusion list           |
| POST   | `/exclusions`               | Body `{plugin_id, excluded}` — toggles exclusion for a plugin |
| POST   | `/restart`                  | Applies any staged self-update, then re-execs uvicorn (Docker) or returns immediately for `{apply_only: true}` (Desktop) |

## Limitations

- GitHub's unauthenticated rate limit is 60 req/hour per IP. Each "Check for updates" makes one API call per installed plugin — well within the limit for normal use
- Only GitHub repositories are supported (registry, compare, and zip endpoints are GitHub-specific)

## Other Plugins

- [MIDI Capo](https://github.com/masc0t/slopsmith-plugin-midi-capo) — send MIDI CC to your amp/modeler or internal VST to match each song's tuning automatically
- [Find More Songs](https://github.com/masc0t/slopsmith-plugin-find-more) — search CustomsForge for more songs by an artist
- [Invert Highway](https://github.com/masc0t/slopsmith-plugin-invert-highway) — flip the chord note stacking order on the highway

## License

MIT
