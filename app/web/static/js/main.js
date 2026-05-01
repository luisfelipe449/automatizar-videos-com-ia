// Main JavaScript for Video Automation Dashboard

// Auto-refresh dashboard stats every 30 seconds
if (window.location.pathname === '/') {
    setInterval(() => {
        fetch('/api/status')
            .then(response => response.json())
            .then(data => {
                // Update stat cards if they exist
                const pendingEl = document.querySelector('[data-stat="pending"]');
                const runningEl = document.querySelector('[data-stat="running"]');

                if (pendingEl && data.jobs.pending !== undefined) {
                    pendingEl.textContent = data.jobs.pending;
                }
                if (runningEl && data.jobs.running !== undefined) {
                    runningEl.textContent = data.jobs.running;
                }
            })
            .catch(error => console.error('Failed to refresh stats:', error));
    }, 30000);
}

// Form validation for account creation
const accountForm = document.querySelector('form[action="/accounts/new"]');
if (accountForm) {
    accountForm.addEventListener('submit', (e) => {
        const name = document.getElementById('name').value.trim();
        const email = document.getElementById('email').value.trim();
        const clientId = document.getElementById('client_id').value.trim();
        const clientSecret = document.getElementById('client_secret').value.trim();
        const refreshToken = document.getElementById('refresh_token').value.trim();

        if (!name || !email || !clientId || !clientSecret || !refreshToken) {
            e.preventDefault();
            alert('All fields marked with * are required');
            return false;
        }

        // Basic email validation
        const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
        if (!emailRegex.test(email)) {
            e.preventDefault();
            alert('Please enter a valid email address');
            return false;
        }
    });
}

// Close modal on background click
document.addEventListener('click', (e) => {
    if (e.target.classList.contains('modal-overlay')) {
        e.target.classList.add('hidden');
    }
});

// Escape key closes modals
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        const modals = document.querySelectorAll('.modal-overlay:not(.hidden)');
        modals.forEach(modal => modal.classList.add('hidden'));
    }
});

// Confirmation for destructive actions
document.querySelectorAll('form[action*="/toggle"]').forEach(form => {
    form.addEventListener('submit', (e) => {
        const action = form.querySelector('button').textContent.trim();
        if (!confirm(`Are you sure you want to ${action.toLowerCase()} this account?`)) {
            e.preventDefault();
        }
    });
});

// Auto-hide success messages after 5 seconds
document.addEventListener('DOMContentLoaded', () => {
    const successMessages = document.querySelectorAll('.alert-success');
    successMessages.forEach(msg => {
        setTimeout(() => {
            msg.style.opacity = '0';
            setTimeout(() => msg.remove(), 300);
        }, 5000);
    });
});
