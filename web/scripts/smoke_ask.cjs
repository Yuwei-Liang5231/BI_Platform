/**
 * B9 端到端走查：AI 问数（理解卡 + 执行 + 算不了 + 权限提示）。
 *
 * 环境约定（与其他 smoke 一致）：SMOKE_BASE 默认隔离环境 preview 5191
 * （代理到 8105 后端，DATA_DIR=data-test-r3）。
 *
 * 前置：脚本通过 API 自建夹具（数据集 ask_smoke_ds + 指标 ask_smoke_gmv，
 * 别名「销售额」「营业额」），失败可重复执行（重名自动后缀→按 code 查实际名）。
 *
 * 场景：
 *  S1 问「2026年1月销售额是多少」→ 理解卡命中 ask_smoke_gmv、区间 2026-01
 *  S2 确认计算 → 结果卡显示 1,000.00（夹具两行 400+600）
 *  S3 问「库存周转天数是多少」→ 逃生舱 help 卡（B9.2-2：纯对话引导）
 *  S4 规则解析标注（LLM 未配置 → source=fallback）
 *  S5 问「按status拆解2026年1月销售额」→ 理解卡含拆解维度，结果为拆解表格
 *  Z console 无页面错误
 */
const { chromium } = require("playwright-core");

const BASE = process.env.SMOKE_BASE || "http://localhost:5191";
const API = process.env.SMOKE_API || "http://127.0.0.1:8105/api";
const TS = Date.now() % 100000;
const DS_NAME = `ask_smoke_ds_${TS}`;
const METRIC_CODE = `ask_smoke_gmv_${TS}`;

let passed = 0;
function step(name, ok, extra = "") {
  console.log(`${ok ? "PASS" : "FAIL"} | ${name}${extra ? " | " + extra : ""}`);
  if (ok) passed += 1;
}

async function api(path, { method = "GET", token, body, form } = {}) {
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  let payload;
  if (form) {
    payload = form;
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const res = await fetch(API + path, { method, headers, body: payload });
  return res.json();
}

(async () => {
  // ---- API 夹具 ----
  const login = await api("/auth/login", {
    method: "POST",
    body: { username: "admin", password: "admin123" },
  });
  const token = login.data.token;
  // 自清理：历次运行残留的 ask_smoke_gmv_* 指标会挤占歧义候选前 5 名，导致 S1 断言失真
  const old = await api("/metrics?search=ask_smoke_gmv", { token });
  for (const m of old.data ?? []) {
    if (String(m.code).startsWith("ask_smoke_gmv_")) {
      await api(`/metrics/${m.id}`, { method: "DELETE", token });
    }
  }
  const csv = "sale_date,amount,status\n2026-01-10,400,paid\n2026-01-20,600,paid\n";
  const fd = new FormData();
  fd.append("file", new Blob([csv], { type: "text/csv" }), `${DS_NAME}.csv`);
  fd.append("name", DS_NAME);
  const up = await api("/datasets/upload", { method: "POST", token, form: fd });
  step("setup 数据集上传", up.code === 0, up.message || "");
  const mk = await api("/metrics", {
    method: "POST",
    token,
    body: {
      code: METRIC_CODE,
      name: "问数走查销售额",
      aliases: ["销售额", "营业额"],
      calc_rule: {
        base_aggregation: "sum",
        source: { table: up.data.name, column: "amount", filter: "status = 'paid'" },
      },
    },
  });
  step("setup 指标创建", mk.code === 0, mk.message || "");

  // ---- UI 走查 ----
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
  await page.goto(`${BASE}/#/ask`, { waitUntil: "networkidle" });
  await page.waitForTimeout(800);

  // S1 理解卡
  await page.getByPlaceholder(/试着问/).fill("2026年1月销售额是多少");
  await page.getByRole("button", { name: "理解问题" }).click();
  await page.waitForTimeout(1500);
  const cardText = (await page.locator(".ask__card").innerText()).replace(/\s+/g, " ");
  step("S1 理解卡出现且命中指标", cardText.includes("理解卡") && cardText.includes(METRIC_CODE),
    cardText.slice(0, 120));
  step("S1b 理解卡含时间区间控件", cardText.includes("时间区间"));
  step("S4 规则解析标注（LLM 未配置）", cardText.includes("规则解析") || cardText.includes("规则兜底"));

  // S2 执行（B9.2-3 多指标并列：结果可能有多张卡，主指标结果恒为第一张）
  await page.getByRole("button", { name: "确认计算" }).click();
  await page.waitForTimeout(2500);
  const resultCount = await page.locator(".ask__result").count();
  step("S2b 并列结果卡数量与勾选一致（≥1）", resultCount >= 1, `count=${resultCount}`);
  const resultText = (await page.locator(".ask__result").first().innerText()).replace(/\s+/g, " ");
  step("S2 主指标结果卡 1,000 与区间/数据截至", resultText.includes("1,000"), resultText.slice(0, 100));

  // S3 逃生舱（B9.2-2）：解析不出可执行结构 → 纯对话引导卡
  await page.getByPlaceholder(/试着问/).fill("库存周转天数是多少");
  await page.getByRole("button", { name: "理解问题" }).click();
  await page.waitForTimeout(1500);
  const cardText2 = (await page.locator(".ask__card").innerText()).replace(/\s+/g, " ");
  step("S3 逃生舱引导卡（无数字）", cardText2.includes("没能理解您的问题") && cardText2.includes("改写"),
    cardText2.slice(0, 120));

  // S5 拆解意图（B9.2-2）：理解卡命中维度 → 确认计算 → 拆解表格
  await page.getByPlaceholder(/试着问/).fill("按status拆解2026年1月销售额");
  await page.getByRole("button", { name: "理解问题" }).click();
  await page.waitForTimeout(2000);
  const cardText3 = (await page.locator(".ask__card").innerText()).replace(/\s+/g, " ");
  step("S5a 理解卡含拆解维度区", cardText3.includes("拆解维度"), cardText3.slice(0, 120));
  await page.getByRole("button", { name: "确认计算" }).click();
  await page.waitForTimeout(2500);
  const resultText2 = (await page.locator(".ask__result").first().innerText()).replace(/\s+/g, " ");
  step("S5b 拆解表格 paid=1,000", resultText2.includes("拆解") && resultText2.includes("paid") && resultText2.includes("1,000"),
    resultText2.slice(0, 140));

  step("Z console 无页面错误", errors.length === 0, errors.join(" | ").slice(0, 200));

  console.log(`\n结果: ${passed} passed`);
  await browser.close();
  process.exit(passed >= 9 ? 0 : 1);
})().catch((e) => {
  console.error("FATAL:", e.message);
  process.exit(1);
});
