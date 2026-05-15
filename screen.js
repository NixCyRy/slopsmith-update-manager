/* Update Manager - install + update plugins and slopsmith core over GitHub */
(function () {
    const RESTART_KEY = 'update_manager:restartPending';
    const API = '/api/plugins/update_manager';

    let plugins = [];        // /api/plugins result (installed list)
    let updates = {};        // API + '/updates' -> { [id]: {local, remote, branch, source, repo} }
    let updateErrors = {};
    let sources = {};        // { [id]: {repo, url, branch, source} | {bundled: true} }
    let excluded = new Set(); // plugin ids the user has opted out of updates for
    let bundledIds = new Set(); // plugin ids that ship with slopsmith core (issue #1)
    let registry = [];       // API + '/registry' -> entries
    let currentTab = 'updates';
    let isDesktop = !!window.slopsmithDesktop?.isDesktop;

    // ── Screen show hook ───────────────────────────────────────────────
    const _origShowScreen = window.showScreen;
    window.showScreen = function (id) {
        _origShowScreen(id);
        if (id === 'plugin-update_manager') updaterOnShow();
    };

    async function updaterOnShow() {
        try {
            const r = await fetch(API + '/config');
            const cfg = await r.json();
            isDesktop = cfg.is_desktop ?? isDesktop;
        } catch (e) { /* fall back to window.slopsmithDesktop */ }
        if (localStorage.getItem(RESTART_KEY) === '1') {
            document.getElementById('updater-restart-banner').classList.remove('hidden');
        }
        if (isDesktop) {
            document.querySelectorAll('[data-docker-only]').forEach(el => el.classList.add('hidden'));
        }
        if (currentTab === 'updates') updaterCheck();
        else updaterLoadRegistry();
    }

    // ── Tabs ───────────────────────────────────────────────────────────
    window.updaterTab = function (tab) {
        currentTab = tab;
        const tUp = document.getElementById('updater-tab-updates');
        const tBr = document.getElementById('updater-tab-browse');
        const pUp = document.getElementById('updater-pane-updates');
        const pBr = document.getElementById('updater-pane-browse');
        const activeCls = 'px-4 py-2 text-sm transition border-b-2 border-accent text-white';
        const idleCls = 'px-4 py-2 text-sm transition border-b-2 border-transparent text-gray-500 hover:text-white';
        if (tab === 'updates') {
            tUp.className = activeCls;
            tBr.className = idleCls;
            pUp.classList.remove('hidden');
            pBr.classList.add('hidden');
            if (!plugins.length) updaterCheck();
        } else {
            tUp.className = idleCls;
            tBr.className = activeCls;
            pUp.classList.add('hidden');
            pBr.classList.remove('hidden');
            if (!registry.length) updaterLoadRegistry();
        }
    };

    // ── Check for updates ──────────────────────────────────────────────
    window.updaterCheck = async function () {
        const btn = document.getElementById('updater-check-btn');
        const loading = document.getElementById('updater-loading');
        const status = document.getElementById('updater-status');
        const table = document.getElementById('updater-table');
        btn.disabled = true;
        btn.textContent = 'Checking...';
        loading.classList.remove('hidden');
        status.textContent = '';
        table.innerHTML = '';
        try {
            const [pRes, uRes] = await Promise.all([
                fetch('/api/plugins'),
                fetch(API + '/updates'),
            ]);
            plugins = await pRes.json();
            const uData = await uRes.json();
            updates = uData.updates || {};
            updateErrors = uData.errors || {};
            sources = uData.sources || {};
            excluded = new Set(uData.excluded || []);
            bundledIds = new Set(uData.bundled || []);
            updaterRenderUpdates();
            document.getElementById('updater-last-checked').textContent =
                'Last checked: ' + new Date().toLocaleTimeString();
            const pluginCount = Object.keys(updates).length;
            status.textContent = pluginCount === 0
                ? 'Everything up to date.'
                : pluginCount + ' update' + (pluginCount > 1 ? 's' : '') + ' available';
            document.getElementById('updater-update-all-btn')
                .classList.toggle('hidden', pluginCount === 0);
        } catch (e) {
            status.textContent = 'Check failed: ' + e.message;
        } finally {
            btn.disabled = false;
            btn.textContent = 'Check for updates';
            loading.classList.add('hidden');
        }
    };

    function updaterRenderUpdates() {
        const c = document.getElementById('updater-table');
        if (!plugins.length) {
            c.innerHTML = '<div class="text-gray-500 text-sm py-8 text-center">No plugins installed.</div>';
            return;
        }

        let html = `<div class="grid grid-cols-[1fr_auto_auto_auto_auto_auto] gap-x-4 text-xs text-gray-500 font-semibold uppercase tracking-wider px-3 py-2 border-b border-gray-800">
            <span>Plugin</span>
            <span class="w-24 text-center hidden sm:block">Local</span>
            <span class="w-24 text-center hidden sm:block">Remote</span>
            <span class="w-28 text-center">Status</span>
            <span class="w-20 text-center" title="Exclude from automatic updates">Exclude</span>
            <span class="w-40 text-center">Action</span>
        </div>`;

        for (const p of plugins) {
            const u = updates[p.id];
            const err = updateErrors[p.id];
            const isExcluded = excluded.has(p.id);
            const isSelf = p.id === 'update_manager';
            const isBundled = bundledIds.has(p.id) || p.bundled === true;

            let statusHtml, actionHtml, rowBg, localStr = '', remoteStr = '';
            if (isBundled) {
                rowBg = 'bg-dark-800/30';
                statusHtml = `<span class="text-sky-400 font-semibold text-xs inline-flex items-center gap-1">
                        <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"/></svg>
                        Bundled
                    </span>
                    <div class="text-[10px] text-gray-600 mt-0.5">Managed by slopsmith core</div>`;
                actionHtml = `<span class="text-gray-700 text-xs" title="Bundled plugins update with slopsmith itself">—</span>`;
            } else if (isExcluded) {
                rowBg = 'bg-dark-900/40 opacity-60';
                statusHtml = `<span class="text-gray-500 font-semibold text-xs">Excluded</span>
                    <div class="text-[10px] text-gray-600 mt-0.5">Updates disabled</div>`;
                actionHtml = `<button data-plugin-id="${esc(p.id)}" onclick="updaterUninstall(this)"
                    class="text-gray-600 hover:text-red-400 text-xs transition">Uninstall</button>`;
            } else if (u) {
                rowBg = 'bg-dark-700/40';
                localStr = u.local;
                remoteStr = u.remote;
                statusHtml = `<span class="text-amber-400 font-semibold text-xs">Update available</span>
                    <div class="text-[10px] text-gray-600 mt-0.5">${esc(u.repo)} · ${esc(u.branch)}</div>`;
                actionHtml = `<button data-plugin-id="${esc(p.id)}" onclick="updaterUpdate(this)"
                    class="bg-accent/20 hover:bg-accent/30 text-accent px-3 py-1 rounded-lg text-xs transition">Update</button>`;
            } else if (err) {
                rowBg = 'bg-dark-800/30';
                const errObj = (typeof err === 'object' && err !== null) ? err : { code: 'error', message: String(err) };
                if (errObj.code === 'branch_not_on_remote') {
                    const br = errObj.branch || 'unknown';
                    statusHtml = `<span class="text-sky-400 font-semibold text-xs" title="Switch to the published branch (usually main), or push '${esc(br)}' to origin">Branch not published</span>
                        <div class="text-[10px] text-gray-600 mt-0.5">Local branch <code class="text-gray-400">${esc(br)}</code> not on remote</div>`;
                } else {
                    statusHtml = `<span class="text-red-400 text-xs" title="${esc(errObj.message || 'Check failed')}">Check failed</span>`;
                }
                actionHtml = `<span class="text-gray-600 text-xs">—</span>`;
            } else if (isSelf) {
                rowBg = 'bg-dark-800/30';
                statusHtml = `<span class="text-green-400 font-semibold text-xs">Up to date</span>`;
                actionHtml = `<span class="text-gray-600 text-xs">—</span>`;
            } else {
                rowBg = 'bg-dark-800/30';
                statusHtml = `<span class="text-green-400 font-semibold text-xs">Up to date</span>`;
                actionHtml = `<button data-plugin-id="${esc(p.id)}" onclick="updaterUninstall(this)"
                    class="text-gray-600 hover:text-red-400 text-xs transition">Uninstall</button>`;
            }

            const exclCheckbox = (isSelf || isBundled)
                ? '<span class="text-gray-700 text-xs">—</span>'
                : `<label class="inline-flex items-center justify-center cursor-pointer" title="Exclude this plugin from update checks and bulk updates">
                    <input type="checkbox" data-plugin-id="${esc(p.id)}" onchange="updaterToggleExclude(this)"
                        ${isExcluded ? 'checked' : ''}
                        class="accent-amber-500 w-3.5 h-3.5 rounded">
                </label>`;

            const src = sources[p.id];
            const nameHtml = (src && src.url)
                ? `<a href="${esc(src.url)}" target="_blank" rel="noopener"
                        class="text-sm text-white hover:text-accent hover:underline truncate inline-flex items-center gap-1"
                        title="${esc(src.repo)} · open on GitHub">${esc(p.name)}
                        <svg class="w-3 h-3 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"/></svg>
                    </a>`
                : `<div class="text-sm text-white truncate">${esc(p.name)}</div>`;

            html += `<div class="grid grid-cols-[1fr_auto_auto_auto_auto_auto] gap-x-4 items-center px-3 py-2.5 rounded-lg ${rowBg} transition" data-row-id="${esc(p.id)}">
                <div class="min-w-0">
                    ${nameHtml}
                    <div class="text-xs text-gray-500 truncate">${esc(p.id)}</div>
                </div>
                <span class="w-24 text-center text-xs text-gray-400 font-mono hidden sm:block">${esc(localStr)}</span>
                <span class="w-24 text-center text-xs text-gray-400 font-mono hidden sm:block">${esc(remoteStr)}</span>
                <span class="w-28 text-center">${statusHtml}</span>
                <span class="w-20 text-center">${exclCheckbox}</span>
                <span class="w-40 text-center">${actionHtml}</span>
            </div>`;
        }
        c.innerHTML = html;
    }

    window.updaterToggleExclude = async function (cb) {
        const id = cb.dataset.pluginId;
        const shouldExclude = cb.checked;
        cb.disabled = true;
        try {
            const resp = await fetch(API + '/exclusions', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ plugin_id: id, excluded: shouldExclude }),
            });
            const data = await resp.json();
            if (data.ok) {
                excluded = new Set(data.excluded || []);
                if (shouldExclude) {
                    delete updates[id];
                    delete updateErrors[id];
                }
                updaterRenderUpdates();
                const pluginCount = Object.keys(updates).length;
                document.getElementById('updater-status').textContent = pluginCount === 0
                    ? 'Everything up to date.'
                    : pluginCount + ' update' + (pluginCount > 1 ? 's' : '') + ' available';
                document.getElementById('updater-update-all-btn')
                    .classList.toggle('hidden', pluginCount === 0);
            } else {
                cb.checked = !shouldExclude;
                cb.title = data.error || 'Failed';
            }
        } catch (e) {
            cb.checked = !shouldExclude;
            cb.title = e.message;
        } finally {
            cb.disabled = false;
        }
    };

    // ── Update one ─────────────────────────────────────────────────────
    window.updaterUpdate = async function (btn) {
        return updaterDoUpdate(btn.dataset.pluginId, btn);
    };

    async function updaterDoUpdate(id, btn) {
        btn.disabled = true;
        const orig = btn.textContent;
        btn.textContent = 'Updating...';
        try {
            const resp = await fetch(API + '/update/' + encodeURIComponent(id), { method: 'POST' });
            const data = await resp.json();
            if (data.ok) {
                if (data.pending_restart) {
                    btn.outerHTML = '<span class="text-amber-400 text-xs font-semibold">Restart to apply</span>';
                    localStorage.setItem(RESTART_KEY, '1');
                    document.getElementById('updater-restart-banner').classList.remove('hidden');
                    return true;
                }
                btn.outerHTML = '<span class="text-green-400 text-xs font-semibold">Updated</span>';
                localStorage.setItem(RESTART_KEY, '1');
                document.getElementById('updater-restart-banner').classList.remove('hidden');
                return true;
            }
            btn.disabled = false;
            btn.textContent = 'Failed';
            btn.title = data.error || 'Unknown error';
            btn.className = 'bg-red-900/30 text-red-400 px-3 py-1 rounded-lg text-xs';
            return false;
        } catch (e) {
            btn.disabled = false;
            btn.textContent = orig;
            btn.title = e.message;
            return false;
        }
    }

    window.updaterUpdateAll = async function () {
        const allBtn = document.getElementById('updater-update-all-btn');
        allBtn.disabled = true;
        allBtn.textContent = 'Updating all...';

        for (const id of Object.keys(updates)) {
            if (excluded.has(id)) continue;
            const row = document.querySelector('[data-row-id="' + CSS.escape(id) + '"]');
            const btn = row ? row.querySelector('button[data-plugin-id]') : null;
            if (!btn) continue;
            await updaterDoUpdate(id, btn);
        }
        allBtn.classList.add('hidden');
        allBtn.disabled = false;
        allBtn.textContent = 'Update all';
    };

    // ── Uninstall ──────────────────────────────────────────────────────
    window.updaterUninstall = async function (btn) {
        const id = btn.dataset.pluginId;
        if (!confirm('Uninstall plugin "' + id + '"?\n\nThis removes its directory. Any local edits will be lost.')) return;
        btn.disabled = true;
        btn.textContent = 'Removing...';
        try {
            const resp = await fetch(API + '/uninstall/' + encodeURIComponent(id), { method: 'POST' });
            const data = await resp.json();
            if (data.ok) {
                btn.outerHTML = '<span class="text-green-400 text-xs font-semibold">Removed</span>';
                localStorage.setItem(RESTART_KEY, '1');
                document.getElementById('updater-restart-banner').classList.remove('hidden');
            } else {
                btn.disabled = false;
                btn.textContent = 'Failed';
                btn.title = data.error || '';
            }
        } catch (e) {
            btn.disabled = false;
            btn.textContent = 'Error';
            btn.title = e.message;
        }
    };

    // ── Registry / Browse ──────────────────────────────────────────────
    window.updaterLoadRegistry = async function () {
        const btn = document.getElementById('updater-reload-btn');
        const loading = document.getElementById('updater-browse-loading');
        const status = document.getElementById('updater-browse-status');
        btn.disabled = true;
        btn.textContent = 'Loading...';
        loading.classList.remove('hidden');
        status.textContent = '';
        try {
            const resp = await fetch(API + '/registry');
            const data = await resp.json();
            if (data.error) throw new Error(data.error);
            registry = data.entries || [];
            status.textContent = registry.length + ' plugins in registry';
            updaterRenderBrowse();
        } catch (e) {
            status.textContent = 'Failed: ' + e.message;
        } finally {
            btn.disabled = false;
            btn.textContent = 'Reload registry';
            loading.classList.add('hidden');
        }
    };

    window.updaterRenderBrowse = function () {
        const container = document.getElementById('updater-browse-list');
        const filter = (document.getElementById('updater-browse-filter').value || '').toLowerCase().trim();
        const installedSet = new Set(plugins.map(p => p.id));

        let rows = registry;
        if (filter) {
            rows = rows.filter(r =>
                r.name.toLowerCase().includes(filter) ||
                r.description.toLowerCase().includes(filter) ||
                r.dirname.toLowerCase().includes(filter) ||
                r.repo.toLowerCase().includes(filter));
        }

        if (!rows.length) {
            container.innerHTML = '<div class="text-gray-500 text-sm py-8 text-center">No plugins match.</div>';
            return;
        }

        let html = '';
        for (const r of rows) {
            const installed = r.installed || installedSet.has(r.dirname);
            const overridesBundled = !!r.overrides_bundled;
            let action;
            if (installed && overridesBundled) {
                action = '<span class="text-sky-400 text-xs font-semibold" title="Ships with slopsmith core">Bundled</span>';
            } else if (installed) {
                action = '<span class="text-green-400 text-xs font-semibold">Installed</span>';
            } else {
                action = `<button data-url="${esc(r.url)}" data-dirname="${esc(r.dirname)}"
                    data-overrides-bundled="${overridesBundled ? '1' : ''}" onclick="updaterInstall(this)"
                    class="bg-accent/20 hover:bg-accent/30 text-accent px-3 py-1 rounded-lg text-xs transition">Install</button>`;
            }

            const bundledBadge = overridesBundled
                ? `<span class="text-[10px] text-sky-300 bg-sky-900/30 border border-sky-500/30 rounded px-1.5 py-0.5"
                        title="A bundled copy of this plugin already ships with slopsmith. Installing this would override it.">Overrides bundled</span>`
                : '';

            html += `<div class="flex items-start gap-3 bg-dark-700/40 rounded-lg px-4 py-3">
                <div class="flex-1 min-w-0">
                    <div class="flex items-center gap-2 flex-wrap">
                        <a href="${esc(r.url)}" target="_blank" class="text-sm text-white hover:text-accent truncate">${esc(r.name)}</a>
                        <span class="text-[10px] text-gray-600 font-mono">${esc(r.repo)}</span>
                        ${bundledBadge}
                    </div>
                    <div class="text-xs text-gray-500 mt-0.5">${esc(r.description)}</div>
                    <div class="text-[10px] text-gray-600 mt-1 font-mono">dir: ${esc(r.dirname)}</div>
                </div>
                <div class="shrink-0 self-center">${action}</div>
            </div>`;
        }
        container.innerHTML = html;
    };

    window.updaterInstall = async function (btn) {
        const url = btn.dataset.url;
        const dirname = btn.dataset.dirname;
        if (btn.dataset.overridesBundled === '1') {
            const ok = confirm(
                'A bundled copy of "' + dirname + '" already ships with slopsmith.\n\n' +
                'Installing this version will override the bundled copy. Continue?'
            );
            if (!ok) return;
        }
        btn.disabled = true;
        btn.textContent = 'Installing...';
        try {
            const resp = await fetch(API + '/install', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url, dirname }),
            });
            const data = await resp.json();
            if (data.ok) {
                btn.outerHTML = '<span class="text-green-400 text-xs font-semibold">Installed</span>';
                localStorage.setItem(RESTART_KEY, '1');
                document.getElementById('updater-restart-banner').classList.remove('hidden');
                // Refresh cached plugin list so Updates tab reflects the new install
                try {
                    const pRes = await fetch('/api/plugins');
                    plugins = await pRes.json();
                } catch (e) { /* ignore */ }
            } else {
                btn.disabled = false;
                btn.textContent = 'Failed';
                btn.title = data.error || '';
                btn.className = 'bg-red-900/30 text-red-400 px-3 py-1 rounded-lg text-xs';
            }
        } catch (e) {
            btn.disabled = false;
            btn.textContent = 'Error';
            btn.title = e.message;
        }
    };

    // ── Restart banner ─────────────────────────────────────────────────
    window.updaterCopyCmd = async function () {
        const btn = document.getElementById('updater-copy-btn');
        try {
            await navigator.clipboard.writeText('docker compose restart');
            btn.textContent = 'Copied';
            setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
        } catch (e) {
            btn.textContent = 'Copy failed';
        }
    };

    window.updaterRestart = async function () {
        const btn = document.getElementById('updater-restart-btn');
        const copyBtn = document.getElementById('updater-copy-btn');
        const statusEl = document.getElementById('updater-restart-status');
        const origLabel = btn.textContent;
        btn.disabled = true;
        btn.textContent = 'Restarting...';
        if (copyBtn) copyBtn.disabled = true;
        statusEl.classList.remove('hidden');
        statusEl.className = 'text-xs text-gray-400 mb-2';
        statusEl.textContent = 'Sending restart signal...';

        if (isDesktop) {
            try {
                await fetch(API + '/restart', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ apply_only: true }),
                });
            } catch (e) { /* ignore */ }
            try { await window.slopsmithDesktop.plugins.restart(); } catch (e) { /* ignore */ }
        } else {
            try { await fetch(API + '/restart', { method: 'POST' }); } catch (e) { /* connection drop expected */ }
        }

        statusEl.textContent = 'Waiting for server to come back...';
        const start = Date.now();
        const deadline = start + 30000;
        let back = false;
        // Give uvicorn a moment to tear down and re-exec before polling.
        // Desktop handles its own restart timing so no delay needed there.
        if (!isDesktop) await new Promise(r => setTimeout(r, 1500));
        while (Date.now() < deadline) {
            try {
                const r = await fetch('/api/plugins', { cache: 'no-store' });
                if (r.ok) { back = true; break; }
            } catch (e) { /* still down */ }
            await new Promise(r => setTimeout(r, 1000));
        }

        if (back) {
            localStorage.removeItem(RESTART_KEY);
            document.getElementById('updater-restart-banner').classList.add('hidden');
            const elapsed = Math.round((Date.now() - start) / 100) / 10;
            const s = document.getElementById('updater-status');
            if (s) {
                s.textContent = 'Restarted in ' + elapsed + 's.';
                s.className = 'text-xs text-green-400';
            }
            if (currentTab === 'updates') updaterCheck();
        } else {
            btn.disabled = false;
            btn.textContent = origLabel;
            if (copyBtn) copyBtn.disabled = false;
            statusEl.className = 'text-xs text-red-400 mb-2';
            statusEl.textContent = 'Server did not respond within 30s. Try restarting the app.';
        }
    };

    window.updaterDismissBanner = function () {
        document.getElementById('updater-restart-banner').classList.add('hidden');
        localStorage.removeItem(RESTART_KEY);
    };

    // ── Utility ────────────────────────────────────────────────────────
    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
})();
