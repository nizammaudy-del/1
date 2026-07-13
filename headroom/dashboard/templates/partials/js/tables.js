function dashboardTablesMixin() {
    return {
                get perModelRows() {
                    const rows = Object.entries(this.stats.cost?.per_model || {}).map(([model, info]) => ({ model, ...info }));
                    return this.sortRows(rows, 'perModel');
                },

                get perProjectRows() {
                    const rows = Object.entries(this.stats.savings?.per_project || {}).map(([project, info]) => ({ project, ...info }));
                    return this.sortRows(rows, 'perProject');
                },

                getProviderPercent(count) {
                    const total = this.stats.requests?.total || 1;
                    return Math.min((count / total) * 100, 100);
                },

    };
}
