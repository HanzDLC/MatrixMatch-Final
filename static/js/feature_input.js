// Reusable feature-card input widget.
//
// Each feature has a short label (chip-like) and a 1-2 sentence description.
// Renders a list of cards inside listEl; each card has a label input,
// description textarea, and a remove button. An "Add feature" button appends
// a blank card. The current array is serialized as JSON into hiddenInput.
//
// Required option keys (all element IDs or DOM nodes):
//   wrapper      : outer container (gets focus styling on focus-within)
//   listEl       : container where cards are rendered
//   hiddenInput  : <input type="hidden"> that receives the JSON array
//   addBtn       : button that appends a blank card on click
//   counterEl    : (optional) element whose textContent reflects the count
//   initial      : (optional) array of {label, description} to pre-populate
//   label        : (optional) singular noun for the counter ("feature" by default)
//
// Exposed as window.initFeatureInput so templates can call it inline.

(function () {
    function initFeatureInput(opts) {
        function $(x) { return typeof x === 'string' ? document.getElementById(x) : x; }

        const wrapper = $(opts.wrapper);
        const listEl = $(opts.listEl);
        const hiddenInput = $(opts.hiddenInput);
        const addBtn = $(opts.addBtn);
        const counterEl = opts.counterEl ? $(opts.counterEl) : null;

        if (!wrapper || !listEl || !hiddenInput || !addBtn) return null;

        const noun = opts.label || 'feature';

        // Each entry: { label: string, description: string }.
        // We don't filter empty entries while editing — that would surprise
        // the user mid-typing — but we do strip them when serializing.
        let entries = (Array.isArray(opts.initial) ? opts.initial : []).map(function (e) {
            if (e && typeof e === 'object') {
                return {
                    label: String(e.label || '').trim(),
                    description: String(e.description || '').trim(),
                };
            }
            return { label: String(e || '').trim(), description: '' };
        });

        function serialize() {
            const clean = entries
                .map(function (e) {
                    return {
                        label: (e.label || '').trim(),
                        description: (e.description || '').trim(),
                    };
                })
                .filter(function (e) { return e.label && e.description; });
            hiddenInput.value = JSON.stringify(clean);
            if (counterEl) {
                const n = clean.length;
                counterEl.textContent = n + ' ' + noun + (n === 1 ? '' : 's') + ' added.';
            }
        }

        function render() {
            listEl.innerHTML = '';
            entries.forEach(function (entry, idx) {
                const card = document.createElement('div');
                card.className = 'feature-card';

                const labelInput = document.createElement('input');
                labelInput.type = 'text';
                labelInput.className = 'feature-card__label form-input';
                labelInput.placeholder = 'Short label (e.g. QR code scanning)';
                labelInput.maxLength = 80;
                labelInput.value = entry.label || '';
                labelInput.addEventListener('input', function () {
                    entries[idx].label = labelInput.value;
                    serialize();
                });

                const descInput = document.createElement('textarea');
                descInput.className = 'feature-card__desc form-textarea';
                descInput.rows = 2;
                descInput.placeholder = 'What does this feature let a user do? '
                    + '(e.g. "Passenger pays the fare using e-wallets like GCash or Maya.")';
                descInput.value = entry.description || '';
                descInput.addEventListener('input', function () {
                    entries[idx].description = descInput.value;
                    serialize();
                });

                const removeBtn = document.createElement('button');
                removeBtn.type = 'button';
                removeBtn.className = 'feature-card__remove btn btn-outline';
                removeBtn.setAttribute('aria-label', 'Remove this feature');
                removeBtn.textContent = '✕';
                removeBtn.addEventListener('click', function () {
                    entries.splice(idx, 1);
                    render();
                });

                card.appendChild(labelInput);
                card.appendChild(descInput);
                card.appendChild(removeBtn);
                listEl.appendChild(card);
            });
            serialize();
        }

        function addBlank() {
            entries.push({ label: '', description: '' });
            render();
            // Focus the new card's label input
            const cards = listEl.querySelectorAll('.feature-card__label');
            if (cards.length) cards[cards.length - 1].focus();
        }

        addBtn.addEventListener('click', function (e) {
            e.preventDefault();
            addBlank();
        });

        render();

        return {
            getFeatures: function () {
                return entries
                    .map(function (e) {
                        return {
                            label: (e.label || '').trim(),
                            description: (e.description || '').trim(),
                        };
                    })
                    .filter(function (e) { return e.label && e.description; });
            },
            getRawEntries: function () {
                return entries.slice();
            },
            // Find first entry with empty label OR description, focus it,
            // and return its zero-based index. Returns -1 if all complete.
            focusFirstIncomplete: function () {
                for (let i = 0; i < entries.length; i++) {
                    const e = entries[i];
                    if (!(e.label || '').trim()) {
                        const labels = listEl.querySelectorAll('.feature-card__label');
                        if (labels[i]) labels[i].focus();
                        return i;
                    }
                    if (!(e.description || '').trim()) {
                        const descs = listEl.querySelectorAll('.feature-card__desc');
                        if (descs[i]) descs[i].focus();
                        return i;
                    }
                }
                return -1;
            },
            clear: function () { entries = []; render(); },
        };
    }

    window.initFeatureInput = initFeatureInput;
})();
