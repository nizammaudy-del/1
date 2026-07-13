function dashboardDiagnosticsMixin() {
    return {
                diagnosticsDeployments: [],
                diagnosticsLearnHistory: [],
                diagnosticsMemorySync: { agents: [], dedup_rate: null },
                doctor: { checks: [], exit_code: 0 },

                // Near-limit warnings: edge-triggered banner + optional native notifications.
                // Thresholds/enabled flag are client-local display prefs (localStorage only).
                async fetchDiagnostics() {
                    try {
                        const [depRes, learnRes, syncRes] = await Promise.all([
                            fetch('/admin/deployments'),
                            fetch('/learn/history?limit=20'),
                            fetch('/debug/memory/sync'),
                        ]);
                        if (depRes.ok) {
                            const data = await depRes.json();
                            this.diagnosticsDeployments = data.deployments || [];
                        }
                        if (learnRes.ok) {
                            const data = await learnRes.json();
                            this.diagnosticsLearnHistory = (data.runs || []).slice().reverse();
                        }
                        if (syncRes.ok) {
                            this.diagnosticsMemorySync = await syncRes.json();
                        }
                    } catch (e) {
                        console.error('Failed to fetch diagnostics:', e);
                    }
                },

                learnRunPending: false,
                async triggerLearnRun() {
                    this.learnRunPending = true;
                    try {
                        const res = await fetch('/learn/run', { method: 'POST' });
                        if (res.ok) {
                            this.showToast('headroom learn --apply started in the background');
                        } else {
                            this.showToast('Failed to start learn run', { error: true });
                        }
                    } catch (e) {
                        this.showToast('Failed to start learn run', { error: true });
                    } finally {
                        // Brief pending state only -- the run itself continues detached
                        // server-side; refresh Learn history later to see the result.
                        setTimeout(() => { this.learnRunPending = false; }, 2000);
                    }
                },

                async fetchDoctor() {
                    try {
                        const res = await fetch('/doctor');
                        if (res.ok) {
                            this.doctor = await res.json();
                        }
                    } catch (e) {
                        console.error('Failed to fetch doctor status:', e);
                    }
                },

    };
}
