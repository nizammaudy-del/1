function dashboardCore() {
    return {
                stats: {},
                healthy: true,
                version: '0.3.0',
                lastUpdate: 'never',
                viewMode: 'session',
                pollInterval: null,
                statsPollMs: 5000,
                historyPollMs: 30000,
                feedPollMs: 5000,
                lastHistoryFetchMs: 0,
                lastFeedFetchMs: 0,
                uptimeSeconds: 0,
                uptimeText: '',
                restartBannerVisible: false,
                restartBannerDismissed: false,
                _lastSeenUptimeSeconds: null,
                _lastPollAtMs: null,
                uptimeBaseMs: 0,

                // --- Toast: brief non-blocking confirmation for one-off actions ---
                toast: null,
                _toastTimer: null,
                showToast(message, { error = false, durationMs = 3500 } = {}) {
                    clearTimeout(this._toastTimer);
                    this.toast = { message, error };
                    this._toastTimer = setTimeout(() => { this.toast = null; }, durationMs);
                },

                // Collapsible panes: true == collapsed.
                async init() {
                    this.loadUiPrefs();
                    await this.fetchStats();
                    this.fetchMcpDashboards();
                    this.fetchDoctor();
                    this.fetchActiveAgents();
                    this.fetchDiagnostics();
                    await this.loadSettingsIntoPoll();

                    this.pollInterval = setInterval(() => {
                        this.pollDashboard();
                    }, this.statsPollMs);

                    // Live-tick the uptime every second. Derive the value from a base
                    // timestamp instead of incrementing, so it never skips or drifts;
                    // fetchStats resyncs the base on each /health poll.
                    setInterval(() => {
                        if (this.uptimeBaseMs > 0) {
                            this.uptimeSeconds = Math.floor((Date.now() - this.uptimeBaseMs) / 1000);
                            this.uptimeText = this.formatUptime(this.uptimeSeconds);
                        }
                    }, 1000);

                    // Instant floating help: delegate hover to any element carrying
                    // data-help. No native title (which has a ~1s delay). The
                    // float only renders while helpBody is non-empty, so it
                    // simply disappears once nothing is hovered.
                    document.addEventListener('mouseover', (e) => {
                        const el = e.target.closest && e.target.closest('[data-help]');
                        if (!el) return;
                        this.helpTitle = el.getAttribute('data-help-title') || '';
                        this.helpBody = el.getAttribute('data-help') || '';
                    });
                    document.addEventListener('mouseout', (e) => {
                        const el = e.target.closest && e.target.closest('[data-help]');
                        if (!el) return;
                        if (el.contains(e.relatedTarget)) return;
                        this.helpTitle = '';
                        this.helpBody = '';
                    });

                    // Live Feed cards are rendered as raw HTML strings (virtual
                    // scroll perf), so Alpine never binds @click on them directly
                    // -- delegate the same way data-help does above.
                    document.addEventListener('click', (e) => {
                        const card = e.target.closest && e.target.closest('.transformation-card');
                        if (!card) return;
                        const idx = parseInt(card.getAttribute('data-idx'), 10);
                        if (!Number.isNaN(idx)) this.openTransformDetail(idx);
                    });

                    // Keyboard shortcuts
                    document.addEventListener('keydown', (e) => {
                        if (e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
                        if (e.key === 'r' || e.key === 'R') {
                            this.pollDashboard(true);
                        }
                    });
                },

                // --- UI preferences (client-local; localStorage only) ---
                loadUiPrefs() {
                    try {
                        const raw = localStorage.getItem('headroom-ui-prefs');
                        if (raw) {
                            const p = JSON.parse(raw);
                            if (p.panes) this.panes = Object.assign(this.panes, p.panes);
                            if (p.paneHidden) this.paneHidden = p.paneHidden;
                            if (p.tabsHidden) this.tabsHidden = p.tabsHidden;
                            if (typeof p.showStatusCluster === 'boolean') this.showStatusCluster = p.showStatusCluster;
                            if (typeof p.showMcpButton === 'boolean') this.showMcpButton = p.showMcpButton;
                        }
                        this.helpCollapsed = localStorage.getItem('headroom-help-collapsed') === '1';
                        this.notifyEnabled = localStorage.getItem('headroom-notify-enabled') === '1'
                            && typeof Notification !== 'undefined' && Notification.permission === 'granted';
                    } catch (e) { /* keep defaults */ }
                },
                saveUiPrefs() {
                    try {
                        localStorage.setItem('headroom-ui-prefs', JSON.stringify({
                            panes: this.panes,
                            paneHidden: this.paneHidden,
                            tabsHidden: this.tabsHidden,
                            showStatusCluster: this.showStatusCluster,
                            showMcpButton: this.showMcpButton,
                        }));
                    } catch (e) { /* ignore */ }
                },
                dismissRestartBanner() {
                    this.restartBannerVisible = false;
                    this.restartBannerDismissed = true;
                },
                async pollDashboard(force = false) {
                    if (!force && document.hidden) return;
                    // Skip if a previous tick is still in flight, so a slow poll
                    // can't be overtaken by a later one and then overwrite it with
                    // stale /stats data.
                    if (this._polling) return;
                    this._polling = true;
                    try {
                        await this.fetchStats();
                        await this.fetchActiveAgents();
                        // Diagnostics pane only renders in session view; skip its
                        // filesystem-backed fetch while on other tabs.
                        if (this.viewMode === 'session') this.fetchDiagnostics();

                        const now = Date.now();
                        if (this.viewMode === 'history' && (force || now - this.lastHistoryFetchMs >= this.historyPollMs)) {
                            await this.fetchHistoryStats();
                        }
                        if (this.feedOpen && (force || now - this.lastFeedFetchMs >= this.feedPollMs)) {
                            await this.fetchTransformations();
                        }
                        if (this.viewMode === 'mcp') {
                            await this.fetchMcpUsage();
                        }
                    } finally {
                        this._polling = false;
                    }
                },

                async setViewMode(mode) {
                    this.viewMode = mode;
                    if (mode === 'history') {
                        await this.fetchHistoryStats();
                    }
                    if (mode === 'mcp') {
                        await this.fetchMcpUsage();
                        await this.fetchCcrFeedback();
                    }
                },

                formatUptime(total) {
                    total = Math.floor(total);
                    const d = Math.floor(total / 86400);
                    const h = Math.floor((total % 86400) / 3600);
                    const m = Math.floor((total % 3600) / 60);
                    const s = total % 60;
                    if (d > 0) return `${d}d ${h}h ${m}m`;
                    if (h > 0) return `${h}h ${m}m`;
                    if (m > 0) return `${m}m ${s}s`;
                    return `${s}s`;
                },

                async fetchStats() {
                    try {
                        const [statsRes, healthRes] = await Promise.all([
                            fetch('/stats?cached=1&recent_limit=' + this.recentRequestLimit),
                            fetch('/health')
                        ]);

                        if (!statsRes.ok || !healthRes.ok) {
                            throw new Error('HTTP ' + (!statsRes.ok ? statsRes.status : healthRes.status) + ' from proxy');
                        }

                        this.stats = await statsRes.json();
                        const health = await healthRes.json();
                        this.healthy = health.status === 'healthy';
                        this.healthErrorReason = this.healthy
                            ? ''
                            : ('Proxy reported status "' + (health.status || 'unknown') + '"' + (health.detail ? ': ' + health.detail : '.'));
                        this.version = health.version || '0.3.0';
                        this.log_full_messages = this.stats.log_full_messages || false;

                        if (typeof health.uptime_seconds === 'number') {
                            const now = Date.now();
                            // Restart heuristic: uptime should only ever grow between polls.
                            // If it dropped, or is lower than the wall-clock time elapsed
                            // since the last poll would allow, the proxy process restarted
                            // and in-memory session stats reset to 0.
                            if (
                                this._lastSeenUptimeSeconds !== null &&
                                this._lastPollAtMs !== null
                            ) {
                                const elapsedSeconds = (now - this._lastPollAtMs) / 1000;
                                const expectedMinUptime = this._lastSeenUptimeSeconds + elapsedSeconds - 5;
                                if (health.uptime_seconds < this._lastSeenUptimeSeconds || health.uptime_seconds < expectedMinUptime) {
                                    // A fresh restart re-arms the banner even if a
                                    // previous one was dismissed — otherwise dismissing
                                    // once would suppress every future restart notice.
                                    this.restartBannerVisible = true;
                                    this.restartBannerDismissed = false;
                                }
                            }
                            this._lastSeenUptimeSeconds = health.uptime_seconds;
                            this._lastPollAtMs = now;

                            this.uptimeSeconds = health.uptime_seconds;
                            this.uptimeBaseMs = Date.now() - health.uptime_seconds * 1000;
                            this.uptimeText = this.formatUptime(this.uptimeSeconds);
                        }

                        // Update history for sparklines
                        this.requestHistory.push(this.stats.requests?.total || 0);
                        this.savingsHistory.push(this.stats.tokens?.saved || 0);
                        this.overheadHistory.push(this.stats.overhead?.average_ms || 0);

                        // Keep last 30 points
                        if (this.requestHistory.length > 30) this.requestHistory.shift();
                        if (this.savingsHistory.length > 30) this.savingsHistory.shift();
                        if (this.overheadHistory.length > 30) this.overheadHistory.shift();

                        this.lastUpdate = new Date().toLocaleTimeString();
                        this.refreshWarnings();
                    } catch (e) {
                        console.error('Failed to fetch stats:', e);
                        this.healthy = false;
                        // Distinguish "proxy unreachable" (TypeError from fetch) from an
                        // HTTP-level error, so the help sidebar can explain WHY.
                        if (e instanceof TypeError) {
                            this.healthErrorReason = 'Cannot reach the proxy at this origin — the Headroom process may be stopped or the network dropped (' + e.message + ').';
                        } else {
                            this.healthErrorReason = e.message || String(e);
                        }
                    }
                },

                escapeHtml(str) {
                    const div = document.createElement('div');
                    div.textContent = str;
                    return div.innerHTML;
                },

                // --- Formatting ---

                formatNumber(n) {
                    if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
                    if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
                    return n.toString();
                },

                formatNum(n, digits = 1) {
                    if (n === null || n === undefined || isNaN(n)) return '—';
                    return new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: digits }).format(Number(n));
                },

                // --- Sortable Tables ---
                formatCurrency(n) {
                    if (n < 0) return '-' + this.formatCurrency(-n);
                    if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
                    if (n >= 1) return n.toFixed(2);
                    if (n >= 0.01) return n.toFixed(3);
                    if (n > 0) return n.toFixed(4);
                    return '0.00';
                },

                formatResetTime(seconds) {
                    if (seconds == null || seconds <= 0) return 'now';
                    const h = Math.floor(seconds / 3600);
                    const m = Math.floor((seconds % 3600) / 60);
                    if (h > 0) return h + 'h ' + m + 'm';
                    if (m > 0) return m + 'm ' + Math.floor(seconds % 60) + 's';
                    return Math.floor(seconds) + 's';
                },

                formatTime(ts) {
                    if (!ts) return '-';
                    const d = new Date(ts);
                    const now = new Date();
                    const diff = (now - d) / 1000;
                    if (diff < 60) return Math.floor(diff) + 's ago';
                    if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
                    return d.toLocaleTimeString();
                },

                formatDate(ts) {
                    if (!ts) return '-';
                    return new Date(ts).toLocaleDateString(undefined, {
                        month: 'short',
                        day: 'numeric',
                    });
                },

                formatMonthlyReset(dateStr) {
                    if (!dateStr) return '-';
                    const d = new Date(dateStr);
                    const days = Math.ceil((d - new Date()) / 86400000);
                    if (days <= 0) return 'today';
                    if (days === 1) return 'tomorrow';
                    return 'in ' + days + ' days (' + d.toLocaleDateString(undefined, {month: 'short', day: 'numeric'}) + ')';
                },

                formatMonth(ts) {
                    if (!ts) return '-';
                    return new Date(ts).toLocaleDateString(undefined, {
                        month: 'short',
                        year: 'numeric',
                    });
                },

                formatDateTime(ts) {
                    if (!ts) return '-';
                    return new Date(ts).toLocaleString(undefined, {
                        month: 'short',
                        day: 'numeric',
                        hour: '2-digit',
                        minute: '2-digit',
                    });
                },

                truncateModel(model) {
                    if (!model) return '-';
                    return model.replace(/^(anthropic\.|openai\.|bedrock\/)/, '')
                               .replace(/-\d{8}$/, '')
                               .substring(0, 20);
                },

                // --- Agent Usage ---

                getSparkline(data) {
                    if (!data || data.length < 2) return '';
                    const min = Math.min(...data);
                    const max = Math.max(...data);
                    const range = max - min || 1;

                    const points = data.map((v, i) => {
                        const x = (i / (data.length - 1)) * 100;
                        const y = 32 - ((v - min) / range) * 28;
                        return `${x},${y}`;
                    });

                    return 'M' + points.join(' L');
                },

                getSparklineArea(data) {
                    if (!data || data.length < 2) return '';
                    const line = this.getSparkline(data);
                    if (!line) return '';
                    return line + ` L100,32 L0,32 Z`;
                },

    };
}
