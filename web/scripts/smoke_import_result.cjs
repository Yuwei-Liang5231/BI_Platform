/**
 * 走查：模板导入结果面板 + 管理页状态过滤（幂等版）
 * 每次运行导入「第一个未导入」的通用经营模板指标，动态确定目标码；无未导入项则只验列表可见性。
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

  await page.goto(`${BASE}/#/login`, { waitUntil: "networkidle" });
  await page.evaluate(() => localStorage.clear());
  await page.goto(`${BASE}/#/login`, { waitUntil: "networkidle" });
  await page.getByPlaceholder("用户名").fill("admin");
  await page.getByPlaceholder("密码").fill("admin123");
  await page.getByRole("button", { name: /登\s*录/ }).click();
  await page.waitForURL(/#\/dashboard/, { timeout: 15000 });

  await page.goto(`${BASE}/#/metrics/admin`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1500);
  // T1 默认「全部状态」下应能看到 pending（模板导入未绑数据集）指标
  const pendingRow = page.locator(".el-table__row").filter({ hasText: "pending" }).first();
  step("T1 管理页默认全部状态可见 pending 指标", await pendingRow.isVisible().catch(() => false),
    ((await pendingRow.textContent().catch(() => "")) || "").slice(0, 80));

  // 打开导入向导
  await page.getByRole("button", { name: "从模板导入" }).click({ timeout: 10000 });
  await page.getByText("通用经营", { exact: true }).first().click();
  await page.waitForTimeout(1200);

  // 只保留第一个勾选项，其余取消；动态确定目标码
  const boxes = page.locator(".admin__metric-check:not(.is-disabled)");
  const n = await boxes.count();
  let targetCode = null;
  for (let i = 0; i < n; i++) {
    const box = boxes.nth(i);
    const text = (await box.textContent()) || "";
    if (await box.locator("input[type=checkbox]").isChecked()) {
      if (targetCode === null) {
        const m = text.match(/（([a-z0-9_]+)）/);
        targetCode = m ? m[1] : text.slice(0, 20);
      } else {
        await box.click();
      }
    }
  }
  if (!targetCode) {
    console.log("[skip] 无未导入项，跳过导入段");
    await browser.close();
    process.exit(results.every(Boolean) ? 0 : 1);
  }
  console.log("[prep] 导入目标:", targetCode);
  await page.getByRole("button", { name: "导入 1 条" }).click();
  await page.waitForTimeout(2000);

  const body = ((await page.locator(".el-dialog").filter({ hasText: "导入结果" }).first().textContent().catch(() => "")) || "")
    .replace(/\s+/g, " ");
  step("T2 结果面板显示新建 1（此前恒为空白）", /新建 1/.test(body), body.slice(0, 170));
  step("T3 结果面板显示状态分布（启用/待绑定数据/已停用）", /待绑定数据 \d+/.test(body) && !/\{\}/.test(body));
  step("T4 含待绑定指标后续动作提示", /重新执行本向导/.test(body));
  await page.getByRole("button", { name: "关闭", exact: true }).click();
  await page.waitForTimeout(1500);

  const newRow = page.locator(".el-table__row", { hasText: targetCode }).first();
  const rowText2 = (await newRow.textContent().catch(() => "")) || "";
  step("T5 导入后列表（全部状态）立即可见该指标", rowText2.includes(targetCode), rowText2.slice(0, 80));
  step("T6 该指标状态徽章为 pending", /pending/.test(rowText2));

  // 切状态过滤 = 启用中 → pending 不可见
  await page.locator(".admin__toolbar .el-select").first().click();
  await page.locator(".el-select-dropdown:visible .el-select-dropdown__item", { hasText: "启用中" }).first().click();
  await page.waitForTimeout(1500);
  const goneText = (await page.locator(".el-table__row", { hasText: targetCode }).first().textContent().catch(() => "")) || "";
  step("T7 切「启用中」后 pending 指标隐藏", !goneText.includes(targetCode));

  // 复位为全部状态
  await page.locator(".admin__toolbar .el-select").first().click();
  await page.locator(".el-select-dropdown:visible .el-select-dropdown__item", { hasText: "全部状态" }).first().click();
  await page.waitForTimeout(1000);

  step("W8 console 无错误", consoleErrors.length === 0, consoleErrors.join(" | ").slice(0, 200));
  await browser.close();
  const passed = results.filter(Boolean).length;
  console.log(`\n${passed}/${results.length} passed, ${results.length - passed} failed`);
  process.exit(passed === results.length ? 0 : 1);
})();
