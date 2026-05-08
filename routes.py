"""Update Manager - installs, updates, and restarts slopsmith plugins
and the slopsmith core itself via GitHub zip downloads.

No `git` CLI needed (the slopsmith container ships without it). State for
installed plugins is tracked in a `.slopsmith-installed.json` marker inside
each plugin directory. For plugins that were installed via host-side
`git clone` (the traditional method) the `.git/` directory is read directly
to infer origin and local commit.
"""

import fnmatch
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from fastapi import Request


REGISTRY_URL = "https://raw.githubusercontent.com/byrongamatos/slopsmith/main/README.md"
SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-]{0,63}$")
GH_REPO_RE = re.compile(
    r"^https://github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$"
)
MARKER = ".slopsmith-installed.json"
UA = "slopsmith-update-manager/1.4"
_env_plugins = os.environ.get("SLOPSMITH_PLUGINS_DIR", "").strip()
IS_DESKTOP = bool(_env_plugins)
PLUGINS_DIR = Path(_env_plugins) if _env_plugins else Path(__file__).resolve().parent.parent
CACHE_DIR = Path(os.environ.get("CONFIG_DIR", "/config")) / "update_manager"
EXCL_FILE = CACHE_DIR / "exclusions.json"

CORE_REPO_OWNER = "byrongamatos"
CORE_REPO_NAME = "slopsmith"
CORE_MOUNTED_PATHS = {"server.py", "ug_browser.py", "lib", "static"}
CORE_IGNORED_PATHS = {"plugins", "*.md", "docs", "tests", ".claude"}
CORE_MARKER_FILE = CACHE_DIR / "core.json"
CORE_EXCLUSION_KEY = "__core__"
APP_ROOT = Path("/app")
REBUILD_CMD = "cd slopsmith && git pull && docker compose build web && docker compose up -d"
SELF_UPDATE_STAGING = CACHE_DIR / "self_update"


def _load_exclusions() -> set[str]:
    if not EXCL_FILE.exists():
        return set()
    try:
        data = json.loads(EXCL_FILE.read_text())
        return set(data.get("excluded", []))
    except Exception:
        return set()


def _save_exclusions(excluded: set[str]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    EXCL_FILE.write_text(json.dumps({"excluded": sorted(excluded)}, indent=2))


def _http_get(url: str, accept: str | None = None, timeout: int = 20) -> bytes:
    headers = {"User-Agent": UA}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _http_json(url: str) -> dict:
    return json.loads(_http_get(url, accept="application/vnd.github+json").decode("utf-8"))


def _parse_repo_url(url: str):
    m = GH_REPO_RE.match((url or "").rstrip("/"))
    if not m:
        return None, None
    return m.group(1), m.group(2)


def _parse_registry(md: str) -> list[dict]:
    """Parse the 'Available Plugins' table out of slopsmith's README."""
    entries = []
    in_section = False
    for line in md.splitlines():
        stripped = line.strip()
        if not in_section:
            if stripped.startswith("### Available Plugins"):
                in_section = True
            continue
        # Stop at next heading
        if stripped.startswith("## ") or (stripped.startswith("### ") and "Available Plugins" not in stripped):
            break
        if not stripped.startswith("|"):
            continue
        # Skip header and separator rows
        if re.match(r"^\|[\s\-:|]+\|$", stripped):
            continue
        m = re.match(
            r"\|\s*\[([^\]]+)\]\(([^)]+)\)\s*\|\s*([^|]+?)\s*\|\s*(.+?)\s*\|",
            stripped,
        )
        if not m:
            continue
        name, url, desc, install_cmd = m.groups()
        owner, repo = _parse_repo_url(url)
        if not owner:
            continue
        dm = re.search(r"\.git`?\s+([A-Za-z0-9_\-]+)`?", install_cmd)
        dirname = dm.group(1) if dm else repo
        entries.append({
            "name": name.strip(),
            "url": f"https://github.com/{owner}/{repo}",
            "repo": f"{owner}/{repo}",
            "description": desc.strip(),
            "dirname": dirname.strip(),
        })
    return entries


def _installed_plugin_dirs() -> dict[str, Path]:
    """Map plugin manifest id → directory Path.

    Keyed by the `id` from plugin.json (not the directory name) because
    the slopsmith core exposes plugins to the frontend by manifest id and
    those two can diverge (e.g. dir `tab_view` with id `tabview`).
    """
    out = {}
    if not PLUGINS_DIR.is_dir():
        return out
    for p in sorted(PLUGINS_DIR.iterdir()):
        if not p.is_dir() or p.name.startswith("_"):
            continue
        manifest = p / "plugin.json"
        if not manifest.exists():
            continue
        pid = p.name
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("id"), str) and data["id"]:
                pid = data["id"]
        except Exception:
            pass
        out[pid] = p
    return out


