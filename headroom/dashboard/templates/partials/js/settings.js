function dashboardSettingsMixin() {
    return {
                showMethodology: false,

                // Settings menu state
                settingsOpen: false,
                settingsSaving: false,
                settingsMessage: '',
                settingsError: false,
                settingsRestartKeys: [],
                settingsForm: { settings: {}, pricingRows: [] },

                async loadSettingsIntoPoll() {
                    try {
                        const res = await fetch('/config');
                        if (!res.ok) return;
                        const cfg = await res.json();
                        const s = cfg.settings || {};
                        if (s.dashboard_stats_poll_ms) this.statsPollMs = s.dashboard_stats_poll_ms;
                        if (s.dashboard_history_poll_ms) this.historyPollMs = s.dashboard_history_poll_ms;
                        if (s.dashboard_feed_poll_ms) this.feedPollMs = s.dashboard_feed_poll_ms;
                    } catch (e) { /* keep defaults */ }
                },

                async openSettings() {
                    this.settingsMessage = '';
                    this.settingsError = false;
                    try {
                        const res = await fetch('/config');
                        const cfg = await res.json();
                        this.settingsRestartKeys = cfg.restart_required_settings || [];
                        const settings = Object.assign({}, cfg.setting_defaults || {}, cfg.settings || {});
                        const pricingRows = Object.entries(cfg.pricing || {}).map(([model, p]) => ({
                            model,
                            input_per_1m: p.input_per_1m ?? null,
                            output_per_1m: p.output_per_1m ?? null,
                            cache_read_per_1m: p.cache_read_per_1m ?? null,
                            cache_write_per_1m: p.cache_write_per_1m ?? null,
                        }));
                        this.settingsForm = { settings, pricingRows };
                        this.settingsOpen = true;
                    } catch (e) {
                        this.settingsError = true;
                        this.settingsMessage = 'Failed to load config: ' + e;
                        this.settingsOpen = true;
                    }
                },

                addPricingRow() {
                    this.settingsForm.pricingRows.push({
                        model: '', input_per_1m: null, output_per_1m: null,
                        cache_read_per_1m: null, cache_write_per_1m: null,
                    });
                },

                async saveSettings() {
                    this.settingsSaving = true;
                    this.settingsMessage = '';
                    this.settingsError = false;

                    // Build pricing map, dropping empty rows/fields.
                    const pricing = {};
                    for (const row of this.settingsForm.pricingRows) {
                        const model = (row.model || '').trim();
                        if (!model) continue;
                        const entry = {};
                        for (const f of ['input_per_1m', 'output_per_1m', 'cache_read_per_1m', 'cache_write_per_1m']) {
                            if (row[f] !== null && row[f] !== '' && row[f] !== undefined) entry[f] = Number(row[f]);
                        }
                        if (Object.keys(entry).length) pricing[model] = entry;
                    }

                    // Normalize settings: blank strings -> null for nullable fields.
                    const s = Object.assign({}, this.settingsForm.settings);
                    for (const k of ['savings_profile', 'target_ratio', 'budget_limit_usd']) {
                        if (s[k] === '' || s[k] === undefined) s[k] = null;
                    }

                    try {
                        const res = await fetch('/config', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ pricing, settings: s }),
                        });
                        const data = await res.json();
                        if (!res.ok) {
                            this.settingsError = true;
                            this.settingsMessage = data.error || 'Save failed';
                            return;
                        }
                        // Apply poll intervals live.
                        if (s.dashboard_stats_poll_ms) this.statsPollMs = s.dashboard_stats_poll_ms;
                        if (s.dashboard_history_poll_ms) this.historyPollMs = s.dashboard_history_poll_ms;
                        if (s.dashboard_feed_poll_ms) this.feedPollMs = s.dashboard_feed_poll_ms;
                        if (this.pollInterval) {
                            clearInterval(this.pollInterval);
                            this.pollInterval = setInterval(() => { this.pollDashboard(); }, this.statsPollMs);
                        }
                        this.settingsError = false;
                        this.settingsMessage = '';
                        this.showToast('Saved. Profile/budget changes need a proxy restart to take effect.');
                        await this.fetchStats();
                    } catch (e) {
                        this.settingsError = true;
                        this.settingsMessage = 'Save failed: ' + e;
                    } finally {
                        this.settingsSaving = false;
                    }
                },

    };
}
