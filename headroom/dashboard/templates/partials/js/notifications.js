function dashboardNotificationsMixin() {
    return {
                warnings: [],
                notifiedBuckets: {},
                // Per-warning dismissal (notifications bell popover): a dismissed
                // key stays hidden until its threshold bucket crosses upward again
                // (see `crossed` handling below), same re-arm semantics the old
                // single inline banner had.
                dismissedWarningKeys: {},
                notifyEnabled: false,

                // Embedded MCP dashboards (lazy-loaded per row, keyed by dashboard name)
                healthErrorReason: '',

                // Floating help tooltip (instant, no native-title delay). Empty
                // until something with data-help is hovered, so it stays hidden.
                async toggleNotifyEnabled(checked) {
                    if (!checked || typeof Notification === 'undefined') {
                        this.notifyEnabled = false;
                        try { localStorage.setItem('headroom-notify-enabled', '0'); } catch (e) {}
                        return;
                    }
                    const perm = await Notification.requestPermission();
                    this.notifyEnabled = perm === 'granted';
                    try { localStorage.setItem('headroom-notify-enabled', this.notifyEnabled ? '1' : '0'); } catch (e) {}
                },
                dismissWarning(key) {
                    this.dismissedWarningKeys[key] = true;
                },
                dismissAllWarnings() {
                    for (const w of this.warnings) this.dismissedWarningKeys[w.key] = true;
                },
                trackWarning(items, crossed, key, pct, textFn) {
                    const bucket = this.bucketFor(pct || 0);
                    if (bucket >= 80 && !this.dismissedWarningKeys[key]) items.push({ key, text: textFn() });
                    const prevBucket = this.notifiedBuckets[key] || 0;
                    if (bucket > prevBucket) {
                        crossed.push({ key, text: textFn() });
                        // Re-arm: a fresh threshold crossing un-dismisses the warning,
                        // same as the old single banner re-showing on any new crossing.
                        delete this.dismissedWarningKeys[key];
                    }
                    this.notifiedBuckets[key] = bucket;
                },
                refreshWarnings() {
                    const items = [];
                    const crossed = [];

                    const sw = this.stats.subscription_window?.latest;
                    if (sw?.five_hour) {
                        this.trackWarning(items, crossed, 'anthropic_5h', sw.five_hour.utilization_pct, () =>
                            `Anthropic 5h window at ${(sw.five_hour.utilization_pct || 0).toFixed(0)}% — resets in ${this.formatResetTime(sw.five_hour.seconds_to_reset)}`);
                    }
                    if (sw?.seven_day) {
                        this.trackWarning(items, crossed, 'anthropic_7d', sw.seven_day.utilization_pct, () =>
                            `Anthropic 7d window at ${(sw.seven_day.utilization_pct || 0).toFixed(0)}% — resets in ${this.formatResetTime(sw.seven_day.seconds_to_reset)}`);
                    }

                    const codex = this.stats.codex_rate_limits;
                    if (codex?.primary) {
                        this.trackWarning(items, crossed, 'codex_primary', codex.primary.used_percent, () =>
                            `Codex primary window at ${(codex.primary.used_percent || 0).toFixed(0)}% — resets in ${this.formatResetTime(codex.primary.seconds_until_reset)}`);
                    }
                    if (codex?.secondary) {
                        this.trackWarning(items, crossed, 'codex_secondary', codex.secondary.used_percent, () =>
                            `Codex secondary window at ${(codex.secondary.used_percent || 0).toFixed(0)}% — resets in ${this.formatResetTime(codex.secondary.seconds_until_reset)}`);
                    }

                    const cats = this.stats.copilot_quota?.latest?.categories;
                    if (cats) {
                        const resetDate = this.stats.copilot_quota.latest.quota_reset_date_utc;
                        const resetLabel = resetDate
                            ? new Date(resetDate).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
                            : 'unknown date';
                        const labels = { chat: 'Copilot Chat', completions: 'Copilot Completions', premium_interactions: 'Copilot Premium' };
                        for (const key of Object.keys(labels)) {
                            const c = cats[key];
                            if (!c || c.unlimited) continue;
                            this.trackWarning(items, crossed, 'copilot_' + key, c.used_percent, () =>
                                `${labels[key]} at ${(c.used_percent || 0).toFixed(0)}% — resets on ${resetLabel}`);
                        }
                    }

                    const cost = this.stats.cost;
                    if (cost?.budget_limit_usd) {
                        this.trackWarning(items, crossed, 'budget', this.budgetPercentUsed, () => {
                            const secs = this.estimateBudgetResetSeconds(cost.budget_period);
                            return `Budget at ${this.budgetPercentUsed.toFixed(0)}% of $${this.formatCurrency(cost.budget_limit_usd)}/${cost.budget_period || 'period'} — resets in ${this.formatResetTime(secs)}`;
                        });
                    }

                    this.warnings = items;
                    if (crossed.length > 0) {
                        if (this.notifyEnabled && typeof Notification !== 'undefined' && Notification.permission === 'granted') {
                            for (const c of crossed) {
                                new Notification('Headroom limit warning', { body: c.text });
                            }
                        }
                    }
                },

                // Load saved poll intervals from /config at startup so the
                // dashboard honors user-set cadences without a page rebuild.
    };
}
