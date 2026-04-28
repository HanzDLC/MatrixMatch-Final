// Reusable chip-input widget.
//
// Each usage needs four elements:
//   - wrapper   : outer div with class "chip-input-container" (click focuses input)
//   - listEl    : inner div that holds rendered chip elements
//   - textInput : <input type="text"> where the user types
//   - hiddenInput : <input type="hidden"> that receives the JSON array on every change
//   - counterEl : (optional) element whose textContent is updated with the count
//
// Accepts an optional `initial` array to pre-populate chips (used on the edit page).
//
// Exposed as window.initChipInput so individual templates can call it from an
// inline <script> without the module system.

(function () {
    function initChipInput(opts) {
        const wrapper = typeof opts.wrapper === 'string'
            ? document.getElementById(opts.wrapper) : opts.wrapper;
        const listEl = typeof opts.listEl === 'string'
            ? document.getElementById(opts.listEl) : opts.listEl;
        const textInput = typeof opts.textInput === 'string'
            ? document.getElementById(opts.textInput) : opts.textInput;
        const hiddenInput = typeof opts.hiddenInput === 'string'
            ? document.getElementById(opts.hiddenInput) : opts.hiddenInput;
        const counterEl = opts.counterEl && (typeof opts.counterEl === 'string'
            ? document.getElementById(opts.counterEl) : opts.counterEl);

        if (!wrapper || !listEl || !textInput || !hiddenInput) return null;

        const initial = Array.isArray(opts.initial) ? opts.initial.slice() : [];
        const label = opts.label || 'item';

        let chips = initial
            .map((s) => String(s).trim())
            .filter(Boolean);

        function render() {
            listEl.innerHTML = '';
            chips.forEach((value, index) => {
                const chip = document.createElement('div');
                chip.className = 'chip';
                chip.textContent = value;

                const removeBtn = document.createElement('button');
                removeBtn.type = 'button';
                removeBtn.className = 'chip__remove';
                removeBtn.innerHTML = '&times;';
                removeBtn.setAttribute('aria-label', `Remove ${value}`);
                removeBtn.addEventListener('click', () => {
                    chips.splice(index, 1);
                    render();
                });

                chip.appendChild(removeBtn);
                listEl.appendChild(chip);
            });

            hiddenInput.value = JSON.stringify(chips);

            if (counterEl) {
                const plural = chips.length === 1 ? '' : 's';
                counterEl.textContent = `${chips.length} ${label}${plural} added.`;
            }
        }

        function addChip(value) {
            const v = value.trim();
            if (!v) return;
            if (chips.includes(v)) return;
            chips.push(v);
            textInput.value = '';
            render();
        }

        textInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ',') {
                e.preventDefault();
                addChip(textInput.value);
            } else if (e.key === 'Backspace' && !textInput.value && chips.length) {
                chips.pop();
                render();
            }
        });

        textInput.addEventListener('blur', () => {
            if (textInput.value.trim()) addChip(textInput.value);
        });

        wrapper.addEventListener('click', (e) => {
            if (e.target === wrapper || e.target === listEl) textInput.focus();
        });

        render();

        return {
            getChips: () => chips.slice(),
            clear: () => { chips = []; render(); },
        };
    }

    window.initChipInput = initChipInput;
})();