def _is_bundled(plugin_dir: Path) -> bool:
    """Return True if the plugin's manifest declares `bundled: true`.

    Bundled plugins ship in-tree with the slopsmith container image
    (slopsmith#160). They aren't `git clone`-installed and don't carry
    a marker file or `.git/`. Updates and uninstalls are handled by
    slopsmith core, not by this plugin.
    """
    manifest = plugin_dir / "plugin.json"
    if not manifest.exists():
        return False
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return bool(isinstance(data, dict) and data.get("bundled") is True)
    except Exception:
        return False


def _read_marker(plugin_dir: Path) -> dict | None:
    f = plugin_dir / MARKER
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:
        return None


def _write_marker(plugin_dir: Path, owner: str, repo: str, branch: str, sha: str) -> None:
    (plugin_dir / MARKER).write_text(json.dumps({
        "repo": f"{owner}/{repo}",
        "url": f"https://github.com/{owner}/{repo}",
        "branch": branch,
        "sha": sha,
        "installed_at": int(time.time()),
    }, indent=2))


def _read_git_origin(plugin_dir: Path) -> str | None:
    cfg = plugin_dir / ".git" / "config"
    if not cfg.exists():
        return None
    try:
        text = cfg.read_text(errors="ignore")
    except Exception:
        return None
    m = re.search(r"url\s*=\s*(\S+)", text)
    return m.group(1) if m else None


def _read_git_local_sha(plugin_dir: Path) -> tuple[str | None, str | None]:
    """Return (sha, branch) for a `.git`-managed plugin dir, without git CLI."""
    head = plugin_dir / ".git" / "HEAD"
    if not head.exists():
        return None, None
    try:
        h = head.read_text().strip()
    except Exception:
        return None, None
    if h.startswith("ref: "):
        ref_name = h[5:].strip()
        branch = ref_name.split("/")[-1] if "/" in ref_name else ref_name
        ref_path = plugin_dir / ".git" / ref_name
        if ref_path.exists():
            try:
                return ref_path.read_text().strip(), branch
            except Exception:
                pass
        packed = plugin_dir / ".git" / "packed-refs"
        if packed.exists():
            try:
                for ln in packed.read_text().splitlines():
                    if ln.endswith(" " + ref_name):
                        return ln.split()[0], branch
            except Exception:
                pass
        return None, branch
    return h, None


def _default_branch(owner: str, repo: str) -> str:
    try:
        data = _http_json(f"https://api.github.com/repos/{owner}/{repo}")
        return data.get("default_branch") or "main"
    except Exception:
        return "main"


def _latest_sha(owner: str, repo: str, branch: str) -> str | None:
    data = _http_json(f"https://api.github.com/repos/{owner}/{repo}/commits/{branch}")
    return data.get("sha")


def _read_local_version(plugin_dir: Path) -> str | None:
    """Return the `version` field from a plugin's local plugin.json, or None."""
    manifest = plugin_dir / "plugin.json"
    if not manifest.exists():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        v = data.get("version") if isinstance(data, dict) else None
        return v if isinstance(v, str) and v else None
    except Exception:
        return None


def _fetch_remote_version(owner: str, repo: str, ref: str) -> str | None:
    """Fetch plugin.json at `ref` (branch / tag / sha) and return its version, or None.

    Uses raw.githubusercontent.com which serves blobs at any ref name and
    is rate-limited far less aggressively than api.github.com.
    """
    if not ref:
        return None
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/plugin.json"
    try:
        body = _http_get(url, timeout=15).decode("utf-8")
        data = json.loads(body)
        v = data.get("version") if isinstance(data, dict) else None
        return v if isinstance(v, str) and v else None
    except Exception:
        return None


