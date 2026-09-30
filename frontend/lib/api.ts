// The one way the site talks to the backend. Every screen shows these errors as they are:
// if the API is down, the page says so. Nothing here invents data.

export const API_BASE = (process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000").replace(/\/+$/, "");

export class ApiError extends Error {
  status: number;
  unreachable: boolean;
  body: unknown;

  constructor(message: string, status: number, unreachable = false, body: unknown = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.unreachable = unreachable;
    this.body = body;
  }
}

type Options = Omit<RequestInit, "body"> & { json?: unknown; body?: BodyInit };

function messageFrom(body: unknown, status: number): string {
  if (body && typeof body === "object") {
    const record = body as Record<string, unknown>;
    if (typeof record.error === "string") return record.error;
    if (typeof record.detail === "string") return record.detail;
    if (Array.isArray(record.detail)) return "Some details were missing or not valid. Check the form and try again.";
  }
  return `The server answered with an error (${status}).`;
}

export async function api<T>(path: string, options: Options = {}): Promise<T> {
  const { json, headers, ...rest } = options;
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...rest,
      headers: json !== undefined ? { "Content-Type": "application/json", ...headers } : headers,
      body: json !== undefined ? JSON.stringify(json) : options.body,
    });
  } catch {
    throw new ApiError(
      `Can't reach the Praman server at ${API_BASE}. Start the backend, then try again.`,
      0,
      true,
    );
  }
  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (!response.ok) throw new ApiError(messageFrom(body, response.status), response.status, false, body);
  return body as T;
}

export function mediaUrl(path: string): string {
  return `${API_BASE}${path}`;
}

export function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "Something went wrong.";
}
