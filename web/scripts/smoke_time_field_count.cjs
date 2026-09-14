/* 走查：新建指标 · 多日期列时间字段自动选择 + count 数值列编译
 * 前置：隔离后端 8105（data-test-r3，含 multi_date_ds 双日期列数据集），
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
  await page.goto(`${BASE}/#/metrics/admin`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);

  const dialog = page.locator(".el-dialog");
  const gridSelects = dialog.locator(".rule-builder__grid .el-select");

  async function openDialog() {
    await page.getByRole("button", { name: "新建指标" }).click();
    await page.waitForTimeout(500);
  }
  async function closeDialog() {
    // 保存成功会自动关框；仅在对话框仍可见时才点关闭
    const btn = page.locator(".el-dialog__headerbtn");
    if (await btn.isVisible().catch(() => false)) {
      await btn.click();
      await page.waitForTimeout(400);
    }
  }
  async function pickGrid(idx, optionText) {
    await gridSelects.nth(idx).click();
    await page.waitForTimeout(400);
    await page.locator(".el-select-dropdown:visible .el-select-dropdown__item", { hasText: optionText }).first().click();
    await page.waitForTimeout(600);
  }
  async function fillBasic(code, name) {
    await dialog.locator("input[placeholder='如 ecom_gmv_paid']").fill(code);
    await dialog.locator(".el-form-item").filter({ hasText: /^名称/ }).locator("input").fill(name);
  }

  // ===== T1: 多日期列数据集 → 自动选择时间字段 + 警示提示 =====
  await openDialog();
  await fillBasic("smoke_md_count", "走查多日期列计数");
  await pickGrid(0, "multi_date_ds");
  await page.waitForTimeout(800);
  const tfText = (await dialog.locator(".el-select").nth(3).innerText()).replace(/\s+/g, " ");
  step("T1a 多日期列自动选中 event_time", tfText.includes("event_time"), `实际: ${tfText.slice(0, 40)}`);
  step("T1b 行内警示提示出现", (await dialog.locator(".admin__hint--warn").count()) === 1);

  // ===== T2: count + 数值列 → 试编译通过 =====
  await pickGrid(1, "amount");
  await pickGrid(2, "计数 count");
  await page.getByRole("button", { name: "试编译" }).click();
  await page.waitForTimeout(1500);
  step("T2 count 数值列试编译通过", (await page.locator(".el-message", { hasText: "编译通过" }).count()) >= 1);

  // ===== T3: 保存 → 成功 =====
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await page.waitForTimeout(2500);
  step("T3 保存成功（列表可见）", (await page.getByRole("row", { hasText: "smoke_md_count" }).count()) >= 1);

  // ===== T4: 清空时间字段 → 确认框知情 → 确认后按全期常数编译通过 =====
  // （2026-09-14「日期可选」契约：留空是合法选择，弹确认框而非拦截）
  await page.reload({ waitUntil: "networkidle" }); // 保存关闭对话框后 overlay 可能残留，刷新拿干净状态
  await page.waitForTimeout(1500);
  await openDialog();
  await fillBasic("smoke_md_notf", "走查无时间字段拦截");
  console.log("T4 step: basic filled");
  await pickGrid(0, "multi_date_ds");
  console.log("T4 step: ds picked");
  await page.waitForTimeout(800);
  const tfSel = dialog.locator(".el-select").nth(3);
  await tfSel.hover(); // hover 状态下才渲染 clear 图标
  await page.waitForTimeout(500);
  // 完整事件序列触发清空（clear 图标 click handler；JS 派发避免 hit-test 被 overlay 拦截）
  await tfSel.locator(".el-select__clear").evaluate((el) => {
    for (const type of ["mousedown", "mouseup", "click"]) {
      el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
    }
  });
  await page.waitForTimeout(400);
  await page.keyboard.press("Escape"); // 清空可能弹开下拉面板，关闭后再操作 grid
  await page.waitForTimeout(400);
  console.log("T4 step: cleared:", (await tfSel.innerText()).replace(/\s+/g, " ").slice(0, 40));
  // 行内警示应说明「全期常数」语义
  const warnHint = await dialog.locator(".admin__hint--warn", { hasText: "全期常数" }).count();
  step("T4a 清空后行内警示说明全期常数语义", warnHint >= 1, `hint=${warnHint}`);
  await pickGrid(1, "amount");
  console.log("T4 step: column picked");
  // 试编译 = 纯预览，不弹确认框、直接按全期常数编译通过
  await page.getByRole("button", { name: "试编译" }).click();
  await page.waitForTimeout(1500);
  const t4bMsg = await page.locator(".el-message", { hasText: "编译通过" }).count();
  const t4bBox = await page.locator(".el-message-box").count();
  step("T4b 试编译直接通过（确认框只在保存时把关）", t4bMsg >= 1 && t4bBox === 0,
    `msg=${t4bMsg} box=${t4bBox}`);
  // 保存 = 口径落库，必须先弹知情确认框（waitFor 轮询，避免固定 sleep 时序抖动）
  await page.getByRole("button", { name: "保存", exact: true }).click();
  const msgBox = page.locator(".el-message-box", { hasText: "未选择时间字段" });
  let boxShown = true;
  try {
    await msgBox.waitFor({ state: "visible", timeout: 6000 });
  } catch {
    boxShown = false;
  }
  step("T4c 保存弹出「未选择时间字段」确认框", boxShown,
    boxShown ? "" : `box=${await page.locator(".el-message-box").count()}, valErrs=${JSON.stringify(await dialog.locator(".el-form-item__error").allInnerTexts())}, msgs=${JSON.stringify(await page.locator(".el-message").allInnerTexts())}`);
  if (boxShown) {
    await page.getByRole("button", { name: "创建全期常数指标" }).click();
    await page.waitForTimeout(2500);
  }
  step("T4d 确认后保存成功（列表可见）",
    (await page.getByRole("row", { hasText: "smoke_md_notf" }).count()) >= 1);
  await closeDialog();

  // ===== T5: 单日期列数据集 → 自动预填该列（开箱即用，仍可改） =====
  await page.reload({ waitUntil: "networkidle" });
  await page.waitForTimeout(1500);
  await openDialog();
  await fillBasic("smoke_single_tf", "走查单日期列留空");
  await pickGrid(0, "transactions");
  await page.waitForTimeout(600);
  const tf2 = (await dialog.locator(".el-select").nth(3).innerText()).replace(/\s+/g, " ");
  step("T5a 单日期列自动预填 tx_date", tf2.includes("tx_date"), `实际: ${tf2.slice(0, 40)}`);
  await pickGrid(1, "amount");
  await page.getByRole("button", { name: "试编译" }).click();
  await page.waitForTimeout(1500);
  step("T5b 单日期列留空试编译通过", (await page.locator(".el-message", { hasText: "编译通过" }).count()) >= 1);
  await closeDialog();

  step("全程无页面错误", consoleErrors.length === 0, consoleErrors.slice(0, 3).join(" / "));

  console.log(`\n结果: ${passed} passed, ${failed} failed`);
  await browser.close();
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error("FATAL", e); process.exit(1); });