def _list_versions(owner: str, repo: str, branch: str, limit: int = 30) -> list[dict]:
    """Return available versions for a plugin, ordered newest-first.

    Each entry: {ref, sha, version, source} where:
      - `ref` is the GitHub-resolvable identifier (refs/tags/X, sha, or branch)
      - `source` is "tag" (from git tags) or "history" (from plugin.json bump
        commits on the default branch).

    Tags are listed first; then plugin.json version-bump commits on the
    branch supplement them so plugins that don't tag their releases still
    expose their semver history. Stops after `limit` total entries to
    keep within GitHub's unauthenticated rate limit.
    """
    versions: list[dict] = []
    seen_versions: set[str] = set()

    # 1. Git tags. One API call. We fetch each tag's plugin.json version
    #    too so the UI can show "v1.5.0 (1.5.0)" for tags whose names
    #    don't match the manifest version exactly.
    try:
        tags = _http_json(
            f"https://api.github.com/repos/{owner}/{repo}/tags?per_page={limit}"
        )
        for t in tags:
            sha = ((t or {}).get("commit") or {}).get("sha")
            tag_name = (t or {}).get("name")
            if not (sha and tag_name):
                continue
            v = _fetch_remote_version(owner, repo, f"refs/tags/{tag_name}")
            entry = {
                "ref": f"refs/tags/{tag_name}",
                "sha": sha,
                "version": v,
                "source": "tag",
                "label": tag_name,
            }
            versions.append(entry)
            if v:
                seen_versions.add(v)
            if len(versions) >= limit:
                return versions
    except Exception:
        pass

    # 2. Scan plugin.json bump commits on the branch. One API call lists
    #    all commits that touched plugin.json; for each, one extra fetch
    #    reads the version. Cap to keep the worst-case unauthenticated
    #    rate-limit cost bounded.
    remaining = max(0, limit - len(versions))
    if remaining <= 0:
        return versions
    try:
        commits = _http_json(
            f"https://api.github.com/repos/{owner}/{repo}/commits"
            f"?path=plugin.json&sha={branch}&per_page={remaining}"
        )
        for c in commits:
            sha = (c or {}).get("sha")
            if not sha:
                continue
            v = _fetch_remote_version(owner, repo, sha)
            if not v or v in seen_versions:
                continue
            seen_versions.add(v)
            versions.append({
                "ref": sha,
                "sha": sha,
                "version": v,
                "source": "history",
                "label": v,
            })
            if len(versions) >= limit:
                break
    except Exception:
        pass

    return versions


def _download_and_replace(owner: str, repo: str, ref: str, target: Path, preserve_git: bool) -> None:
    """Download repo zip at `ref` and atomically replace `target` dir contents.

    `ref` is anything codeload.github.com accepts after the trailing slash
    of `/zip/`: a branch path (`refs/heads/main`), a tag path
    (`refs/tags/v1.0.0`), or a commit sha. The legacy callers passed
    branch names without the `refs/heads/` prefix; preserve that
    behaviour by prepending it when the caller didn't.
    """
    if not (ref.startswith("refs/") or len(ref) >= 7 and all(c in "0123456789abcdef" for c in ref.lower())):
        # Bare branch name → expand to full ref so codeload finds it.
        ref = f"refs/heads/{ref}"
    url = f"https://codeload.github.com/{owner}/{repo}/zip/{ref}"
    data = _http_get(url, timeout=120)
    zf = zipfile.ZipFile(io.BytesIO(data))
    members = [m for m in zf.namelist() if not m.endswith("/")]
    if not members:
        raise RuntimeError("Empty archive")
    prefix = members[0].split("/")[0] + "/"

    staging = Path(tempfile.mkdtemp(prefix="slopsmith-plugin-", dir=str(target.parent)))
    try:
        for m in members:
            if not m.startswith(prefix):
                continue
            rel = m[len(prefix):]
            if not rel or ".." in Path(rel).parts:
                continue
            out = staging / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(zf.read(m))

        if preserve_git and (target / ".git").exists():
            shutil.move(str(target / ".git"), str(staging / ".git"))

        backup = target.with_name(target.name + ".bak")
        if target.exists():
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            shutil.move(str(target), str(backup))
        shutil.move(str(staging), str(target))
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _resolve_source(plugin_dir: Path) -> dict | None:
    """Return {'owner','repo','branch','local_sha','local_version','source'} or None."""
    local_version = _read_local_version(plugin_dir)
    marker = _read_marker(plugin_dir)
    if marker:
        owner, repo = _parse_repo_url(marker.get("url", ""))
        if owner:
            return {
                "owner": owner, "repo": repo,
                "branch": marker.get("branch"),
                "local_sha": marker.get("sha"),
                "local_version": local_version,
                "source": "zip",
            }
    origin = _read_git_origin(plugin_dir)
    if origin:
        owner, repo = _parse_repo_url(origin)
        if owner:
            local_sha, branch = _read_git_local_sha(plugin_dir)
            return {
                "owner": owner, "repo": repo,
                "branch": branch,
                "local_sha": local_sha,
                "local_version": local_version,
                "source": "git",
            }
    # Plugin has neither a marker nor a .git/ — typical for the bundled
    # Electron desktop install where each plugin is a plain directory.
    # Try two fallbacks before giving up:
    #
    #   a) plugin.json's optional `url` / `repository` field.
    #   b) The Available Plugins registry in slopsmith's README, keyed by
    #      directory name. This is what makes the bundled Electron case
    #      work — the bundled install ships plain directories, but as
    #      long as the directory name matches a registry entry we can
    #      still resolve the upstream repo and detect updates.
    manifest = plugin_dir / "plugin.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            url = data.get("url") or data.get("repository") or ""
            if isinstance(url, str):
                owner, repo = _parse_repo_url(url)
                if owner:
                    return {
                        "owner": owner, "repo": repo,
                        "branch": None,
                        "local_sha": None,
                        "local_version": local_version,
                        "source": "manifest",
                    }
        except Exception:
            pass
    try:
        for entry in _registry_cached():
            if entry.get("dirname") == plugin_dir.name:
                owner, repo = _parse_repo_url(entry.get("url") or "")
                if owner:
                    return {
                        "owner": owner, "repo": repo,
                        "branch": None,
                        "local_sha": None,
                        "local_version": local_version,
                        "source": "registry",
                    }
    except Exception:
        pass
    return None


