/**
 * Tax Reports — Schedule C generation + Sales Tax Payment
 * Feature 19: Generate and export Schedule C from P&L data
 * Feature 5: Pay Sales Tax
 */
const TaxPage = {
    async render() {
        const year = new Date().getFullYear();
        return `
            <div class="page-header">
                <h2>Tax Reports — Schedule C</h2>
                <div style="font-size:10px; color:var(--text-muted);">
                    Profit or Loss from Business (Sole Proprietorship)
                </div>
            </div>
            <div class="toolbar">
                <label style="font-size:10px;font-weight:700;">From:</label>
                <input type="date" id="tax-start" value="${year}-01-01">
                <label style="font-size:10px;font-weight:700;">To:</label>
                <input type="date" id="tax-end" value="${year}-12-31">
                <button class="btn btn-primary" onclick="TaxPage.generate()">Generate</button>
                <button class="btn btn-secondary" onclick="TaxPage.exportCSV()">Export CSV</button>
                <button class="btn btn-secondary" onclick="TaxPage.showPaySalesTax()">Pay Sales Tax</button>
            </div>
            <div style="background:#fef3c7;border:1px solid #fbbf24;padding:6px 10px;margin-bottom:12px;font-size:10px;color:#92400e;">
                <strong>Disclaimer:</strong> This report is for reference only. Please verify all figures with a qualified tax professional.
            </div>
            <div id="tax-results"></div>`;
    },

    async generate() {
        const start = $('#tax-start').value;
        const end = $('#tax-end').value;
        const data = await API.get(`/tax/schedule-c?start_date=${start}&end_date=${end}`);
        const container = $('#tax-results');

        let html = `<div class="table-container"><table>
            <thead><tr><th scope="col">Tax Line</th><th scope="col">Account</th><th scope="col" class="amount">Amount</th></tr></thead><tbody>`;

        for (const line of data.lines) {
            html += `<tr style="background:var(--toolbar-bg);"><td colspan="2" style="font-weight:700;font-size:11px;">${escapeHtml(line.line)}</td><td class="amount" style="font-weight:700;">${formatCurrency(line.total)}</td></tr>`;
            for (const acct of line.accounts) {
                html += `<tr><td style="padding-left:24px;">${escapeHtml(acct.account_number || '')}</td><td>${escapeHtml(acct.account_name)}</td><td class="amount">${formatCurrency(acct.amount)}</td></tr>`;
            }
        }

        // Part I in the form's own order: returns, cost of goods sold and
        // other income only when there are any.
        const row = (label, value) => `<div class="total-row"><span class="label">${label}</span><span class="value">${formatCurrency(value)}</span></div>`;
        const partOne = [
            row('Gross receipts (line 1)', data.gross_receipts),
            data.returns_and_allowances ? row('Returns and allowances (line 2)', -data.returns_and_allowances) : '',
            data.cost_of_goods_sold ? row('Cost of goods sold (line 4)', -data.cost_of_goods_sold) : '',
            data.other_income ? row('Other income (line 6)', data.other_income) : '',
        ].join('');
        html += `</tbody></table></div>
            <div class="invoice-totals" style="margin-top:12px;">
                ${partOne}
                <div class="total-row"><span class="label">Gross Income (line 7)</span><span class="value">${formatCurrency(data.gross_income)}</span></div>
                <div class="total-row"><span class="label">Total Expenses (line 28)</span><span class="value">${formatCurrency(data.total_expenses)}</span></div>
                <div class="total-row grand-total"><span class="label">Net Profit (Loss) (line 31)</span><span class="value">${formatCurrency(data.net_profit)}</span></div>
            </div>`;
        container.innerHTML = html;
    },

    exportCSV() {
        const start = $('#tax-start').value;
        const end = $('#tax-end').value;
        window.open(`/api/tax/schedule-c/csv?start_date=${start}&end_date=${end}`, '_blank');
    },

    // opts (the GST/HST return's Pay): {title, amount, taxAccounts:
    // [{id, account_number, name, balance}], credits: {account_id, amount}
    // or null, done, onDone}. The tax is then paid
    // down on the account picked from taxAccounts, the amount the return
    // owes offered, instead of Sales Tax Payable's balance.
    async showPaySalesTax(opts = {}) {
        TaxPage._payOpts = opts;
        const owedOn = opts.taxAccounts && opts.taxAccounts.length ? opts.taxAccounts : null;
        // Tax is paid out of a bank or card account — never Accounts
        // Receivable, Inventory or Undeposited Funds, which the old
        // every-asset list offered (the server refuses those too).
        const [accounts, balSheet] = await Promise.all([
            API.get('/accounts?bank=1&active_only=true'),
            owedOn ? null : API.get('/reports/balance-sheet'),
        ]);

        let taxBalance, summary, taxPicker = '';
        if (owedOn) {
            taxBalance = Math.max(Number(opts.amount) || 0, 0);
            summary = `<strong>Amount owing:</strong> ${formatCurrency(taxBalance)}`;
            if (opts.credits && opts.credits.amount > 0) {
                summary += `<div class="hint" style="margin-top:4px;">The ${formatCurrency(opts.credits.amount)} of input tax credits claimed comes off their own account with this payment.</div>`;
            }
            const taxOpts = owedOn.map(a => `<option value="${a.id}">${escapeHtml(a.account_number || '')} ${escapeHtml(a.name)} (${formatCurrency(a.balance)})</option>`).join('');
            taxPicker = `<div class="form-group"><label>Tax Account *</label>
                        <select name="tax_account_id" required>${taxOpts}</select></div>`;
        } else {
            // Find Sales Tax Payable balance
            const taxLiability = balSheet.liabilities.find(l => l.account_number === '2200');
            taxBalance = taxLiability ? Math.abs(taxLiability.amount) : 0;
            summary = `<strong>Sales Tax Payable Balance:</strong> ${formatCurrency(taxBalance)}`;
        }
        const title = opts.title || 'Pay Sales Tax';

        const bankOpts = accounts.map(a => `<option value="${a.id}">${escapeHtml(a.name)} (${formatCurrency(a.balance)})</option>`).join('');
        const noBank = accounts.length ? '' : '<small style="color:var(--danger);">No bank or credit card account yet. Add one under Banking first.</small>';

        openModal(escapeHtml(title), `
            <form onsubmit="TaxPage.submitPaySalesTax(event)">
                <div style="margin-bottom:12px; padding:8px; background:var(--primary-light); border-radius:4px;">
                    ${summary}
                </div>
                <div class="form-grid">
                    <div class="form-group"><label>Amount *</label>
                        <input name="amount" type="number" step="0.01" required value="${taxBalance.toFixed(2)}"></div>
                    <div class="form-group"><label>Date *</label>
                        <input name="date" type="date" required value="${todayISO()}"></div>
                    ${taxPicker}
                    <div class="form-group"><label>Pay From Account *</label>
                        <select name="pay_from_account_id" required><option value="">Select...</option>${bankOpts}</select>${noBank}</div>
                    <div class="form-group"><label>Check #</label>
                        <input name="check_number"></div>
                </div>
                <div class="form-actions">
                    <button type="button" class="btn btn-secondary" onclick="closeModal()">Cancel</button>
                    <button type="submit" class="btn btn-primary">${escapeHtml(title)}</button>
                </div>
            </form>`);
    },

    async submitPaySalesTax(e) {
        e.preventDefault();
        const form = e.target;
        const opts = TaxPage._payOpts || {};
        const body = {
            amount: parseFloat(form.amount.value),
            date: form.date.value,
            pay_from_account_id: parseInt(form.pay_from_account_id.value),
            check_number: form.check_number.value || null,
        };
        if (form.tax_account_id) body.tax_account_id = parseInt(form.tax_account_id.value);
        if (opts.credits && opts.credits.amount > 0) {
            body.recoverable_account_id = opts.credits.account_id;
            body.recoverable_amount = opts.credits.amount;
        }
        try {
            await API.post('/reports/sales-tax/pay', body);
            toast(opts.done || 'Sales tax payment recorded');
            if (typeof opts.onDone === 'function') opts.onDone();
            else App.refresh();
        } catch (err) { toast(err.message, 'error'); }
    },
};
