import request from "@/config/request";

/** 操作审计日志（C2，admin）：分页查询（用户/动作前缀/资源类型/日期过滤）。 */
export const listAuditLogs = (params) =>
  request.get("/audit-logs", {
    params: {
      user_id: params?.userId ?? undefined,
      action: params?.action ?? undefined,
      resource_type: params?.resourceType ?? undefined,
      start: params?.start ?? undefined,
      end: params?.end ?? undefined,
      page: params?.page ?? 1,
      page_size: params?.pageSize ?? 20,
    },
  });

/** 过滤器选项：出现过的用户与动作类型（distinct）。 */
export const auditMeta = () => request.get("/audit-logs/meta");
