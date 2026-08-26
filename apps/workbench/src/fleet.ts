/** Return a readable, unused managed-container name that remains editable in the create form. */
export function suggestContainerName(existingNames: Iterable<string>, prefix = "workstation"): string { const names = new Set(existingNames); for (let number = 1; ; number += 1) { const candidate = `${prefix}-${number}`; if (!names.has(candidate)) return candidate; } }
export function imageJobLog(job: { logs?: string[] | string; message?: string }): string { return Array.isArray(job.logs) ? job.logs.join("\n") : job.logs || job.message || "Working…"; }
export function apiErrorMessage(status: number, data: { message?: unknown; detail?: unknown; correlation_id?: unknown }): string {
  const reason = typeof data.message === "string" ? data.message : typeof data.detail === "string" ? data.detail : "Request failed";
  const reference = typeof data.correlation_id === "string" ? `, reference ${data.correlation_id}` : "";
  return `${reason} (HTTP ${status}${reference})`;
}
