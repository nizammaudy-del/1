function dashboardHistoryMixin() {
    return {
                historyGranularity: 'daily',
                historyChartMode: 'tokens',
                historySelectedModel: null,
                requestHistory: [],
                savingsHistory: [],
                overheadHistory: [],
                historyStats: {},
                toggleHistoryModel(model) {
                    this.historySelectedModel = this.historySelectedModel === model ? null : model;
                },

                historyModelColor(index) {
                    const palette = ['#a78bfa', '#34d399', '#fbbf24', '#f87171', '#60a5fa'];
                    return palette[index % palette.length];
                },

                historyModelLine(index) {
                    if (this.historyChartMode !== 'tokens') return '';
                    // Checkpoint view plots raw checkpoints; daily-derived model
                    // lines would not share its x-axis.
                    if (this.historySelectedSeriesKey === 'history') return '';
                    const allSeries = this.historyModelChartSeries;
                    const series = allSeries[index];
                    if (!series || series.values.length < 2) return '';
                    const activeModel = this.historyActiveModel;
                    if (activeModel && series.model !== activeModel) return '';
                    // A single filtered model gets its own scale; the full set
                    // shares one so the lines stay comparable.
                    const scaleSeries = activeModel ? [series] : allSeries;
                    const max = Math.max(...scaleSeries.flatMap(s => s.values), 1);
                    return this.buildTrendPath(series.values, 0, max);
                },

                historyCostLine(kind) {
                    if (this.historyChartMode !== 'cost') return '';
                    const trend = this.historyCostTrend;
                    if (trend.length < 2) return '';
                    const all = trend.flatMap(point => [point.actual, point.expected]);
                    const min = Math.min(...all);
                    const max = Math.max(...all);
                    return this.buildTrendPath(trend.map(point => point[kind]), min, max);
                },

                buildTrendPath(values, min, max) {
                    if (!values || values.length < 2) return '';
                    const range = max - min || 1;
                    const points = values.map((value, index) => {
                        const x = (index / (values.length - 1)) * 200;
                        const y = 60 - ((value - min) / range) * 56;
                        return `${x},${y}`;
                    });
                    return 'M' + points.join(' L');
                },

                async downloadHistory(format = 'json', series = null) {
                    const selectedSeries = series || this.historySelectedSeriesKey;
                    const params = new URLSearchParams({ format, series: selectedSeries });
                    if (format === 'json' && selectedSeries === 'history') {
                        params.set('history_mode', 'full');
                    }
                    const response = await fetch('/stats-history?' + params.toString());
                    if (!response.ok) throw new Error('Failed to export history');

                    const blob = await response.blob();
                    const url = window.URL.createObjectURL(blob);
                    const link = document.createElement('a');
                    link.href = url;
                    link.download = `headroom-stats-history-${selectedSeries}.${format}`;
                    document.body.appendChild(link);
                    link.click();
                    link.remove();
                    window.URL.revokeObjectURL(url);
                },

                // --- Prefix Cache ---

                getTrendLine(history) {
                    if (!history || history.length < 2) return '';
                    const values = history.map(h => Array.isArray(h) ? h[1] : (h?.total_tokens_saved || 0));
                    const min = Math.min(...values);
                    const max = Math.max(...values);
                    const range = max - min || 1;

                    const points = values.map((v, i) => {
                        const x = (i / (values.length - 1)) * 200;
                        const y = 60 - ((v - min) / range) * 56;
                        return `${x},${y}`;
                    });

                    return 'M' + points.join(' L');
                },

                getTrendArea(history) {
                    if (!history || history.length < 2) return '';
                    const line = this.getTrendLine(history);
                    if (!line) return '';
                    return line + ` L200,64 L0,64 Z`;
                },

                getObjectTrendLine(history, valueKey) {
                    if (!history || history.length < 2) return '';
                    const values = history.map(point => point?.[valueKey] || 0);
                    const min = Math.min(...values);
                    const max = Math.max(...values);
                    const range = max - min || 1;

                    const points = values.map((value, index) => {
                        const x = (index / (values.length - 1)) * 200;
                        const y = 60 - ((value - min) / range) * 56;
                        return `${x},${y}`;
                    });

                    return 'M' + points.join(' L');
                },

                getObjectTrendArea(history, valueKey) {
                    if (!history || history.length < 2) return '';
                    const line = this.getObjectTrendLine(history, valueKey);
                    if (!line) return '';
                    return line + ` L200,64 L0,64 Z`;
                },
                async fetchHistoryStats() {
                    try {
                        const response = await fetch('/stats-history');
                        if (response.ok) {
                            this.historyStats = await response.json();
                            this.lastHistoryFetchMs = Date.now();
                        }
                    } catch (e) {
                        console.error('Failed to fetch history stats:', e);
                    }
                },

                get historyGranularityOptions() {
                    return [
                        ['Daily', 'daily'],
                        ['Weekly', 'weekly'],
                        ['Monthly', 'monthly'],
                        ['Checkpoints', 'history'],
                    ];
                },

                get historyChartModeOptions() {
                    return [
                        ['Tokens', 'tokens'],
                        ['Cost', 'cost'],
                    ];
                },

                get historyModelSourceSeries() {
                    // Rollup buckets carrying by_model attribution. Raw checkpoints
                    // have no by_model, so the checkpoint view falls back to daily.
                    const key = this.historySelectedSeriesKey === 'history'
                        ? 'daily'
                        : this.historySelectedSeriesKey;
                    return this.historyStats.series?.[key] || [];
                },

                get historyModelChartSeries() {
                    const buckets = this.historyModelSourceSeries;
                    const totals = {};
                    for (const bucket of buckets) {
                        for (const [model, entry] of Object.entries(bucket.by_model || {})) {
                            totals[model] = (totals[model] || 0) + (entry.tokens_saved || 0);
                        }
                    }
                    const topModels = Object.entries(totals)
                        .filter(([, saved]) => saved > 0)
                        .sort((a, b) => b[1] - a[1])
                        .slice(0, 5)
                        .map(([model]) => model);
                    // A breakdown-row selection outside the top 5 takes the
                    // last chart slot so the filter works for every row (the
                    // template renders a fixed set of line slots).
                    const selected = this.historySelectedModel;
                    if (
                        selected &&
                        (totals[selected] || 0) > 0 &&
                        topModels.length > 0 &&
                        !topModels.includes(selected)
                    ) {
                        topModels[topModels.length - 1] = selected;
                    }
                    return topModels.map(model => {
                        let running = 0;
                        return {
                            model,
                            values: buckets.map(bucket => {
                                running += bucket.by_model?.[model]?.tokens_saved || 0;
                                return running;
                            }),
                        };
                    });
                },

                get historyModelBreakdown() {
                    const totals = {};
                    for (const bucket of this.historyModelSourceSeries) {
                        for (const [model, entry] of Object.entries(bucket.by_model || {})) {
                            const row = totals[model] || (totals[model] = {
                                model,
                                tokens_saved: 0,
                                savings_usd: 0,
                                input_cost_usd: 0,
                            });
                            row.tokens_saved += entry.tokens_saved || 0;
                            row.savings_usd += entry.compression_savings_usd_delta || 0;
                            row.input_cost_usd += entry.total_input_cost_usd_delta || 0;
                        }
                    }
                    const rows = Object.values(totals).map(row => ({
                        ...row,
                        expected_cost_usd: row.input_cost_usd + row.savings_usd,
                    }));
                    return this.sortRows(rows, 'historyModel', 'tokens_saved', 'desc');
                },

                get historyActiveModel() {
                    // A model filter only applies while that model is present in
                    // the charted series; otherwise fall back to showing all.
                    // Raw checkpoint view plots no per-model lines (they are
                    // derived from rollup buckets), so the filter must not
                    // suppress the aggregate line there.
                    if (this.historySelectedSeriesKey === 'history') return null;
                    const model = this.historySelectedModel;
                    if (!model) return null;
                    return this.historyModelChartSeries.some(series => series.model === model)
                        ? model
                        : null;
                },

                get historyCostTrend() {
                    return this.historicalTrend.map(point => {
                        const actual = point?.total_input_cost_usd || 0;
                        const saved = point?.compression_savings_usd || 0;
                        return { actual, expected: actual + saved };
                    });
                },

                get hasHistoricalData() {
                    return (this.historyStats.history || []).length > 0;
                },

                get historySelectedSeriesKey() {
                    return this.historyGranularity === 'history' ? 'history' : this.historyGranularity;
                },

                get historySelectedSeriesLabel() {
                    const labels = {
                        history: 'Checkpoints',
                        daily: 'Daily',
                        weekly: 'Weekly',
                        monthly: 'Monthly',
                    };
                    return labels[this.historySelectedSeriesKey] || 'History';
                },

                get historyModelSourceSeriesLabel() {
                    // historyModelSourceSeries substitutes the daily rollup
                    // at raw checkpoint granularity; label what is shown.
                    return this.historySelectedSeriesKey === 'history'
                        ? 'Daily'
                        : this.historySelectedSeriesLabel;
                },

                get historySelectedPointCount() {
                    if (this.historySelectedSeriesKey === 'history') {
                        return (this.historyStats.history || []).length;
                    }
                    return (this.historyStats.series?.[this.historySelectedSeriesKey] || []).length;
                },

                get historicalTrend() {
                    if (this.historySelectedSeriesKey === 'history') {
                        return this.historyStats.history || [];
                    }
                    return this.historyStats.series?.[this.historySelectedSeriesKey] || [];
                },

                get historyTrendLabel() {
                    const labels = {
                        history: 'Checkpoint history',
                        daily: 'Daily cumulative savings',
                        weekly: 'Weekly cumulative savings',
                        monthly: 'Monthly cumulative savings',
                    };
                    return labels[this.historySelectedSeriesKey] || 'Historical savings';
                },

                get historyWindowLabel() {
                    const history = this.historyStats.history || [];
                    if (history.length === 0) return 'Waiting for saved requests';
                    const first = history[0]?.timestamp;
                    const last = history[history.length - 1]?.timestamp;
                    if (!first || !last) return 'Persisted locally';
                    return this.formatDate(first) + ' to ' + this.formatDate(last);
                },

                get historyAverageTokensPerDay() {
                    const daily = this.historyStats.series?.daily || [];
                    const lifetime = this.historyStats.lifetime?.tokens_saved || 0;
                    if (daily.length === 0) return 0;
                    return Math.round(lifetime / daily.length);
                },

                get historyAverageTokensPerWeek() {
                    const weekly = this.historyStats.series?.weekly || [];
                    const lifetime = this.historyStats.lifetime?.tokens_saved || 0;
                    if (weekly.length === 0) return 0;
                    return Math.round(lifetime / weekly.length);
                },

                get historyAverageTokensPerMonth() {
                    const monthly = this.historyStats.series?.monthly || [];
                    const lifetime = this.historyStats.lifetime?.tokens_saved || 0;
                    if (monthly.length === 0) return 0;
                    return Math.round(lifetime / monthly.length);
                },

                get recentDailyHistory() {
                    return [...(this.historyStats.series?.daily || [])].slice(-7).reverse();
                },

                get recentWeeklyHistory() {
                    return [...(this.historyStats.series?.weekly || [])].slice(-6).reverse();
                },

                get recentMonthlyHistory() {
                    return [...(this.historyStats.series?.monthly || [])].slice(-6).reverse();
                },

                get recentHistoricalPoints() {
                    return [...(this.historyStats.history || [])].slice(-8).reverse();
                },

    };
}
