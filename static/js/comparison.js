document.addEventListener("DOMContentLoaded", () => {
    const keywordInput = document.getElementById("keywordInput");
    const keywordList = document.getElementById("keywordList");
    const keywordsHidden = document.getElementById("keywordsHidden");
    const keywordCountText = document.getElementById("keywordCountText");
    const form = document.getElementById("comparisonForm");
    const threshold = document.getElementById("threshold");
    const thresholdValue = document.getElementById("thresholdValue");

    let keywords = [];

    function renderKeywords() {
        keywordList.innerHTML = "";
        keywords.forEach((kw, index) => {
            const chip = document.createElement("div");
            chip.className = "chip";
            chip.textContent = kw;

            const removeBtn = document.createElement("button");
            removeBtn.type = "button";
            removeBtn.className = "chip__remove";
            removeBtn.innerHTML = "×";
            removeBtn.addEventListener("click", () => {
                keywords.splice(index, 1);
                renderKeywords();
            });

            chip.appendChild(removeBtn);
            keywordList.appendChild(chip);
        });

        keywordsHidden.value = JSON.stringify(keywords);
        keywordCountText.textContent = `${keywords.length} keyword${keywords.length !== 1 ? "s" : ""} added.`;
    }

    if (keywordInput) {
        keywordInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                e.preventDefault();
                const value = keywordInput.value.trim();
                if (value && !keywords.includes(value)) {
                    keywords.push(value);
                    keywordInput.value = "";
                    renderKeywords();
                }
            }
        });
    }

    if (form) {
        form.addEventListener("submit", (e) => {
            if (keywords.length < 5) {
                e.preventDefault();
                alert("Please add at least 5 keywords before running comparison.");
            }
        });
    }

    if (threshold && thresholdValue) {
        const updateThresholdText = () => {
            thresholdValue.textContent = `${threshold.value}%`;
        };
        threshold.addEventListener("input", updateThresholdText);
        updateThresholdText();
    }
});
