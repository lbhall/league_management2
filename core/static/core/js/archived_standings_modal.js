(function () {
    const MODAL_ENDPOINT = '/archived-standings-modal/';
    const MODAL_CONTAINER_ID = 'archived-standings-modal-container';
    const MODAL_ELEMENT_ID = 'archivedStandingsModal';
    const CONTENT_WRAPPER_ID = 'archived-standings-content';

    function buildUrl(seasonId) {
        const url = new URL(MODAL_ENDPOINT, window.location.origin);
        if (seasonId) {
            url.searchParams.set('season', seasonId);
        }
        return url.toString();
    }

    async function fetchModalHtml(seasonId) {
        const response = await fetch(buildUrl(seasonId), {
            headers: {'X-Requested-With': 'XMLHttpRequest'},
        });
        const data = await response.json();
        return data.html;
    }

    async function openModal() {
        const html = await fetchModalHtml(null);

        let container = document.getElementById(MODAL_CONTAINER_ID);
        if (!container) {
            container = document.createElement('div');
            container.id = MODAL_CONTAINER_ID;
            document.body.appendChild(container);
        }
        container.innerHTML = html;

        const modalElement = document.getElementById(MODAL_ELEMENT_ID);
        const modal = new bootstrap.Modal(modalElement);
        modal.show();
    }

    async function refreshContent(seasonId) {
        const html = await fetchModalHtml(seasonId);

        const parser = new DOMParser();
        const doc = parser.parseFromString(html, 'text/html');
        const newContent = doc.getElementById(CONTENT_WRAPPER_ID);
        const currentContent = document.getElementById(CONTENT_WRAPPER_ID);

        if (newContent && currentContent) {
            currentContent.innerHTML = newContent.innerHTML;
        }
    }

    document.addEventListener('click', function (event) {
        const trigger = event.target.closest('[data-archived-standings-modal]');
        if (!trigger) {
            return;
        }
        event.preventDefault();
        openModal();
    });

    document.addEventListener('change', function (event) {
        const select = event.target.closest('[data-archived-season-select]');
        if (!select) {
            return;
        }
        refreshContent(select.value);
    });
})();
