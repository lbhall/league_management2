(function () {
    async function loadMatchResultModal(matchId, parentModalElement) {
        const url = new URL(`/matches/${matchId}/result-modal/`, window.location.origin);

        const response = await fetch(url.toString(), {
            headers: {
                'X-Requested-With': 'XMLHttpRequest',
            },
        });

        if (!response.ok) {
            console.error('Unable to load match result modal.', response.status, response.statusText);
            reopenParent(parentModalElement);
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
            reopenParent(parentModalElement);
            return;
        }

        if (parentModalElement) {
            // Bring back whichever popup the match was opened from once this one closes.
            modalElement.addEventListener('hidden.bs.modal', function () {
                reopenParent(parentModalElement);
            }, { once: true });
        }

        const modal = new bootstrap.Modal(modalElement);
        modal.show();
    }

    function reopenParent(parentModalElement) {
        if (!parentModalElement || !document.body.contains(parentModalElement)) {
            return;
        }
        const body = parentModalElement.querySelector('.modal-body');
        const scrollTop = Number(parentModalElement.dataset.resumeScrollTop || 0);
        if (body && scrollTop) {
            parentModalElement.addEventListener('shown.bs.modal', function () {
                body.scrollTop = scrollTop;
            }, { once: true });
        }
        bootstrap.Modal.getOrCreateInstance(parentModalElement).show();
    }

    document.addEventListener('click', function (event) {
        const trigger = event.target.closest('[data-match-result-modal]');
        if (!trigger) {
            return;
        }

        event.preventDefault();
        event.stopPropagation();

        const matchId = trigger.getAttribute('data-match-id');
        if (!matchId) {
            return;
        }

        // Opened from inside another popup (full schedule, player page)?
        // Stacked popups hide behind each other, so close the parent first
        // and reopen it when the result popup is dismissed.
        const parentModalElement = trigger.closest('.modal.show');
        if (parentModalElement && typeof bootstrap !== 'undefined') {
            const body = parentModalElement.querySelector('.modal-body');
            parentModalElement.dataset.resumeScrollTop = body ? String(body.scrollTop) : '0';
            parentModalElement.addEventListener('hidden.bs.modal', function () {
                loadMatchResultModal(matchId, parentModalElement).catch(function () {
                    reopenParent(parentModalElement);
                });
            }, { once: true });
            bootstrap.Modal.getOrCreateInstance(parentModalElement).hide();
            return;
        }

        loadMatchResultModal(matchId, null).catch(function () {
            // intentionally silent
        });
    });
})();
