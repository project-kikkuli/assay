// A few real composition checks; the business-state space lives in the external verifier.
const { chromium } = await import(process.argv[3]);
const base = process.argv[2];
const browser = await chromium.launch({headless: true});
const page = await browser.newPage();
page.setDefaultTimeout(5000);
const errors = [];
page.on('pageerror', e => errors.push(String(e)));
try {
  await page.goto(base);
  await page.getByLabel('Identity', {exact:true}).selectOption('alpha-maker');
  await page.getByLabel('Quantity', {exact:true}).fill('2');
  await page.getByRole('button', {name:'Reserve', exact:true}).click();
  await page.getByText('reserved', {exact:true}).first().waitFor();
  const response = await fetch(base + '/reservations', {headers:{Authorization:'Bearer alpha-maker'}});
  const data = await response.json();
  if (!data.items.some(row => row.quantity === 2 && row.status === 'reserved')) throw Error('UI did not persist requested reservation');
  await page.getByLabel('Identity', {exact:true}).selectOption('beta-maker');
  await page.getByRole('button', {name:'Refresh', exact:true}).click();
  await page.getByText('No reservations', {exact:true}).waitFor();
  if (errors.length) throw Error(errors.join('\n'));
  console.log(JSON.stringify({passed:true, obligations:['browser-submission-persists','browser-tenant-switch'], requests_observed:true}));
} finally { await browser.close(); }
