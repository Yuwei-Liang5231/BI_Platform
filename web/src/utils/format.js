/**
 * 展示格式化工具。
 * 留空约定：value === null / undefined → "—"（不完整周期或无数据，绝不显示 0）。
 * 数字约定（2026-09-18 用户修订）：平台所有指标相关数字统一保留 1 位小数（四舍五入
 * + 千分位），百分比同样保留 1 位小数；非指标数字（建模建议综合分、上传进度、
 * 文件大小 formatBytes 等）按各自原有格式不变。
 */

/** Date → "YYYY-MM-DD"（按本地时区，不用 UTC——避免东八区零点前移一天）。 */
export function isoDate(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

export function formatMetricValue(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value !== "number") return String(value);
  // 统一 1 位小数 + 千分位（2026-09-18 修订：指标数字保留 1 位小数）
  return value.toLocaleString("zh-CN", {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  });
}

export function formatPercent(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value !== "number") return String(value);
  // 入参已是百分数单位（后端 change_pct 约定：*100 后的值，如 13.57 = 13.57%），
  // 统一 1 位小数——切勿再乘 100（曾致 -13.57% 显示为 -1357.4%）。
  return `${value.toFixed(1)}%`;
}

/**
 * D19 涨跌语义：方向判定（good=向好 / bad=恶化 / flat），配合文字/三角不单靠颜色。
 * 比率类（如退款率、流失率）上升为恶化（goodWhenUp=false）；总量类上升为向好。
 * 配色（2026-09-15 起红涨绿跌）由 TrendBadge 按变化方向决定，与本函数的 good/bad 解耦。
 */
export function trendDirection(change, goodWhenUp = true) {
  if (change === null || change === undefined) return "none";
  if (change > 0) return goodWhenUp ? "good" : "bad";
  if (change < 0) return goodWhenUp ? "bad" : "good";
  return "flat";
}

export function formatBytes(bytes) {
  if (!bytes && bytes !== 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}
