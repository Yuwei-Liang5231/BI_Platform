// 竞态回归验证：加载后立刻点目标主题，常数指标应显示「全期值」而非「区间无数据」。
// 环境可参数化（默认 = 隔离走查环境 8105(data-test-r3) + 5191 preview）：
//   SMOKE_BASE=http://localhost:5191  SMOKE_METRIC=交易全期常数73680  SMOKE_TOPIC=general
//   SMOKE_OTHER_TOPIC=finance  SMOKE_USER=admin  SMOKE_PASS=admin123
const { chromium } = require("playwright-core");

const BASE = process.env.SMOKE_BASE || "http://localhost:5191";
const METRIC = process.env.SMOKE_METRIC || "交易全期常数73680";
const TOPIC = process.env.SMOKE_TOPIC || "general";
const OTHER_TOPIC = process.env.SMOKE_OTHER_TOPIC || "finance";
const USER = process.env.SMOKE_USER || "admin";
const PASS = process.env.SMOKE_PASS || "admin123";

async function login(page) {
  await page.goto(`${BASE}/#/login`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(1200);
  const inputs = page.locator("input");
  await inputs.nth(0).fill(USER);
  await inputs.nth(1).fill(PASS);
  await page.locator("button", { hasText: /登\s*录/ }).last().click();
  await page.waitForTimeout(1500);
}

async function checkConstCard(page, label) {
  await page.waitForTimeout(5000); // 等取数稳定
  const cards = page.locator(".stat-card");
  const n = await cards.count();
  for (let i = 0; i < n; i++) {
    const text = (await cards.nth(i).innerText()).replace(/\s+/g, " ").trim();
    if (text.includes(METRIC)) {
      // 卡片文本含 TrendBadge 的「—」（常数指标无环比，正常），不能据此判负
      const ok = text.includes("全期值") && !text.includes("区间无数据") && /\d/.test(text);
      console.log(`[${label}] ${METRIC} card: ${JSON.stringify(text)} => ${ok ? "PASS" : "FAIL"}`);
      return ok;
    }
  }
  console.log(`[${label}] ${METRIC} card NOT FOUND => FAIL`);
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
  const chip = (page, t) => page.locator("button.tag-chip", { hasText: new RegExp(`^${t}$`) });

  // 场景 1：竞态路径——加载后立刻点目标主题
  {
    const page = await (await browser.newContext()).newPage();
    await login(page);
    await page.goto(`${BASE}/#/dashboard`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(150); // 不等取数完成，立刻切主题（复现竞态窗口）
    await chip(page, TOPIC).click();
    allPass = (await checkConstCard(page, "race: instant topic click")) && allPass;
    await checkChartSection(page, "race: instant topic click");
    await page.close();
  }

  // 场景 2：常规路径——等加载完成后再点目标主题
  {
    const page = await (await browser.newContext()).newPage();
    await login(page);
    await page.goto(`${BASE}/#/dashboard`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(6000);
    await chip(page, TOPIC).click();
    allPass = (await checkConstCard(page, "normal: delayed topic click")) && allPass;
    await checkChartSection(page, "normal: delayed topic click");
    await page.close();
  }

  // 场景 3：竞态后切走再切回（用户原始 workaround 仍应有效）
  {
    const page = await (await browser.newContext()).newPage();
    await login(page);
    await page.goto(`${BASE}/#/dashboard`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(150);
    await chip(page, TOPIC).click();
    await page.waitForTimeout(500);
    await chip(page, OTHER_TOPIC).click();
    await page.waitForTimeout(1500);
    await chip(page, TOPIC).click();
    allPass = (await checkConstCard(page, "switch away and back")) && allPass;
    await page.close();
  }

  await browser.close();
  console.log(allPass ? "ALL PASS" : "SOME FAILED");
  process.exit(allPass ? 0 : 1);
})().catch((e) => {
  console.error("FATAL:", e.message);
  process.exit(1);
});
