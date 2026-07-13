function dashboardRequestsMixin() {
    return {
                feedOpen: true,
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

                // Word-level LCS diff (detail panel only -- the compact card list
                // stays plain text). Tokenizes on whitespace boundaries, keeping
                // separators as their own tokens so reconstruction preserves
                // original formatting.
                //
                // Real compressed pairs here are typically a huge, mostly-shared
                // system prompt / tool schema dump with one changed slice in the
                // middle (a truncated tool result, a shaped message) -- NOT two
                // arbitrary unrelated texts. A naive O(n*m) LCS over the whole
                // thing would be too slow (and an earlier version of this function
                // capped out at 4000 combined words and silently fell back to
                // plain text for anything bigger, which is most real content here
                // -- that's why the diff looked like it "did nothing"). Trimming
                // the common prefix/suffix first means the expensive LCS only
                // runs over the actual differing middle, which stays small even
                // when the surrounding document is huge.
                computeWordDiff(before, after) {
                    const tokenize = s => (s || '').split(/(\s+)/).filter(t => t.length > 0);
                    const a = tokenize(before);
                    const b = tokenize(after);

                    let start = 0;
                    const maxStart = Math.min(a.length, b.length);
                    while (start < maxStart && a[start] === b[start]) start++;
                    let endA = a.length, endB = b.length;
                    while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) { endA--; endB--; }

                    const prefix = a.slice(0, start);
                    const suffix = a.slice(endA);
                    const midA = a.slice(start, endA);
                    const midB = b.slice(start, endB);

                    // Even after trimming, guard against a pathological worst case
                    // (e.g. two long texts that share almost nothing).
                    if (midA.length * midB.length > 4_000_000) return null;

                    const n = midA.length, m = midB.length;
                    const lcs = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
                    for (let i = n - 1; i >= 0; i--) {
                        for (let j = m - 1; j >= 0; j--) {
                            lcs[i][j] = midA[i] === midB[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
                        }
                    }

                    const midSegments = [];
                    let i = 0, j = 0;
                    while (i < n && j < m) {
                        if (midA[i] === midB[j]) {
                            midSegments.push({ type: 'equal', text: midA[i] });
                            i++; j++;
                        } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
                            midSegments.push({ type: 'delete', text: midA[i] });
                            i++;
                        } else {
                            midSegments.push({ type: 'insert', text: midB[j] });
                            j++;
                        }
                    }
                    while (i < n) { midSegments.push({ type: 'delete', text: midA[i] }); i++; }
                    while (j < m) { midSegments.push({ type: 'insert', text: midB[j] }); j++; }

                    const segments = [
                        ...prefix.map(text => ({ type: 'equal', text })),
                        ...midSegments,
                        ...suffix.map(text => ({ type: 'equal', text })),
                    ];

                    // Merge adjacent same-type segments so the DOM isn't one node per word.
                    const merged = [];
                    for (const seg of segments) {
                        const last = merged[merged.length - 1];
                        if (last && last.type === seg.type) last.text += seg.text;
                        else merged.push({ ...seg });
                    }
                    return merged;
                },

                renderDiffHtml(segments) {
                    return segments.map(seg => {
                        const text = this.escapeHtml(seg.text);
                        if (seg.type === 'delete') return `<del class="diff-removed">${text}</del>`;
                        if (seg.type === 'insert') return `<ins class="diff-added">${text}</ins>`;
                        // Unchanged spans are typically shared system-prompt/tool-schema
                        // boilerplate -- dim them so the actual diff (the part someone
                        // opened this panel to see) is what draws the eye.
                        return `<span class="diff-unchanged">${text}</span>`;
                    }).join('');
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
                    // "Unchanged": the request passed through untouched -- before and
                    // after are identical and non-empty. Distinct from the "no
                    // messages captured yet" empty state (hasMessages false), which
                    // is handled separately below.
                    const isUnchanged = hasMessages && msgs === compressed;
                    const before = msgs.substring(0, 2000) + (msgs.length > 2000 ? '\n\n[truncated, click to see full content]' : '');
                    const after = compressed.substring(0, 2000) + (compressed.length > 2000 ? '\n\n[truncated, click to see full content]' : '');

                    const time = t.timestamp ? new Date(t.timestamp).toLocaleTimeString() : '--:--:--';
                    const model = (t.model || 'unknown').replace(/^(anthropic\.|openai\.)/, '').substring(0, 25);
                    const tokensSaved = t.tokens_saved || 0;
                    const savingsPct = ((t.savings_percent || 0)).toFixed(0);

                    const emptyState = '<span class="text-gray-600 italic">Enable HEADROOM_LOG_MESSAGES=true to see content</span>';
                    const beforeContent = hasMessages ? this.escapeHtml(before) : emptyState;
                    const afterContent = hasMessages ? this.escapeHtml(after) : emptyState;

                    const transformsApplied = t.transforms_applied || [];
                    // Tags row is capped hard: 1 inline tag + a "+N more" chip, in a
                    // fixed-height overflow-hidden container, so it is structurally
                    // incapable of overflowing into the next card regardless of tag
                    // count or text length. The full list is reachable via a real
                    // anchored popover on the "+N more" chip (see .tags-more-chip
                    // delegation in core.js) instead of the corner help widget --
                    // this is a list of specific values to inspect, not a hover
                    // explainer, so it gets its own small floating panel.
                    const tagsRow = transformsApplied.length > 0 ? `
                                <div class="flex items-center gap-1 mt-1.5 overflow-hidden max-h-[18px]">
                                    <span class="text-[9px] px-1.5 py-0.5 bg-border rounded font-mono text-gray-500 truncate max-w-[55%]">${this.escapeHtml(transformsApplied[0])}</span>
                                    ${transformsApplied.length > 1 ? `<span class="tags-more-chip text-[9px] px-1.5 py-0.5 rounded font-mono text-gray-500 border border-border/60 shrink-0 cursor-pointer" data-tags="${this.escapeHtml(JSON.stringify(transformsApplied.slice(1)))}">+${transformsApplied.length - 1} more</span>` : ''}
                                </div>
                            ` : '';

                    const diffBody = isUnchanged ? `
                            <div class="rounded border border-gray-600/30 overflow-hidden flex flex-col" style="height: 115px;">
                                <div class="px-2 py-1 border-b border-gray-600/30 shrink-0 bg-card-alt flex items-center gap-1.5"
                                     data-help-title="{{help.requests.unchanged.title}}" data-help="{{help.requests.unchanged.body}}">
                                    <span class="text-[10px] text-gray-400 uppercase tracking-wide font-semibold">Unchanged</span>
                                    <span class="inline-flex items-center text-[9px] text-gray-500 bg-gray-700/30 px-1.5 py-0.5 rounded-full uppercase tracking-wide">not compressed</span>
                                </div>
                                <div class="p-2 font-mono text-[11px] text-gray-300 overflow-auto flex-1">${beforeContent}</div>
                            </div>
                    ` : `
                            <div class="grid grid-cols-2 gap-2" style="height: 115px;">
                                <div class="rounded border border-red-900/30 overflow-hidden flex flex-col">
                                    <div class="diff-before-header px-2 py-1 border-b border-red-900/30 shrink-0"
                                         data-help-title="{{help.requests.before.title}}" data-help="{{help.requests.before.body}}">
                                        <span class="text-[10px] text-red-400 uppercase tracking-wide font-semibold">Before</span>
                                    </div>
                                    <div class="diff-before p-2 font-mono text-[11px] text-gray-300 overflow-auto flex-1"
                                         >${beforeContent}</div>
                                </div>
                                <div class="rounded border border-emerald-900/30 overflow-hidden flex flex-col">
                                    <div class="diff-after-header px-2 py-1 border-b border-emerald-900/30 shrink-0"
                                         data-help-title="{{help.requests.after.title}}" data-help="{{help.requests.after.body}}">
                                        <span class="text-[10px] text-emerald-400 uppercase tracking-wide font-semibold">After</span>
                                    </div>
                                    <div class="diff-after p-2 font-mono text-[11px] text-gray-300 overflow-auto flex-1"
                                         >${afterContent}</div>
                                </div>
                            </div>
                    `;

                    return `
                        <div class="transformation-card border-b border-border p-3 cursor-pointer hover:bg-card-alt transition-colors" data-idx="${idx}" style="height: ${this.feedItemHeight}px; box-sizing: border-box;"
                             data-help-title="{{help.requests.transformation_detail.title}}" data-help="{{help.requests.transformation_detail.body}}">
                            <div class="flex items-center justify-between mb-2">
                                <div class="flex items-center gap-2 min-w-0"
                                     data-help-title="{{help.requests.request.title}}" data-help="{{help.requests.request.body}}">
                                    <span class="text-xs font-mono text-gray-400 truncate">${this.escapeHtml(model)}</span>
                                    <span class="text-xs text-gray-600 shrink-0">·</span>
                                    <span class="text-xs text-emerald-400 shrink-0">${tokensSaved} tok</span>
                                    <span class="text-xs text-gray-600 shrink-0">(${savingsPct}%)</span>
                                </div>
                                <span class="text-xs text-gray-600 shrink-0" data-help-title="{{help.requests.time_2.title}}" data-help="{{help.requests.time_2.body}}">${time}</span>
                            </div>
                            ${diffBody}
                            ${tagsRow}
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
                    const msgs = this.extractMessagesText(t.request_messages);
                    const compressed = this.extractMessagesText(t.compressed_messages);
                    const hasMessages = msgs.length > 0 || compressed.length > 0;
                    const isUnchanged = hasMessages && msgs === compressed;
                    // Diff is detail-panel-only (the compact card list stays plain
                    // text). null when the pair is unchanged (nothing to diff) or
                    // too large for the O(n*m) LCS table -- renderDiffHtml falls
                    // back to plain text in that case.
                    const diffSegments = (!isUnchanged && hasMessages) ? this.computeWordDiff(msgs, compressed) : null;
                    this.feedDetailTransform = {
                        model: t.model || 'unknown',
                        time: t.timestamp ? new Date(t.timestamp).toLocaleString() : '--',
                        tokensSaved: t.tokens_saved || 0,
                        savingsPct: (t.savings_percent || 0).toFixed(0),
                        transformsApplied: t.transforms_applied || [],
                        before: msgs || '(no content captured)',
                        after: compressed || '(no content captured)',
                        afterDiffHtml: diffSegments ? this.renderDiffHtml(diffSegments) : null,
                        // Same "unchanged" detection as the compact card: only when
                        // both sides are non-empty and byte-identical, not the
                        // separate "nothing captured yet" empty state.
                        isUnchanged,
                        // "Original message" starts collapsed, "Compressed" starts
                        // open showing the diff -- the changes are the point of
                        // the detail view, the untouched original is reference.
                        beforeCollapsed: true,
                        afterCollapsed: false,
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
