function dashboardMcpMixin() {
    return {
                mcpDashboards: [],
                mcpUsage: { servers: [], note: '', log_full_messages: false },
                mcpDashboardExpanded: {},
                mcpDashboardStatus: {},
                mcpDashboardSummary: {},
                mcpDashboardTimers: {},
                mcpExpandedServer: null,

                get mcpServerRows() {
                    const rows = (this.mcpUsage.servers || []).map(s => ({ server: s.server, calls: s.calls, tools: s.tools || [] }));
                    return this.sortRows(rows, 'mcp', 'calls', 'desc');
                },
                toggleMcpServer(name) {
                    this.mcpExpandedServer = this.mcpExpandedServer === name ? null : name;
                },

                // Health error detail (populated in fetchStats catch)
                toggleMcpDashboard(dash) {
                    const name = dash.name;
                    this.mcpDashboardExpanded[name] = !this.mcpDashboardExpanded[name];
                    if (!this.mcpDashboardExpanded[name]) {
                        // Collapsing: cancel any pending fallback timer so it
                        // doesn't fire against a now-hidden row.
                        clearTimeout(this.mcpDashboardTimers[name]);
                        return;
                    }
                    if (this.mcpDashboardStatus[name]) return;
                    if (name !== 'serena') return; // no iframe attempt for unknown MCPs, link-only
                    this.mcpDashboardStatus[name] = 'loading';
                    this.mcpDashboardTimers[name] = setTimeout(() => {
                        if (this.mcpDashboardStatus[name] === 'loading') this.loadMcpFallback(name);
                    }, 2000);
                },
                onMcpIframeLoad(name) {
                    clearTimeout(this.mcpDashboardTimers[name]);
                    if (this.mcpDashboardStatus[name] === 'loading') this.mcpDashboardStatus[name] = 'loaded';
                },
                onMcpIframeError(name) {
                    clearTimeout(this.mcpDashboardTimers[name]);
                    this.loadMcpFallback(name);
                },
                async loadMcpFallback(name) {
                    this.mcpDashboardStatus[name] = 'fallback';
                    if (name !== 'serena') return;
                    try {
                        const res = await fetch('/mcp/dashboards/serena/summary');
                        if (res.ok) this.mcpDashboardSummary[name] = await res.json();
                    } catch (e) {
                        console.error('Failed to fetch Serena dashboard summary:', e);
                    }
                },

                // --- Near-limit warnings (edge-triggered; recomputed each poll cycle) ---
                async fetchMcpDashboards() {
                    try {
                        const res = await fetch('/mcp/dashboards');
                        if (res.ok) {
                            const data = await res.json();
                            this.mcpDashboards = data.dashboards || [];
                        }
                    } catch (e) {
                        console.error('Failed to fetch MCP dashboards:', e);
                    }
                },

                async fetchMcpUsage() {
                    try {
                        const res = await fetch('/mcp/usage?limit=100');
                        if (res.ok) {
                            this.mcpUsage = await res.json();
                        }
                    } catch (e) {
                        console.error('Failed to fetch MCP usage:', e);
                    }
                },

                async copyMetricsUrl() {
                    const url = window.location.origin + '/metrics';
                    try {
                        await navigator.clipboard.writeText(url);
                    } catch (e) {
                        console.error('Failed to copy /metrics URL:', e);
                    }
                },

    };
}
