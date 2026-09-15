const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } });
  const errors = [];
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', e => errors.push('PAGEERROR: ' + e.message));

  await page.goto('http://127.0.0.1:4200/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);

  const body = await page.textContent('body');
  const checks = [
    ['标题', body.includes('离线生料配比试算工作台')],
    ['免责声明', body.includes('虚构工艺边界')],
    ['S1 自动求解出现方案', body.includes('最低成本方案')],
    ['三方案齐全', body.includes('指标居中方案') && body.includes('廉价原料最大化方案')],
    ['率值 KPI', /KH 目标 0\.86/.test(body)],
    ['氧化物来源表头', body.includes('氧化物来源与比例')],
    ['化验版本追溯', body.includes('A-旱季基线')],
    ['干湿基换算', body.includes('湿吨/干吨')],
    ['分母保护', body.includes('率值分母保护校验')],
    ['多方案对比表', body.includes('多可行解对比')],
  ];

  // 触发雨季求解
  await page.getByRole('button', { name: '② 按雨季含水率/附加成本求解' }).click();
  await page.waitForTimeout(1500);
  const body2 = await page.textContent('body');
  checks.push(['旱雨季对比表出现', body2.includes('旱季 vs 雨季')]);
  checks.push(['雨季覆盖标记', body2.includes('雨季覆盖')]);

  // 成本对比数值
  const cmp = await page.locator('.cmp-table').first().textContent();
  checks.push(['对比表含成本列', cmp.includes('干基成本')]);

  // S2 不可行场景
  await page.getByRole('button', { name: /S2/ }).click();
  await page.waitForTimeout(1500);
  const s2 = await page.textContent('body');
  checks.push(['S2 冲突面板', s2.includes('约束冲突诊断')]);
  checks.push(['S2 KH 冲突带实际率值与下限', /KH ≥ 0\.900/.test(s2) && /0\.046/.test(s2)]);
  checks.push(['S2 碱当量冲突', s2.includes('碱当量')]);
  checks.push(['S2 最小违约配比', s2.includes('最小总违约')]);

  // S3 缺测报错
  await page.getByRole('button', { name: /S3/ }).click();
  await page.waitForTimeout(1200);
  const s3 = await page.textContent('body');
  checks.push(['S3 缺测硬错误', s3.includes('MISSING_ANALYTES')]);
  checks.push(['S3 指出原料与项目', s3.includes('SST_NEW') && s3.includes('fe2o3')]);

  // S4 分母地板冲突
  await page.getByRole('button', { name: /S4/ }).click();
  await page.waitForTimeout(1200);
  const s4 = await page.textContent('body');
  checks.push(['S4 分母地板被点名', s4.includes('Fe₂O₃ ≥ 0.05%')]);

  // ===== 自定义场景端到端 =====
  await page.getByRole('button', { name: '场景试算与方案对比' }).click();
  await page.waitForTimeout(800);

  // 内置场景不可改：S1 下无编辑/删除按钮
  await page.getByRole('button', { name: /S1/ }).click();
  await page.waitForTimeout(1000);
  checks.push(['内置场景无编辑/删除按钮',
    (await page.getByRole('button', { name: '编辑此场景' }).count()) === 0 &&
    (await page.getByRole('button', { name: '删除此场景' }).count()) === 0]);

  const uname = '浏览器新矿点场景 ' + Date.now();

  // 非法提交：空名称 + 取消（先验证客户端拦截）
  await page.getByRole('button', { name: '＋ 新建自定义场景' }).click();
  await page.waitForTimeout(600);
  await page.getByRole('button', { name: '保存场景' }).click();
  await page.waitForTimeout(300);
  checks.push(['空名称客户端拦截', (await page.textContent('body')).includes('名称必填')]);

  // 填名称并勾选两个原料，但把最低掺量设为 >100 的非法组合（70+70）
  await page.locator('input').first().fill(''); // name input is first input in modal
  const nameInput = page.locator('.modal input').first();
  await nameInput.fill(uname);
  // 勾选高钙石灰石与砂岩行
  const pickRow = (code) => page.locator('.mat-pick tbody tr').filter({ hasText: code });
  await pickRow('LS_H').locator('input[type="checkbox"]').first().check();
  await pickRow('SST').locator('input[type="checkbox"]').first().check();
  await pickRow('LS_H').locator('input[type="number"]').first().fill('70');
  await pickRow('SST').locator('input[type="number"]').first().fill('70');
  await page.getByRole('button', { name: '保存场景' }).click();
  await page.waitForTimeout(300);
  checks.push(['最低掺量和>100客户端拦截', (await page.textContent('body')).includes('超过 100')]);
  await page.getByRole('button', { name: '取消' }).click();
  await page.waitForTimeout(300);

  // 重复名称拒绝：先合法保存一个，再用同名提交
  const createWith = async (name, opts = {}) => {
    await page.getByRole('button', { name: '＋ 新建自定义场景' }).click();
    await page.waitForTimeout(500);
    await page.locator('.modal input').first().fill(name);
    const pickRow = (code) => page.locator('.mat-pick tbody tr').filter({ hasText: code });
    for (const c of ['LS_H','LS_L','SST','SH','FA','FE']) {
      await pickRow(c).locator('input[type="checkbox"]').first().check();
    }
    if (opts.cheap) {
      await pickRow('LS_L').locator('input[type="checkbox"]').last().check();
    }
    if (opts.rain) {
      // 雨季表 FA 行含水率覆盖
      const rainFa = page.locator('table').filter({ hasText: '雨季含水率覆盖' })
        .locator('tbody tr').filter({ hasText: 'FA' });
      await rainFa.locator('input').first().fill('26');
    }
    await page.getByRole('button', { name: '保存场景' }).click();
  };

  await createWith(uname, { cheap: true, rain: true });
  await page.waitForTimeout(2000);
  let bodyNow = await page.textContent('body');
  checks.push(['合法场景保存后自动求解', bodyNow.includes('最低成本方案')]);
  checks.push(['场景列表出现自建徽标', bodyNow.includes('自建')]);

  // 该场景可编辑/删除
  checks.push(['自建场景有编辑/删除按钮',
    (await page.getByRole('button', { name: '编辑此场景' }).count()) === 1]);

  // 雨季求解 + 历史
  await page.getByRole('button', { name: '② 按雨季含水率/附加成本求解' }).click();
  await page.waitForTimeout(1800);
  checks.push(['自定义场景雨季对比', (await page.textContent('body')).includes('旱季 vs 雨季')]);
  await page.getByRole('button', { name: '查看历史解（追溯化验版本）' }).click();
  await page.waitForTimeout(800);
  const histBody = await page.textContent('body');
  checks.push(['历史解含 base/rain 与快照版本',
    histBody.includes('基线') && histBody.includes('雨季') && histBody.includes('A-旱季基线')]);
  await page.getByRole('button', { name: '收起历史解' }).click();

  // 重名拒绝
  await createWith(uname);
  await page.waitForTimeout(800);
  const dupBody = await page.textContent('body');
  checks.push(['重复名称被服务端拒绝', dupBody.includes('已存在')]);
  await page.getByRole('button', { name: '取消' }).click();

  // 自定义冲突场景：低钙石灰石35%+煤矸石15%，紧边界
  const cname = '浏览器冲突场景 ' + Date.now();
  await page.getByRole('button', { name: '＋ 新建自定义场景' }).click();
  await page.waitForTimeout(500);
  await page.locator('.modal input').first().fill(cname);
  // 收紧率值：KH 0.90-0.96
  const numInputs = page.locator('.modal fieldset input[type="number"]');
  await numInputs.nth(0).fill('0.90');
  await numInputs.nth(2).fill('2.5');
  await numInputs.nth(3).fill('2.9');
  await numInputs.nth(4).fill('1.2');
  await numInputs.nth(5).fill('1.7');
  // 有害上限：碱当量与Cl（fieldset 内 hazard-grid 第3、4个）
  const haz = page.locator('.modal .hazard-grid input');
  await haz.nth(2).fill('0.6');
  await haz.nth(3).fill('0.015');
  // 勾选 LS_L(1) CG(6) SST(2) FE(5)，并设最低掺量
  const pickRowC = (code) => page.locator('.mat-pick tbody tr').filter({ hasText: code });
  for (const c of ['LS_L','CG','SST','FE']) {
    await pickRowC(c).locator('input[type="checkbox"]').first().check();
  }
  const setMin = (code, v) => pickRowC(code)
    .locator('input[type="number"]').first().fill(String(v));
  await setMin('LS_L', '35');
  await setMin('CG', '15');
  await page.getByRole('button', { name: '保存场景' }).click();
  await page.waitForTimeout(1800);
  const confBody = await page.textContent('body');
  checks.push(['自定义冲突场景展示具体冲突项',
    confBody.includes('约束冲突诊断') && confBody.includes('KH ≥ 0.900') && confBody.includes('碱当量')]);
  // 冲突诊断也进历史
  await page.getByRole('button', { name: '查看历史解（追溯化验版本）' }).click();
  await page.waitForTimeout(800);
  checks.push(['冲突诊断持久化到历史', (await page.textContent('body')).includes('冲突诊断')]);

  // 删除自定义场景
  await page.getByRole('button', { name: '收起历史解' }).click().catch(() => {});
  page.once('dialog', d => d.accept());
  await page.getByRole('button', { name: '删除此场景' }).click();
  await page.waitForTimeout(1200);
  const afterDel = await page.textContent('body');
  checks.push(['删除后场景消失并回到内置', !afterDel.includes(cname) && afterDel.includes('S1')]);

  // 手工试算页
  await page.getByRole('button', { name: '手工配比试算' }).click();
  await page.waitForTimeout(1500);
  const m0 = await page.textContent('body');
  checks.push(['手工页默认率值', m0.includes('石灰饱和系数')]);
  await page.getByRole('button', { name: '演示：零分母报错（92%石英砂）' }).click();
  await page.waitForTimeout(1000);
  const m1 = await page.textContent('body');
  checks.push(['零分母错误', m1.includes('ZERO_DENOMINATOR')]);
  await page.getByRole('button', { name: '演示：雨季含水率覆盖' }).click();
  await page.waitForTimeout(1000);
  await page.getByRole('button', { name: '雨季手工合成恢复', exact: false }).count().catch(() => 0);
  const m2 = await page.textContent('body');
  checks.push(['雨季手工合成恢复', /硅率 SM/.test(m2) && !m2.includes('ZERO_DENOMINATOR')]);
  // 雨季覆盖应反映在换算表（粉煤灰 26%）
  checks.push(['雨季含水率覆盖生效', m2.includes('雨季覆盖') || /26\.00/.test(m2)]);

  // 原料库页
  await page.getByRole('button', { name: '原料库 / 化验版本' }).click();
  await page.waitForTimeout(1200);
  const mat = await page.textContent('body');
  checks.push(['原料库 9 个原料', mat.includes('煤矸石') && mat.includes('石英砂')]);
  checks.push(['缺测显示', mat.includes('缺测')]);
  // 点开第一行原料的「版本」抽屉（排除抽屉内的「切为生效」按钮）
  await page.locator('tr').filter({ hasText: '高钙石灰石' }).first().locator('button', { hasText: '版本' }).click();
  await page.waitForTimeout(800);
  const mat2 = await page.textContent('body');
  // 高钙石灰石只有旱季版本；改点粉煤灰行验证 A/B 双版本
  if (!mat2.includes('B-雨季复测')) {
    await page.locator('tr').filter({ hasText: '粉煤灰' }).first().locator('button', { hasText: '版本' }).click();
    await page.waitForTimeout(800);
  }
  checks.push(['展开看到双版本(雨季)', (await page.textContent('body')).includes('B-雨季复测')]);

  let fail = 0;
  for (const [name, ok] of checks) {
    console.log((ok ? 'PASS' : 'FAIL') + '  ' + name);
    if (!ok) fail++;
  }
  console.log('\nConsole errors (' + errors.length + '):');
  for (const e of errors.slice(0, 10)) console.log('  ', e.slice(0, 200));
  await page.screenshot({ path: '/tmp/app-solver.png', fullPage: false });
  await browser.close();
  process.exit(fail ? 1 : 0);
})();
