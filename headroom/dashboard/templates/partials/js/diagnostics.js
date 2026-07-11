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
