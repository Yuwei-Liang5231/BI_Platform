/**
 * 展示格式化工具。
 * 留空约定：value === null / undefined → "—"（不完整周期或无数据，绝不显示 0）。
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
  if (Number.isInteger(value)) return value.toLocaleString("zh-CN");
  // 保留 2 位有效小数并千分位
  return value.toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export function formatPercent(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value !== "number") return String(value);
  return `${(value * 100).toFixed(2)}%`;
}

/**
 * D19 涨跌语义：状态色（绿=向好 / 红=恶化）+ 箭头 + 文字，不单靠颜色。
 * 比率类（如退款率、流失率）上升为恶化；总量类上升为向好。
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
