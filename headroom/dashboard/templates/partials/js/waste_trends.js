function dashboardWasteTrendsMixin() {
    return {
                wasteSignalLabel(signal) {
                    const labels = {
                        json_bloat: 'JSON Bloat',
                        html_noise: 'HTML Noise',
                        base64: 'Base64 Blobs',
                        whitespace: 'Whitespace',
                        dynamic_date: 'Dynamic Dates',
                        repetition: 'Repetition',
                        reread: 'Re-read Tool Results',
                        reread_compressed: 'Re-read After Compression',
                    };
                    return labels[signal] || signal;
                },

                wasteSignalColor(signal) {
                    const colors = {
                        json_bloat: 'bg-amber-500',
                        html_noise: 'bg-orange-500',
                        base64: 'bg-red-500',
                        whitespace: 'bg-blue-500',
                        dynamic_date: 'bg-purple-500',
                        repetition: 'bg-pink-500',
                        reread: 'bg-teal-500',
                        reread_compressed: 'bg-rose-500',
                    };
                    return colors[signal] || 'bg-gray-500';
                },

                wasteSignalBadgeColor(signal) {
                    const colors = {
                        json_bloat: 'bg-amber-500/20 text-amber-400',
                        html_noise: 'bg-orange-500/20 text-orange-400',
                        base64: 'bg-red-500/20 text-red-400',
                        whitespace: 'bg-blue-500/20 text-blue-400',
                        dynamic_date: 'bg-purple-500/20 text-purple-400',
                        repetition: 'bg-pink-500/20 text-pink-400',
                        reread: 'bg-teal-500/20 text-teal-400',
                        reread_compressed: 'bg-rose-500/20 text-rose-400',
                    };
                    return colors[signal] || 'bg-gray-500/20 text-gray-400';
                },

                get sortedWasteSignals() {
                    const signals = this.stats.waste_signals || {};
                    return Object.entries(signals)
                        .filter(([, v]) => v > 0)
                        .sort((a, b) => b[1] - a[1]);
                },

                getWastePercent(tokens) {
                    const signals = this.stats.waste_signals || {};
                    const max = Math.max(...Object.values(signals), 1);
                    return Math.min((tokens / max) * 100, 100);
                },

                get compressionTotalBefore() {
                    return this.stats.tokens?.total_before_compression || 0;
                },

                get proxyShareOfTotal() {
                    const total = this.compressionTotalBefore;
                    if (total <= 0) return 0;
                    return (this.stats.tokens?.proxy_compression_saved || 0) / total * 100;
                },

                get cliFilteringLabel() {
                    const raw = this.stats.savings?.by_layer?.cli_filtering?.label
                        || this.stats.context_tool?.label
                        || this.stats.context_tool?.configured
                        || 'Context Tool';
                    if (String(raw).toLowerCase() === 'lean-ctx') return 'Lean-ctx';
                    return String(raw);
                },

                get cliFilteringSaved() {
                    return this.stats.tokens?.cli_filtering_saved
                        ?? this.stats.tokens?.cli_tokens_avoided
                        ?? this.stats.tokens?.rtk_saved
                        ?? 0;
                },

                get cliFilteringShareOfTotal() {
                    const total = this.compressionTotalBefore;
                    if (total <= 0) return 0;
                    return this.cliFilteringSaved / total * 100;
                },

                // --- Headline savings percent ---
                //
                // Active ratio (saved / attempted) is the right metric when we have
                // a working denominator. Some backend-routed paths don't yet
                // populate `proxy_attempted_tokens` consistently — in that case
                // the active number reads 0% even when compression is happening,
                // so fall back to the whole-request ratio (`proxy_savings_percent`)
                // instead of showing a misleading zero. Issue #455.
                get headlineSavingsPercent() {
                    const attempted = this.stats.tokens?.proxy_attempted_tokens || 0;
                    const active = this.stats.tokens?.active_savings_percent || 0;
                    if (attempted > 0 && active > 0) return active;
                    return this.stats.tokens?.proxy_savings_percent || 0;
                },

                get headlineSavingsTitle() {
                    const attempted = this.stats.tokens?.proxy_attempted_tokens || 0;
                    const active = this.stats.tokens?.active_savings_percent || 0;
                    if (attempted > 0 && active > 0) {
                        return 'Of compressible tokens attempted';
                    }
                    return 'Of whole pre-compression request (attempted-tokens metric unavailable)';
                },

    };
}
