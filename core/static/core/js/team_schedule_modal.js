(function () {
    async function loadTeamScheduleModal(teamId) {
        const url = new URL(`/teams/${teamId}/schedule-modal/`, window.location.origin);

        const response = await fetch(url.toString(), {
            headers: {
                'X-Requested-With': 'XMLHttpRequest',
            },
        });

        const data = await response.json();

        let modalContainer = document.getElementById('team-schedule-modal-container');
        if (!modalContainer) {
            modalContainer = document.createElement('div');
            modalContainer.id = 'team-schedule-modal-container';
            document.body.appendChild(modalContainer);
        }

        modalContainer.innerHTML = data.html;

        const modalElement = document.getElementById('teamScheduleModal');
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
            loadTeamScheduleModal(teamId);
        }
    });
})();