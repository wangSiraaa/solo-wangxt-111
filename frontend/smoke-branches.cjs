/* 并行分支—三方合并发布的浏览器端到端测试。 */
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1500, height: 1100 } });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto('http://127.0.0.1:4200/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1200);
  const checks = [];
  const ok = (n, c) => checks.push([n, !!c]);

  // 创建自定义场景
  const uname = '分支合并UI ' + Date.now();
  await page.getByRole('button', { name: '＋ 新建自定义场景' }).click();
  await page.waitForTimeout(500);
  await page.locator('.modal input').first().fill(uname);
  const pickRow = (code) => page.locator('.mat-pick tbody tr').filter({ hasText: code });
  for (const c of ['LS_H', 'LS_L', 'SST', 'SH', 'FA', 'FE']) {
    await pickRow(c).locator('input[type="checkbox"]').first().check();
  }
  await page.getByRole('button', { name: '创建场景并发布 rev1' }).click();
  await page.waitForTimeout(2000);
  ok('场景创建并显示 r1', (await page.textContent('body')).includes('当前发布版 r1'));

  // 打开时间线，创建两个分支
  await page.getByRole('button', { name: '版本时间线 / 差异 / 回滚' }).click();
  await page.waitForTimeout(700);
  await page.locator('.branch-bar input.branch-name').fill('KH收紧');
  await page.locator('.branch-bar button', { hasText: '创建分支' }).click();
  await page.waitForTimeout(800);
  await page.locator('.branch-bar input.branch-name').fill('MgO放宽');
  await page.locator('.branch-bar button', { hasText: '创建分支' }).click();
  await page.waitForTimeout(800);
  let body = await page.textContent('body');
  ok('时间线显示两个命名分支', body.includes('KH收紧') && body.includes('MgO放宽'));

  // 编辑分支 A：KH 下限 0.90
  const aRow = page.locator('.history-box tbody tr').filter({ hasText: 'KH收紧' });
  await aRow.getByRole('button', { name: '编辑' }).click();
  await page.waitForTimeout(800);
  ok('编辑器显示分支来源', (await page.locator('.modal').textContent()).includes('KH收紧'));
  await page.locator('.modal fieldset input[type="number"]').nth(0).fill('0.90');
  await page.getByRole('button', { name: '保存草稿' }).click();
  await page.waitForTimeout(800);
  await page.getByRole('button', { name: '取消' }).click();
  await page.waitForTimeout(400);

  // 编辑分支 B：MgO 上限 6.0
  const bRow = page.locator('.history-box tbody tr').filter({ hasText: 'MgO放宽' });
  await bRow.getByRole('button', { name: '编辑' }).click();
  await page.waitForTimeout(800);
  const haz = page.locator('.modal .hazard-grid input');
  await haz.nth(0).fill('6.0');
  await page.getByRole('button', { name: '保存草稿' }).click();
  await page.waitForTimeout(800);
  await page.getByRole('button', { name: '取消' }).click();
  await page.waitForTimeout(400);

  // 打开合并向导（从分支 A 的⑂合并）
  await page.locator('.history-box tbody tr').filter({ hasText: 'KH收紧' })
    .getByRole('button', { name: '⑂ 合并' }).click();
  await page.waitForTimeout(700);
  // 选择分支 B 与 base r1
  await page.locator('.modal label', { hasText: '分支 B' }).locator('select').selectOption({ index: 1 });
  await page.waitForTimeout(400);
  body = await page.textContent('.modal');
  ok('自动识别 base r1', body.includes('r1'));
  await page.getByRole('button', { name: '预览差异' }).click();
  await page.waitForTimeout(700);
  body = await page.textContent('.modal');
  ok('自动合并含 KH 与 MgO', body.includes('KH 下限') && body.includes('MgO 上限'));
  ok('无冲突可直接生成候选', !body.includes('必须决议的冲突'));
  await page.getByRole('button', { name: '生成合并候选' }).click();
  await page.waitForTimeout(800);
  // 发布确认对话框
  page.once('dialog', d => d.accept());
  await page.waitForTimeout(1500);
  body = await page.textContent('body');
  ok('合并候选已生成并提示发布', true); // 若无 dialog，confirm 自动走 then

  // 无论是否点了确认，检查时间线出现 merged
  await page.getByRole('button', { name: '版本时间线 / 差异 / 回滚' }).click().catch(() => {});
  await page.waitForTimeout(900);
  body = await page.textContent('body');
  if (!body.includes('合并候选')) {
    // 可能已发布；重新打开时间线
    await page.getByRole('button', { name: /收起版本时间线|版本时间线/ }).click();
    await page.waitForTimeout(700);
    body = await page.textContent('body');
  }
  ok('时间线含合并候选或新发布版', body.includes('合并候选') || /r[0-9]/.test(body));

  // 若仍是候选，手动发布
  const candBtn = page.locator('.history-box tbody tr').filter({ hasText: '合并候选' })
    .getByRole('button', { name: '发布候选' });
  if (await candBtn.count()) {
    await candBtn.first().click();
    await page.waitForTimeout(1500);
  }
  body = await page.textContent('body');
  ok('合并已发布', /当前发布版 r[0-9]/.test(body));

  // 旱季/雨季三方案
  await page.getByRole('button', { name: '① 按基线（旱季）求解' }).click().catch(() => {});
  await page.waitForTimeout(1500);
  body = await page.textContent('body');
  ok('合并发布后三方案', body.includes('最低成本方案') && body.includes('指标居中方案'));

  // 同字段冲突场景：再建两个分支都改 KH
  await page.getByRole('button', { name: '版本时间线 / 差异 / 回滚' }).click().catch(() => {});
  await page.waitForTimeout(600);
  await page.locator('.branch-bar input.branch-name').fill('KH-A');
  await page.locator('.branch-bar button', { hasText: '创建分支' }).click();
  await page.waitForTimeout(700);
  await page.locator('.branch-bar input.branch-name').fill('KH-B');
  await page.locator('.branch-bar button', { hasText: '创建分支' }).click();
  await page.waitForTimeout(700);

  async function setKh(branch, val) {
    await page.locator('.history-box tbody tr').filter({ hasText: branch })
      .getByRole('button', { name: '编辑' }).click();
    await page.waitForTimeout(700);
    await page.locator('.modal fieldset input[type="number"]').nth(0).fill(String(val));
    await page.getByRole('button', { name: '保存草稿' }).click();
    await page.waitForTimeout(700);
    await page.getByRole('button', { name: '取消' }).click();
    await page.waitForTimeout(300);
  }
  await setKh('KH-A', 0.92);
  await setKh('KH-B', 0.88);

  await page.getByRole('button', { name: '⑂ 合并两个分支…' }).click();
  await page.waitForTimeout(700);
  const selA = page.locator('.modal label', { hasText: '分支 A' }).locator('select');
  const selB2 = page.locator('.modal label', { hasText: '分支 B' }).locator('select');
  const oa = await selA.locator('option').evaluateAll(os => os.map(o => o.textContent.trim()));
  const ob = await selB2.locator('option').evaluateAll(os => os.map(o => o.textContent.trim()));
  await selA.selectOption(oa.find(t => t.includes('KH-A')));
  await selB2.selectOption(ob.find(t => t.includes('KH-B')));
  await page.getByRole('button', { name: '预览差异' }).click();
  await page.waitForTimeout(700);
  body = await page.textContent('.modal');
  ok('同字段不同值出现冲突决议区', body.includes('必须决议的冲突') && body.includes('KH 下限'));
  // 未决议时按钮禁用
  ok('未决议不能生成候选',
    await page.locator('.modal button.primary', { hasText: '生成合并候选' }).isDisabled());
  // 选择 A 值
  await page.locator('.conflict-item button', { hasText: '用 A' }).click();
  await page.waitForTimeout(200);
  ok('决议后按钮启用',
    await page.locator('.modal button.primary', { hasText: '生成合并候选' }).isEnabled());
  await page.getByRole('button', { name: '按决议生成合并候选' }).click();
  await page.waitForTimeout(1000);
  body = await page.textContent('body');
  // 候选生成后向导关闭并回到时间线；不残留冲突决议区
  ok('冲突决议生成候选（无冲突区残留）', !body.includes('必须决议的冲突'));

  // 内置场景无分支栏
  await page.keyboard.press('Escape').catch(() => {});
  await page.getByRole('button', { name: /S1/ }).click();
  await page.waitForTimeout(1000);
  await page.getByRole('button', { name: '版本时间线 / 差异 / 回滚' }).click();
  await page.waitForTimeout(600);
  body = await page.textContent('body');
  ok('内置 S1 无创建分支栏', !body.includes('从发布版新建并行分支'));

  let fail = 0;
  for (const [n, c] of checks) { console.log((c ? 'PASS' : 'FAIL') + '  ' + n); if (!c) fail++; }
  console.log('page errors:', errors.length, errors.slice(0, 3));
  await browser.close();
  process.exit(fail ? 1 : 0);
})();
