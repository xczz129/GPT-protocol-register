export async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (response.status === 401) {
    location.replace("/login");
    throw new Error("登录会话已过期");
  }
  if (!response.ok) {
    const detail = data?.detail || data?.message || data?.error || response.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

export function jsonBody(value) {
  return JSON.stringify(value);
}

export async function downloadPost(path, payload, filename) {
  const response = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error(await response.text());
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
  return {
    exported: Number(response.headers.get("X-Exported-Count") || 0),
    missing: Number(response.headers.get("X-Missing-Count") || 0),
  };
}
