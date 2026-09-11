/**
 * 走查：模板导入「重新导入升级」闭环（修复后）
 * 场景（隔离 8102 后端 + 空 data-test 库）：
 *   T1 首次导入通用经营 → 进入导入结果（新建 > 0）
 *   T2 重开向导 → pending 指标可勾选且默认勾上（修复点：过去全禁用、按钮死锁）
 *   T3 API 上传 transactions/customers 数据集 → UI 再次导入 → 升级 > 0
 *   T4 终态：已启用指标禁用、剩余 pending 仍可勾选；console 0 错误
 */
const { chromium } = require("playwright-core");

const BASE = "http://127.0.0.1:5186";
const API = "http://127.0.0.1:8102/api";
const results = [];
function step(name, ok, extra = "") {
  results.push(ok);
  console.log(`[${ok ? "PASS" : "FAIL"}] ${name}${extra ? " <- " + extra : ""}`);
}

const TX_CSV = [
  "tx_id,tx_date,amount,type,category,customer_id",
  "T001,2026-09-01,1000.50,收入,主营业务,C001",
  "T002,2026-09-02,200,支出,房租,C001",
  "T003,2026-09-03,50,退款,主营业务,C002",
  "T004,2026-09-03,3000,收入,主营业务,C002",
  "T005,2026-09-04,800,支出,人力,C003",
].join("\n");
const CUST_CSV = [
  "customer_id,customer_name,register_date,region",
  "C001,甲,2026-08-01,华东",
  "C002,乙,2026-08-15,华北",
  "C003,丙,2026-09-01,华南",
].join("\n");

async function apiLogin() {
  const res = await fetch(`${API}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: "admin", password: "admin123" }),
  });
  const body = await res.json();
  if (body.code !== 0) throw new Error("登录失败: " + JSON.stringify(body));
  return body.data.token;
}

async function apiUpload(token, name, csv) {
  const fd = new FormData();
  fd.append("name", name);
  fd.append(
    "file",
    new Blob([Buffer.from(csv, "utf-8")], { type: "text/csv" }),
    `${name}.csv`, // 必须带文件名，否则后端按 .bin 拒绝
  );
  const res = await fetch(`${API}/datasets/upload`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
    body: fd,
  });
  const body = await res.json();
  step(`API 上传数据集 ${name}`, body.code === 0, JSON.stringify(body).slice(0, 120));
}

(async () => {
  const token = await apiLogin();

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

  async function openWizardChooseGeneral() {
    await page.getByRole("button", { name: "从模板导入" }).click({ timeout: 10000 });
    await page.getByText("通用经营", { exact: true }).first().click();
    await page.waitForTimeout(1200);
  }

  // ---------- T1 首次导入 ----------
  await openWizardChooseGeneral();
  const boxes0 = page.locator(".admin__metric-check:not(.is-disabled)");
  const n0 = await boxes0.count();
  const checked0 = await page.locator(".admin__metric-check input:checked").count();
  step("T1.1 全新库：未导入指标全部可勾选且默认勾选", n0 >= 19 && checked0 === n0, `可勾选=${n0} 已勾=${checked0}`);
  await page.getByRole("button", { name: /导入 \d+ 条/ }).click();
  await page.waitForTimeout(1800);
  const stepActive = await page.locator(".el-steps").first().getAttribute("aria-valuenow").catch(() => null);
  const resultText1 = (await page.locator(".admin__wizard-body").innerText().catch(() => "")).slice(0, 120);
  step("T1.2 进入导入结果且新建 > 0", /新建 [1-9]/.test(resultText1) || /新建 [1-9]/.test(await page.locator(".el-dialog").innerText()), resultText1.replace(/\n/g, " | "));

  // 关闭对话框
  await page.keyboard.press("Escape");
  await page.waitForTimeout(600);

  // ---------- T2 重开向导：pending 可勾选（核心修复点） ----------
  await openWizardChooseGeneral();
  const pendingHints = await page.getByText("待绑定数据，勾选后重新导入即可启用").count();
  const pendingChecked = await page
    .locator(".admin__metric-check", { hasText: "待绑定数据，勾选后重新导入即可启用" })
    .locator("input:checked")
    .count();
  const btn2 = page.getByRole("button", { name: /导入 \d+ 条/ });
  const btnEnabled = await btn2.isEnabled().catch(() => false);
  const btnLabel = (await btn2.textContent().catch(() => "")).trim();
  step("T2.1 pending 指标显示可重导提示且默认勾选", pendingHints > 0 && pendingChecked === pendingHints, `提示=${pendingHints} 勾中=${pendingChecked}`);
  step("T2.2 导入按钮可用（修复前被禁用死锁）", btnEnabled, `文案="${btnLabel}"`);
  const activeDisabled = await page
    .locator(".admin__metric-check.is-disabled", { hasText: "已导入" })
    .count();
  step("T2.3 已停用(扩展算子)指标仍禁用", activeDisabled >= 2, `禁用项=${activeDisabled}`);

  // ---------- T3 上传数据集后 UI 重新导入 → 升级 ----------
  await apiUpload(token, "transactions", TX_CSV);
  await apiUpload(token, "customers", CUST_CSV);
  await btn2.click();
  await page.waitForTimeout(2000);
  const dialogText = (await page.locator(".el-dialog").innerText().catch(() => "")).replace(/\n/g, " | ");
  const upgradeMatch = dialogText.match(/升级 (\d+)/);
  step("T3 重新导入进入结果页且升级 > 0", !!upgradeMatch && Number(upgradeMatch[1]) > 0, dialogText.slice(0, 200));

  // ---------- T4 终态：全部导入启用后，无可勾选项 + 空态提示 + 按钮禁用 ----------
  await page.keyboard.press("Escape");
  await page.waitForTimeout(600);
  await openWizardChooseGeneral();
  const remainChecked = await page.locator(".admin__metric-check input:checked").count();
  const remainEnabled = await page.locator(".admin__metric-check:not(.is-disabled)").count();
  const emptyHint = await page.getByText("该行业模板指标均已导入且启用，无需再次导入。").count();
  step("T4 全部启用后无可勾选项且显示空态提示", remainChecked === 0 && remainEnabled === 0 && emptyHint === 1, `勾中=${remainChecked} 可选=${remainEnabled} 提示=${emptyHint}`);
  const btn4 = page.getByRole("button", { name: /导入 \d+ 条/ });
  step("T4.2 终态导入按钮禁用（0 条）", (await btn4.isEnabled().catch(() => true)) === false, `文案="${(await btn4.textContent().catch(() => "")).trim()}"`);

  step("T5 console 无错误", consoleErrors.length === 0, consoleErrors.slice(0, 3).join(" ; "));

  await browser.close();
  const passed = results.filter(Boolean).length;
  console.log(`\n${passed}/${results.length} passed`);
  process.exit(passed === results.length ? 0 : 1);
})().catch((e) => {
  console.error("[FATAL]", e);
  process.exit(1);
});
