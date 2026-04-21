# Slopsmith Plugin: Update Manager

Plugin for [Slopsmith](https://github.com/byrongamatos/slopsmith). Single screen for **installing**, **updating**, **excluding**, and **uninstalling** plugins **and the slopsmith core itself** — plus in-place **server restart** — no git CLI, no terminal, no `docker compose` commands required.

## Features

- **Browse the registry** — parses the "Available Plugins" table from slopsmith's README and lists every plugin with a one-click Install button
- **Filter** — search by name, description, or directory
- **Update detection** — compares each installed plugin's commit SHA against its GitHub default branch (works without the `git` binary)
- **One-click update** — re-downloads latest source via GitHub zip and replaces the plugin directory atomically
- **Update all** — sequentially updates the core plus every plugin that's behind
- **Slopsmith core updates** — tracks `byrongamatos/slopsmith` alongside your plugins, overlays updates onto the bind-mounted code paths, and warns when a rebuild is required
- **Uninstall** — removes a plugin directory
- **Exclusion list** — flag plugins (or the core) to skip during update checks and "update all" (survives container restarts)
- **In-place restart** — `Restart now` button re-execs the uvicorn process without touching Docker, preserving PID 1 and container lifetime

## Installation

```bash
cd /path/to/slopsmith/plugins
git clone https://github.com/masc0t/slopsmith-update-manager.git update_manager
docker compose restart
```

After this one-time bootstrap, further plugins can be installed and the manager itself updated through the UI.

## How It Works

The slopsmith docker image does not ship with `git`. To stay plugin-only and avoid touching the core image or `docker-compose.yml`, the manager uses:

- `GET raw.githubusercontent.com/byrongamatos/slopsmith/main/README.md` — plugin registry
- `GET api.github.com/repos/{owner}/{repo}` — default branch
- `GET api.github.com/repos/{owner}/{repo}/commits/{branch}` — latest commit SHA
- `GET codeload.github.com/{owner}/{repo}/zip/refs/heads/{branch}` — source archive

For plugins installed through the manager, a `.slopsmith-installed.json` marker inside each plugin directory records origin, branch, and installed SHA. For plugins installed via host-side `git clone`, the manager reads `.git/config` and `.git/HEAD` directly to infer the same info. Either source type can be updated in place.

Restart works by calling `os.execv` on the current process's argv (recovered from `/proc/self/cmdline`), replacing the uvicorn image in-memory. Container `sh` stays PID 1; the child Python process keeps its PID, so Docker never sees the container exit.

Exclusions are persisted to `/config/update_manager/exclusions.json` — stored on the `/config` volume, so they survive image rebuilds and container restarts.

## Slopsmith core updates

The plugin can also track and update the slopsmith core repository itself (`byrongamatos/slopsmith`). The core row appears above the plugin table on the Updates tab.

**How tracking works.** Because the container doesn't ship `.git`, the plugin records the installed core commit SHA in `/config/update_manager/core.json`. On first use, the card shows "Tracking not initialized"; click **Initialize tracking** to stamp the current remote HEAD as the baseline (or call `POST /core/init` with an explicit `{sha}` if you want to mark an older commit).

**What actually gets updated.** Only paths that are bind-mounted into the container per `docker-compose.yml` can be rewritten from inside it. Those are:

- `server.py`
- `ug_browser.py`
- `lib/`
- `static/`

The update is a **non-destructive overlay** — files in the zip overwrite their counterparts under `/app`, but files present locally and absent from the zip (e.g. `static/audio_*.mp3`, `__pycache__/`, plugin dirs) are left alone.

**Rebuild-required condition.** If the GitHub compare between your installed SHA and the remote HEAD reports changes to any path outside the mounted set (for example `Dockerfile`, `requirements.txt`, `docker-compose.yml`, `scripts/`, `docs/`), the update is blocked and the UI shows the list of blocker files plus a copy-paste command:

```bash
cd slopsmith && git pull && docker compose build web && docker compose up -d
```

`plugins/` is always excluded from core updates — plugins are managed by this tool's own per-plugin logic and would be clobbered otherwise.

## API

All endpoints are namespaced under `/api/plugins/update_manager/`:

| Method | Path                        | Description                                  |
|--------|-----------------------------|----------------------------------------------|
| GET    | `/registry`                 | Parses slopsmith's README and returns the plugin list |
| GET    | `/updates`                  | Compares installed plugins against GitHub    |
| POST   | `/install`                  | Body `{url, dirname}` — installs from a GitHub repo |
| POST   | `/update/{plugin_id}`       | Re-downloads latest source and swaps         |
| POST   | `/uninstall/{plugin_id}`    | Removes the plugin directory                 |
| GET    | `/exclusions`               | Returns the current exclusion list           |
| POST   | `/exclusions`               | Body `{plugin_id, excluded}` — toggles exclusion (use `plugin_id: "__core__"` for the core) |
| GET    | `/core`                     | Returns `{repo, branch, local_sha, remote_sha, behind, blockers, changed_files, tracking, excluded, rebuild_required}` |
| POST   | `/core/init`                | Body `{sha?}` — stamp marker with `sha` (or current remote HEAD) to enable core tracking |
| POST   | `/core/update`              | Overlays mounted core files from GitHub zip. Returns `{error: "rebuild_required", blockers, command}` if any unmounted file changed |
| POST   | `/restart`                  | Re-execs the server process in place         |

## Requirements

- Outbound HTTPS to `github.com`, `api.github.com`, `raw.githubusercontent.com`, `codeload.github.com`
- No additional Python dependencies — stdlib only (`urllib`, `zipfile`)

## Limitations

- GitHub's unauthenticated rate limit is 60 req/hour per IP. Each "Check for updates" makes one API call per installed plugin — well within the limit for normal use
- Only GitHub repositories are supported (registry and API checks are GitHub-specific)
- The in-place restart relies on `/proc/self/cmdline` being readable (standard on Linux containers)

## License

MIT
