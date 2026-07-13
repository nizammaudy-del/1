function dashboardWindowsMixin() {
    return {
                get cacheSavingsPercent() {
                    const t = this.stats.prefix_cache?.totals || {};
                    const total = (t.cache_read_tokens || 0) + (t.cache_write_tokens || 0);
                    if (total === 0) return 0;
                    return Math.round((t.cache_read_tokens || 0) / total * 100);
                },

                get cacheWritePercent() {
                    const t = this.stats.prefix_cache?.totals || {};
                    const total = (t.cache_read_tokens || 0) + (t.cache_write_tokens || 0);
                    if (total === 0) return 0;
                    return Math.round((t.cache_write_tokens || 0) / total * 100);
                },

                get hasObservedTtlBuckets() {
                    const buckets = this.stats.prefix_cache?.totals?.observed_ttl_buckets || {};
                    return ((buckets['5m']?.tokens || 0) + (buckets['1h']?.tokens || 0)) > 0;
                },

                get observedTtlHeadline() {
                    const mix = this.stats.prefix_cache?.totals?.observed_ttl_mix || {};
                    const oneHour = mix['1h_pct'] || 0;
                    const fiveMinute = mix['5m_pct'] || 0;
                    if (oneHour === fiveMinute) return 'Balanced';
                    return oneHour > fiveMinute ? '1h leaning' : '5m leaning';
                },

                get observedTtlWindowLabel() {
                    const mix = this.stats.prefix_cache?.totals?.observed_ttl_mix || {};
                    const active = mix.active_buckets || [];
                    if (!active.length) return 'No TTL bucket data';
                    return active.length === 1 ? active[0] + ' only' : active.join(' / ');
                },

                get compressionVsCacheNet() {
                    const cvc = this.stats.prefix_cache?.compression_vs_cache || {};
                    return cvc.net_tokens ?? ((cvc.tokens_saved_by_compression || 0) - (cvc.tokens_lost_to_cache_bust || 0));
                },

                get prefixFreezeNet() {
                    const pf = this.stats.prefix_cache?.prefix_freeze || {};
                    return pf.net_benefit_tokens ?? ((pf.tokens_preserved || 0) - (pf.compression_foregone_tokens || 0));
                },

                get hasCompressionVsCache() {
                    const cvc = this.stats.prefix_cache?.compression_vs_cache || {};
                    const pf = this.stats.prefix_cache?.prefix_freeze || {};
                    return (cvc.tokens_saved_by_compression || 0) > 0
                        || (cvc.tokens_lost_to_cache_bust || 0) > 0
                        || (pf.tokens_preserved || 0) > 0
                        || (pf.compression_foregone_tokens || 0) > 0;
                },

                // --- Cache miss attribution (#1313) ---

                get missAttribution() {
                    return this.stats.prefix_cache?.miss_attribution?.totals || {};
                },

                get hasMissAttribution() {
                    return (this.missAttribution.total || 0) > 0;
                },

                // --- Waste Signals ---

    };
}
