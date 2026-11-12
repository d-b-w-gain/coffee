const pw = require(process.env.PW || 'playwright-core');
const URL = 'http://127.0.0.1:28080/';
const engines = (process.env.ENGINES || 'chromium,firefox').split(',');
(async () => {
  let bad = 0;
  for (const name of engines) {
    const b = await pw[name].launch();
    const ctx = await b.newContext({ viewport: { width: 390, height: 844 }, hasTouch: name !== 'firefox' });
    const p = await ctx.newPage();
    const errs = []; p.on('pageerror', e => errs.push(e.message));
    // straight on the page
    await p.goto(URL); await p.waitForTimeout(2000);
    await p.click('#barista > summary'); await p.waitForTimeout(500);
    const a = await p.evaluate(() => ({ open: document.getElementById('barista').open, label: document.getElementById('barista-act').textContent,
      grinder: document.getElementById('grinder').getBoundingClientRect().height > 0 }));
    await p.click('#barista > summary'); await p.waitForTimeout(400);
    const closed = await p.evaluate(() => !document.getElementById('barista').open);
    // inside an iframe, like the ScrivOS desk app
    await p.evaluate(() => localStorage.setItem('coffee-barista', '0'));
    await p.setContent(`<iframe src="${URL}" style="width:460px;height:700px;border:0" allow="autoplay; clipboard-write; fullscreen"></iframe>`);
    await p.waitForTimeout(2500);
    const f = p.frames().find(fr => fr !== p.mainFrame() && fr.url().startsWith(URL));
    await f.click('#barista > summary'); await p.waitForTimeout(500);
    const inFrame = await f.evaluate(() => ({ open: document.getElementById('barista').open,
      grinder: document.getElementById('grinder').getBoundingClientRect().height > 0 }));
    const ok = a.open && a.grinder && a.label.startsWith('Hide') && closed && inFrame.open && inFrame.grinder && !errs.length;
    if (!ok) bad++;
    console.log((ok ? 'PASS ' : 'FAIL ') + name, JSON.stringify({ page: a, closesAgain: closed, inFrame, errs }));
    await b.close();
  }
  process.exit(bad ? 1 : 0);
})();
