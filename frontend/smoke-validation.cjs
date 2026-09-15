/* 自定义场景校验缺口的浏览器字段展示测试：
 * 1) 服务端对未知雨季编码返回 422（字段级），Angular 显示具体字段错误且不关闭弹窗、不落库；
 * 2) 同一原料同时无生效化验和成本时，行内同时出现两类可定位原因。
 */
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto('http://127.0.0.1:4200/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1200);
  const checks = [];
  const ok = (n, c) => checks.push([n, !!c]);

  // ---- 用例 1：未知雨季编码（拦截 POST /api/scenarios 返回字段错误） ----
  await page.getByRole('button', { name: '＋ 新建自定义场景' }).click();
  await page.waitForTimeout(500);
  await page.route('**/api/scenarios', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    await route.fulfill({
      status: 422,
      contentType: 'application/json',
      body: JSON.stringify({
        detail: {
          message: '场景校验失败',
          fields: {
            'rain_overrides.GHOST_R2': '雨季含水率覆盖的原料编码 GHOST_R2 不在本次参与原料列表中',
            'rain_extra_cost.GHOST_R3': '雨季附加成本的原料编码 GHOST_R3 不在本次参与原料列表中',
          },
        },
      }),
    });
  });
  await page.locator('.modal input').first().fill('未知雨季编码字段测试');
  const pickRow = (code) => page.locator('.mat-pick tbody tr').filter({ hasText: code });
  await pickRow('LS_H').locator('input[type="checkbox"]').first().check();
  await pickRow('LS_L').locator('input[type="checkbox"]').first().check();
  await page.getByRole('button', { name: '保存场景' }).click();
  await page.waitForTimeout(600);
  const body = await page.textContent('body');
  ok('未知雨季含水率编码字段错误展示', body.includes('GHOST_R2') && body.includes('不在本次参与原料列表中'));
  ok('未知雨季附加成本编码字段错误展示', body.includes('GHOST_R3'));
  ok('校验失败弹窗不关闭', (await page.locator('.modal').count()) === 1);
  await page.unroute('**/api/scenarios');
  await page.getByRole('button', { name: '取消' }).click();
  await page.waitForTimeout(300);

  // ---- 用例 2：真实 API — 同一原料双缺（化验 + 成本），两类原因分列 ----
  // 通过代理在后端真实创建一个无化验无成本的原料
  const created = await page.evaluate(async () => {
    const r = await fetch('/api/materials', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: 'BOTH_MISS', name: '双缺料', category: '测试' }),
    });
    return r.status === 200;
  });
  ok('双缺测试原料已创建', created);

  await page.getByRole('button', { name: '＋ 新建自定义场景' }).click();
  await page.waitForTimeout(700);
  await page.locator('.modal input').first().fill('双缺字段测试');
  await pickRow('BOTH_MISS').locator('input[type="checkbox"]').first().check();
  await pickRow('LS_H').locator('input[type="checkbox"]').first().check();
  await page.getByRole('button', { name: '保存场景' }).click();
  await page.waitForTimeout(800);
  const body2 = await page.textContent('body');
  ok('双缺：化验缺失原因', /BOTH_MISS[\s\S]{0,80}没有生效化验/.test(body2));
  ok('双缺：成本缺失原因', /BOTH_MISS[\s\S]{0,160}没有生效成本/.test(body2));
  // 错误落在该原料所在行
  const badRow = page.locator('.mat-pick tbody tr').filter({ hasText: 'BOTH_MISS' });
  const rowText = await badRow.textContent();
  ok('双缺原因定位在同一原料行', rowText.includes('没有生效化验') && rowText.includes('没有生效成本'));
  ok('双缺提交后弹窗仍在（不落半成品）', (await page.locator('.modal').count()) === 1);

  let fail = 0;
  for (const [n, c] of checks) { console.log((c ? 'PASS' : 'FAIL') + '  ' + n); if (!c) fail++; }
  console.log('page errors:', errors.length, errors.slice(0, 3));
  await browser.close();
  process.exit(fail ? 1 : 0);
})();
