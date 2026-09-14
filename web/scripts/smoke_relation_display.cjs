/* 走查：数据集与表关系页 · 「左表.字段」显示修复 + UI 登记关系 + 跨表指标
 * 前置：隔离后端 8105（data-test-r3，含 rel_fact/rel_dim 与已登记关系），
 *       preview 5191 服务 dist-run 新包。
 */
const { chromium } = require("playwright-core");

const BASE = "http://localhost:5191";
let passed = 0;
let failed = 0;

function step(name, ok, detail = "") {
  if (ok) { passed++; console.log(`  PASS  ${name}${detail ? " | " + detail : ""}`); }
  else { failed++; console.log(`  FAIL  ${name}${detail ? " | " + detail : ""}`); }
}

(async () => {
  const browser = await chromium.launch({
    executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
    headless: true,
  });
  const page = await browser.newPage();
  const consoleErrors = [];
  page.on("pageerror", (e) => consoleErrors.push(String(e).slice(0, 150)));

  await page.goto(`${BASE}/#/login`, { waitUntil: "networkidle" });
  await page.evaluate(() => localStorage.clear());
  await page.goto(`${BASE}/#/login`, { waitUntil: "networkidle" });
  await page.getByPlaceholder("用户名").fill("admin");
  await page.getByPlaceholder("密码").fill("admin123");
  await page.getByRole("button", { name: /登\s*录/ }).click();
  await page.waitForURL(/#\/dashboard/, { timeout: 15000 });

  await page.goto(`${BASE}/#/datasets`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1500);

  // 点击左侧 rel_fact 数据集
  await page.locator(".el-table__row", { hasText: "rel_fact" }).first().click();
  await page.waitForTimeout(1500);

  // 关系表格：定位表头含「左表.字段」的 el-table
  const relTable = page.locator(".el-table").filter({
    hasText: "左表.字段",
  }).first();
  const relRows = relTable.locator(".el-table__body .el-table__row");

  // ===== R1: 已登记表关系左列显示 表名.字段 =====
  const relRow = relRows.filter({ hasText: "rel_dim" }).first();
  const leftCell = (await relRow.locator("td").first().innerText()).trim();
  const rightCell = (await relRow.locator("td").nth(1).innerText()).trim();
  step("R1a 左表.字段显示完整", leftCell === "rel_fact.cust_key", `实际: "${leftCell}"`);
  step("R1b 右表.字段显示完整", rightCell === "rel_dim.dim_id", `实际: "${rightCell}"`);

  // ===== R2: 登记表关系对话框可打开 =====
  await page.getByRole("button", { name: "登记表关系" }).click();
  await page.waitForTimeout(600);
  step("R2 登记关系对话框可打开", (await page.locator(".el-dialog:visible").count()) === 1);
  await page.locator(".el-dialog__headerbtn").click();
  await page.waitForTimeout(400);

  // ===== R3: 跨表指标（双引号列名 + 类型 cast）试编译通过 =====
  await page.goto(`${BASE}/#/metrics/admin`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);
  await page.getByRole("button", { name: "新建指标" }).click();
  await page.waitForTimeout(500);
  const dialog = page.locator(".el-dialog");
  const gridSelects = dialog.locator(".rule-builder__grid .el-select");
  await dialog.locator("input[placeholder='如 ecom_gmv_paid']").fill("smoke_rel_cross");
  await dialog.locator(".el-form-item").filter({ hasText: /^名称/ }).locator("input").fill("走查跨表过滤");
  await gridSelects.nth(0).click();
  await page.waitForTimeout(400);
  await page.locator(".el-select-dropdown:visible .el-select-dropdown__item", { hasText: "rel_fact" }).first().click();
  await page.waitForTimeout(600);
  await gridSelects.nth(1).click();
  await page.waitForTimeout(400);
  await page.locator(".el-select-dropdown:visible .el-select-dropdown__item", { hasText: "amount" }).first().click();
  await page.waitForTimeout(400);
  await dialog.locator("input[placeholder*='过滤条件']").fill("\"cust city\" = '上海'");
  await page.getByRole("button", { name: "试编译" }).click();
  await page.waitForTimeout(2000);
  step("R3 跨表过滤（双引号列名+JOIN cast）试编译通过",
    (await page.locator(".el-message", { hasText: "编译通过" }).count()) >= 1);
  await page.locator(".el-dialog__headerbtn").click();

  step("全程无页面错误", consoleErrors.length === 0, consoleErrors.slice(0, 3).join(" / "));
  console.log(`\n结果: ${passed} passed, ${failed} failed`);
  await browser.close();
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error("FATAL", e); process.exit(1); });
