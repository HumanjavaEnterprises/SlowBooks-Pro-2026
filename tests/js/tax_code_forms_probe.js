// The Tax Code picker on every tax-bearing form other than the invoice
// (which has its own tests), rendered against real API answers read from
// stdin as JSON: the company's tax codes and saved documents. For each
// form it prints what the picker offered and chose, the tax the form
// showed, and what the save sent, so tests/test_tax_code_forms.py can send
// that back to the API and hold the form's figure to the server's.
const fs = require('fs'), vm = require('vm');

const given = JSON.parse(fs.readFileSync(0, 'utf8'));
let codes = given.codes;
const answers = {
  '/customers?active_only=true': [],
  '/items?active_only=true': [],
  '/accounts': [],
  '/accounts?active_only=true': [],
  '/vendors': given.vendors,
  '/vendors?active_only=true': given.vendors,
  '/settings': {},
  [`/estimates/${given.estimate.id}`]: given.estimate,
  [`/recurring/${given.recurring.id}`]: given.recurring,
  [`/purchase-orders/${given.po.id}`]: given.po,
  [`/bills/${given.bill.id}`]: given.bill,
  [`/bills/${given.ratedBill.id}`]: given.ratedBill,
};

let els = {};
let modal = '';
let sent = null;
const ctx = {
  console,
  window: {},
  location: { hash: '#/' },
  document: { getElementById: (id) => els['#' + id] || null, querySelector: (sel) => els[sel] || null },
  escapeHtml: (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
  formatCurrency: (n) => '$' + (Number(n) || 0).toFixed(2),
  formatDate: (s) => s || '',
  todayISO: () => given.day,
  statusBadge: (s) => s,
  T: (s) => s,
  Terms: { text: (s) => s, isNonprofit: () => false },
  App: { settings: { home_currency: 'USD' }, navigate() {}, refresh() {}, shortcutAttrs: () => '' },
  toast(message, kind) { if (kind === 'error') throw new Error('toast: ' + message); },
  closeModal() {},
  confirm: () => true,
  openModal: (title, html) => { modal = html; },
  classFormGroupHtml: async () => '',
  jobFormGroupHtml: async () => '',
  currencyFormGroupsHtml: () => '',
  classIdFromForm: () => null,
  jobIdFromForm: () => null,
  currencyPayloadFromForm: () => ({}),
  ClassWarn: { ok: async () => true },
  CostCodes: { load: async () => {}, headHtml: () => '', cellHtml: () => '<td></td>', fromRow: () => null },
  Nonprofit: { loadFunds: async () => {}, headHtml: () => '', fundFromRow: () => null, linePayload: () => ({}) },
  TaxExempt: { enforce() {} },
  ScanHelper: { scanRowHtml: () => '', wire() {}, discard() {}, attachAfterSave: async () => {} },
  fetchAllPages: async () => [],
  $: (sel) => els[sel] || null,
  $$: (sel) => els[sel] || [],
  API: {
    get: async (path) => {
      if (path === '/tax-codes?include_archived=true') {
        if (codes === null) throw new Error('the codes could not be read');
        return JSON.parse(JSON.stringify(codes));
      }
      if (!(path in answers)) throw new Error('unexpected ' + path);
      return JSON.parse(JSON.stringify(answers[path]));
    },
    post: async (path, data) => { sent = { path, data }; return { id: 1, invoice: { id: 1, invoice_number: '1' } }; },
    put: async (path, data) => { sent = { path, data }; return { id: 1 }; },
  },
};
vm.createContext(ctx);
const pages = {
  'vendors.js': ['PurchaseLines', 'PurchaseAccounts', 'VendorQuickAdd'],
  'tax_codes.js': ['TaxCodes'],
  'invoices.js': ['SalesLines'],
  'sales_receipts.js': ['SalesReceiptsPage'],
  'estimates.js': ['EstimatesPage'],
  'credit_memos.js': ['CreditMemosPage'],
  'recurring.js': ['RecurringPage'],
  'purchase_orders.js': ['PurchaseOrdersPage'],
  'vendor_credits.js': ['VendorCreditsPage'],
  'bills.js': ['BillsPage'],
};
for (const [file, names] of Object.entries(pages)) {
  const expose = names.map((n) => `this.${n} = ${n};`).join(' ');
  vm.runInContext(fs.readFileSync('app/static/js/' + file, 'utf8') + '\n' + expose, ctx);
}
ctx.VendorQuickAdd.ensure = async () => given.vendorId;
ctx.EstimatesPage.customerSelected = () => {};
ctx.RecurringPage.customerSelected = () => {};
// a bill's line rows are put in by the test, not the form
ctx.BillsPage.lineHtml = () => '';
ctx.BillsPage.addLine = () => {};

const OPTION = /<option value="([^"]*)"\s*(selected)?\s*>([^<]*)<\/option>/g;

