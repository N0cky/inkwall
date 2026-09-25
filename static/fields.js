/* Formularfelder aus der JSON-Beschreibung (/api/settings/<modul>) rendern,
   Werte einsammeln, Fehler anzeigen. Wird von Inhalte, Gerät und System genutzt. */
window.fields = (function () {
    'use strict';
    var esc = ui.esc;

    /* ── Dauer: Sekunden ↔ passende Einheit ── */
    function bestUnit(seconds) {
        var s = parseInt(seconds, 10);
        if (isNaN(s)) return { value: '', unit: 's' };
        if (s >= 3600 && s % 3600 === 0) return { value: s / 3600, unit: 'h' };
        if (s >= 60 && s % 60 === 0) return { value: s / 60, unit: 'min' };
        return { value: s, unit: 's' };
    }
    var UNIT_FACTOR = { s: 1, min: 60, h: 3600 };

    function renderField(f) {
        var id = 'f-' + f.name;
        var html = '';
        var attrs = ' data-field="' + esc(f.name) + '"';
        switch (f.type) {
            case 'select':
                // Gespeicherter Wert, den die Liste nicht (mehr) kennt: als eigene Option
                // behalten, sonst wählt der Browser die erste und Speichern löscht ihn
                var known = f.options.some(function (o) { return String(f.value) === String(o[0]); });
                html = '<select id="' + id + '"' + attrs + '>'
                    + (!known && f.value !== '' && f.value != null ? '<option value="' + esc(f.value) + '" selected>' + esc(f.value) + ' (nicht mehr in der Liste)</option>' : '')
                    + f.options.map(function (o) {
                        return '<option value="' + esc(o[0]) + '"' + (String(f.value) === String(o[0]) ? ' selected' : '') + '>' + esc(o[1]) + '</option>';
                    }).join('') + '</select>';
                break;
            case 'checkbox_group':
                html = '<div class="checks" id="' + id + '"' + attrs + '>' + f.options.map(function (o, i) {
                    return '<label class="check"><input type="checkbox" value="' + esc(o[0]) + '"' + (f.value.indexOf(o[0]) >= 0 ? ' checked' : '') + '><span>' + esc(o[1]) + '</span></label>';
                }).join('') + '</div>';
                break;
            case 'priority_list': {
                var order = f.value.slice();
                f.options.forEach(function (o) { if (order.indexOf(o[0]) < 0) order.push(o[0]); });
                var byVal = {}; f.options.forEach(function (o) { byVal[o[0]] = o[1]; });
                html = '<div class="prio" id="' + id + '"' + attrs + '>' + order.map(function (v) {
                    var name = byVal[v] || v;
                    return '<div class="prio-item" draggable="true" data-value="' + esc(v) + '"><span class="handle" aria-hidden="true">⠿</span>'
                        + '<label class="check"><input type="checkbox" value="' + esc(v) + '"' + (f.value.indexOf(v) >= 0 ? ' checked' : '') + '><span>' + esc(name) + '</span></label>'
                        + '<span class="rank"></span>' + moveButtons(name) + '</div>';
                }).join('') + '</div>';
                break;
            }
            case 'password':
                html = '<div class="pw"><input id="' + id + '" type="password" value="" autocomplete="off" spellcheck="false"'
                    + ' placeholder="' + esc(f.is_set ? 'Gesetzt – leer lassen zum Beibehalten' : (f.placeholder || '')) + '"' + attrs + '>'
                    + '<button type="button" class="btn small pw-toggle" aria-label="Anzeigen">👁</button></div>';
                break;
            case 'number':
                html = '<input id="' + id + '" type="number" value="' + esc(f.value) + '"' + (f.min != null ? ' min="' + f.min + '"' : '') + (f.max != null ? ' max="' + f.max + '"' : '')
                    + ' placeholder="' + esc(f.placeholder || '') + '"' + attrs + '>';
                break;
            case 'duration': {
                var d = bestUnit(f.value);
                html = '<div class="duration" id="' + id + '"' + attrs + '><input type="number" min="1" value="' + esc(d.value) + '" aria-label="' + esc(f.label) + '">'
                    + '<select aria-label="Einheit">' + ['s', 'min', 'h'].map(function (u) { return '<option value="' + u + '"' + (u === d.unit ? ' selected' : '') + '>' + (u === 's' ? 'Sekunden' : u === 'min' ? 'Minuten' : 'Stunden') + '</option>'; }).join('') + '</select></div>';
                break;
            }
            case 'list': {
                var cols = f.item_fields.length ? f.item_fields : [{ name: 'value', label: f.label }];
                html = '<div class="list" id="' + id + '"' + attrs + ' data-cols=\'' + esc(JSON.stringify(cols)) + '\'>'
                    + '<div class="list-rows">' + (f.value || []).map(function (item) { return listRow(cols, item); }).join('') + '</div>'
                    + '<button type="button" class="btn small list-add">+ Zeile</button></div>';
                break;
            }
            case 'mapping': {
                var mcols = f.item_fields.length ? f.item_fields : [{ name: 'key', label: 'Stichwort' }, { name: 'value', label: 'Wert' }];
                html = '<div class="list mapping" id="' + id + '"' + attrs + ' data-cols=\'' + esc(JSON.stringify(mcols)) + '\' data-value-options=\'' + esc(JSON.stringify(f.value_options || [])) + '\'>'
                    + '<div class="list-rows">' + (f.value || []).map(function (item) { return mappingRow(mcols, f.value_options || [], item); }).join('') + '</div>'
                    + '<button type="button" class="btn small list-add">+ Zeile</button></div>';
                break;
            }
            default:
                if (f.datalist_url) {
                    html = '<select id="' + id + '"' + attrs + ' data-datalist-url="' + esc(f.datalist_url) + '" data-current="' + esc(f.value) + '">'
                        + (f.value ? '<option value="' + esc(f.value) + '" selected>' + esc(f.value) + '</option>' : '<option value="">– Wird geladen … –</option>') + '</select>';
                } else {
                    html = '<input id="' + id + '" type="text" value="' + esc(f.value) + '" placeholder="' + esc(f.placeholder || '') + '" spellcheck="false" autocomplete="off"' + attrs + '>';
                }
        }
        var link = f.link_href ? '<a class="field-link" href="' + esc(f.link_href) + '" target="_blank" rel="noreferrer">→ ' + esc(f.link_label || f.link_href) + '</a>' : '';
        var note = f.link_note ? '<div class="hint">' + esc(f.link_note) + '</div>' : '';
        return '<div class="field' + (f.wide ? ' wide' : '') + '" data-name="' + esc(f.name) + '"' + (f.show_when ? ' data-show-when=\'' + esc(JSON.stringify(f.show_when)) + '\'' : '') + '>'
            + '<label class="field-label" for="' + id + '">' + esc(f.label) + '</label>' + html
            + (f.help ? '<div class="hint">' + esc(f.help) + '</div>' : '') + link + note
            + '<div class="field-error" data-error-for="' + esc(f.name) + '"></div></div>';
    }

    /* ↑/↓ zum Sortieren – Ziehen geht nur mit der Maus */
    function moveButtons(name) {
        return '<span class="move"><button type="button" class="btn small" data-move="-1" aria-label="' + esc(name) + ' nach oben">↑</button>'
            + '<button type="button" class="btn small" data-move="1" aria-label="' + esc(name) + ' nach unten">↓</button></span>';
    }

    function listRow(cols, item) {
        return '<div class="list-row" style="grid-template-columns:' + cols.map(function (c) { return c.wide ? '3fr' : '1fr'; }).join(' ') + ' auto">'
            + cols.map(function (c) { return '<input type="text" data-col="' + esc(c.name) + '" value="' + esc((item || {})[c.name] || '') + '" placeholder="' + esc(c.placeholder || c.label || '') + '" aria-label="' + esc(c.label || c.name) + '">'; }).join('')
            + '<button type="button" class="btn small list-del" aria-label="Zeile entfernen">✕</button></div>';
    }

    function mappingRow(cols, valueOptions, item) {
        var key = (item || {}).key || '', val = (item || {}).value || '';
        var valueHtml = valueOptions.length
            ? '<select data-col="value" aria-label="' + esc(cols[1].label || 'Wert') + '">' + valueOptions.map(function (o) { return '<option value="' + esc(o[0]) + '"' + (o[0] === val ? ' selected' : '') + '>' + esc(o[1]) + '</option>'; }).join('') + '</select>'
            : '<input type="text" data-col="value" value="' + esc(val) + '" placeholder="' + esc(cols[1].placeholder || '') + '">';
        return '<div class="list-row" style="grid-template-columns:1fr 1fr auto"><input type="text" data-col="key" value="' + esc(key) + '" placeholder="' + esc(cols[0].placeholder || cols[0].label || '') + '" aria-label="' + esc(cols[0].label || 'Stichwort') + '">'
            + valueHtml + '<button type="button" class="btn small list-del" aria-label="Zeile entfernen">✕</button></div>';
    }

    /* ── Verhalten nach dem Einfügen ── */
    function bind(container, onChange) {
        container.querySelectorAll('.pw-toggle').forEach(function (b) {
            b.addEventListener('click', function () { var inp = b.previousElementSibling; inp.type = inp.type === 'password' ? 'text' : 'password'; });
        });
        container.querySelectorAll('.list-add').forEach(function (b) {
            b.addEventListener('click', function () {
                var list = b.closest('.list'); var cols = JSON.parse(list.getAttribute('data-cols'));
                var rows = list.querySelector('.list-rows');
                rows.insertAdjacentHTML('beforeend', list.classList.contains('mapping') ? mappingRow(cols, JSON.parse(list.getAttribute('data-value-options') || '[]'), {}) : listRow(cols, {}));
                bindRowButtons(list, onChange); rows.lastElementChild.querySelector('input').focus(); onChange();
            });
        });
        container.querySelectorAll('.list').forEach(function (list) { bindRowButtons(list, onChange); });
        container.querySelectorAll('select[data-datalist-url]').forEach(loadDatalist);
        container.querySelectorAll('.prio').forEach(function (list) { bindPrio(list, onChange); });
        // Die Listener am Container nur einmal – bind() läuft nach jedem Neuaufbau des Inhalts erneut
        container._fieldsOnChange = onChange;
        if (!container._fieldsBound) {
            container._fieldsBound = true;
            container.addEventListener('input', function () { container._fieldsOnChange(); refreshConditional(container); });
            container.addEventListener('change', function () { container._fieldsOnChange(); refreshConditional(container); });
            // Enter in einem Feld schickt sonst das Formular ab und lädt die Seite neu – die Änderungen wären weg
            if (container.tagName === 'FORM') container.addEventListener('submit', function (e) { e.preventDefault(); if (container._fieldsOnSubmit) container._fieldsOnSubmit(); });
        }
        refreshConditional(container);
    }

    /* ── Speicherleiste: ein Baustein für alle Formulare ──
       bar: <div class="savebar"> mit [data-save], optional [data-discard] und <span class="state">.
       opts.save():    Promise – aufgelöst heißt gespeichert; abgelehnt heißt nicht gespeichert
                       (Fehler an den Feldern zeigt die Seite selbst, den Toast macht die Leiste).
       opts.discard(): stellt den zuletzt geladenen Stand wieder her; ohne discard bleibt „Verwerfen“ verborgen.
       Rückgabe: { markDirty, markSaved, reset, isDirty, edits, bind(form) } – bind() koppelt ein Formular an die Leiste
       (Enter im Formular speichert). edits() zählt jede Änderung: wer während des Speicherns weitertippt,
       bleibt „nicht gespeichert“ – die Seite darf dann den Serverstand nicht über das Formular legen. */
    var bars = [];
    function savebar(bar, opts) {
        var stateEl = bar.querySelector('.state'), saveBtn = bar.querySelector('[data-save]'), discardBtn = bar.querySelector('[data-discard]');
        var dirty = false, saving = false, edits = 0, saveLabel = saveBtn.textContent;
        stateEl.setAttribute('role', 'status');
        function set(text, isDirty) {
            dirty = isDirty; stateEl.textContent = text; bar.classList.toggle('dirty', isDirty);
            if (discardBtn) discardBtn.hidden = !(isDirty && opts.discard);
        }
        var api = {
            markDirty: function () { edits++; if (!dirty) set('Nicht gespeicherte Änderungen', true); },
            markSaved: function () { set('Gespeichert', false); },
            reset: function () { set('Keine Änderungen', false); },
            isDirty: function () { return dirty; },
            edits: function () { return edits; },
            bind: function (form) { form._fieldsOnSubmit = function () { if (dirty && !saving) saveBtn.click(); }; bind(form, api.markDirty); return api; }
        };
        saveBtn.addEventListener('click', async function () {
            if (saving) return;
            saving = true; saveBtn.disabled = true; saveBtn.textContent = 'Speichert …';
            var before = edits;
            try {
                await opts.save();
                if (edits === before) api.markSaved();
                else set('Gespeichert – danach Geändertes noch nicht', true);
            }
            catch (e) { ui.toast('Nicht gespeichert', 'error'); }
            saving = false; saveBtn.disabled = false; saveBtn.textContent = saveLabel;
        });
        if (discardBtn) discardBtn.addEventListener('click', function () { api.reset(); if (opts.discard) opts.discard(); });
        bars.push(api);
        return api;
    }
    // Eine Warnung beim Verlassen der Seite, sobald irgendeine Leiste ungespeicherte Änderungen hat
    window.addEventListener('beforeunload', function (e) {
        if (bars.some(function (b) { return b.isDirty(); })) { e.preventDefault(); e.returnValue = ''; }
    });

    /* Roter Kasten über dem Formular für Fehler, die keinem Feld zuzuordnen sind */
    function banner(el, lines) {
        if (!lines || !lines.length) { el.classList.remove('show'); el.innerHTML = ''; return; }
        el.innerHTML = 'Das konnte nicht gespeichert werden:<ul>' + lines.map(function (g) { return '<li>' + esc(g) + '</li>'; }).join('') + '</ul>';
        el.classList.add('show');
    }

    function bindRowButtons(list, onChange) {
        list.querySelectorAll('.list-del').forEach(function (b) {
            if (b.dataset.bound) return; b.dataset.bound = '1';
            b.addEventListener('click', function () { b.closest('.list-row').remove(); onChange(); });
        });
    }

    function loadDatalist(sel) {
        var current = sel.getAttribute('data-current') || '';
        fetch(sel.getAttribute('data-datalist-url')).then(function (r) { return r.ok ? r.json() : []; }).then(function (items) {
            sel.innerHTML = '<option value="">– Keine Auswahl –</option>' + items.map(function (n) { return '<option value="' + esc(n) + '"' + (n === current ? ' selected' : '') + '>' + esc(n) + '</option>'; }).join('');
            if (current && items.indexOf(current) < 0) sel.insertAdjacentHTML('afterbegin', '<option value="' + esc(current) + '" selected>' + esc(current) + ' ⚠</option>');
        }).catch(function () { sel.innerHTML = '<option value="' + esc(current) + '" selected>' + esc(current || '– Fehler beim Laden –') + '</option>'; });
    }

    function bindPrio(list, onChange) {
        var dragEl = null;
        function ranks() { var r = 1; list.querySelectorAll('.prio-item').forEach(function (it) { var cb = it.querySelector('input'); it.querySelector('.rank').textContent = cb.checked ? 'Prio ' + (r++) : ''; }); }
        list.querySelectorAll('.prio-item').forEach(function (item) {
            item.querySelectorAll('[data-move]').forEach(function (b) {
                b.addEventListener('click', function () {
                    var step = +b.getAttribute('data-move');
                    var other = step < 0 ? item.previousElementSibling : item.nextElementSibling;
                    if (!other) return;
                    if (step < 0) other.before(item); else other.after(item);
                    b.focus(); ranks(); onChange();
                });
            });
            item.addEventListener('dragstart', function (e) { dragEl = item; item.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', item.getAttribute('data-value')); });
            item.addEventListener('dragend', function () { item.classList.remove('dragging'); });
            item.addEventListener('dragover', function (e) { e.preventDefault(); });
            item.addEventListener('drop', function (e) {
                e.preventDefault(); if (!dragEl || dragEl === item) return;
                var items = Array.from(list.querySelectorAll('.prio-item'));
                if (items.indexOf(dragEl) < items.indexOf(item)) item.after(dragEl); else item.before(dragEl);
                ranks(); onChange();
            });
        });
        list.addEventListener('change', ranks); ranks();
    }

    function readValue(container, name) {
        var f = container.querySelector('.field[data-name="' + name + '"]'); if (!f) return [];
        var el = f.querySelector('[data-field]'); if (!el) return [];
        if (el.classList.contains('checks') || el.classList.contains('prio')) return Array.from(el.querySelectorAll('input:checked')).map(function (c) { return c.value; });
        return [el.value];
    }

    function refreshConditional(container) {
        container.querySelectorAll('.field[data-show-when]').forEach(function (f) {
            var rules = JSON.parse(f.getAttribute('data-show-when'));
            var show = Object.keys(rules).every(function (n) { return readValue(container, n).indexOf(String(rules[n])) >= 0; });
            f.classList.toggle('is-hidden', !show);
        });
    }

    /* ── Werte einsammeln → {name: value} ── */
    function collect(container) {
        var out = {};
        container.querySelectorAll('[data-field]').forEach(function (el) {
            var name = el.getAttribute('data-field');
            if (el.classList.contains('checks') || el.classList.contains('prio')) {
                out[name] = Array.from(el.querySelectorAll('input:checked')).map(function (c) { return c.value; });
                if (el.classList.contains('prio')) out[name] = Array.from(el.querySelectorAll('.prio-item')).filter(function (it) { return it.querySelector('input').checked; }).map(function (it) { return it.getAttribute('data-value'); });
            } else if (el.classList.contains('duration')) {
                var n = parseFloat(el.querySelector('input').value); var u = el.querySelector('select').value;
                out[name] = isNaN(n) ? '' : String(Math.round(n * UNIT_FACTOR[u]));
            } else if (el.classList.contains('list')) {
                out[name] = Array.from(el.querySelectorAll('.list-row')).map(function (row) {
                    var item = {}; row.querySelectorAll('[data-col]').forEach(function (inp) { item[inp.getAttribute('data-col')] = inp.value; }); return item;
                });
            } else {
                out[name] = el.value;
            }
        });
        return out;
    }

    function showErrors(container, errors) {
        clearErrors(container);
        var f = (errors && errors.fields) || {}, general = (errors && errors.general) || [];
        Object.keys(f).forEach(function (name) {
            var el = container.querySelector('[data-error-for="' + name + '"]');
            if (el) {
                el.textContent = f[name];
                var field = el.closest('.field'); if (field) field.classList.add('has-error');
                // Screenreader: das Feld ist ungültig, und die Meldung gehört zu ihm
                var control = container.querySelector('#f-' + CSS.escape(name));
                if (control) { el.id = 'e-' + name; control.setAttribute('aria-invalid', 'true'); control.setAttribute('aria-describedby', el.id); }
            }
            else general.push(name + ': ' + f[name]);
        });
        return general;
    }

    function clearErrors(container) {
        container.querySelectorAll('.field-error').forEach(function (el) { el.textContent = ''; });
        container.querySelectorAll('.has-error').forEach(function (el) { el.classList.remove('has-error'); });
        container.querySelectorAll('[aria-invalid]').forEach(function (el) { el.removeAttribute('aria-invalid'); el.removeAttribute('aria-describedby'); });
    }

    /* ── Felder nach Gruppen rendern ── */
    function renderGroups(fields, groups, filter) {
        var visible = fields.filter(filter || function () { return true; });
        var rendered = {};
        var html = '';
        (groups || []).forEach(function (g) {
            var inGroup = visible.filter(function (f) { return g.fields.indexOf(f.name) >= 0; });
            if (!inGroup.length) return;
            inGroup.forEach(function (f) { rendered[f.name] = true; });
            html += '<div class="group"><div class="group-title">' + esc(g.title) + '</div>' + (g.desc ? '<div class="hint group-desc">' + esc(g.desc) + '</div>' : '')
                + '<div class="grid">' + inGroup.map(renderField).join('') + '</div></div>';
        });
        var rest = visible.filter(function (f) { return !rendered[f.name]; });
        if (rest.length) html += '<div class="group"><div class="grid">' + rest.map(renderField).join('') + '</div></div>';
        return html;
    }

    return { renderField: renderField, renderGroups: renderGroups, bind: bind, collect: collect, showErrors: showErrors, clearErrors: clearErrors, bestUnit: bestUnit,
             savebar: savebar, banner: banner };
})();
