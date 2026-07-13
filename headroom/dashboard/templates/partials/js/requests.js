function dashboardRequestsMixin() {
    return {
                feedOpen: false,
                transformations: [],
                feedScrolled_: false,
                feedNewCount: 0,
                feedScrollY: 0,
                // 195px budget: header row + fixed 115px before/after grid + the
                // optional transforms_applied tag row. Uniform per-row height is
                // required by the virtual-scroll math below, so this has to fit
                // the tallest card (one with tags) or the next card's absolutely
                // positioned block overlaps this one's tag row.
                feedItemHeight: 195,
                feedBuffer: 5,
                feedFollow: true,
                _suppressFeedScrollHandler: false,
                feedDetailTransform: null,
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
                            if (!this.feedFollow && this.feedScrolled_ && newLen > prevLen) {
                                this.feedNewCount = newLen - prevLen;
                            }
                            this.transformations = data.transformations || [];
                            this.log_full_messages = data.log_full_messages ?? this.log_full_messages;
                            this.lastFeedFetchMs = Date.now();
                            if (this.feedFollow) {
                                this.scrollToFeedTop();
                            } else {
                                this.renderTransformations();
                            }
                        }
                    } catch (e) {
                        console.error('Failed to fetch transformations:', e);
                    }
                },

                // Message `content` isn't always a plain string: Anthropic/OpenAI
                // shapes send an array of blocks (text, tool_use, tool_result, each
                // of which can itself nest arrays/objects). Naive string
                // concatenation on those renders "[object Object]"; walk the shape
                // and pull out something readable instead.
                extractContentText(content) {
                    if (content == null) return '';
                    if (typeof content === 'string') return content;
                    if (Array.isArray(content)) {
                        return content.map(block => this.extractContentText(block)).filter(Boolean).join('\n');
                    }
                    if (typeof content === 'object') {
                        if (typeof content.text === 'string') return content.text;
                        if (content.type === 'tool_use') {
                            return `[tool_use: ${content.name || 'unknown'}]`;
                        }
                        if (content.type === 'tool_result' || content.content !== undefined) {
                            return this.extractContentText(content.content);
                        }
                        try {
                            return JSON.stringify(content);
                        } catch {
                            return String(content);
                        }
                    }
                    return String(content);
                },

                extractMessagesText(messages) {
                    return (messages || []).map(m => this.extractContentText(m && m.content)).filter(Boolean).join('\n');
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

                // `after` is the compressed request (what actually went upstream),
                // not the model's reply -- `response_content` is a different field
                // entirely (often unset, e.g. for streaming) and was never the
                // right thing to diff against `before` here.
                renderTransformationCard(t, idx) {
                    const msgs = this.extractMessagesText(t.request_messages);
                    const compressed = this.extractMessagesText(t.compressed_messages);
                    const hasMessages = msgs.length > 0 || compressed.length > 0;
                    const before = msgs.substring(0, 2000) + (msgs.length > 2000 ? '\n\n[truncated, click to see full content]' : '');
                    const after = compressed.substring(0, 2000) + (compressed.length > 2000 ? '\n\n[truncated, click to see full content]' : '');

                    const time = t.timestamp ? new Date(t.timestamp).toLocaleTimeString() : '--:--:--';
                    const model = (t.model || 'unknown').replace(/^(anthropic\.|openai\.)/, '').substring(0, 25);
                    const tokensSaved = t.tokens_saved || 0;
                    const savingsPct = ((t.savings_percent || 0)).toFixed(0);

                    const emptyState = '<span class="text-gray-600 italic">Enable HEADROOM_LOG_MESSAGES=true to see content</span>';
                    const beforeContent = hasMessages ? this.escapeHtml(before) : emptyState;
                    const afterContent = hasMessages ? this.escapeHtml(after) : emptyState;

                    return `
                        <div class="transformation-card border-b border-border p-3 cursor-pointer hover:bg-card-alt transition-colors" data-idx="${idx}" style="height: ${this.feedItemHeight}px; box-sizing: border-box;"
                             data-help-title="Transformation detail" data-help="Click this card to open a larger, unclipped view of the before/after content.">
                            <div class="flex items-center justify-between mb-2">
                                <div class="flex items-center gap-2 min-w-0"
                                     data-help-title="Request" data-help="The model this request was routed to, tokens Headroom removed, and the resulting savings percentage.">
                                    <span class="text-xs font-mono text-gray-400 truncate">${this.escapeHtml(model)}</span>
                                    <span class="text-xs text-gray-600 shrink-0">·</span>
                                    <span class="text-xs text-emerald-400 shrink-0">${tokensSaved} tok</span>
                                    <span class="text-xs text-gray-600 shrink-0">(${savingsPct}%)</span>
                                </div>
                                <span class="text-xs text-gray-600 shrink-0" data-help-title="Time" data-help="When the proxy handled this request.">${time}</span>
                            </div>
                            <div class="grid grid-cols-2 gap-2" style="height: 115px;">
                                <div class="rounded border border-red-900/30 overflow-hidden flex flex-col">
                                    <div class="diff-before-header px-2 py-1 border-b border-red-900/30 shrink-0"
                                         data-help-title="Before" data-help="The original prompt exactly as sent to Headroom, before any compression.">
                                        <span class="text-[10px] text-red-400 uppercase tracking-wide font-semibold">Before</span>
                                    </div>
                                    <div class="diff-before p-2 font-mono text-[11px] text-gray-300 overflow-auto flex-1"
                                         >${beforeContent}</div>
                                </div>
                                <div class="rounded border border-emerald-900/30 overflow-hidden flex flex-col">
                                    <div class="diff-after-header px-2 py-1 border-b border-emerald-900/30 shrink-0"
                                         data-help-title="After" data-help="What Headroom actually sent upstream to the provider after compression.">
                                        <span class="text-[10px] text-emerald-400 uppercase tracking-wide font-semibold">After</span>
                                    </div>
                                    <div class="diff-after p-2 font-mono text-[11px] text-gray-300 overflow-auto flex-1"
                                         >${afterContent}</div>
                                </div>
                            </div>
                            ${(t.transforms_applied || []).length > 0 ? `
                                <div class="flex flex-wrap gap-1 mt-1.5"
                                     data-help-title="Transforms applied" data-help="The compression transforms Headroom ran on this request, in order.">
                                    ${t.transforms_applied.slice(0, 4).map(tr => `<span class="text-[9px] px-1.5 py-0.5 bg-border rounded font-mono text-gray-500">${this.escapeHtml(tr)}</span>`).join('')}
                                    ${t.transforms_applied.length > 4 ? `<span class="text-[9px] text-gray-600">+${t.transforms_applied.length - 4}</span>` : ''}
                                </div>
                            ` : ''}
                        </div>
                    `;
                },

                // Jumping to the newest message always resumes follow mode --
                // matches the mental model of "follow" in a log tail/chat scroll.
                scrollToFeedTop() {
                    const container = document.getElementById('feed-container');
                    if (container) {
                        this._suppressFeedScrollHandler = true;
                        container.scrollTop = 0;
                        this.feedFollow = true;
                        this.feedScrolled_ = false;
                        this.feedNewCount = 0;
                        this.renderTransformations();
                        requestAnimationFrame(() => { this._suppressFeedScrollHandler = false; });
                    }
                },

                toggleFeedFollow() {
                    if (this.feedFollow) {
                        this.feedFollow = false;
                    } else {
                        this.scrollToFeedTop();
                    }
                },

                handleFeedScroll() {
                    const container = document.getElementById('feed-container');
                    if (!container) return;

                    this.feedScrollY = container.scrollTop;
                    if (this._suppressFeedScrollHandler) {
                        this.renderTransformations();
                        return;
                    }
                    this.feedScrolled_ = container.scrollTop > 50;
                    // A manual scroll away from the top is the user opting out of
                    // auto-scroll; the Follow button (or the "N new" jump chip,
                    // via scrollToFeedTop) is what re-enables it.
                    if (this.feedScrolled_) this.feedFollow = false;
                    this.renderTransformations();
                },

                openTransformDetail(idx) {
                    const t = this.transformations[idx];
                    if (!t) return;
                    this.feedDetailTransform = {
                        model: t.model || 'unknown',
                        time: t.timestamp ? new Date(t.timestamp).toLocaleString() : '--',
                        tokensSaved: t.tokens_saved || 0,
                        savingsPct: (t.savings_percent || 0).toFixed(0),
                        transformsApplied: t.transforms_applied || [],
                        before: this.extractMessagesText(t.request_messages) || '(no content captured)',
                        after: this.extractMessagesText(t.compressed_messages) || '(no content captured)',
                    };
                },

                closeTransformDetail() {
                    this.feedDetailTransform = null;
                },

                expandedRows: {},
                toggleExpanded(id) {
                    this.expandedRows[id] = !this.expandedRows[id];
                },

                // --- Charts ---

    };
}
