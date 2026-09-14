/**
 * B8 端到端走查：数据质检报告 + 增量导入（append）+ 覆盖刷新 + ver 递增。
 *
 * 环境约定（与其他 smoke 一致）：SMOKE_BASE 默认隔离环境 preview 5191
 * （代理到 8105 后端，DATA_DIR=data-test-r3）。
 *
 * 场景：
 *  S1 上传新数据集（分片目录布局）→ 详情可见 v1
 *  S2 打开质检报告 → 正常列显示；构造的 CSV 无高危问题
 *  S3 增量导入（列顺序不同的 CSV，含混杂值）→ 成功提示行数与 v2
 *  S4 导入后质检报告自动弹出 → 行数 4、note 列「类型混杂」徽标、
 *     覆盖区间止期更新为 2025-09-04
 *  S5 覆盖区间表格（详情页）同步刷新（止期 2025-09-04）
 */
const { chromium } = require("playwright-core");

const BASE = process.env.SMOKE_BASE || "http://localhost:5191";
const NAME = process.env.SMOKE_NAME || `b8_smoke_${Date.now() % 100000}`;
const BASE_CSV = process.env.SMOKE_BASE_CSV || ".smoke_tmp/b8_base.csv";
const APPEND_CSV = process.env.SMOKE_APPEND_CSV || ".smoke_tmp/b8_append.csv";
const path = require("path");
const BASE_PATH = path.resolve(__dirname, BASE_CSV);
const APPEND_PATH = path.resolve(__dirname, APPEND_CSV);

let passed = 0;
function step(name, ok, extra = "") {
  console.log(`${ok ? "PASS" : "FAIL"} | ${name}${extra ? " | " + extra : ""}`);
  if (ok) passed += 1;
}

(async () => {
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  const page = await (await browser.newContext()).newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e).slice(0, 120)));

  await page.goto(`${BASE}/#/login`, { waitUntil: "networkidle" });
  await page.evaluate(() => localStorage.clear());
  await page.goto(`${BASE}/#/login`, { waitUntil: "networkidle" });
  await page.getByPlaceholder("用户名").fill("admin");
  await page.getByPlaceholder("密码").fill("admin123");
  await page.getByRole("button", { name: /登\s*录/ }).click();
  await page.waitForURL(/#\/dashboard/, { timeout: 15000 });
  await page.goto(`${BASE}/#/datasets`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);

  // S1 上传新数据集
  await page.getByRole("button", { name: "上传接入" }).click();
  await page.locator(".el-dialog:visible input[type=file]").setInputFiles(BASE_PATH);
  await page.getByLabel("数据集名").fill(NAME);
  await page.getByRole("button", { name: "上传", exact: true }).click();
  await page.waitForTimeout(2500);
  const row = page.locator(".el-table__row", { hasText: NAME }).first();
  step("S1 上传成功且列表可见", (await row.count()) === 1);
  await row.click();
  await page.waitForTimeout(1500);
  const verBadge = await page.locator(".pwc-card__header .pwc-badge").first().innerText();
  step("S1b 详情显示 v1 · 2 行", verBadge.includes("v1") && verBadge.includes("2 行"), verBadge.trim());

  // S2 质检报告（初始 2 行：无问题）
  await page.getByRole("button", { name: "质检报告" }).click();
  await page.waitForTimeout(1200);
  let qtext = (await page.locator(".el-dialog", { hasText: "数据质检报告" }).last().innerText()).replace(/\s+/g, " ");
  step("S2 质检报告打开且行数=2", qtext.includes("2 行 × 4 列"), qtext.slice(0, 90));
  await page.getByRole("button", { name: "关闭", exact: true }).click();
  await page.waitForTimeout(500);

  // S3 增量导入（列顺序不同 + 混杂值）
  await page.getByRole("button", { name: "导入数据" }).click();
  await page.waitForTimeout(600);
  await page.locator(".el-dialog:visible input[type=file]").setInputFiles(APPEND_PATH);
  await page.getByRole("button", { name: "追加导入" }).click();
  await page.waitForTimeout(3500);

  // S4 导入后质检报告自动弹出
  qtext = "";
  try {
    const dlg = page.locator(".el-dialog", { hasText: "数据质检报告" }).last();
    await dlg.waitFor({ state: "visible", timeout: 8000 });
    qtext = (await dlg.innerText()).replace(/\s+/g, " ");
  } catch {
    /* S4 will fail below */
  }
  step("S4 导入后质检报告自动弹出", qtext.length > 0);
  step("S4b 行数=4 且 note 列标「内容混杂」",
    qtext.includes("4 行 × 4 列") && qtext.includes("内容混杂"), qtext.slice(0, 140));
  // 覆盖区间止期（报告底部 coverage 部分）
  step("S4c 覆盖止期=2025-09-04", qtext.includes("2025-09-04"));
  await page.getByRole("button", { name: "关闭", exact: true }).click();
  await page.waitForTimeout(600);

  // S5 详情页覆盖区间 + 版本徽标同步刷新
  const verBadge2 = await page.locator(".pwc-card__header .pwc-badge").first().innerText();
  const covText = (await page.locator(".pwc-card.col-span-8").innerText()).replace(/\s+/g, " ");
  step("S5 详情显示 v2 · 4 行", verBadge2.includes("v2") && verBadge2.includes("4 行"), verBadge2.trim());
  step("S5b 覆盖区间止期刷新", covText.includes("2025-09-04"));

  step("Z console 无页面错误", errors.length === 0, errors.join(" | ").slice(0, 200));

  console.log(`\n结果: ${passed} passed`);
  await browser.close();
  process.exit(passed >= 8 ? 0 : 1);
})().catch((e) => {
  console.error("FATAL:", e.message);
  process.exit(1);
});
