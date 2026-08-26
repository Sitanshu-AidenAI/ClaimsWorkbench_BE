import { chromium } from 'playwright';
const out = process.argv[3] || 'hero.png';
const wait = +(process.argv[4] || 5000);
const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
await p.goto(process.argv[2], { waitUntil: 'networkidle' });
await p.waitForTimeout(wait);
await p.screenshot({ path: out });
await b.close();
