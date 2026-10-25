(function () {
    async function loadMakeupMatchesModal() {
        const response = await fetch('/one-pocket/makeup-matches-modal/', {
            headers: {
                'X-Requested-With': 'XMLHttpRequest',
            },
        });

        if (!response.ok) {
            return;
        }

        const data = await response.json();

        let modalContainer = document.getElementById('makeup-matches-modal-container');
        if (!modalContainer) {
            modalContainer = document.createElement('div');
            modalContainer.id = 'makeup-matches-modal-container';
            document.body.appendChild(modalContainer);
        }

        modalContainer.innerHTML = data.html;

        const modalElement = document.getElementById('makeupMatchesModal');
        if (!modalElement || typeof bootstrap === 'undefined') {
            return;
        }

        const modal = new bootstrap.Modal(modalElement);
        modal.show();
    }

    document.addEventListener('click', function (event) {
        const trigger = event.target.closest('[data-one-pocket-makeup-matches-modal]');
        if (!trigger) {
            return;
        }

        event.preventDefault();
        loadMakeupMatchesModal().catch(function () {
            // intentionally silent
        });
    });
})();
