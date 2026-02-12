/* VoiceBot Dashboard — live polling */
(function() {
    const usageEl = document.querySelector('[data-usage]');
    const activeEl = document.querySelector('[data-active-calls]');

    function poll() {
        fetch('/api/usage')
            .then(r => r.json())
            .then(data => {
                if (usageEl) usageEl.textContent = Math.round(data.minutes_used);
                if (activeEl) activeEl.textContent = data.active_calls;
            })
            .catch(() => {});
    }

    if (usageEl || activeEl) {
        setInterval(poll, 15000);
    }

    // Auto-dismiss flash messages
    document.querySelectorAll('.flash').forEach(el => {
        setTimeout(() => {
            el.style.opacity = '0';
            el.style.transform = 'translateY(-8px)';
            el.style.transition = 'all 0.3s';
            setTimeout(() => el.remove(), 300);
        }, 5000);
    });
})();
