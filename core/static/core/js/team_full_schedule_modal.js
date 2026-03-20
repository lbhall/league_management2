(function () {
    async function loadOnePocketFullScheduleModal() {
        const response = await fetch('/one-pocket/full-schedule-modal/', {
            headers: {
                'X-Requested-With': 'XMLHttpRequest',
            },
        });

        if (!response.ok) {
            return;
        }

        const data = await response.json();

        let modalContainer = document.getElementById('team-full-schedule-modal-container');
        if (!modalContainer) {
            modalContainer = document.createElement('div');
            modalContainer.id = 'team-full-schedule-modal-container';
            document.body.appendChild(modalContainer);
        }

        modalContainer.innerHTML = data.html;

        const modalElement = document.getElementById('teamFullScheduleModal');
        if (!modalElement || typeof bootstrap === 'undefined') {
            return;
        }

        const modal = new bootstrap.Modal(modalElement);
        modal.show();
    }

    document.addEventListener('click', function (event) {
        const trigger = event.target.closest('[data-one-pocket-full-schedule-modal]');
        if (!trigger) {
            return;
        }

        event.preventDefault();
        loadOnePocketFullScheduleModal().catch(function () {
            // intentionally silent
        });
    });
})();