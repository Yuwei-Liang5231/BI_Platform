/* 上传对话框修复走查：
 * U1 空表单校验提示
 * U2 选文件+中文名上传成功（对话框关闭、列表刷新出现）
 * U3 重开对话框文件列表已重置（修复「看似已选实则未选」）
 * U4 console 0 错误
 */
const { chromium } = require("playwright-core");

const BASE = "http://127.0.0.1:5183";
const results = [];
const consoleErrors = [];

function step(name, cond, detail = "") {
  results.push([name, cond]);
  console.log(`[${cond ? "PASS" : "FAIL"}] ${name}` + (detail && !cond ? `  <- ${detail}` : ""));
}

(async () => {
  const browser = await chromium.launch({
    executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
    headless: true,
  });
  const page = await browser.newPage({ viewport: { width: 1600, height: 900 } });
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", (e) => consoleErrors.push(String(e)));

  await page.goto(`${BASE}/#/login`, { waitUntil: "networkidle" });
  await page.evaluate(() => localStorage.clear());
  await page.reload({ waitUntil: "networkidle" });
  await page.getByPlaceholder("用户名").fill("admin");
  await page.getByPlaceholder("密码").fill("admin123");
  await page.getByRole("button", { name: /登\s*录/ }).click();
  await page.waitForURL(/#\/dashboard/, { timeout: 15000 });

  await page.goto(`${BASE}/#/datasets`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);

  // U1 打开对话框，不选文件直接点上传 → 校验提示
  await page.getByRole("button", { name: "上传接入" }).click();
  await page.waitForTimeout(400);
  await page.getByRole("button", { name: "上传", exact: true }).click();
  await page.waitForTimeout(600);
  step("U1 空表单校验提示", (await page.content()).includes("请填写数据集名称并选择文件"));

  // U2 填中文名 + 选中文文件名 CSV → 上传成功
  await page.getByPlaceholder("如 orders").fill("监控走查验证");
  await page.setInputFiles("input[type=file]", process.cwd() + "/.tmp/监控测试20260911.csv");
  await page.waitForTimeout(400);
  await page.getByRole("button", { name: "上传", exact: true }).click();
  await page.waitForTimeout(2500);
  step("U2 中文文件名+中文名上传成功", (await page.content()).includes("已接入：监控走查验证"));
  step("U2b 列表出现新数据集", (await page.content()).includes("监控走查验证"));

  // U3 重开对话框：文件列表与表单应已重置 → 直接点上传应提示
  await page.getByRole("button", { name: "上传接入" }).click();
  await page.waitForTimeout(400);
  const residual = await page.locator(".el-upload-list__item").count();
  step("U3 重开对话框文件列表已重置", residual === 0, `残留列表项=${residual}`);
  await page.getByRole("button", { name: "上传", exact: true }).click();
  await page.waitForTimeout(600);
  step("U3b 未选文件提示（不再被残留文件名误导）",
    (await page.content()).includes("请填写数据集名称并选择文件"));

  step("U4 全程 console 0 错误", consoleErrors.length === 0, consoleErrors.join(" | ").slice(0, 300));

  await browser.close();
  const failed = results.filter(([, ok]) => !ok);
  console.log(`\n合计 ${results.length - failed.length} passed, ${failed.length} failed`);
  process.exit(failed.length ? 1 : 0);
})();
