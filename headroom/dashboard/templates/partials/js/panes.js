function dashboardPanesMixin() {
    return {
                panes: { diagnostics: true },
                // Client-local visibility prefs (panes/cards, tabs, header clusters)
                paneHidden: {},
                // Sortable-table state: { [tableId]: { field, dir } }
                togglePane(key) {
                    this.panes[key] = !this.panes[key];
                    this.saveUiPrefs();
                },
                paneVisible(key) { return !this.paneHidden[key]; },
                setPaneVisible(key, visible) {
                    this.paneHidden[key] = !visible;
                    this.saveUiPrefs();
                },
                tabsHidden: {},
                tabVisible(key) { return !this.tabsHidden[key]; },
                setTabVisible(key, visible) {
                    this.tabsHidden[key] = !visible;
                    // Never leave the user on a hidden tab.
                    if (!visible && this.viewMode === key) {
                        const order = ['session', 'history', 'mcp'];
                        const next = order.find(m => !this.tabsHidden[m]);
                        if (next) this.setViewMode(next);
                    }
                    this.saveUiPrefs();
                },
                showStatusCluster: true,
                showMcpButton: true,

                // Methodology modal ("how is this calculated?")
    };
}