function picker(html) {
  const m = /<select name="tax_code_id"[^>]*>([\s\S]*?)<\/select>/.exec(html);
  if (!m) return null;
  const sel = {
    options: [],
    value: '',
    insertAdjacentHTML(_where, more) {
      for (const o of more.matchAll(OPTION)) sel.options.push({ value: o[1], text: o[3] });
    },
  };
  for (const o of m[1].matchAll(OPTION)) {
    sel.options.push({ value: o[1], text: o[3] });
    if (o[2]) sel.value = o[1];
  }
  return sel;
}

function element(attrs) {
  const label = { textContent: '' };
  return {
    textContent: '',
    hidden: /\shidden(\s|$|>)/.test(attrs + ' '),
    style: {},
    previousElementSibling: label,
    querySelector: (s) => (s === '.label' ? label : null),
  };
}

function row(qty, rate, accountId) {
  const cells = {
    '.line-item': { value: '' },
    '.line-desc': { value: 'Shelving' },
    '.line-qty': { value: String(qty) },
    '.line-rate': { value: String(rate) },
    '.line-taxable': { checked: true, disabled: false, dataset: {} },
    '.line-amount': { textContent: '' },
  };
  if (accountId) cells['.line-account'] = { value: String(accountId), disabled: false };
  return { querySelector: (s) => cells[s] || null };
}

// The form last opened, from its HTML: the picker, the Tax Rate box (with
// the form group the picker is put before), every element with an id, and
// the named fields the save reads.
function build(formSel, linesSel, rows, values = {}) {
  const ids = {};
  for (const m of modal.matchAll(/<\w+([^>]*?)\sid="([^"]+)"([^>]*)>/g)) ids['#' + m[2]] = element(m[1] + m[3]);
  let sel = picker(modal);
  const tag = /<input name="tax_rate"[^>]*>/.exec(modal);
  const rate = tag
    ? {
        value: /value="([^"]*)"/.exec(tag[0])[1],
        readOnly: false,
        title: '',
        closest: () => ({ insertAdjacentHTML: (_where, html) => { sel = picker(html); } }),
      }
    : null;
  const fields = {
    customer_id: String(given.customerId), vendor_id: String(given.vendorId), date: given.day,
    notes: '', frequency: 'monthly', terms: 'Net 30', end_date: '', start_date: given.day,
    expiration_date: '', expected_date: '', original_invoice_id: '', bill_number: '',
    ref_number: '', method: '', check_number: '', reference: '', deposit_to_account_id: '',
    ...values,
  };
  const named = Object.fromEntries(Object.entries(fields).map(([k, v]) => [k, { value: v }]));
  const form = {
    querySelector: (s) => (s === '[name="tax_code_id"]' ? sel : s === '[name="tax_rate"]' ? rate : null),
    get tax_rate() { return rate; },
    get tax_code_id() { return sel; },
    ...named,
  };
  els = {
    ...ids,
    [formSel]: form,
    [`${formSel} [name="tax_rate"]`]: rate,
    [linesSel]: { querySelectorAll: () => rows },
    [`${linesSel} tr`]: rows,
  };
  return { form, rate, ids, picker: () => sel };
}

// The picker as it stands: what it offers, what it chose, and whether the
// choice is one it offers (a real select can only hold one of those).
function shown(f) {
  const sel = f.picker();
  return {
    picker: sel
      ? { options: sel.options.map((o) => o.value), labels: sel.options.map((o) => o.text), selected: sel.value,
          offered: sel.options.some((o) => o.value === sel.value) }
      : null,
    rate: f.rate ? { value: String(f.rate.value), readOnly: f.rate.readOnly } : null,
  };
}

// What the picker's onchange does: sync the rate box, then the page's recalc.
function pick(f, id, recalc) {
  f.picker().value = id == null ? '' : String(id);
  ctx.TaxCodes.sync(f.form);
  recalc();
}

async function save(fn, f) {
  sent = null;
  await fn({ preventDefault() {}, target: f.form });
  return sent;
}

const text = (f, id) => (f.ids['#' + id] ? f.ids['#' + id].textContent : null);

