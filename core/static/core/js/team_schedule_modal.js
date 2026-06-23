(function () {
    async function loadTeamScheduleModal(teamId) {
        const url = new URL(`/teams/${teamId}/schedule-modal/`, window.location.origin);

        const response = await fetch(url.toString(), {
            headers: {
                'X-Requested-With': 'XMLHttpRequest',
            },
        });

        if (!response.ok) {
            console.error('Unable to load team schedule modal.', response.status, response.statusText);
            return;
        }

        const data = await response.json();

        let modalContainer = document.getElementById('team-schedule-modal-container');
        if (!modalContainer) {
            modalContainer = document.createElement('div');
            modalContainer.id = 'team-schedule-modal-container';
            document.body.appendChild(modalContainer);
        }

        modalContainer.innerHTML = data.html;

        const modalElement = document.getElementById('teamScheduleModal');
        if (!modalElement || typeof bootstrap === 'undefined') {
            console.error('Team schedule modal could not be initialized.');
            return;
        }

        const modal = new bootstrap.Modal(modalElement);
        modal.show();
    }

    document.addEventListener('click', function (event) {
        const trigger = event.target.closest('[data-team-schedule-modal]');
        if (!trigger) {
            return;
        }

        event.preventDefault();

        const teamId = trigger.getAttribute('data-team-id');
        if (teamId) {
            loadTeamScheduleModal(teamId).catch(function () {
                // intentionally silent
            });
        }
    });
})();