/**
 * Tax codes (#456): a document's tax as one or more components.
 *
 * On every tax-bearing form: a Tax Code picker beside the Tax Rate box. With
 * a code chosen, the rate box shows the code's combined rate and is not
 * typed in, and the form's totals tax each component and round it on its
 * own, as the server does (so 5% + 7% of $10.10 is $1.22, not $1.21). With
 * none, the form is exactly what it was: one rate, typed. The bill form has
 * no rate box: its picker sits with the header fields, and its totals show
 * the code's tax and the part of it a recoverable component claims back.
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
    // `noneLabel` names the no-code choice on a form with no rate to type
    // (a bill keeps the rate it has).
    groupHtml(selectedId, onchange, noneLabel = 'None (the rate typed)') {
        const offered = TaxCodes._codes.filter(c => !c.is_archived || String(c.id) === String(selectedId));
        if (!offered.length) return '';
        const opts = offered.map(c => TaxCodes._optionHtml(c, String(c.id) === String(selectedId))).join('');
        return `<div class="form-group"><label>Tax Code</label>
            <select name="tax_code_id" onchange="TaxCodes.sync(this.form); ${onchange}">
                <option value="">${escapeHtml(noneLabel)}</option>${opts}
            </select></div>`;
    },

    _optionHtml(code, selected) {
        return `<option value="${code.id}" ${selected ? 'selected' : ''}>${escapeHtml(TaxCodes.label(code))}</option>`;
    },

    // Put `id` on the form's picker (null for none), as picking another
    // document does: a credit memo takes the code of the invoice it
    // credits. That code may be archived, so it is added to the picker if
    // the picker does not offer it, and the picker itself is added, before
    // the Tax Rate box, when the form opened with nothing to offer.
    choose(form, id, onchange) {
        if (!form) return;
        let sel = form.querySelector('[name="tax_code_id"]');
        const code = id != null ? TaxCodes.byId(id) : null;
        if (!sel && code) {
            const box = form.querySelector('[name="tax_rate"]');
            const group = box && box.closest ? box.closest('.form-group') : null;
            if (group) group.insertAdjacentHTML('beforebegin', TaxCodes.groupHtml(code.id, onchange));
            sel = form.querySelector('[name="tax_code_id"]');
        }
        if (sel) {
            if (code && ![...sel.options].some(o => String(o.value) === String(code.id))) {
                sel.insertAdjacentHTML('beforeend', TaxCodes._optionHtml(code, false));
            }
            sel.value = code ? String(code.id) : '';
        }
        TaxCodes.sync(form);
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

    // Each component's tax on `base` for the code chosen on `form`, rounded
    // on its own as the server posts it ([{component, amount}]), or null
    // when no code is chosen.
    amounts(form, base) {
        const code = TaxCodes.selected(form);
        if (!code) return null;
        return code.components.map(c => ({
            component: c,
            amount: SalesLines.tax(SalesLines.cents(base), +(parseFloat(c.rate || 0) * 100).toFixed(4)),
        }));
    },

    // The part of a purchase's tax that is claimed back, for the code chosen
    // on `form`: what posts to the recoverable accounts, not to the lines.
    recoverable(form, base) {
        const parts = (TaxCodes.amounts(form, base) || [])
            .filter(p => p.component.is_recoverable && p.component.recoverable_account_id);
        return {
            amount: SalesLines.cents(parts.reduce((sum, p) => sum + p.amount, 0)),
            names: parts.map(p => p.component.name),
        };
    },

    // The payload field: the code's id, or null for none. A form with no
    // picker (no codes, or they could not be read) sends nothing, so an
    // edit keeps the code its document has rather than clearing it.
    payload(form) {
        if (!form || !form.querySelector('[name="tax_code_id"]')) return {};
        const code = TaxCodes.selected(form);
        return { tax_code_id: code ? code.id : null };
    },

    // A paid receipt (an expense, a card charge): its Amount is the total
    // paid, tax included, so the picker splits that total rather than adding
    // to it. The receipt's own tax figure, when typed, wins over the rate
    // backed out of the total (they can differ by a cent). '' with no codes.
    receiptGroupHtml() {
        const offered = TaxCodes._codes.filter(c => !c.is_archived);
        if (!offered.length) return '';
        const opts = offered.map(c => TaxCodes._optionHtml(c, false)).join('');
        return `<div class="form-group"><label>Tax Code</label>
                <select name="tax_code_id" title="The Amount is the total paid, tax included"><option value="">None</option>${opts}</select></div>
            <div class="form-group"><label>Tax on the receipt</label>
                <input name="tax_amount" type="number" step="0.01" min="0" placeholder="Worked out from the code"
                    title="The tax the receipt prints, if it differs from the code's rate by a cent"></div>`;
    },

    receiptPayload(form) {
        if (!form || !form.querySelector('[name="tax_code_id"]')) return {};
        const code = TaxCodes.selected(form);
        const typed = form.querySelector('[name="tax_amount"]');
        const tax = code && typed && typed.value !== '' ? parseFloat(typed.value) : null;
        return { tax_code_id: code ? code.id : null, tax_amount: tax };
    },
};
