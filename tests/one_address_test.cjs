const { chromium } = require('playwright-core');
const OLD = 'http://127.0.0.1:28080', NEW = 'http://localhost:28080';
const fails = [];
const check = (name, cond, extra) => { console.log((cond ? 'PASS ' : 'FAIL ') + name + (extra !== undefined ? '  [' + JSON.stringify(extra) + ']' : '')); if (!cond) fails.push(name); };
(async () => {
  const b = await chromium.launch(); const ctx = await b.newContext(); const p = await ctx.newPage();
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  // the new address already has this morning's order (Regular)
  await p.goto(NEW + '/api/status');
  await p.evaluate(() => {
    localStorage.setItem('coffee-order', JSON.stringify({ name: 'Hayley', drink: 'Long black', size: 'Regular', milk: 'No milk', beans: 'Regular', shots: 'Single', sugar: 'No sugar', cup: "Dan's choice" }));
    localStorage.setItem('coffee-history', JSON.stringify([{ drink: 'Long black', size: 'Regular', milk: 'No milk', beans: 'Regular', shots: 'Single', sugar: 'No sugar', cup: "Dan's choice", at: 2 }]));
  });
  // coffee.lan has her real preset
  await p.goto(OLD + '/api/status');
  check('API on the old address is not redirected', p.url().startsWith(OLD + '/api/status'), p.url());
  await p.evaluate(() => {
    localStorage.setItem('coffee-order', JSON.stringify({ name: 'Hayley', drink: 'Long black', size: 'Regular', milk: 'No milk', beans: 'Half & half', shots: 'Single', sugar: 'No sugar', cup: "Hayley's cup" }));
    localStorage.setItem('coffee-phone', JSON.stringify('0412 345 678'));
    localStorage.setItem('coffee-history', JSON.stringify([{ drink: 'Flat white', size: 'Regular', milk: 'Oat', beans: 'Half & half', shots: 'Single', sugar: 'No sugar', cup: "Hayley's cup", at: 1 }]));
  });
  await p.goto(OLD + '/'); await p.waitForTimeout(2500);
  check('old address lands on the new one', p.url().startsWith(NEW + '/'), p.url());
  check('address bar tidied (no import data left)', !p.url().includes('import='), p.url());
  const beans = await p.inputValue('#beans').catch(() => null);
  check('her half & half preset is back', beans === 'Half & half', beans);
  check('name came across', (await p.inputValue('#name')) === 'Hayley', await p.inputValue('#name'));
  check('phone came across', (await p.inputValue('#phone')) === '0412 345 678', await p.inputValue('#phone'));
  const hist = await p.evaluate(() => JSON.parse(localStorage.getItem('coffee-history')).length);
  check('recent orders from both addresses kept', hist === 2, hist);
  // later: an old bookmark with stale data mustn't overwrite newer choices
  await p.goto(NEW + '/api/status');
  await p.evaluate(() => { const o = JSON.parse(localStorage.getItem('coffee-order')); o.beans = 'Decaf'; localStorage.setItem('coffee-order', JSON.stringify(o)); });
  await p.goto(OLD + '/'); await p.waitForTimeout(2000);
  check('a second visit via the old address keeps newer choices', (await p.inputValue('#beans')) === 'Decaf', await p.inputValue('#beans'));
  // links keep their #section; other pages move without carrying data
  await p.goto(OLD + '/#grinder'); await p.waitForTimeout(2000);
  check('#grinder link survives the move', p.url() === NEW + '/#grinder', p.url());
  await p.goto(OLD + '/settings'); await p.waitForTimeout(1500);
  check('settings page moves too', p.url() === NEW + '/settings', p.url());
  check('no page errors', errs.length === 0, errs);
  console.log(fails.length ? 'FAILED: ' + fails.join(', ') : 'ALL PASSED');
  await b.close();
})();
