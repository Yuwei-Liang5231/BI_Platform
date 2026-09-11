/* B7 前后端联调走查：登录 → 看板 → 目录搜索 → 详情（ver1 当前值+趋势图）→
 * API 改口径 → 刷新验证 ver2 新值全局生效（页面值 == API 值）+ 变更历史留痕 → console 0 错误。
 * 运行：NODE_PATH=<workspace>/node_modules node scripts/smoke_b7_web.cjs
 */
const { chromium } = require("playwright-core");

const BASE = "http://127.0.0.1:5183";
const API = "http://127.0.0.1:8102/api";
const results = [];
const consoleErrors = [];

function step(name, cond, detail = "") {
  results.push([name, cond]);
  console.log(`[${cond ? "PASS" : "FAIL"}] ${name}` + (detail && !cond ? `  <- ${detail}` : ""));
}

async function api(h, path, opts = {}) {
  const r = await fetch(`${API}${path}`, {
    ...opts,
    headers: { "Content-Type": "application/json", ...h },
  });
  return { status: r.status, json: await r.json().catch(() => null) };
}

(async () => {
  const h = {};
  await api(h, "/auth/login", {
    method: "POST",
    body: JSON.stringify({ username: "admin", password: "admin123" }),
  }).then(({ json }) => { h.Authorization = `Bearer ${json.data.token}`; });

  // 详情页默认区间 = 上一个自然月，与页面同源计算
  const now = new Date();
  const rs = new Date(now.getFullYear(), now.getMonth() - 1, 1);
  const re = new Date(now.getFullYear(), now.getMonth(), 0);
  const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const range = { start: iso(rs), end: iso(re) };

  const browser = await chromium.launch({
    executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
    headless: true,
  });
  const page = await browser.newPage({ viewport: { width: 1600, height: 900 } });
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", (e) => consoleErrors.push(String(e)));

  // 1 登录（清空 localStorage，排除上一轮走查的选中项残留）
  await page.goto(`${BASE}/#/login`, { waitUntil: "networkidle" });
  await page.evaluate(() => localStorage.clear());
  await page.reload({ waitUntil: "networkidle" });
  await page.getByPlaceholder("用户名").fill("admin");
  await page.getByPlaceholder("密码").fill("admin123");
  await page.getByRole("button", { name: /登\s*录/ }).click();
  await page.waitForURL(/#\/dashboard/, { timeout: 15000 });
  await page.waitForTimeout(1500);
  step("W1 登录成功进入看板", page.url().includes("#/dashboard"), page.url());

  // 2 看板渲染
  const dashText = await page.content();
  step("W2 看板页渲染（统一看板 + 导航）",
    dashText.includes("统一看板") && dashText.includes("指标目录"), "");

  // 3 指标目录搜索夹具指标
  const code = process.env.B7_CODE || "b7_gmv_r5";
  const table = code.replace("b7_gmv", "b7_orders");
  await page.goto(`${BASE}/#/metrics`, { waitUntil: "networkidle" });
  await page.getByPlaceholder("搜索中文名 / 别名 / 编码").fill(code);
  await page.getByRole("button", { name: "搜索" }).click();
  await page.waitForTimeout(1200);
  step("W3 目录搜索命中 B7联调-已支付GMV",
    (await page.content()).includes("B7联调-已支付GMV"), "");

  // 4 进详情：ver1 当前值非 "—"（默认区间有数）+ 趋势图 canvas
  await page.getByText("B7联调-已支付GMV").first().click();
  await page.waitForURL(/#\/metrics\/\d+/, { timeout: 15000 });
  await page.waitForTimeout(2500);
  const v1Text = (await page.locator(".detail__value").first().textContent() || "").trim();
  step("W4 详情当前值非空且为数字（ver1）", /^\d[\d,.\s¥%]*$/.test(v1Text), `显示=${v1Text}`);
  const canvasCount = await page.locator("canvas").count();
  step("W5 趋势图 canvas 渲染", canvasCount >= 1, `canvas=${canvasCount}`);

  // 5 API 改口径（已支付 → 已退款）
  const mid = page.url().match(/#\/metrics\/(\d+)/)[1];
  const pr = await api(h, `/metrics/${mid}`, {
    method: "PATCH",
    body: JSON.stringify({
      calc_rule: {
        base_aggregation: "sum",
        source: { table: table, column: "pay_amount", filter: "order_status = '已退款'" },
        time_field: "order_date",
      },
      reason: "B7 联调：口径切换为已退款金额",
    }),
  });
  step("W6 API 改口径成功 ver=2",
    pr.status === 200 && pr.json?.data?.ver === 2, JSON.stringify(pr.json).slice(0, 200));

  // 6 页面刷新：ver2 数值与 API 一致（全局生效）+ 变更历史留痕
  const qv = await api(h, "/query/metric-value", {
    method: "POST",
    body: JSON.stringify({ metric: mid, start: range.start, end: range.end }),
  });
  const expected = Number(qv.json.data.value).toLocaleString("zh-CN");
  await page.reload({ waitUntil: "networkidle" });
  await page.waitForTimeout(2500);
  const v2Text = (await page.locator(".detail__value").first().textContent() || "").trim();
  step(`W7 刷新后当前值=${expected}（ver2，与 API 同源一致）`, v2Text === expected, `显示=${v2Text}`);
  step("W8 变更历史显示 reason 留痕",
    (await page.content()).includes("B7 联调：口径切换为已退款金额"), "");

  step("W9 全程 console 0 错误", consoleErrors.length === 0, consoleErrors.join(" | ").slice(0, 400));

  await browser.close();
  const failed = results.filter(([, ok]) => !ok);
  console.log(`\n合计 ${results.length - failed.length} passed, ${failed.length} failed`);
  process.exit(failed.length ? 1 : 0);
})();
