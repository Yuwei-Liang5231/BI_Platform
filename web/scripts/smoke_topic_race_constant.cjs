// 竞态回归验证：加载后立刻点 general 主题，Sum_Saving 应显示「全期值」而非「区间无数据」
const { chromium } = require("playwright-core");

async function login(page) {
  await page.goto("http://localhost:5173/#/login", { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(1200);
  const inputs = page.locator("input");
  await inputs.nth(0).fill("admin");
  await inputs.nth(1).fill("admin123");
  await page.locator("button", { hasText: /登\s*录/ }).last().click();
  await page.waitForTimeout(1500);
}

async function checkSavingCard(page, label) {
  await page.waitForTimeout(5000); // 等取数稳定
  const cards = page.locator(".stat-card");
  const n = await cards.count();
  for (let i = 0; i < n; i++) {
    const text = (await cards.nth(i).innerText()).replace(/\s+/g, " ").trim();
    if (text.includes("Sum_Saving")) {
      const ok = text.includes("13,403.15") && text.includes("全期值");
      const bad = text.includes("区间无数据");
      console.log(`[${label}] Sum_Saving card: ${JSON.stringify(text)} => ${ok && !bad ? "PASS" : "FAIL"}`);
      return ok && !bad;
    }
  }
  console.log(`[${label}] Sum_Saving card NOT FOUND => FAIL`);
  return false;
}

async function checkChartSection(page, label) {
  const section = page.locator(".dash__chart-card");
  if (!(await section.count())) return;
  const text = (await section.innerText()).replace(/\s+/g, " ").slice(0, 260);
  const hasCanvas = await section.locator("canvas").count();
  console.log(`[${label}] chart section: ${JSON.stringify(text)} | canvas=${hasCanvas}`);
}

(async () => {
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  let allPass = true;

  // 场景 1：竞态路径——加载后立刻点 general
  {
    const page = await (await browser.newContext()).newPage();
    await login(page);
    await page.goto("http://localhost:5173/#/dashboard", { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(150); // 不等取数完成，立刻切主题（复现竞态窗口）
    await page.locator("button.tag-chip", { hasText: /^general$/ }).click();
    allPass = (await checkSavingCard(page, "race: instant general click")) && allPass;
    await checkChartSection(page, "race: instant general click");
    await page.close();
  }

  // 场景 2：常规路径——等加载完成后再点 general
  {
    const page = await (await browser.newContext()).newPage();
    await login(page);
    await page.goto("http://localhost:5173/#/dashboard", { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(6000);
    await page.locator("button.tag-chip", { hasText: /^general$/ }).click();
    allPass = (await checkSavingCard(page, "normal: delayed general click")) && allPass;
    await checkChartSection(page, "normal: delayed general click");
    await page.close();
  }

  // 场景 3：竞态后切走再切回（用户原始 workaround 仍应有效）
  {
    const page = await (await browser.newContext()).newPage();
    await login(page);
    await page.goto("http://localhost:5173/#/dashboard", { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(150);
    await page.locator("button.tag-chip", { hasText: /^general$/ }).click();
    await page.waitForTimeout(500);
    await page.locator("button.tag-chip", { hasText: /^trade$/ }).click();
    await page.waitForTimeout(1500);
    await page.locator("button.tag-chip", { hasText: /^general$/ }).click();
    allPass = (await checkSavingCard(page, "switch away and back")) && allPass;
    await page.close();
  }

  await browser.close();
  console.log(allPass ? "ALL PASS" : "SOME FAILED");
  process.exit(allPass ? 0 : 1);
})().catch((e) => {
  console.error("FATAL:", e.message);
  process.exit(1);
});
