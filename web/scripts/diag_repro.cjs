/* 诊断：对用户正在跑的 5173 复现"登录后/刷新后报服务异常"。
 * 抓取：所有 >=400 的 API 响应（URL + 状态 + body 片段）、页面 toast 文本、控制台错误。 */
const { chromium } = require("playwright-core");
const fs = require("fs");

const BASE = "http://localhost:5173";
const OUT = String.raw`C:\Users\William Y Liang\Desktop\工作内容\AI Agent\BI Platform_WorkBuddy\diag_result.txt`;

(async () => {
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  const page = await browser.newPage();
  const bad = [];
  const toasts = [];
  const consoleErrs = [];

  page.on("response", async (r) => {
    const url = r.url();
    if (!url.includes("/api/")) return;
    if (r.status() >= 300) {
      let snippet = "";
      try { snippet = (await r.text()).slice(0, 220); } catch {}
      bad.push(`${r.status()} ${r.request().method()} ${url}\n    body: ${snippet}`);
    }
  });
  page.on("requestfailed", (r) => {
    bad.push(
      `FAILED ${r.request().method()} ${r.url()}\n    reason: ${r.failure()?.errorText} type: ${r.resourceType()}`,
    );
  });
  page.on("console", (m) => {
    if (m.type() === "error") consoleErrs.push(m.text().slice(0, 220));
  });
  // 抓 ElMessage toast
  page.on("domcontentloaded", () => {});
  const watchToasts = async (label) => {
    await page.waitForTimeout(2500);
    const found = await page.evaluate(() =>
      Array.from(document.querySelectorAll(".el-message"))
        .map((n) => n.textContent.trim())
    );
    found.forEach((t) => toasts.push(`[${label}] ${t}`));
  };

  // 场景 A：全新登录（模拟关浏览器后重开）
  await page.goto(`${BASE}/#/login`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(1500);
  const inputs = page.locator("input");
  await inputs.nth(0).fill("admin");
  await page.locator("input[type=password]").fill("admin123");
  await page.getByRole("button", { name: /登\s*录/ }).click();
  await page.waitForURL(/dashboard|overview/, { timeout: 20000 }).catch(() => {});
  await watchToasts("登录后");

  // 场景 B：整页刷新（用户复现路径）
  await page.reload({ waitUntil: "domcontentloaded" });
  await watchToasts("刷新后");

  // 场景 C：切到 AI 问数（整页内路由）
  await page.goto(`${BASE}/#/ask`);
  await watchToasts("切问数后");

  const lines = [
    "=== >=400 API 响应 ===",
    bad.length ? bad.join("\n") : "（无）",
    "=== toast ===",
    toasts.length ? toasts.join("\n") : "（无）",
    "=== console errors ===",
    consoleErrs.length ? consoleErrs.slice(0, 8).join("\n") : "（无）",
  ];
  fs.writeFileSync(OUT, lines.join("\n"), "utf8");
  await browser.close();
})();
