(function () {
    async function loadPlayerScoresModal(playerId, weekId) {
        const url = new URL(`/players/${playerId}/scores-modal/`, window.location.origin);

        if (weekId) {
            url.searchParams.set('week', weekId);
        }

        const response = await fetch(url.toString(), {
            headers: {
                'X-Requested-With': 'XMLHttpRequest',
            },
        });

        const data = await response.json();

        let modalContainer = document.getElementById('player-scores-modal-container');
        if (!modalContainer) {
            modalContainer = document.createElement('div');
            modalContainer.id = 'player-scores-modal-container';
            document.body.appendChild(modalContainer);
        }

        modalContainer.innerHTML = data.html;

        const modalElement = document.getElementById('playerScoresModal');
        const modal = new bootstrap.Modal(modalElement);
        modal.show();
    }

    document.addEventListener('click', function (event) {
        const trigger = event.target.closest('[data-player-scores-modal]');
        if (!trigger) {
            return;
        }

        event.preventDefault();

        const playerId = trigger.getAttribute('data-player-id');
        const weekId = trigger.getAttribute('data-week-id');

        if (playerId) {
            loadPlayerScoresModal(playerId, weekId);
        }
    });
})();