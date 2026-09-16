/* 完整诊断：登录 → 看板静置 → 总览 → 管理对话框（行数/取消行为）。 */
const { chromium } = require("playwright-core");

const BASE = process.env.DIAG_BASE || "http://localhost:5194";
const out = [];

(async () => {
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  const page = await browser.newPage();
  const bad = [];
  page.on("response", (r) => {
    if (r.status() >= 400) bad.push(`${r.status()} ${r.request().method()} ${r.url().replace(BASE, "")}`);
  });
  page.on("pageerror", (e) => out.push("PAGEERROR: " + String(e).slice(0, 300)));

  await page.goto(`${BASE}/#/login`, { waitUntil: "networkidle" });
  await page.locator("input").first().fill("admin");
  await page.locator("input[type=password]").fill("admin123");
  await page.getByRole("button", { name: /登\s*录/ }).click();
  await page.waitForSelector(".layout__user", { timeout: 30000 });
  await page.waitForTimeout(8000);
  out.push("STEP2 看板失败请求: " + (bad.length ? bad.join(" | ") : "无"));

  await page.goto(`${BASE}/#/overview`, { waitUntil: "networkidle" });
  await page.waitForTimeout(5000);
  await page.screenshot({ path: "C:/Users/William Y Liang/Desktop/工作内容/AI Agent/BI Platform_WorkBuddy/diag_overview.png" });
  const manageBtn = page.getByRole("button", { name: "管理检测指标" });
  const btnDisabled = await manageBtn.isDisabled();
  out.push("STEP3 管理按钮 disabled=" + btnDisabled);
  await manageBtn.click();
  await page.waitForTimeout(3000);
  const rows = await page.locator(".el-dialog:visible .el-table__row").count();
  out.push("STEP3 对话框表格行数=" + rows);
  await page.screenshot({ path: "C:/Users/William Y Liang/Desktop/工作内容/AI Agent/BI Platform_WorkBuddy/diag_dialog.png" });

  await page.locator(".el-dialog:visible").getByRole("button", { name: "取消" }).click();
  await page.waitForTimeout(1500);
  const stillOpen = await page.locator(".el-dialog:visible").count();
  out.push("STEP4 取消后对话框: " + (stillOpen > 0 ? "仍打开(无反应复现)" : "已关闭(正常)"));

  out.push("STEP5 失败请求汇总: " + (bad.length ? bad.join(" | ") : "无"));
  require("fs").writeFileSync(
    "C:/Users/William Y Liang/Desktop/工作内容/AI Agent/BI Platform_WorkBuddy/diag_result.txt",
    out.join("\n"),
    "utf8",
  );
  await browser.close();
})().catch((e) => {
  out.push("FATAL: " + String(e).slice(0, 400));
  require("fs").writeFileSync(
    "C:/Users/William Y Liang/Desktop/工作内容/AI Agent/BI Platform_WorkBuddy/diag_result.txt",
    out.join("\n"),
    "utf8",
  );
  process.exit(1);
});
