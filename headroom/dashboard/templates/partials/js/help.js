function dashboardHelpMixin() {
    return {
                helpTitle: '',
                helpBody: '',
                helpCollapsed: false,

                // Uptime ticking (timestamp-based so it never skips/drifts)
                toggleHelp() {
                    this.helpCollapsed = !this.helpCollapsed;
                    try { localStorage.setItem('headroom-help-collapsed', this.helpCollapsed ? '1' : '0'); } catch (e) {}
                },

                // Opt-in native OS notifications for near-limit warnings. Permission is only
                // ever requested from this user-gesture click handler, never automatically.
    };
}
