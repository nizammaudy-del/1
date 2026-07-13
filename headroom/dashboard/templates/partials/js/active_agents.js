function dashboardActiveAgentsMixin() {
    return {
                activeAgents: { agents: [] },
                async fetchActiveAgents() {
                    try {
                        const res = await fetch('/stats/active_agents?window_seconds=60');
                        if (res.ok) {
                            this.activeAgents = await res.json();
                        }
                    } catch (e) {
                        console.error('Failed to fetch active agents:', e);
                    }
                },

                agentSavedWidth(agent) {
                    const before = agent.before_tokens || 0;
                    if (before <= 0) return 0;
                    return Math.min(100, Math.max(0, (agent.tokens_saved || 0) / before * 100)).toFixed(1);
                },

                agentAfterWidth(agent) {
                    const before = agent.before_tokens || 0;
                    if (before <= 0) return 0;
                    return Math.min(100, Math.max(0, (agent.after_tokens || 0) / before * 100)).toFixed(1);
                },

                agentDotClass(agent) {
                    const colors = {
                        'claude-code': 'bg-orange-400',
                        claude: 'bg-orange-400',
                        codex: 'bg-emerald-400',
                        cursor: 'bg-cyan-400',
                        copilot: 'bg-violet-400',
                        openai: 'bg-sky-400',
                        anthropic: 'bg-orange-400',
                        gemini: 'bg-rose-400',
                        aider: 'bg-amber-400',
                        unknown: 'bg-gray-500',
                    };
                    return colors[agent] || 'bg-gray-400';
                },

                // --- Historical View ---

                get agentRows() {
                    return this.stats.agent_usage?.agents || [];
                },

                get agentCoverageLabel() {
                    const coverage = this.stats.agent_usage?.coverage || {};
                    if (coverage.mode === 'request_logs') {
                        return this.formatNumber(coverage.logged_requests || 0) + ' logged requests';
                    }
                    return 'aggregate fallback';
                },

    };
}
