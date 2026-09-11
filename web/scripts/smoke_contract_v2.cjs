/**
 * 走查：契约 v2（部分周期真实值 + 数据截至标注）+ 图表消失 BUG 回归
 * 环境：8105（data-test-r3：transactions 覆盖 2026-09-01~04）+ 5189 preview
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

  // T1 默认区间 2026-08：与数据覆盖（09-01~04）无交集 → 「区间无数据」（零数据场景维持）
  const empty1 = await page.getByText("区间无数据").count();
  step("T1 无交集区间显示「区间无数据」", empty1 > 0, `徽标数=${empty1}`);

  // T2 切到 09-01~09-30（部分周期）→ 卡片出真实值 + 「数据截至」徽标
  await page.getByPlaceholder("开始日期").fill("2026-09-01");
  await page.getByPlaceholder("结束日期").fill("2026-09-30");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(2500);
  const cutoff = await page.getByText(/数据截至/).count();
  const empty2 = await page.getByText("区间无数据").count();
  const hasValue = await page
    .locator(".stat-card__value")
    .evaluateAll((els) => els.some((el) => el.textContent.trim() !== "—"));
  step("T2 部分周期出真实值", hasValue, "有值卡=true");
  step("T2.1 「数据截至」徽标可见", cutoff > 0, `徽标数=${cutoff}`);
  step("T2.2 无「区间无数据」误标", empty2 === 0, `误标数=${empty2}`);

  // T3 折线图渲染且带截止线（截屏复核）
  await page.locator(".dash__chart-card").scrollIntoViewIfNeeded().catch(() => {});
  await page.waitForTimeout(1200);
  const canvas1 = await page.locator(".dash__chart canvas").count();
  step("T3 折线图 canvas 已渲染", canvas1 > 0, `canvas=${canvas1}`);
  await page.screenshot({ path: "scripts/__v2_dashboard.png" });

  // T4 BUG 回归：切到无数据区间（图表隐藏）再切回 → 图表必须重现
  // 注：先 Escape 关掉可能残留的日期面板，并确认区间真的切换了
  async function setRange(s, e) {
    await page.keyboard.press("Escape");
    await page.locator(".page-header__title").click(); // blur 关面板
    await page.getByPlaceholder("开始日期").fill(s);
    await page.getByPlaceholder("结束日期").fill(e);
    await page.keyboard.press("Enter");
    await page.waitForFunction(
      (args) => {
        const el = document.querySelector(".el-date-editor input");
        return true; // 区间断言在下方用 inputValue 校验
      },
      { s, e },
      { timeout: 1000 },
    ).catch(() => {});
  }

  await setRange("2026-10-01", "2026-10-31");
  const startVal = await page.getByPlaceholder("开始日期").inputValue();
  const endVal = await page.getByPlaceholder("结束日期").inputValue();
  step("T4.0 区间已切换到 2026-10", startVal === "2026-10-01" && endVal === "2026-10-31", `${startVal}~${endVal}`);
  let canvasGone = 1;
  try {
    await page.waitForFunction(
      () => document.querySelectorAll(".dash__chart canvas").length === 0,
      { timeout: 10000 },
    );
    canvasGone = 0;
  } catch {
    canvasGone = await page.locator(".dash__chart canvas").count();
  }
  step("T4.1 无数据区间图表隐藏（设计行为）", canvasGone === 0, `canvas=${canvasGone}`);

  await setRange("2026-09-01", "2026-09-30");
  let canvasBack = 0;
  try {
    await page.waitForFunction(
      () => document.querySelectorAll(".dash__chart canvas").length > 0,
      { timeout: 10000 },
    );
    canvasBack = 1;
  } catch {
    canvasBack = await page.locator(".dash__chart canvas").count();
  }
  step("T4.2 切回有数据区间图表重现（修复验证）", canvasBack > 0, `canvas=${canvasBack}`);

  // T5 主题切换路径同样回归（用户原始复现路径）：切主题两次再回来
  const chip = page.locator(".dash__topics button", { hasText: /^(?!全部主题)/ }).first();
  await chip.click();
  await page.waitForTimeout(1500);
  const chipBack = page.locator(".dash__topics button", { hasText: "全部主题" });
  await chipBack.click();
  await page.waitForTimeout(1500);
  const canvasAfterTopics = await page.locator(".dash__chart canvas").count();
  step("T5 主题切换后图表仍在", canvasAfterTopics > 0, `canvas=${canvasAfterTopics}`);

  step("T6 全程 console 无错误", consoleErrors.length === 0, `errors=${consoleErrors.length}`);

  await browser.close();
  const passed = results.filter(Boolean).length;
  console.log(`\n${passed}/${results.length} passed`);
  process.exit(passed === results.length ? 0 : 1);
})();