# Module-level registry cache. _registry_cached() refreshes the parsed
# README at most every REGISTRY_TTL_S seconds, so the bundled-install
# fallback in _resolve_source doesn't fetch the README on every request.
_registry_cache: list[dict] = []
_registry_cache_at: float = 0.0
REGISTRY_TTL_S = 600  # 10 minutes


def _registry_cached() -> list[dict]:
    global _registry_cache, _registry_cache_at
    now = time.time()
    if _registry_cache and (now - _registry_cache_at) < REGISTRY_TTL_S:
        return _registry_cache
    try:
        md = _http_get(REGISTRY_URL, timeout=10).decode("utf-8")
        _registry_cache = _parse_registry(md)
        _registry_cache_at = now
    except Exception:
        # Keep stale cache rather than nuke it on transient network errors;
        # the cache will retry on the next call after TTL.
        pass
    return _registry_cache


def _load_core_marker() -> dict | None:
    if not CORE_MARKER_FILE.exists():
        return None
    try:
        return json.loads(CORE_MARKER_FILE.read_text())
    except Exception:
        return None


def _save_core_marker(sha: str, branch: str) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CORE_MARKER_FILE.write_text(json.dumps({
        "repo": f"{CORE_REPO_OWNER}/{CORE_REPO_NAME}",
        "url": f"https://github.com/{CORE_REPO_OWNER}/{CORE_REPO_NAME}",
        "branch": branch,
        "sha": sha,
        "installed_at": int(time.time()),
    }, indent=2))


def _core_changed_files(local_sha: str, remote_sha: str) -> list[dict]:
    """GitHub compare API. Returns [{filename, status}, ...]."""
    url = (
        f"https://api.github.com/repos/{CORE_REPO_OWNER}/{CORE_REPO_NAME}"
        f"/compare/{local_sha}...{remote_sha}"
    )
    data = _http_json(url)
    files = data.get("files") or []
    return [{"filename": f.get("filename", ""), "status": f.get("status", "")} for f in files]


def _is_ignored(path: str) -> bool:
    """Check if path matches any pattern in CORE_IGNORED_PATHS, supporting fnmatch wildcards."""
    top = path.split("/", 1)[0]
    for pattern in CORE_IGNORED_PATHS:
        if fnmatch.fnmatch(top, pattern):
            return True
    return False


