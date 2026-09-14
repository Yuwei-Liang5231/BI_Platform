/**
 * 走查：主题 × 时间区间 组合场景（BUG 回归）
 * BUG：刷新 → 进某主题 → 改区间（reloadForRange 清空全部卡片且只重载当前主题）
 * → 切其他主题 → 卡片停空显示"区间无数据"、折线却有数。
 * 环境：8105（data-test-r3）+ 5191 preview（dist-run 新包，BI_BACKEND_ORIGIN=8105）
 * 数据：tx_date 覆盖 2026-09-01~09-04；register_date 覆盖 2026-08-01~09-01
 * 区间 2026-08-01~2026-09-04 与两者相交 → 所有主题卡片都应有值
 */
const { chromium } = require("playwright-core");

const BASE = "http://localhost:5191";
const results = [];
function step(name, ok, extra = "") {
  results.push(ok);
  console.log(`[${ok ? "PASS" : "FAIL"}] ${name}${extra ? " <- " + extra : ""}`);
}

async function setRange(page, start, end) {
  await page.getByPlaceholder("开始日期").fill(start);
  await page.getByPlaceholder("结束日期").fill(end);
  await page.keyboard.press("Enter");
  await page.waitForTimeout(2500);
}

async function cardState(page) {
  return {
    loading: await page.locator(".stat-card__loading").count(),
    empty: await page.getByText("区间无数据").count(),
    values: await page.locator(".stat-card__value").count(),
    chart: await page.locator(".dash__chart-card canvas").count(),
  };
}

async function gotoTopic(page, name) {
  await page.locator(".dash__topics button", { hasText: name === "all" ? "全部主题" : name }).click();
  await page.waitForTimeout(2500);
}

// 等卡片加载落定（loading 归零）：并发取数+环比可能需要数秒
async function waitCardsSettled(page, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if ((await page.locator(".stat-card__loading").count()) === 0) return true;
    await page.waitForTimeout(400);
  }
  return false;
}

(async () => {
  const browser = await chromium.launch({
    executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
    headless: true,
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const consoleErrors = [];
  page.on("console", (m) => m.type() === "error" && consoleErrors.push(m.text()));

  async function freshLogin() {
    // 已登录时 hash 跳转不会重载页面（内存登录态仍在），必须真实 reload 重置状态
    await page.goto(`${BASE}/`, { waitUntil: "networkidle" });
    await page.evaluate(() => localStorage.clear());
    await page.reload({ waitUntil: "networkidle" });
    await page.waitForURL(/#\/login/, { timeout: 15000 }).catch(() => {});
    await page.getByPlaceholder("用户名").fill("admin");
    await page.getByPlaceholder("密码").fill("admin123");
    await page.getByRole("button", { name: /登\s*录/ }).click();
    await page.waitForURL(/#\/dashboard/, { timeout: 15000 });
    await page.waitForTimeout(2500);
  }

  // ===== 场景 A：全部主题 → 改区间 → 逐个切主题（历史正常路径，回归） =====
  await freshLogin();
  await setRange(page, "2026-08-01", "2026-09-04");
  for (const t of ["finance", "transaction", "customer", "all"]) {
    await gotoTopic(page, t);
    const s = await cardState(page);
    step(
      `A[${t}] 改区间后切主题卡片全部有值`,
      s.empty === 0 && s.loading === 0 && s.values > 0 && s.chart > 0,
      JSON.stringify(s),
    );
  }

  // ===== 场景 B：刷新 → 先进主题 → 改区间 → 再切其他主题（用户报的 BUG 路径） =====
  await freshLogin(); // 刷新态：全部主题 + 默认区间（2026-08），finance 卡片此时为"区间无数据"
  const b0 = await cardState(page);
  await gotoTopic(page, "finance");
  await setRange(page, "2026-08-01", "2026-09-04");
  const b1 = await cardState(page);
  step("B[finance 改区间] 当前主题卡片有值", b1.empty === 0 && b1.loading === 0 && b1.values > 0 && b1.chart > 0, JSON.stringify(b1));

  for (const t of ["transaction", "customer", "all"]) {
    await gotoTopic(page, t);
    const s = await cardState(page);
    step(
      `B[${t}] 切主题后卡片全部有值（BUG 修复点）`,
      s.empty === 0 && s.loading === 0 && s.values > 0 && s.chart > 0,
      JSON.stringify(s),
    );
  }

  // ===== 场景 C：快速连击（改区间后立刻切主题，防竞态/卡 loading） =====
  await gotoTopic(page, "finance");
  await page.getByPlaceholder("开始日期").fill("2026-08-01");
  await page.getByPlaceholder("结束日期").fill("2026-09-04");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(120); // 立刻切走，制造竞态
  await gotoTopic(page, "customer");
  await waitCardsSettled(page);
  const c1 = await cardState(page);
  step("C 快速连击后无卡 loading / 无区间无数据", c1.loading === 0 && c1.empty === 0 && c1.values > 0, JSON.stringify(c1));
  await gotoTopic(page, "finance");
  const settled = await waitCardsSettled(page);
  const c2 = await cardState(page);
  step("C 切回 finance 加载落定后无卡永久 loading", settled && c2.loading === 0 && c2.empty === 0 && c2.values > 0, JSON.stringify(c2));

  step("Z 全程 console 无错误", consoleErrors.length === 0, `errors=${consoleErrors.length}${consoleErrors[0] ? " first=" + consoleErrors[0].slice(0, 120) : ""}`);

  await browser.close();
  const passed = results.filter(Boolean).length;
  console.log(`\n${passed}/${results.length} passed`);
  process.exit(passed === results.length ? 0 : 1);
})();
