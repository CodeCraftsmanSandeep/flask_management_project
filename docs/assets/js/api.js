/* Talks only to this app's own /api/* endpoints. */

export class ApiError extends Error {
  constructor(message, code = "error", status = 0) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

async function request(url, options) {
  let response;
  try {
    response = await fetch(url, options);
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new ApiError(
      "Could not reach the local server. Is `make run` still running?",
      "network",
      0,
    );
  }
  let body = null;
  try {
    body = await response.json();
  } catch {
    // an empty or non-JSON body is handled below
  }
  if (!response.ok) {
    const detail = body?.error;
    throw new ApiError(
      detail?.message ?? `The server answered ${response.status}.`,
      detail?.code ?? "http_error",
      response.status,
    );
  }
  if (body === null) throw new ApiError("The server sent an unreadable answer.", "bad_json", response.status);
  return body;
}

/*
 * The static website has no server: <html data-static="site"> makes the backtest requests answer
 * from JSON files in the page itself (static-api.js). The live app never sets it.
 */
const STATIC_SITE = typeof document !== "undefined" && document.documentElement.dataset.static === "site";

/** GET JSON. The browser revalidates with the server's ETag, so repeat views are cheap. */
export function getJson(url, { signal } = {}) {
  if (STATIC_SITE) return import("./static-api.js").then((module) => module.staticGet(url, { signal }));
  return request(url, { signal, headers: { Accept: "application/json" } });
}

/** POST JSON. The custom header is what the server's CSRF check looks for. */
export function postJson(url, body = {}) {
  return request(url, {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      "X-Requested-With": "fetch",
    },
    body: JSON.stringify(body),
  });
}

/** DELETE. Same CSRF header as POST. */
export function deleteJson(url) {
  return request(url, {
    method: "DELETE",
    headers: { Accept: "application/json", "X-Requested-With": "fetch" },
  });
}

export const isAbort = (error) => error?.name === "AbortError";
