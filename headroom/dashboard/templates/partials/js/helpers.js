        function toggleTheme() {
            const isDark = document.documentElement.classList.toggle('dark');
            localStorage.setItem('headroom-theme', isDark ? 'dark' : 'light');
        }
