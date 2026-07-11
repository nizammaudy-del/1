function dashboardSortingMixin() {
    return {
                tableSort: {},
                sortRows(rows, tableId, defaultField = null, defaultDir = 'desc') {
                    const sort = this.tableSort[tableId] || (defaultField ? { field: defaultField, dir: defaultDir } : null);
                    if (!sort) return rows;
                    return [...rows].sort((a, b) => {
                        let av = a[sort.field];
                        let bv = b[sort.field];
                        if (typeof av === 'string' || typeof bv === 'string') {
                            av = (av ?? '').toString().toLowerCase();
                            bv = (bv ?? '').toString().toLowerCase();
                            const cmp = av.localeCompare(bv);
                            return sort.dir === 'asc' ? cmp : -cmp;
                        }
                        av = Number(av) || 0;
                        bv = Number(bv) || 0;
                        return sort.dir === 'asc' ? av - bv : bv - av;
                    });
                },
                setSort(tableId, field, defaultDir = 'desc') {
                    const current = this.tableSort[tableId];
                    if (current && current.field === field) {
                        current.dir = current.dir === 'asc' ? 'desc' : 'asc';
                    } else {
                        this.tableSort[tableId] = { field, dir: defaultDir };
                    }
                },
                sortIcon(tableId, field) {
                    const s = this.tableSort[tableId];
                    if (!s || s.field !== field) return '';
                    return s.dir === 'asc' ? ' ▲' : ' ▼';
                },

    };
}
