function dashboardCcrMixin() {
    return {
                ccrFeedback: { tool_patterns: {} },
                ccrDetails: {},
                ccrExpandedTool: null,
                async fetchCcrFeedback() {
                    try {
                        const res = await fetch('/v1/feedback');
                        if (res.ok) {
                            const data = await res.json();
                            this.ccrFeedback = data.feedback || { tool_patterns: {} };
                        }
                    } catch (e) {
                        console.error('Failed to fetch CCR feedback:', e);
                    }
                },

                async toggleCcrTool(name) {
                    if (this.ccrExpandedTool === name) {
                        this.ccrExpandedTool = null;
                        return;
                    }
                    this.ccrExpandedTool = name;
                    if (!this.ccrDetails[name]) {
                        try {
                            const res = await fetch('/v1/feedback/' + encodeURIComponent(name));
                            if (res.ok) {
                                this.ccrDetails[name] = await res.json();
                            }
                        } catch (e) {
                            console.error('Failed to fetch CCR tool detail:', e);
                        }
                    }
                },

                get ccrToolRows() {
                    const patterns = this.ccrFeedback.tool_patterns || {};
                    const rows = Object.entries(patterns).map(([name, p]) => ({
                        name,
                        compressions: p.compressions || 0,
                        retrievals: p.retrievals || 0,
                        retrieval_rate: p.retrieval_rate || 0,
                    }));
                    return this.sortRows(rows, 'ccr', 'retrieval_rate', 'desc');
                },

    };
}
