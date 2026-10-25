(function () {
    async function loadMatchResultModal(matchId) {
        const url = new URL(`/matches/${matchId}/result-modal/`, window.location.origin);

        const response = await fetch(url.toString(), {
            headers: {
                'X-Requested-With': 'XMLHttpRequest',
            },
        });

        if (!response.ok) {
            console.error('Unable to load match result modal.', response.status, response.statusText);
            return;
        }

        const data = await response.json();

        let modalContainer = document.getElementById('match-result-modal-container');
        if (!modalContainer) {
            modalContainer = document.createElement('div');
            modalContainer.id = 'match-result-modal-container';
            document.body.appendChild(modalContainer);
        }

        modalContainer.innerHTML = data.html;

        const modalElement = document.getElementById('matchResultModal');
        if (!modalElement || typeof bootstrap === 'undefined') {
            console.error('Match result modal could not be initialized.');
            return;
        }

        const modal = new bootstrap.Modal(modalElement);
        modal.show();
    }

    document.addEventListener('click', function (event) {
        const trigger = event.target.closest('[data-match-result-modal]');
        if (!trigger) {
            return;
        }

        event.preventDefault();
        event.stopPropagation();

        const matchId = trigger.getAttribute('data-match-id');
        if (matchId) {
            loadMatchResultModal(matchId).catch(function () {
                // intentionally silent
            });
        }
    });
})();
