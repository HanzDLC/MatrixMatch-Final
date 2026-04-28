document.addEventListener("DOMContentLoaded", () => {
    const searchInput = document.getElementById("historySearch");
    const table = document.getElementById("historyTable");

    if (!searchInput || !table) return;

    const rows = Array.from(table.querySelectorAll("tbody tr"));

    searchInput.addEventListener("input", () => {
        const q = searchInput.value.toLowerCase();

        rows.forEach(row => {
            const text = row.innerText.toLowerCase();
            row.style.display = text.includes(q) ? "" : "none";
        });
    });
});
