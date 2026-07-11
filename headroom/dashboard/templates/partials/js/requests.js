function dashboardRequestsMixin() {
    return {
                feedOpen: false,
                transformations: [],
                feedScrolled_: false,
                feedNewCount: 0,
                feedScrollY: 0,
                feedItemHeight: 160,
                feedBuffer: 5,
                log_full_messages: false,
                recentRequestLimit: 10,
                // Restart detection (#uptime heuristic): if uptime drops between
                // polls the proxy process restarted and in-memory stats reset to 0.
                async showMoreRecentRequests() {
                    this.recentRequestLimit = Math.min(this.recentRequestLimit + 10, 50);
                    await this.fetchStats();
                },

                // --- Embedded MCP dashboards (lazy expand/collapse, iframe with native fallback) ---
                async toggleFeed() {
                    this.feedOpen = !this.feedOpen;
                    if (this.feedOpen) {
                        await this.fetchTransformations();
                    }
                },

                async fetchTransformations() {
                    try {
                        const prevLen = this.transformations.length;
                        const response = await fetch('/transformations/feed?limit=50');
                        if (response.ok) {
                            const data = await response.json();
                            const newLen = (data.transformations || []).length;
                            if (this.feedScrolled_ && newLen > prevLen) {
                                this.feedNewCount = newLen - prevLen;
                            }
                            this.transformations = data.transformations || [];
                            this.log_full_messages = data.log_full_messages ?? this.log_full_messages;
                            this.lastFeedFetchMs = Date.now();
                            this.renderTransformations();
                        }
                    } catch (e) {
                        console.error('Failed to fetch transformations:', e);
                    }
                },

                renderTransformations() {
                    const container = document.getElementById('feed-virtual-list');
                    if (!container) return;

                    const scrollTop = this.feedScrollY;
                    const viewportHeight = (document.getElementById('feed-container')?.clientHeight || 600);

                    const totalHeight = this.transformations.length * this.feedItemHeight;
                    container.style.height = totalHeight + 'px';

                    // Calculate visible range with buffer
                    const startIdx = Math.max(0, Math.floor(scrollTop / this.feedItemHeight) - this.feedBuffer);
                    const endIdx = Math.min(
                        this.transformations.length,
                        Math.ceil((scrollTop + viewportHeight) / this.feedItemHeight) + this.feedBuffer
                    );

                    const visible = this.transformations.slice(startIdx, endIdx);
                    const offsetTop = startIdx * this.feedItemHeight;

                    let html = `<div style="position: absolute; top: ${offsetTop}px; width: 100%;">`;
                    html += visible.map((t, i) => this.renderTransformationCard(t, startIdx + i)).join('');
                    html += '</div>';

                    container.innerHTML = html;
                },

                renderTransformationCard(t, idx) {
                    const msgs = (t.request_messages || []).map(m => m.content || '').join('');
                    const response = t.response_content || '';
                    const hasMessages = msgs.length > 0 || response.length > 0;
                    const before = msgs.substring(0, 2000) + (msgs.length > 2000 ? '\n\n[truncated]' : '');
                    const after = response.substring(0, 2000) + (response.length > 2000 ? '\n\n[truncated]' : '');

                    const time = t.timestamp ? new Date(t.timestamp).toLocaleTimeString() : '--:--:--';
                    const model = (t.model || 'unknown').replace(/^(anthropic\.|openai\.)/, '').substring(0, 25);
                    const tokensSaved = t.tokens_saved || 0;
                    const savingsPct = ((t.savings_percent || 0)).toFixed(0);

                    const emptyState = '<span class="text-gray-600 italic">Enable HEADROOM_LOG_MESSAGES=true to see content</span>';
                    const beforeContent = hasMessages ? this.escapeHtml(before) : emptyState;
                    const afterContent = hasMessages ? this.escapeHtml(after) : emptyState;

                    return `
                        <div class="transformation-card border-b border-border p-3" data-idx="${idx}" style="height: ${this.feedItemHeight}px; box-sizing: border-box;">
                            <div class="flex items-center justify-between mb-2">
                                <div class="flex items-center gap-2 min-w-0">
                                    <span class="text-xs font-mono text-gray-400 truncate">${this.escapeHtml(model)}</span>
                                    <span class="text-xs text-gray-600 shrink-0">·</span>
                                    <span class="text-xs text-emerald-400 shrink-0">${tokensSaved} tok</span>
                                    <span class="text-xs text-gray-600 shrink-0">(${savingsPct}%)</span>
                                </div>
                                <span class="text-xs text-gray-600 shrink-0">${time}</span>
                            </div>
                            <div class="grid grid-cols-2 gap-2" style="height: 115px;">
                                <div class="rounded border border-red-900/30 overflow-hidden flex flex-col">
                                    <div class="diff-before-header px-2 py-1 border-b border-red-900/30 shrink-0">
                                        <span class="text-[10px] text-red-400 uppercase tracking-wide font-semibold">Before</span>
                                    </div>
                                    <div class="diff-before p-2 font-mono text-[11px] text-gray-300 overflow-auto flex-1"
                                         >${beforeContent}</div>
                                </div>
                                <div class="rounded border border-emerald-900/30 overflow-hidden flex flex-col">
                                    <div class="diff-after-header px-2 py-1 border-b border-emerald-900/30 shrink-0">
                                        <span class="text-[10px] text-emerald-400 uppercase tracking-wide font-semibold">After</span>
                                    </div>
                                    <div class="diff-after p-2 font-mono text-[11px] text-gray-300 overflow-auto flex-1"
                                         >${afterContent}</div>
                                </div>
                            </div>
                            ${(t.transforms_applied || []).length > 0 ? `
                                <div class="flex flex-wrap gap-1 mt-1.5">
                                    ${t.transforms_applied.slice(0, 4).map(tr => `<span class="text-[9px] px-1.5 py-0.5 bg-border rounded font-mono text-gray-500">${this.escapeHtml(tr)}</span>`).join('')}
                                    ${t.transforms_applied.length > 4 ? `<span class="text-[9px] text-gray-600">+${t.transforms_applied.length - 4}</span>` : ''}
                                </div>
                            ` : ''}
                        </div>
                    `;
                },

                scrollToFeedTop() {
                    const container = document.getElementById('feed-container');
                    if (container) {
                        container.scrollTop = 0;
                        this.feedScrolled_ = false;
                        this.feedNewCount = 0;
                        this.renderTransformations();
                    }
                },

                handleFeedScroll() {
                    const container = document.getElementById('feed-container');
                    if (!container) return;

                    this.feedScrollY = container.scrollTop;
                    this.feedScrolled_ = container.scrollTop > 50;
                    this.renderTransformations();
                },

                expandedRows: {},
                toggleExpanded(id) {
                    this.expandedRows[id] = !this.expandedRows[id];
                },

                // --- Charts ---

    };
}