def _classify_core_changes(files: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split changed files into (writable_to_mount, blockers).

    Files under CORE_IGNORED_PATHS are omitted from both lists (plugins/
    is managed separately; touching it here would clobber user installs).
    """
    writable, blockers = [], []
    for f in files:
        name = f.get("filename", "")
        if not name:
            continue
        if _is_ignored(name):
            continue
        top = name.split("/", 1)[0]
        if top in CORE_MOUNTED_PATHS:
            writable.append(f)
        else:
            blockers.append(f)
    return writable, blockers


def _download_core_stage(branch: str) -> tuple[Path, str]:
    """Download the core repo zip, extract to a tempdir, return (stage_root, prefix)."""
    url = f"https://codeload.github.com/{CORE_REPO_OWNER}/{CORE_REPO_NAME}/zip/refs/heads/{branch}"
    data = _http_get(url, timeout=180)
    zf = zipfile.ZipFile(io.BytesIO(data))
    members = [m for m in zf.namelist() if not m.endswith("/")]
    if not members:
        raise RuntimeError("Empty core archive")
    prefix = members[0].split("/")[0] + "/"
    stage = Path(tempfile.mkdtemp(prefix="slopsmith-core-"))
    for m in members:
        if not m.startswith(prefix):
            continue
        rel = m[len(prefix):]
        if not rel or ".." in Path(rel).parts:
            continue
        if _is_ignored(rel):
            continue
        top = rel.split("/", 1)[0]
        if top not in CORE_MOUNTED_PATHS:
            continue
        out = stage / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(zf.read(m))
    return stage, prefix


def _overlay_core(stage: Path) -> list[str]:
    """Non-destructive copy from stage into APP_ROOT.

    For every file under the staged mount-whitelist tree, write it into
    the matching location under /app, creating parent dirs as needed.
    Never delete existing files that are absent from the stage — this
    preserves runtime artifacts (e.g. static/audio_*.mp3, __pycache__/).
    Returns the list of relative paths written.
    """
    written: list[str] = []
    for src in stage.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(stage)
        dst = APP_ROOT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))
        written.append(str(rel).replace("\\", "/"))
    return written


def _self_update(owner: str, repo: str, ref: str, sha: str) -> dict:
    """Download new version to staging and mark for pending restart.

    Returns {"ok": true, "pending_restart": true} so the UI can prompt
    the user to restart. On restart, _apply_pending_self_update will
    swap the files before re-execing the server. `ref` accepts the same
    forms as _download_and_replace (branch / refs/tags/X / sha).
    """
    staging = SELF_UPDATE_STAGING
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    if not (ref.startswith("refs/") or len(ref) >= 7 and all(c in "0123456789abcdef" for c in ref.lower())):
        ref = f"refs/heads/{ref}"
    url = f"https://codeload.github.com/{owner}/{repo}/zip/{ref}"
    data = _http_get(url, timeout=120)
    zf = zipfile.ZipFile(io.BytesIO(data))
    members = [m for m in zf.namelist() if not m.endswith("/")]
    if not members:
        return {"error": "Empty archive"}
    prefix = members[0].split("/")[0] + "/"

    for m in members:
        if not m.startswith(prefix):
            continue
        rel = m[len(prefix):]
        if not rel or ".." in Path(rel).parts:
            continue
        out = staging / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(zf.read(m))

    marker = staging / ".self_update_pending"
    marker.write_text(json.dumps({
        "owner": owner, "repo": repo,
        "ref": ref, "sha": sha,
        "staged_at": int(time.time()),
    }, indent=2))
    return {"ok": True, "pending_restart": True, "sha": sha[:7], "ref": ref}


def _apply_pending_self_update(target: Path) -> bool:
    """Swap staged self-update files into target dir. Returns True if swap occurred."""
    marker = SELF_UPDATE_STAGING / ".self_update_pending"
    if not marker.exists():
        return False
    staging = SELF_UPDATE_STAGING
    if not staging.exists():
        return False

    for src in staging.rglob("*"):
        if src.name.startswith("."):
            continue
        if not src.is_file():
            continue
        rel = src.relative_to(staging)
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))
    shutil.rmtree(staging, ignore_errors=True)
    return True


def setup(app, context):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    @app.get("/api/plugins/update_manager/config")
    def get_config():
        return {"is_desktop": IS_DESKTOP}

    @app.get("/api/plugins/update_manager/registry")
    def registry():
        try:
            md = _http_get(REGISTRY_URL, timeout=15).decode("utf-8")
        except Exception as e:
            return {"error": f"Failed to fetch registry: {e}"}
        entries = _parse_registry(md)
        installed = _installed_plugin_dirs()
        installed_dirs = {p.name for p in installed.values()}
        bundled_dirs = {p.name for p in installed.values() if _is_bundled(p)}
        for e in entries:
            e["installed"] = e["dirname"] in installed_dirs
            # Surface dirname-collision with a bundled plugin so the UI
            # can prompt before installing an override. Doesn't catch the
            # case where the registry entry's install command targets a
            # different directory than the bundled one — that requires
            # an `upstream` annotation that isn't standardised yet.
            e["overrides_bundled"] = e["dirname"] in bundled_dirs
        return {"count": len(entries), "entries": entries, "bundled_dirs": sorted(bundled_dirs)}

    @app.post("/api/plugins/update_manager/install")
    async def install(body: dict):
        url = (body.get("url") or "").strip()
        dirname = (body.get("dirname") or "").strip()
        if not SLUG_RE.match(dirname):
            return {"error": "Invalid dirname"}
        owner, repo = _parse_repo_url(url)
        if not owner:
            return {"error": "URL must be a GitHub repo"}
        target = PLUGINS_DIR / dirname
        if target.exists():
            return {"error": f"Plugin directory '{dirname}' already exists"}
        try:
            branch = _default_branch(owner, repo)
            sha = _latest_sha(owner, repo, branch)
            if not sha:
                return {"error": "Could not resolve latest commit"}
            _download_and_replace(owner, repo, branch, target, preserve_git=False)
            _write_marker(target, owner, repo, branch, sha)
            return {
                "ok": True, "dirname": dirname, "branch": branch,
                "sha": sha[:7], "repo": f"{owner}/{repo}",
            }
        except urllib.error.HTTPError as e:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            return {"error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            return {"error": str(e)}

    @app.get("/api/plugins/update_manager/updates")
    def check_updates():
        results = {}
        errors = {}
        sources = {}
        bundled = []
        excluded = _load_exclusions()
        for name, p in _installed_plugin_dirs().items():
            if _is_bundled(p):
                bundled.append(name)
                # Bundled plugins are managed by slopsmith core: no marker,
                # no .git/, no remote to compare against. Surface them in
                # sources[] so the UI can render a "Bundled" badge instead
                # of leaving them invisible — but skip the rest of the
                # update-detection loop.
                sources[name] = {"bundled": True}
                continue
            info = _resolve_source(p)
            if info and info["owner"]:
                sources[name] = {
                    "repo": f"{info['owner']}/{info['repo']}",
                    "url": f"https://github.com/{info['owner']}/{info['repo']}",
                    "branch": info["branch"],
                    "source": info["source"],
                    "local_version": info.get("local_version"),
                }
            if name in excluded:
                continue
            if not info:
                continue
            owner, repo = info["owner"], info["repo"]
            branch = info["branch"] or _default_branch(owner, repo)
            # We always need the remote tip to surface the remote version
            # (and, when SHA tracking is available, to detect commit-level
            # updates). For sources without a local SHA we still fetch it
            # so the UI can show local_version → remote_version even on
            # bundled / plain-dir installs.
            try:
                remote_sha = _latest_sha(owner, repo, branch)
            except urllib.error.HTTPError as e:
                if e.code == 422:
                    errors[name] = {
                        "code": "branch_not_on_remote",
                        "branch": branch,
                        "message": f"Local branch '{branch}' not on {owner}/{repo}",
                    }
                else:
                    errors[name] = {
                        "code": "http",
                        "message": f"HTTP {e.code}: {e.reason}",
                    }
                continue
            except Exception as e:
                errors[name] = {"code": "error", "message": str(e)}
                continue
            remote_version = _fetch_remote_version(owner, repo, branch) if remote_sha else None
            if remote_sha and name in sources:
                sources[name]["remote_version"] = remote_version
            local_version = info.get("local_version")
            local_sha = info.get("local_sha")
            # Two ways to detect "needs update":
            #   (1) SHA-based: local_sha known, differs from remote_sha
            #   (2) Version-fallback: no local_sha (e.g. bundled / plain
            #       dir install) but local_version differs from remote_version
            #
            # Version-fallback is approximate — it can flag a "downgrade"
            # if the local install is somehow ahead of the published
            # remote — but for the bundled-install case it's the only
            # signal we have. The UI surfaces version strings so the user
            # can judge.
            sha_update    = local_sha and remote_sha and remote_sha != local_sha
            ver_update    = (not local_sha) and local_version and remote_version and remote_version != local_version
            if sha_update or ver_update:
                results[name] = {
                    "local": (local_sha or "")[:7],
                    "remote": (remote_sha or "")[:7],
                    "local_version": local_version,
                    "remote_version": remote_version,
                    "branch": branch,
                    "source": info["source"],
                    "repo": f"{owner}/{repo}",
                }
        return {
            "updates": results,
            "errors": errors,
            "excluded": sorted(excluded),
            "sources": sources,
            "bundled": sorted(bundled),
        }

    @app.get("/api/plugins/update_manager/exclusions")
    def get_exclusions():
        return {"excluded": sorted(_load_exclusions())}

    @app.post("/api/plugins/update_manager/exclusions")
    async def set_exclusion(body: dict):
        plugin_id = (body.get("plugin_id") or "").strip()
        exclude = bool(body.get("excluded"))
        if plugin_id != CORE_EXCLUSION_KEY and not SLUG_RE.match(plugin_id):
            return {"error": "Invalid plugin id"}
        excl = _load_exclusions()
        if exclude:
            excl.add(plugin_id)
        else:
            excl.discard(plugin_id)
        _save_exclusions(excl)
        return {"ok": True, "excluded": sorted(excl)}

    @app.get("/api/plugins/update_manager/versions/{plugin_id}")
    def list_plugin_versions(plugin_id: str):
        """List available versions a plugin can be pinned to.

        Surfaces git tags first (one API call), then plugin.json
        version-bump commits scanned from the default branch (one
        listing call + one fetch per commit, capped to keep within the
        unauthenticated GitHub rate limit). The UI uses this for the
        per-plugin "Versions" picker that supports both upgrade-to-
        specific and downgrade-to-specific flows.
        """
        if not SLUG_RE.match(plugin_id):
            return {"error": "Invalid plugin id"}
        target = _installed_plugin_dirs().get(plugin_id)
        if not target or not target.is_dir():
            return {"error": "Plugin not found"}
        if _is_bundled(target):
            return {
                "error": "Bundled with slopsmith core; version pinning not supported.",
                "bundled": True,
            }
        info = _resolve_source(target)
        if not info:
            return {"error": "Plugin source unknown (no marker, no .git/config)"}
        owner, repo = info["owner"], info["repo"]
        branch = info["branch"] or _default_branch(owner, repo)
        try:
            entries = _list_versions(owner, repo, branch)
            return {
                "plugin_id": plugin_id,
                "current_sha": info.get("local_sha"),
                "current_version": info.get("local_version"),
                "branch": branch,
                "repo": f"{owner}/{repo}",
                "versions": entries,
            }
        except urllib.error.HTTPError as e:
            return {"error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    @app.post("/api/plugins/update_manager/update/{plugin_id}")
    async def apply_update(plugin_id: str, request: Request):
        if not SLUG_RE.match(plugin_id):
            return {"error": "Invalid plugin id"}
        if plugin_id in _load_exclusions():
            return {"error": "Plugin is excluded from updates"}
        target = _installed_plugin_dirs().get(plugin_id)
        if not target or not target.is_dir():
            return {"error": "Plugin not found"}
        if _is_bundled(target):
            return {
                "error": "Bundled with slopsmith core; updates ship with the slopsmith app itself.",
                "bundled": True,
            }
        info = _resolve_source(target)
        if not info:
            return {"error": "Plugin source unknown (no marker, no .git/config)"}
        # Optional `ref` in the JSON body lets the caller pin to a
        # specific tag or sha (upgrade or downgrade). Body parsing is
        # tolerant: missing body, empty body, or no `ref` field all
        # mean "update to latest on the tracked branch" — preserving
        # backwards-compatible behaviour for the existing Update button.
        ref = None
        try:
            body = await request.json()
            if isinstance(body, dict):
                rv = body.get("ref")
                if isinstance(rv, str) and rv.strip():
                    ref = rv.strip()
        except Exception:
            pass
        owner, repo = info["owner"], info["repo"]
        branch = info["branch"] or _default_branch(owner, repo)
        try:
            if ref:
                # User-provided ref. Resolve to a sha for the marker
                # write. For tag refs (refs/tags/X) we hit the API to
                # get the tagged commit's sha; for direct shas we
                # accept the value as-is.
                if ref.startswith("refs/tags/"):
                    tag = ref[len("refs/tags/"):]
                    try:
                        tag_data = _http_json(
                            f"https://api.github.com/repos/{owner}/{repo}/git/refs/tags/{tag}"
                        )
                        sha = ((tag_data or {}).get("object") or {}).get("sha")
                    except Exception:
                        sha = None
                    if not sha:
                        return {"error": f"Could not resolve tag {tag!r}"}
                else:
                    sha = ref
                resolved_branch = branch  # remember tracked branch for the marker
            else:
                sha = _latest_sha(owner, repo, branch)
                if not sha:
                    return {"error": "Could not resolve latest commit"}
                ref = branch
                resolved_branch = branch
            if plugin_id == "update_manager":
                return _self_update(owner, repo, ref, sha)
            _download_and_replace(owner, repo, ref, target, preserve_git=(info["source"] == "git"))
            _write_marker(target, owner, repo, resolved_branch, sha)
            return {"ok": True, "sha": sha[:7], "branch": resolved_branch, "ref": ref}
        except urllib.error.HTTPError as e:
            return {"error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    @app.post("/api/plugins/update_manager/restart")
    def restart_server():
        """Restart the uvicorn process in-place via os.execv.

        Replaces the current Python process image with a fresh uvicorn,
        same PID. Parent shell (PID 1) sees no child exit, so the
        container stays alive. No docker-compose restart policy needed.
        """
        if IS_DESKTOP:
            # Desktop restart is handled by the Electron renderer via slopsmithDesktop.plugins.restart().
            return {"ok": True, "desktop": True}
        plugin_target = PLUGINS_DIR / "update_manager"
        if _apply_pending_self_update(plugin_target):
            marker = SELF_UPDATE_STAGING / ".self_update_pending"
            marker.unlink(missing_ok=True)

        # Snapshot the original argv before returning (after exec, this
        # function never runs to completion).
        try:
            with open("/proc/self/cmdline", "rb") as f:
                argv = [x.decode("utf-8", "replace") for x in f.read().split(b"\x00") if x]
        except Exception:
            argv = []
        if not argv:
            argv = [sys.executable, "-m", "uvicorn", "server:app",
                    "--host", "0.0.0.0", "--port", "8000"]

        def _do_exec():
            # Small delay so this HTTP response can flush to the client
            # before we replace the process image.
            time.sleep(0.6)
            try:
                os.execv(argv[0], argv)
            except Exception:
                # Fallback to the documented CMD
                try:
                    os.execv(sys.executable,
                             [sys.executable, "-m", "uvicorn", "server:app",
                              "--host", "0.0.0.0", "--port", "8000"])
                except Exception:
                    # Last resort: terminate so the user knows something went wrong
                    os._exit(1)

        threading.Thread(target=_do_exec, daemon=True).start()
        return {"ok": True, "argv": argv}

    @app.get("/api/plugins/update_manager/core")
    def core_status():
        if IS_DESKTOP:
            return {"is_desktop": True, "hidden": True}
        marker = _load_core_marker()
        excluded = CORE_EXCLUSION_KEY in _load_exclusions()
        try:
            branch = (marker or {}).get("branch") or _default_branch(CORE_REPO_OWNER, CORE_REPO_NAME)
        except Exception:
            branch = "main"
        resp: dict = {
            "repo": f"{CORE_REPO_OWNER}/{CORE_REPO_NAME}",
            "url": f"https://github.com/{CORE_REPO_OWNER}/{CORE_REPO_NAME}",
            "branch": branch,
            "tracking": marker is not None,
            "local_sha": (marker or {}).get("sha"),
            "excluded": excluded,
        }
        try:
            remote_sha = _latest_sha(CORE_REPO_OWNER, CORE_REPO_NAME, branch)
        except Exception as e:
            resp["error"] = f"Failed to check remote: {e}"
            return resp
        resp["remote_sha"] = remote_sha
        if not marker or not marker.get("sha"):
            resp["behind"] = False
            return resp
        local_sha = marker["sha"]
        if local_sha == remote_sha:
            resp["behind"] = False
            resp["changed_files"] = []
            resp["blockers"] = []
            return resp
        try:
            changed = _core_changed_files(local_sha, remote_sha)
        except Exception as e:
            resp["behind"] = True
            resp["changed_files"] = []
            resp["blockers"] = []
            resp["compare_error"] = str(e)
            return resp
        writable, blockers = _classify_core_changes(changed)
        resp["behind"] = True
        resp["changed_files"] = writable + blockers
        resp["blockers"] = blockers
        resp["rebuild_required"] = bool(blockers)
        resp["rebuild_command"] = REBUILD_CMD if blockers else None
        return resp

    @app.post("/api/plugins/update_manager/core/init")
    async def core_init(body: dict):
        if IS_DESKTOP:
            return {"error": "Core updates are not supported in the desktop app."}
        sha = ((body or {}).get("sha") or "").strip() if isinstance(body, dict) else ""
        try:
            branch = _default_branch(CORE_REPO_OWNER, CORE_REPO_NAME)
            if not sha:
                sha = _latest_sha(CORE_REPO_OWNER, CORE_REPO_NAME, branch) or ""
            if not sha:
                return {"error": "Could not resolve SHA"}
            _save_core_marker(sha, branch)
            return {"ok": True, "sha": sha[:7], "branch": branch}
        except urllib.error.HTTPError as e:
            return {"error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    @app.post("/api/plugins/update_manager/core/update")
    def core_update():
        if IS_DESKTOP:
            return {"error": "Core updates are not supported in the desktop app."}
        if CORE_EXCLUSION_KEY in _load_exclusions():
            return {"error": "Core is excluded from updates"}
        marker = _load_core_marker()
        if not marker or not marker.get("sha"):
            return {"error": "Core tracking not initialized. Click 'Initialize tracking' first."}
        branch = marker.get("branch") or _default_branch(CORE_REPO_OWNER, CORE_REPO_NAME)
        try:
            remote_sha = _latest_sha(CORE_REPO_OWNER, CORE_REPO_NAME, branch)
        except urllib.error.HTTPError as e:
            return {"error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            return {"error": str(e)}
        if not remote_sha:
            return {"error": "Could not resolve latest commit"}
        if remote_sha == marker["sha"]:
            return {"ok": True, "sha": remote_sha[:7], "branch": branch, "written_files": [], "unchanged": True}
        try:
            changed = _core_changed_files(marker["sha"], remote_sha)
        except Exception as e:
            return {"error": f"Compare failed: {e}"}
        writable, blockers = _classify_core_changes(changed)
        if blockers:
            return {
                "error": "rebuild_required",
                "message": "Remote commits touch files that aren't bind-mounted. Rebuild the container.",
                "blockers": blockers,
                "command": REBUILD_CMD,
                "branch": branch,
                "remote_sha": remote_sha[:7],
            }
        if not writable:
            _save_core_marker(remote_sha, branch)
            return {"ok": True, "sha": remote_sha[:7], "branch": branch, "written_files": []}
        stage = None
        try:
            stage, _ = _download_core_stage(branch)
            written = _overlay_core(stage)
            _save_core_marker(remote_sha, branch)
            return {
                "ok": True,
                "sha": remote_sha[:7],
                "branch": branch,
                "written_files": written,
            }
        except urllib.error.HTTPError as e:
            return {"error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            return {"error": str(e)}
        finally:
            if stage and stage.exists():
                shutil.rmtree(stage, ignore_errors=True)

    @app.post("/api/plugins/update_manager/uninstall/{plugin_id}")
    def uninstall(plugin_id: str):
        if not SLUG_RE.match(plugin_id):
            return {"error": "Invalid plugin id"}
        if plugin_id == "update_manager":
            return {"error": "Cannot uninstall the update manager itself"}
        target = _installed_plugin_dirs().get(plugin_id)
        if not target or not target.is_dir():
            return {"error": "Plugin not found"}
        if _is_bundled(target):
            return {
                "error": "Bundled with slopsmith core; cannot be uninstalled. Install a standalone copy under a different directory to override.",
                "bundled": True,
            }
        try:
            shutil.rmtree(target)
            return {"ok": True}
        except Exception as e:
            return {"error": str(e)}
