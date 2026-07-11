function dashboard() {
    // Several mixins define `get` accessors (computed/reactive properties).
    // A plain object spread (`{...mixin()}`) would evaluate those getters
    // immediately and freeze them into static values, breaking Alpine's
    // reactivity. Merge property descriptors instead so accessors stay lazy.
    const mixins = [
        dashboardCore(),
        dashboardHelpMixin(),
        dashboardPanesMixin(),
        dashboardSortingMixin(),
        dashboardBudgetMixin(),
        dashboardActiveAgentsMixin(),
        dashboardCcrMixin(),
        dashboardDiagnosticsMixin(),
        dashboardMcpMixin(),
        dashboardNotificationsMixin(),
        dashboardSettingsMixin(),
        dashboardRequestsMixin(),
        dashboardWindowsMixin(),
        dashboardWasteTrendsMixin(),
        dashboardTablesMixin(),
        dashboardHistoryMixin(),
    ];
    const merged = {};
    for (const mixin of mixins) {
        Object.defineProperties(merged, Object.getOwnPropertyDescriptors(mixin));
    }
    return merged;
}
