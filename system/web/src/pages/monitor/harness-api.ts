import { getApiToken } from "@/lib/api";
import { API } from "./types";

export async function harnessRequest(path: string, options: RequestInit = {}) {
  const headers = new Headers(options.headers);
  const token = getApiToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (options.body) headers.set("Content-Type", "application/json");
  const response = await fetch(`${API}/harness${path}`, {
    ...options, headers, credentials: "same-origin",
  });
  const result = await response.json();
  if (!response.ok || result.status !== 1) {
    throw new Error(result.message || (response.status === 401 || response.status === 403
      ? "Sign in as an administrator to manage Harness."
      : "The Harness request failed."));
  }
  return result.data;
}
