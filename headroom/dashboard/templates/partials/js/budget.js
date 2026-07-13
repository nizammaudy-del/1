function dashboardBudgetMixin() {
    return {
                bucketFor(pct) {
                    if (pct >= 100) return 100;
                    if (pct >= 90) return 90;
                    if (pct >= 80) return 80;
                    return 0;
                },
                // Valid periods are exactly hourly/daily/monthly (see
                // _BUDGET_PERIODS in user_config.py) — "weekly" is never a
                // real value here.
                estimateBudgetResetSeconds(period) {
                    const now = new Date();
                    let next;
                    if (period === 'hourly') {
                        next = new Date(now);
                        next.setHours(next.getHours() + 1, 0, 0, 0);
                    } else if (period === 'monthly') {
                        next = new Date(now.getFullYear(), now.getMonth() + 1, 1);
                    } else {
                        next = new Date(now);
                        next.setDate(next.getDate() + 1);
                        next.setHours(0, 0, 0, 0);
                    }
                    return Math.max(0, Math.floor((next - now) / 1000));
                },
                // Tracks each metric's last-crossed threshold bucket (0/80/90/100) so the
                // banner/notification only escalates on a NEW higher bucket, and clears once
                // the metric drops back under 80% (its window/period has reset).
                get budgetPercentUsed() {
                    const limit = this.stats.cost?.budget_limit_usd || 0;
                    if (!limit) return 0;
                    const spend = this.stats.cost?.period_spend_usd || 0;
                    return (spend / limit) * 100;
                },

                // --- Expandable Rows ---

    };
}
