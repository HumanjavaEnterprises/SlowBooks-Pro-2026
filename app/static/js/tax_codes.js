/**
 * Tax codes (#456): a document's tax as one or more components.
 *
 * On every tax-bearing form: a Tax Code picker beside the Tax Rate box. With
 * a code chosen, the rate box shows the code's combined rate and is not
 * typed in, and the form's totals tax each component and round it on its
 * own, as the server does (so 5% + 7% of $10.10 is $1.22, not $1.21). With
 * none, the form is exactly what it was: one rate, typed.
 *
 * A new document is offered the current codes; a document that already has
 * an archived one keeps it on its picker, so editing it never changes its
 * code by accident. With no codes set up, there is no picker at all.
 *
 * Settings → Tax Codes adds, renames, archives and deletes them; a code's
 * rates and accounts change only while no document uses it.
 */
const TaxCodes = {
    _codes: [],

    // Every code, archived included: the picker filters. Returns the list.
    async load() {
        try { TaxCodes._codes = await API.get('/tax-codes?include_archived=true'); }
        catch (e) { TaxCodes._codes = []; }
        return TaxCodes._codes;
    },

    byId(id) {
        return TaxCodes._codes.find(c => String(c.id) === String(id)) || null;
    },

    // The code's combined rate as the percent the rate box shows ("7.25").
    percent(code) {
        return +(parseFloat(code.rate || 0) * 100).toFixed(4);
    },

    label(code) {
        return `${code.name} (${TaxCodes.percent(code)}%)${code.is_archived ? ' (archived)' : ''}`;
    },

    // The picker's form group, or '' when the company has no codes. Call
    // after load(). `onchange` is the page's recalc (a string of JS).
    groupHtml(selectedId, onchange) {
        const offered = TaxCodes._codes.filter(c => !c.is_archived || String(c.id) === String(selectedId));
        if (!offered.length) return '';
        const opts = offered.map(c =>
            `<option value="${c.id}" ${String(c.id) === String(selectedId) ? 'selected' : ''}>${escapeHtml(TaxCodes.label(c))}</option>`
        ).join('');
        return `<div class="form-group"><label>Tax Code</label>
            <select name="tax_code_id" onchange="TaxCodes.sync(this.form); ${onchange}">
                <option value="">None (the rate typed)</option>${opts}
            </select></div>`;
    },

    // The code chosen on `form`, or null.
    selected(form) {
        const sel = form && form.querySelector('[name="tax_code_id"]');
        return sel && sel.value ? TaxCodes.byId(sel.value) : null;
    },

    // With a code chosen, the rate box shows its rate and is read-only.
    sync(form) {
        const code = TaxCodes.selected(form);
        const rate = form && form.querySelector('[name="tax_rate"]');
        if (!rate) return;
        if (code) {
            rate.value = TaxCodes.percent(code);
            rate.readOnly = true;
            rate.title = 'The rate of the tax code chosen';
        } else {
            rate.readOnly = false;
            rate.title = '';
        }
    },

    // What SalesLines.totals takes for the tax: the code's component
    // percents (each rounded on its own), else the rate typed.
    taxPct(form, typed) {
        TaxCodes.sync(form);
        const code = TaxCodes.selected(form);
        if (!code) return typed;
        return code.components.map(c => +(parseFloat(c.rate || 0) * 100).toFixed(4));
    },

    // The payload field: the code's id, or null for none.
    payload(form) {
        const code = TaxCodes.selected(form);
        return { tax_code_id: code ? code.id : null };
    },
};
