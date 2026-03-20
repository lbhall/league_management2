(function () {
    function updateRow(row) {
        const winsInput = row.querySelector('input[name$="-wins"]');
        const lossesInput = row.querySelector('input[name$="-losses"]');
        const sweepInput = row.querySelector('input[name$="-won_all_games"]');
        const teamSizeInput = document.getElementById('result-team-size');

        if (!winsInput || !lossesInput || !sweepInput || !teamSizeInput) {
            return;
        }

        const teamSize = parseInt(teamSizeInput.value || '0', 10);
        const wins = parseInt(winsInput.value || '0', 10);
        const losses = Math.max(teamSize - wins, 0);

        lossesInput.value = losses;
        sweepInput.checked = losses === 0;
    }

    function bindRows() {
        document.querySelectorAll('.dynamic-player_results').forEach(function (row) {
            const winsInput = row.querySelector('input[name$="-wins"]');
            if (!winsInput || winsInput.dataset.bound === '1') {
                return;
            }

            winsInput.addEventListener('input', function () {
                updateRow(row);
            });
            winsInput.dataset.bound = '1';

            updateRow(row);
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        bindRows();

        document.body.addEventListener('click', function () {
            setTimeout(bindRows, 100);
        });
    });
})();