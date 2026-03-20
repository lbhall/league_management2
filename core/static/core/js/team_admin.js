(function () {
    function buildOptions(selectElement, items, emptyLabel, selectedValue) {
        selectElement.innerHTML = '';

        const emptyOption = document.createElement('option');
        emptyOption.value = '';
        emptyOption.textContent = emptyLabel;
        selectElement.appendChild(emptyOption);

        items.forEach(function (item) {
            const option = document.createElement('option');
            option.value = item.id;
            option.textContent = item.name;

            if (selectedValue && String(selectedValue) === String(item.id)) {
                option.selected = true;
            }

            selectElement.appendChild(option);
        });
    }

    function getBaseAdminUrl() {
        return window.location.pathname.replace(/add\/$|[0-9]+\/change\/$/, '');
    }

    function getTeamId() {
        const match = window.location.pathname.match(/\/(\d+)\/change\/$/);
        return match ? match[1] : null;
    }

    function updateLeagueDependentFields() {
        const leagueField = document.getElementById('id_league');
        const venueField = document.getElementById('id_venue');
        const captainField = document.getElementById('id_captain');

        if (!leagueField || !venueField || !captainField) {
            return;
        }

        const leagueId = leagueField.value;
        const teamId = getTeamId();
        const selectedVenue = venueField.value;
        const selectedCaptain = captainField.value;

        if (!leagueId) {
            buildOptions(venueField, [], '---------', null);
            buildOptions(captainField, [], '---------', null);
            return;
        }

        let url = getBaseAdminUrl() + 'league-options/?league_id=' + encodeURIComponent(leagueId);

        if (teamId) {
            url += '&team_id=' + encodeURIComponent(teamId);
        }

        fetch(url, {
            headers: {
                'X-Requested-With': 'XMLHttpRequest',
            },
        })
            .then(function (response) {
                return response.json();
            })
            .then(function (data) {
                buildOptions(venueField, data.venues, '---------', selectedVenue);
                buildOptions(captainField, data.captains, '---------', selectedCaptain);
            });
    }

    document.addEventListener('DOMContentLoaded', function () {
        const leagueField = document.getElementById('id_league');

        if (!leagueField) {
            return;
        }

        leagueField.addEventListener('change', updateLeagueDependentFields);
        updateLeagueDependentFields();
    });
})();