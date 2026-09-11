/**
 * 走查：表关系登记 relation_type 枚举修复（v2 精确选择器）
 * 按表单标签定位各下拉；关系列表限定在「已登记表关系」区块内断言。
 */
const { chromium } = require("playwright-core");

const BASE = "http://127.0.0.1:5183";
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
  let relStatus = null;
  page.on("response", (r) => {
    if (r.url().includes("/relations") && r.request().method() === "POST") relStatus = r.status();
  });

  const dialog = page.locator(".el-dialog").filter({ hasText: "登记表关系" });
  const field = (label) =>
    dialog.locator(`.el-form-item:has(.el-form-item__label:text-is("${label}")) .el-select`);

  await page.goto(`${BASE}/#/login`, { waitUntil: "networkidle" });
  await page.evaluate(() => localStorage.clear());
  await page.goto(`${BASE}/#/login`, { waitUntil: "networkidle" });
  await page.getByPlaceholder("用户名").fill("admin");
  await page.getByPlaceholder("密码").fill("admin123");
  await page.getByRole("button", { name: /登\s*录/ }).click();
  await page.waitForURL(/#\/dashboard/, { timeout: 15000 });

  await page.goto(`${BASE}/#/datasets`, { waitUntil: "networkidle" });
  await page.getByText("走查主表", { exact: true }).first().click();
  await page.waitForTimeout(1200);
  // 清理上一轮登记的关系（若有），避免 409
  const oldDel = page
    .locator(".el-table__row")
    .filter({ hasText: "走查维表" })
    .getByRole("button", { name: "删除" })
    .first();
  if (await oldDel.isVisible().catch(() => false)) {
    await oldDel.click();
    await page.getByRole("button", { name: /确\s*定/ }).click();
    await page.waitForTimeout(1000);
    console.log("[prep] 已清理上轮登记的关系");
  }
  await page.getByRole("button", { name: "登记表关系" }).click({ timeout: 10000 });
  await dialog.waitFor({ state: "visible", timeout: 5000 });

  // 左表.字段 = channel_id
  await field("左表.字段").click();
  await page.locator(".el-select-dropdown:visible .el-select-dropdown__item", { hasText: /^channel_id$/ }).first().click();
  // 右表 = 走查维表
  await field("右表").click();
  await page.locator(".el-select-dropdown:visible .el-select-dropdown__item", { hasText: "走查维表" }).first().click();
  await page.waitForTimeout(800);
  // 右表.字段 = channel_id
  await field("右表.字段").click();
  await page.locator(".el-select-dropdown:visible .el-select-dropdown__item", { hasText: /^channel_id$/ }).first().click();

  // 关联类型：默认显示 + 选项标签
  const typeSel = field("关联类型");
  const defaultText = ((await typeSel.innerText().catch(() => "")) || "").trim();
  step("R1 默认关联类型显示业务符号（n:1）", /n:1/.test(defaultText), defaultText);
  await typeSel.click();
  await page.waitForTimeout(800); // 等上一个下拉收起过渡结束，type 弹层完全展开
  const optionTexts = await page
    .locator(".el-select-dropdown:visible .el-select-dropdown__item", { hasText: /（.+）/ })
    .allTextContents();
  step("R2 四个选项=枚举值+中文说明", optionTexts.length === 4, optionTexts.join(" | "));
  await page.locator(".el-select-dropdown:visible .el-select-dropdown__item", { hasText: "n:1" }).first().click();

  await dialog.getByRole("button", { name: "保存" }).click();
  await page.waitForTimeout(1800);
  step("R3 保存成功（POST relations 200）", relStatus === 200, `status=${relStatus}`);

  // 关系列表：过滤同时含右表名与左列名的行（即关系表，而非数据集列表）
  const relRow = page
    .locator(".el-table__row")
    .filter({ hasText: "channel_id" })
    .filter({ hasText: "走查维表" })
    .first();
  const rowText = (await relRow.textContent().catch(() => "")) || "";
  step("R4 关系列表显示类型符号 n:1", /n:1/.test(rowText), rowText.slice(0, 120));

  step("W5 console 无错误", consoleErrors.length === 0, consoleErrors.join(" | ").slice(0, 200));
  await browser.close();
  const passed = results.filter(Boolean).length;
  console.log(`\n${passed}/${results.length} passed, ${results.length - passed} failed`);
  process.exit(passed === results.length ? 0 : 1);
})();
