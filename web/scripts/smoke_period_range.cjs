/**
 * 走查：自定义统计周期（usePeriodRange）+ 覆盖区间提示
 * 隔离环境：8102（data-test-r3：gen_* 全启用，transactions 覆盖 2026-09-01~04）
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
  const page = await browser.newPage();
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

  // T1 看板默认上一自然月（2026-08），数据覆盖 2026-09-01~04 → 显示「区间无数据」徽标
  const badges = await page.getByText("区间无数据").count();
  step("T1 默认区间(2026-08)超出覆盖 → 区间无数据徽标", badges > 0, `徽标数=${badges}`);

  // T2 图表区显示覆盖提示（序列全 null 也视为无数据）
  const hint = await page.getByText(/数据覆盖为 2026-09-01 ~ 2026-09-04/).count();
  step("T2 覆盖范围提示可见", hint > 0, `提示数=${hint}`);

  // T3 改成覆盖内区间 → 多数卡片出值（customers 覆盖仅到 09-01，个别区间外属正确行为）
  await page.getByPlaceholder("开始日期").fill("2026-09-01");
  await page.getByPlaceholder("结束日期").fill("2026-09-04");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(2500);
  const badgesAfter = await page.getByText("区间无数据").count();
  const anyValue = await page
    .locator(".stat-card__value")
    .evaluateAll((els) => els.some((el) => el.textContent.trim() !== "—"));
  step("T3 切到 2026-09-01~04 后卡片出值、徽标大幅减少", anyValue && badgesAfter < 17, `徽标=${badgesAfter} 有值卡=${anyValue}`);

  // T4 目录页同样支持自定义区间
  await page.goto(`${BASE}/#/metrics`, { waitUntil: "networkidle" });
  await page.waitForTimeout(2000);
  await page.getByPlaceholder("开始日期").first().fill("2026-09-01");
  await page.getByPlaceholder("结束日期").first().fill("2026-09-04");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(2500);
  const catalogValues = await page
    .locator(".catalog-card__num")
    .evaluateAll((els) => els.some((el) => el.textContent.trim() !== "—"));
  step("T4 目录页切换区间后出值", catalogValues);

  // T5 详情页同样支持
  await page.goto(`${BASE}/#/metrics/1`, { waitUntil: "networkidle" });
  await page.waitForTimeout(2000);
  await page.getByPlaceholder("开始日期").first().fill("2026-09-01");
  await page.getByPlaceholder("结束日期").first().fill("2026-09-04");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(2500);
  const detailValue = await page
    .locator(".detail__value")
    .first()
    .textContent()
    .catch(() => "");
  step("T5 详情页切换区间后当前值非 —", (detailValue || "").trim() !== "—", `当前值="${(detailValue || "").trim()}"`);

  step("T6 console 无错误", consoleErrors.length === 0, consoleErrors.slice(0, 3).join(" ; "));

  await browser.close();
  const passed = results.filter(Boolean).length;
  console.log(`\n${passed}/${results.length} passed`);
  process.exit(passed === results.length ? 0 : 1);
})().catch((e) => {
  console.error("[FATAL]", e);
  process.exit(1);
});
