/**
 * 走查：看板切换主题时默认选中该主题第一个指标（图表跟随主题，不残留上一主题指标）
 * 隔离环境：8102（data-test-r3）+ 5186 preview（dist-run 新包）
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

  async function selectedName() {
    return page.locator(".stat-card.is-selected .stat-card__label").first().textContent();
  }
  async function chartTitle() {
    const t = await page.locator(".dash__chart-card h4").first().textContent();
    return t.trim();
  }

  // T1 默认选中「全部主题」第一个指标，图表标题一致
  const firstAll = await page.locator(".stat-card__label").first().textContent();
  const sel1 = (await selectedName())?.trim();
  const title1 = await chartTitle();
  step("T1 默认选中第一个指标且图表跟随", sel1 === firstAll.trim() && title1.includes(sel1), `选中=${sel1} 图表=${title1.split("·")[0].trim()}`);

  // T2 在全部主题里点一个非首个指标（复现用户操作：先查看某个指标）
  const secondCard = page.locator(".stat-card").nth(1);
  const secondName = (await secondCard.locator(".stat-card__label").textContent())?.trim();
  await secondCard.click();
  await page.waitForTimeout(1500);
  const title2 = await chartTitle();
  step("T2 手动点选第二个指标后图表跟随", title2.includes(secondName ?? "~none~"), `点选=${secondName} 图表=${title2.split("·")[0].trim()}`);

  // T3 切到某个具体主题 → 默认选中该主题第一个指标，图表不再是上一主题的指标
  const chip = page.locator(".dash__topics button", { hasText: /^(?!全部主题)/ }).first();
  const chipName = (await chip.textContent())?.trim();
  await chip.click();
  await page.waitForTimeout(2000);
  const topicFirst = (await page.locator(".stat-card__label").first().textContent())?.trim();
  const sel3 = (await selectedName())?.trim();
  const title3 = await chartTitle();
  step(
    "T3 切主题后自动选中该主题第一个指标且图表跟随",
    sel3 === topicFirst && title3.includes(topicFirst ?? "~none~") && sel3 !== secondName,
    `主题=${chipName} 首卡=${topicFirst} 选中=${sel3} 图表=${title3.split("·")[0].trim()}`,
  );

  // T4 切回全部主题 → 重置回全部主题第一个指标
  await page.locator(".dash__topics button", { hasText: "全部主题" }).click();
  await page.waitForTimeout(2000);
  const sel4 = (await selectedName())?.trim();
  const title4 = await chartTitle();
  step("T4 切回全部主题重置为首卡且图表跟随", sel4 === firstAll.trim() && title4.includes(firstAll.trim() ?? "~none~"), `选中=${sel4} 图表=${title4.split("·")[0].trim()}`);

  step("T5 全程 console 无错误", consoleErrors.length === 0, `errors=${consoleErrors.length}`);

  await browser.close();
  const passed = results.filter(Boolean).length;
  console.log(`\n${passed}/${results.length} passed`);
  process.exit(passed === results.length ? 0 : 1);
})();
