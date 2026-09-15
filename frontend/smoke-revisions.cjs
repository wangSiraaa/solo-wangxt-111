/* 版本草稿—发布冻结—重放审计的浏览器端到端测试。
 * 后端乐观并发/幂等/恢复的强保证由 pytest 覆盖；本文件验证 Angular 工作流：
 * 创建即发布 → 草稿（差异）→ 发布 → 时间线 → 重放 → 旧解修订绑定 → 回滚草稿 → 内置 403 UI。
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
  const toggleTimeline = async (wantOpen) => {
    const openBtn = page.getByRole('button', { name: '版本时间线 / 差异 / 回滚' });
    const closeBtn = page.getByRole('button', { name: '收起版本时间线' });
    if (wantOpen && await openBtn.count()) await openBtn.click();
    else if (!wantOpen && await closeBtn.count()) await closeBtn.click();
    await page.waitForTimeout(600);
  };

  // 内置场景显示发布版 r1、无修订入口
  await page.getByRole('button', { name: /S1/ }).click();
  await page.waitForTimeout(1000);
  let body = await page.textContent('body');
  ok('内置显示当前发布版 r1', body.includes('当前发布版 r1'));
  ok('内置无新建草稿入口', body.includes('内置场景，无修订流程'));

  // 1) 创建新场景（建单即发布 rev1）
  const uname = '修订流场景 ' + Date.now();
  await page.getByRole('button', { name: '＋ 新建自定义场景' }).click();
  await page.waitForTimeout(500);
  ok('创建模式按钮文案', (await page.textContent('.modal'))?.includes('创建场景并发布 rev1'));
  await page.locator('.modal input').first().fill(uname);
  const pickRow = (code) => page.locator('.mat-pick tbody tr').filter({ hasText: code });
  for (const c of ['LS_H', 'LS_L', 'SST', 'SH', 'FA', 'FE']) {
    await pickRow(c).locator('input[type="checkbox"]').first().check();
  }
  await pickRow('LS_L').locator('input[type="checkbox"]').last().check(); // 廉价
  const rainSection = page.locator('h3', { hasText: '雨季配置' }).locator('xpath=following-sibling::div[1]');
  await rainSection.locator('tbody tr').filter({ hasText: 'FA' }).locator('input[type="number"]').first().fill('26');
  await page.getByRole('button', { name: '创建场景并发布 rev1' }).click();
  await page.waitForTimeout(2000);
  body = await page.textContent('body');
  ok('创建后自动求解三方案', body.includes('最低成本方案') && body.includes('指标居中方案'));
  ok('显示发布版 r1', body.includes('当前发布版 r1'));
  // 雨季三方案
  await page.getByRole('button', { name: '② 按雨季含水率/附加成本求解' }).click();
  await page.waitForTimeout(1800);
  body = await page.textContent('body');
  ok('rev1 雨季对比', body.includes('旱季 vs 雨季'));

  // 2) 打开时间线
  await toggleTimeline(true);
  body = await page.textContent('body');
  ok('时间线有 r1 已发布冻结', body.includes('r1') && body.includes('已发布冻结'));

  // 3) 从发布版新建草稿，改 KH 下限
  await page.getByRole('button', { name: '从发布版新建修订草稿' }).click();
  await page.waitForTimeout(800);
  ok('草稿模式标题', (await page.textContent('.modal'))?.includes('创建修订草稿'));
  const fieldsetNums = page.locator('.modal fieldset input[type="number"]');
  await fieldsetNums.nth(0).fill('0.90'); // KH min
  await page.getByRole('button', { name: '保存草稿' }).click();
  await page.waitForTimeout(700);
  ok('保存草稿后提示 lock', (await page.textContent('.modal'))?.includes('草稿 r2 已保存'));
  // 发布
  await page.getByRole('button', { name: '保存并发布' }).click();
  await page.waitForTimeout(2000);
  body = await page.textContent('body');
  ok('发布后显示 r2', body.includes('当前发布版 r2'));
  ok('发布后自动重算', body.includes('最低成本方案'));
  ok('无草稿状态', body.includes('无草稿，当前发布版冻结'));

  // 4) 时间线 r2 + 重放 r1（发布后重新打开刷新）
  await toggleTimeline(false);
  await toggleTimeline(true);
  body = await page.textContent('body');
  ok('时间线含 r1 与 r2', body.includes('r1') && body.includes('r2'));
  // 重放 r1（旧发布版），产生绑定 r1 的新解
  const rows = page.locator('.history-box tbody tr');
  const r1Row = rows.filter({ hasText: ' r1 ' }).first();
  await r1Row.getByRole('button', { name: '重放求解' }).click();
  await page.waitForTimeout(1800);
  body = await page.textContent('body');
  ok('重放 r1 求解完成', body.includes('最低成本方案'));

  // 5) 历史解按修订关联
  await page.getByRole('button', { name: '查看历史解（追溯化验版本）' }).click();
  await page.waitForTimeout(800);
  body = await page.textContent('body');
  ok('历史解显示修订列 r1/r2', body.includes('r1') && body.includes('r2'));

  // 6) 回滚为新草稿（复制自 r1）
  await toggleTimeline(false);
  await toggleTimeline(true);
  await page.locator('.history-box tbody tr').filter({ hasText: ' r1 ' }).first()
    .getByRole('button', { name: '复制为新草稿' }).click();
  await page.waitForTimeout(1000);
  ok('回滚草稿打开编辑器',
    (await page.textContent('.modal'))?.includes('复制自 r1')
    || (await page.textContent('body')).includes('草稿 r3'));
  await page.getByRole('button', { name: '取消' }).click();
  await page.waitForTimeout(400);
  body = await page.textContent('body');
  ok('回滚草稿状态显示 r3', body.includes('草稿 r3'));
  await toggleTimeline(false);
  await toggleTimeline(true);
  body = await page.textContent('body');
  ok('r3 标记来源 r1', body.includes('复制自 r1'));
  // 放弃草稿
  page.once('dialog', d => d.accept());
  await page.getByRole('button', { name: '放弃' }).first().click();
  await page.waitForTimeout(800);
  body = await page.textContent('body');
  ok('放弃后回到仅发布版', body.includes('当前发布版 r2') && !body.includes('草稿 r3'));

  // 7) 内置场景修订按钮不可见（API 403 由 pytest 覆盖）
  await page.getByRole('button', { name: /S2/ }).click();
  await page.waitForTimeout(1200);
  body = await page.textContent('body');
  ok('S2 冲突诊断仍正常', body.includes('约束冲突诊断'));
  ok('内置无复制草稿按钮', !(await page.getByRole('button', { name: '复制为新草稿' }).count()));

  let fail = 0;
  for (const [n, c] of checks) { console.log((c ? 'PASS' : 'FAIL') + '  ' + n); if (!c) fail++; }
  console.log('page errors:', errors.length, errors.slice(0, 3));
  await browser.close();
  process.exit(fail ? 1 : 0);
})();
