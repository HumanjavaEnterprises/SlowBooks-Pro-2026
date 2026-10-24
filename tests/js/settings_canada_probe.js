// Render the Settings page for a US company, a Quebec one, a Nova Scotia
// one and a Canadian one with no province yet, with the real terms.js, then
// press "Set up Quebec sales tax codes". Prints one JSON object.
const fs = require('fs'), vm = require('vm');

let settings = {};
const posts = [];
const els = { 'tax-presets-result': { innerHTML: '', hidden: true } };

const ctx = {
  console,
  JSON,
  escapeHtml: (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'),
  toast: () => {},
  setTimeout: () => 0, // the page's loaders are not part of this
  API: {
    get: async () => settings,
    post: async (url, body) => {
      posts.push({ url, body });
      return {
        province: 'QC', province_name: 'Quebec', rates_verified: '2026-10-23',
        created: { accounts: [], tax_codes: [{ name: 'GST + QST QC 14.975%' }] },
        existing: { accounts: [{ account_number: '2250', name: 'GST/HST Payable' }], tax_codes: [] },
        notes: [],
      };
    },
  },
  document: { getElementById: (id) => els[id] || null },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync('app/static/js/terms.js', 'utf8'), ctx);
vm.runInContext(fs.readFileSync('app/static/js/settings.js', 'utf8') + '\nthis.SettingsPage = SettingsPage; this.Terms = Terms;', ctx);
const S = ctx.SettingsPage;

async function page(s) {
  settings = s;
  ctx.Terms.init(s);
  return S.render();
}

(async () => {
  const base = { company_name: 'Maple Signs', company_type: 'business' };
  const out = {};
  out.us = await page(base);
  out.qc = await page({ ...base, tax_country: 'CA', tax_province: 'QC' });
  out.ns = await page({ ...base, tax_country: 'CA', tax_province: 'NS' });
  out.no_province = await page({ ...base, tax_country: 'CA', tax_province: '' });
  S.loadTaxCodes = () => {};
  settings = { ...base, tax_country: 'CA', tax_province: 'QC' };
  await S.setupCanadaTaxCodes(false);
  out.posted = posts[0];
  out.result = els['tax-presets-result'].innerHTML;
  console.log(JSON.stringify(out));
})();
