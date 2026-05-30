(function () {
    async function loadArchivedPlayerHistoryModal(seasonId, playerName) {
        const response = await fetch(`/archived-player-history/${seasonId}/${encodeURIComponent(playerName)}/`, {
            headers: {
                'X-Requested-With': 'XMLHttpRequest',
            },
        });

        if (!response.ok) {
            return;
        }

        const data = await response.json();

        let modalContainer = document.getElementById('archived-player-history-modal-container');
        if (!modalContainer) {
            modalContainer = document.createElement('div');
            modalContainer.id = 'archived-player-history-modal-container';
            document.body.appendChild(modalContainer);
        }

        modalContainer.innerHTML = data.html;

        const modalElement = document.getElementById('archivedPlayerHistoryModal');
        if (!modalElement || typeof bootstrap === 'undefined') {
            return;
        }

        const modal = new bootstrap.Modal(modalElement);
        modal.show();
    }

    document.addEventListener('click', function (event) {
        const trigger = event.target.closest('[data-archived-player-history-modal]');
        if (!trigger) {
            return;
        }

        event.preventDefault();
        const seasonId = trigger.dataset.seasonId;
        const playerName = trigger.dataset.playerName;

        loadArchivedPlayerHistoryModal(seasonId, playerName).catch(function () {
            // intentionally silent
        });
    });
})();
