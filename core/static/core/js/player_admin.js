(function ($) {
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

    function updateLeagueDependentTeams() {
        const leagueField = document.getElementById('id_league');
        const teamField = document.getElementById('id_team');

        if (!leagueField || !teamField) {
            return;
        }

        const leagueId = leagueField.value;
        const selectedTeam = teamField.value;

        if (!leagueId) {
            buildOptions(teamField, [], '---------', null);
            return;
        }

        const url = getBaseAdminUrl() + 'league-teams/?league_id=' + encodeURIComponent(leagueId);

        fetch(url, {
            headers: {
                'X-Requested-With': 'XMLHttpRequest',
            },
        })
            .then(function (response) {
                return response.json();
            })
            .then(function (data) {
                buildOptions(teamField, data.teams, '---------', selectedTeam);
            });
    }

    document.addEventListener('DOMContentLoaded', function () {
        const leagueField = document.getElementById('id_league');

        if (!leagueField) {
            return;
        }

        leagueField.addEventListener('change', updateLeagueDependentTeams);
        updateLeagueDependentTeams();
    });
})(django.jQuery);