# Slopsmith Plugin: Update Manager

A plugin for [Slopsmith](https://github.com/byrongamatos/slopsmith) that installs, updates, and uninstalls other plugins **and the slopsmith core itself** from a single in-app screen. Uses GitHub downloads directly — no `git` CLI inside the container, no terminal, no `docker compose` commands required.

<img width="997" height="999" alt="image" src="https://github.com/user-attachments/assets/57f68aca-7362-4f94-ab9e-73e269a6ad1e" />
<img width="994" height="1239" alt="updateman2" src="https://github.com/user-attachments/assets/4c1457c0-5bd2-48c7-982f-7cff33406119" />

## Features

- **Browse the registry** — parses the "Available Plugins" table from slopsmith's README and lists every plugin with a one-click Install button
- **Filter** — search by name, description, or directory
- **Plugin update detection** — compares each installed plugin's commit SHA against its GitHub default branch (works without the `git` binary)
- **One-click plugin update** — re-downloads the latest source via GitHub zip and replaces the plugin directory atomically
- **Slopsmith core updates** — tracks `byrongamatos/slopsmith` alongside your plugins, overlays updates onto the bind-mounted code paths (`server.py`, `ug_browser.py`, `lib/`, `static/`)
- **Rebuild-required detection** — if an upstream commit touches unmounted files (`Dockerfile`, `requirements.txt`, `docker-compose.yml`, etc.) the update is blocked and the UI shows a copy-paste host command
- **Update all** — sequentially updates the core plus every plugin that's behind
- **Uninstall** — removes a plugin directory
- **Exclusion list** — flag plugins (or the core) to skip during update checks and "update all" — persisted on the `/config` volume so it survives container restarts
- **In-place restart** — `Restart now` button re-execs the uvicorn process via `os.execv` without touching Docker, preserving PID 1 and container lifetime

## What's New

### v1.8.1
- **Fix stuck "Update available" after `git pull`** — `_resolve_source` now picks the freshest of the marker (`installed_at`) and `.git/refs/heads/<branch>` (mtime) instead of unconditionally preferring the marker. Cloned plugins whose marker is older than the current `.git` HEAD (typical after `git pull`) no longer appear behind forever; UI-zip-updated cloned plugins still surface the marker since it gets a newer `installed_at` than the preserved `.git/`.

### v1.8.0
- **Per-plugin Check button** — each plugin row now has a "Check" button next to its primary action. Re-checks just that plugin against GitHub, useful when the bulk cold pass has burnt through GitHub's anonymous rate-limit window and left some rows in "Check failed". Backed by a new `GET /check/{plugin_id}` endpoint that reuses the same conditional-fetch / version-first short-circuit as the bulk pass.

### v1.5.0
- **Ignore non-core files** — updates to documentation (`*.md`, `docs/`), tests (`tests/`), and Claude config (`.claude/`) no longer block core updates. These paths are silently skipped during both blocker detection and extraction.
- **Self-update** — the Update Manager can now update itself. When an update is available, clicking Update downloads the new version to a staging area, then prompts you to restart to apply it.

### v1.4.0
- **Slopsmith core tracking** — a new "Slopsmith Core" row appears above the plugin table. Click **Initialize tracking** once to stamp the current commit, then the card reports behind/ahead state against `byrongamatos/slopsmith`.
- **Non-destructive core overlay** — updates write only the files that exist in the upstream zip; files present locally and absent from the zip (e.g. `static/audio_*.mp3`, `__pycache__/`, every installed plugin dir) are left alone.
- **Rebuild-required guard** — if the GitHub compare between your installed SHA and the remote HEAD reports changes to any path outside the bind-mounted set, the update is blocked and an amber banner lists the offending files plus a copy-paste rebuild command.
- **Rename** — plugin id and install path are now `update_manager` (was `plugin_manager`). API routes moved from `/api/plugins/plugin_manager/*` to `/api/plugins/update_manager/*`.

## Requirements

- Outbound HTTPS from the slopsmith container to `github.com`, `api.github.com`, `raw.githubusercontent.com`, `codeload.github.com`
- No additional Python dependencies — stdlib only (`urllib`, `zipfile`)
- A readable `/proc/self/cmdline` inside the container (standard on Linux) — used by the in-place restart

## Installation

```bash
cd /path/to/slopsmith/plugins
git clone https://github.com/masc0t/slopsmith-update-manager.git update_manager
docker compose restart
```

After this one-time bootstrap, further plugins can be installed, the core can be tracked, and the manager itself can be updated through the UI.

## How It Works

1. Open **Update Manager** in the nav — the Updates tab checks installed plugins and the core against GitHub in parallel
2. For each installed plugin, the installed commit SHA is read from either a `.slopsmith-installed.json` marker (plugins installed through this tool) or the plugin's `.git/config` and `.git/HEAD` (plugins cloned manually on the host)
3. Core tracking lives at `/config/update_manager/core.json` — click **Initialize tracking** on first use to stamp the current remote HEAD as the baseline
4. Clicking **Update** on a plugin downloads its repo zip and atomically swaps the directory
5. Clicking **Update** on the core downloads the slopsmith zip and non-destructively overlays just the mounted paths, leaving runtime artifacts (audio files, pycache, other plugins) untouched
6. When the update completes, a green banner appears — click **Restart now** and the uvicorn process re-execs in place so the new code loads without a Docker restart

> **Note:** The plugin only writes to paths that `docker-compose.yml` bind-mounts into the container. Updates that touch image-baked files (`Dockerfile`, `requirements.txt`, etc.) are surfaced as **Rebuild required** and must be applied from the host.

## Slopsmith Core Updates

The core row appears above the plugin table on the Updates tab. It shows the installed SHA, the remote SHA, and a status pill:

- **Tracking not initialized** — click **Initialize tracking** to stamp the current remote HEAD (or call `POST /core/init` with an explicit `{sha}` to mark an older commit)
- **Up to date** — local matches remote
- **Update available** — safe to apply from the UI; all changed files fall under the mounted whitelist
- **Rebuild required** — remote commits touch files outside the mount whitelist; the Update button is disabled and the amber banner lists every blocker plus a copy-paste command

### What gets overlayed

Only paths that `docker-compose.yml` bind-mounts into the container can be rewritten from inside it:

- `server.py`
- `ug_browser.py`
- `lib/`
- `static/`

`plugins/` is always excluded from core updates — each plugin is tracked independently by this tool and would be clobbered otherwise.

### Rebuild command

When the core update is blocked, the UI surfaces this host-side command:

```bash
cd slopsmith && git pull && docker compose build web && docker compose up -d
```

### Exclusions

Toggle the **Exclude** checkbox on the core row to skip it during future checks and "update all". Exclusions are persisted to `/config/update_manager/exclusions.json`.

## API

All endpoints are namespaced under `/api/plugins/update_manager/`:

| Method | Path                        | Description                                  |
|--------|-----------------------------|----------------------------------------------|
| GET    | `/registry`                 | Parses slopsmith's README and returns the plugin list |
| GET    | `/updates`                  | Compares installed plugins against GitHub    |
| GET    | `/check/{plugin_id}`        | Re-checks one plugin. Success: `{plugin_id, update, error, source, excluded, bundled}`. Validation failure (invalid id / not installed): `{error}` only (no `plugin_id`) |
| POST   | `/install`                  | Body `{url, dirname}` — installs from a GitHub repo |
| POST   | `/update/{plugin_id}`       | Re-downloads latest source and swaps         |
| POST   | `/uninstall/{plugin_id}`    | Removes the plugin directory                 |
| GET    | `/exclusions`               | Returns the current exclusion list           |
| POST   | `/exclusions`               | Body `{plugin_id, excluded}` — toggles exclusion (use `plugin_id: "__core__"` for the core) |
| GET    | `/core`                     | Returns `{repo, branch, local_sha, remote_sha, behind, blockers, changed_files, tracking, excluded, rebuild_required}` |
| POST   | `/core/init`                | Body `{sha?}` — stamp marker with `sha` (or current remote HEAD) to enable core tracking |
| POST   | `/core/update`              | Overlays mounted core files from GitHub zip. Returns `{error: "rebuild_required", blockers, command}` if any unmounted file changed |
| POST   | `/restart`                  | Re-execs the server process in place         |

## Limitations

- GitHub's unauthenticated rate limit is 60 req/hour per IP. Each "Check for updates" makes one API call per installed plugin, plus one for the core — well within the limit for normal use
- Only GitHub repositories are supported (registry, compare, and zip endpoints are GitHub-specific)

## Other Plugins

- [MIDI Capo](https://github.com/masc0t/slopsmith-plugin-midi-capo) — send MIDI CC to your amp/modeler or internal VST to match each song's tuning automatically
- [Find More Songs](https://github.com/masc0t/slopsmith-plugin-find-more) — search CustomsForge for more songs by an artist
- [Invert Highway](https://github.com/masc0t/slopsmith-plugin-invert-highway) — flip the chord note stacking order on the highway

## License

MIT
