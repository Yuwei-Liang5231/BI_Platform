/**
 * 走查：折线图「数据截至」截止线（截屏人工复核 + console 检查）
 * 场景：区间设 2026-09-01~09-30，transactions 数据只覆盖 09-01~04
 * → 卡片应显示「区间无数据」，折线只画 09-01~04 且末端出现「数据截至 09-04」虚线
 */
const { chromium } = require("playwright-core");

const BASE = "http://localhost:5189";
const results = [];
function step(name, ok, extra = "") {
  results.push(ok);
  console.log(`[${ok ? "PASS" : "FAIL"}] ${name}${extra ? " <- " + extra : ""}`);
}

(async () => {
  const browser = await chromium.launch({
    executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
    headless: true,
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const consoleErrors = [];
  page.on("console", (m) => m.type() === "error" && consoleErrors.push(m.text()));

  await page.goto(`${BASE}/#/login`, { waitUntil: "networkidle" });
  await page.evaluate(() => localStorage.clear());
  await page.goto(`${BASE}/#/login`, { waitUntil: "networkidle" });
  await page.getByPlaceholder("用户名").fill("admin");
  await page.getByPlaceholder("密码").fill("admin123");
  await page.getByRole("button", { name: /登\s*录/ }).click();
  await page.waitForURL(/#\/dashboard/, { timeout: 15000 });
  await page.waitForTimeout(2500);

  // 区间 09-01~09-30（transactions 覆盖仅 09-01~04 → 尾部 26 天无数据）
  await page.getByPlaceholder("开始日期").fill("2026-09-01");
  await page.getByPlaceholder("结束日期").fill("2026-09-30");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(2500);

  // 卡片显示区间无数据徽标
  const badges = await page.getByText("区间无数据").count();
  step("T1 卡片显示区间无数据徽标", badges > 0, `徽标数=${badges}`);

  // 选中一个指标（默认已选首卡），滚动到图表后截屏复核截止线
  await page.waitForTimeout(1500);
  const chartCard = page.locator(".dash__chart-card");
  await chartCard.scrollIntoViewIfNeeded().catch(() => {});
  await page.waitForTimeout(1200);
  const chartVisible = await chartCard.count();
  await page.screenshot({ path: "scripts/__cutoff_dashboard.png", fullPage: false });
  step("T2 图表卡可见且截屏已保存（复核：折线末端应有「数据截至 09-04」虚线）", chartVisible > 0, `图表卡=${chartVisible}`);

  // 详情页同样验证
  await page.goto(`${BASE}/#/metrics`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1500);
  await page.locator(".stat-card, .metric-card, [role=button]").first().click();
  await page.waitForTimeout(2500);
  step("T3 进入指标详情页", /#\/metrics\/\d+/.test(page.url()), page.url());
  const detailChart = page.locator(".detail__chart, .pwc-card:has(canvas)").first();
  await detailChart.scrollIntoViewIfNeeded().catch(() => {});
  await page.waitForTimeout(1200);
  await page.screenshot({ path: "scripts/__cutoff_detail.png", fullPage: false });
  step("T4 详情页截屏已保存", true);

  step("T5 全程 console 无错误", consoleErrors.length === 0, `errors=${consoleErrors.length}`);

  await browser.close();
  const passed = results.filter(Boolean).length;
  console.log(`\n${passed}/${results.length} passed`);
  process.exit(passed === results.length ? 0 : 1);
})();
