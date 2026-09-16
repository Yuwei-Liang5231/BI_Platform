import request from "@/config/request";

export const listProjects = () => request.get("/projects");

export const createProject = (data) => request.post("/projects", data);

export const updateProject = (projectId, data) =>
  request.patch(`/projects/${projectId}`, data);

export const deleteProject = (projectId) =>
  request.delete(`/projects/${projectId}`);
