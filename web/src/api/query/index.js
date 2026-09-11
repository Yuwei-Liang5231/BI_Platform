import request from "@/config/request";

/**
 * 单值计算。body: { metric: id|code, start, end, compare: none|mom|yoy }
 * 返回 { value, compare_value, change, period_complete, ... }（以 B3 契约为准）。
 * 周期不完整时 value=null、period_complete=false → 前端显示 "—"。
 */
export const metricValue = (data) => request.post("/query/metric-value", data);

/** 拿 CSV blob（UTF-8 BOM），不触发浏览器下载——画序列图等内部用途走这个。 */
export const exportCsvBlob = (data) =>
  request.post("/query/export", data, { responseType: "blob" });

/** 导出 CSV 并触发浏览器下载（仅用户点「导出」按钮时调用）。 */
export const exportMetric = async (data) => {
  const response = await exportCsvBlob(data);
  const { headers, data: blob } = response;
  const disposition = headers["content-disposition"] || "";
  const match = disposition.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i);
  const filename = match ? decodeURIComponent(match[1]) : "export.csv";
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
};
