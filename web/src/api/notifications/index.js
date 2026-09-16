import request from "@/config/request";

/** 当前用户站内通知（最近 50 条）。params: { project_id?, unread_only? } */
export const listNotifications = (params) =>
  request.get("/notifications", { params });

/** 未读数（导航栏小红点）。projectId: 项目 id（数字/可空）——注意参数必须是
 *  对象或 undefined，传裸数字会触发 axios toFormData "target must be an object"。 */
export const unreadCount = (projectId) =>
  request.get("/notifications/unread-count", {
    params: projectId ? { project_id: projectId } : undefined,
  });

/** 单条已读。 */
export const markRead = (id) => request.post("/notifications/read", { id });

/** 全部已读（project_id 指定时仅该项目范围）。 */
export const markAllRead = (projectId) =>
  request.post("/notifications/read-all", null, {
    params: projectId ? { project_id: projectId } : undefined,
  });