(async () => {
  const out = {};
  const ten = () => [row(1, 10.10)];
  const tenOn = () => [row(1, 10.10, given.accountId)];

  // ── with codes ──
  await ctx.SalesReceiptsPage.showForm();
  let f = build('#sales-receipt-form', '#sr-lines', ten());
  out.srOpened = shown(f);
  pick(f, given.gstPst, () => ctx.SalesReceiptsPage.recalc());
  out.sr = { ...shown(f), tax: text(f, 'sr-tax'), sent: await save(ctx.SalesReceiptsPage.save, f) };

  await ctx.EstimatesPage.showForm(given.estimate.id);
  f = build('#est-form', '#est-lines', ten());
  ctx.EstimatesPage.recalc();
  out.estimate = { ...shown(f), tax: text(f, 'est-tax'),
    sent: await save((e) => ctx.EstimatesPage.save(e, given.estimate.id), f) };

  await ctx.RecurringPage.showForm(given.recurring.id);
  f = build('#rec-form', '#rec-lines', ten());
  ctx.RecurringPage.recalc();
  out.recurring = { ...shown(f), tax: text(f, 'rec-tax'),
    sent: await save((e) => ctx.RecurringPage.save(e, given.recurring.id), f) };

  // Crediting an invoice whose code has since been archived.
  await ctx.CreditMemosPage.showForm();
  f = build('#cm-form', '#cm-lines', ten(), { original_invoice_id: String(given.archivedInvoice.id) });
  out.cmOpened = shown(f);
  ctx.CreditMemosPage._invoices = [given.archivedInvoice];
  ctx.CreditMemosPage.invoiceSelected(String(given.archivedInvoice.id));
  out.cm = { ...shown(f), tax: text(f, 'cm-tax'), sent: await save(ctx.CreditMemosPage.save, f) };

  await ctx.PurchaseOrdersPage.showForm(given.po.id);
  f = build('#po-form', '#po-lines', ten());
  ctx.PurchaseOrdersPage.recalc();
  out.po = { ...shown(f), tax: text(f, 'po-tax'),
    sent: await save((e) => ctx.PurchaseOrdersPage.save(e, given.po.id), f) };

  await ctx.VendorCreditsPage.showForm();
  f = build('#vc-form', '#vc-lines', tenOn());
  pick(f, given.gstPst, () => ctx.VendorCreditsPage.recalc());
  out.vc = { ...shown(f), tax: text(f, 'vc-tax'), sent: await save(ctx.VendorCreditsPage.save, f) };

  const billShown = (f) => ({
    ...shown(f),
    tax: text(f, 'bill-tax'),
    taxLabel: f.ids['#bill-tax'] ? f.ids['#bill-tax'].previousElementSibling.textContent : null,
    taxHidden: f.ids['#bill-tax-row'] ? f.ids['#bill-tax-row'].hidden : null,
    recoverable: text(f, 'bill-recoverable'),
    recoverableLabel: f.ids['#bill-recoverable-row'] ? f.ids['#bill-recoverable-row'].querySelector('.label').textContent : null,
    recoverableHidden: f.ids['#bill-recoverable-row'] ? f.ids['#bill-recoverable-row'].hidden : null,
    total: text(f, 'bill-total'),
  });
  await ctx.BillsPage.showForm();
  f = build('#bill-form', '#bill-lines', tenOn());
  ctx.BillsPage.recalc();
  out.billOpened = billShown(f);
  pick(f, given.gstPst, () => ctx.BillsPage.recalc());
  out.bill = { ...billShown(f), sent: await save((e) => ctx.BillsPage.save(e, null), f) };

  // A bill under a code, edited without touching the picker, then with
  // the code taken off.
  await ctx.BillsPage.showForm(given.bill.id);
  f = build('#bill-form', '#bill-lines', tenOn());
  ctx.BillsPage.recalc();
  out.billKept = { ...billShown(f), sent: await save((e) => ctx.BillsPage.save(e, given.bill.id), f) };
  pick(f, null, () => ctx.BillsPage.recalc());
  out.billCleared = { ...billShown(f), sent: await save((e) => ctx.BillsPage.save(e, given.bill.id), f) };

  // A bill with a rate of its own (an API edit, #250) and no code.
  await ctx.BillsPage.showForm(given.ratedBill.id);
  f = build('#bill-form', '#bill-lines', tenOn());
  ctx.BillsPage.recalc();
  out.ratedBill = { ...billShown(f), sent: await save((e) => ctx.BillsPage.save(e, given.ratedBill.id), f) };

  // ── a company with no codes, and codes that could not be read ──
  for (const [key, none] of [['noCodes', []], ['unread', null]]) {
    codes = none;
    const got = {};
    await ctx.EstimatesPage.showForm(given.estimate.id);
    f = build('#est-form', '#est-lines', ten());
    ctx.EstimatesPage.recalc();
    got.estimate = { ...shown(f), tax: text(f, 'est-tax'),
      sent: await save((e) => ctx.EstimatesPage.save(e, given.estimate.id), f) };
    await ctx.BillsPage.showForm();
    f = build('#bill-form', '#bill-lines', tenOn());
    ctx.BillsPage.recalc();
    got.bill = { ...billShown(f), sent: await save((e) => ctx.BillsPage.save(e, null), f) };
    await ctx.BillsPage.showForm(given.bill.id);
    f = build('#bill-form', '#bill-lines', tenOn());
    ctx.BillsPage.recalc();
    got.billKept = { ...billShown(f), sent: await save((e) => ctx.BillsPage.save(e, given.bill.id), f) };
    out[key] = got;
  }

  console.log(JSON.stringify(out));
})().catch((err) => { console.error(err && err.stack || String(err)); process.exit(1); });
