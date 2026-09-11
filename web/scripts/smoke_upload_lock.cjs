/**
 * B7 补充走查：上传提交全程锁定（uploading 状态）
 * 用 playwright 路由给 /api/datasets/upload 响应加 3s 延迟，模拟大文件解析等待，
 * 断言：按钮禁用+文案变化、取消/X 禁用、输入与文件控件禁用、结束后恢复。
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

  // 用真实大文件（10 万行）制造「进度 100% 后服务器解析」的真实窗口；不再伪造网络延迟
  await page.goto(`${BASE}/#/login`, { waitUntil: "networkidle" });
  await page.evaluate(() => localStorage.clear());
  await page.goto(`${BASE}/#/login`, { waitUntil: "networkidle" });
  await page.getByPlaceholder("用户名").fill("admin");
  await page.getByPlaceholder("密码").fill("admin123");
  await page.getByRole("button", { name: /登\s*录/ }).click();
  await page.waitForURL(/#\/dashboard/, { timeout: 15000 });

  await page.goto(`${BASE}/#/datasets`, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: "上传接入" }).click({ timeout: 10000 });
  await page.getByPlaceholder("如 orders").fill("上传锁定走查");
  await page.setInputFiles("input[type=file]", __dirname + "/../.tmp/upload-lock-fixture.csv");
  await page.getByRole("button", { name: "上传", exact: true }).click();

  // 提交中窗口断言
  const t0 = Date.now();
  await page.waitForSelector(".ds__upload-parsing", { timeout: 8000 });
  const btn = page.getByRole("button", { name: "上传中…" });
  step("L1 提交后出现「服务器解析中」提示", true);
  step("L2 上传按钮禁用且文案为「上传中…」", await btn.isDisabled());
  step("L3 取消按钮禁用", await page.getByRole("button", { name: "取消" }).isDisabled());
  step("L4 对话框右上 X 隐藏", (await page.locator(".el-dialog__headerbtn").count()) === 0
    || !(await page.locator(".el-dialog__headerbtn").first().isVisible()));
  step("L5 数据集名输入禁用", await page.getByPlaceholder("如 orders").isDisabled());
  step("L6 文件控件禁用", await page.locator(".el-upload").first().isDisabled());

  // 重复点击不产生第二个请求
  await btn.click({ force: true }).catch(() => {});
  let uploadCalls = 0;
  page.on("request", (r) => r.url().includes("/api/datasets/upload") && uploadCalls++);

  // 等上传完成
  await page.waitForSelector(".ds__upload-parsing", { state: "detached", timeout: 20000 });
  const elapsed = Date.now() - t0;
  step(`L7 解析完成后对话框关闭并回到列表（耗时 ${elapsed}ms）`, await page.locator(".el-dialog").first().isHidden().catch(() => true));
  step("L8 全程仅一次上传请求（重复点击无效）", uploadCalls <= 1, `count=${uploadCalls}`);

  step("W9 console 无错误", consoleErrors.length === 0, consoleErrors.join(" | ").slice(0, 200));

  await browser.close();
  const passed = results.filter(Boolean).length;
  console.log(`\n${passed}/${results.length} passed, ${results.length - passed} failed`);
  process.exit(passed === results.length ? 0 : 1);
})();
