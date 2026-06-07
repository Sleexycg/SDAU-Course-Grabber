import { getJwBaseUrl, getJwTimeoutMs, getJwRetryCount, getJwUserAgent } from "./env";
import { JwError } from "./errors";

export interface JwHttpResponse {
  status: number;
  text: string;
  cookieHeader: string;
  finalUrl: string;
}

interface RequestOptions {
  method?: "GET" | "POST";
  body?: URLSearchParams;
  cookieHeader?: string;
  referer?: string;
  accept?: string;
}

function mergeCookies(currentHeader: string, setCookie: string[] | undefined): string {
  const jar = new Map<string, string>();

  for (const item of currentHeader.split(";").map((s) => s.trim()).filter(Boolean)) {
    const [name, ...valueParts] = item.split("=");
    if (name) jar.set(name, valueParts.join("="));
  }

  for (const cookie of setCookie ?? []) {
    const [name, ...valueParts] = cookie.split(";")[0].split("=");
    if (name) jar.set(name.trim(), valueParts.join("=").trim());
  }

  return Array.from(jar.entries())
    .map(([k, v]) => `${k}=${v}`)
    .join("; ");
}

function getSetCookieHeaders(res: Response): string[] {
  const getSetCookie = (res.headers as unknown as { getSetCookie?: () => string[] }).getSetCookie;
  if (typeof getSetCookie === "function") {
    return getSetCookie.call(res.headers);
  }
  const raw = res.headers.get("set-cookie");
  return raw ? raw.split(/, (?=[^;]+?=)/g) : [];
}

export async function jwRequest(
  path: string,
  options: RequestOptions = {}
): Promise<JwHttpResponse> {
  const method = options.method ?? "GET";
  const baseUrl = getJwBaseUrl();
  const url = path.startsWith("http") ? path : `${baseUrl}${path}`;
  const retries = getJwRetryCount();

  let attempt = 0;
  let lastError: unknown;

  while (attempt <= retries) {
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), getJwTimeoutMs());

      const res = await fetch(url, {
        method,
        body: options.body,
        headers: {
          "User-Agent": getJwUserAgent(),
          Accept: options.accept ?? "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
          "Content-Type":
            method === "POST" ? "application/x-www-form-urlencoded; charset=UTF-8" : "text/plain",
          ...(options.cookieHeader ? { Cookie: options.cookieHeader } : {}),
          ...(options.referer ? { Referer: options.referer } : {}),
        },
        redirect: "follow",
        signal: controller.signal,
        cache: "no-store",
      });

      clearTimeout(timeout);

      const setCookie = getSetCookieHeaders(res);
      const cookieHeader = mergeCookies(options.cookieHeader ?? "", setCookie);

      return {
        status: res.status,
        text: await res.text(),
        cookieHeader,
        finalUrl: res.url,
      };
    } catch (error) {
      lastError = error;
      attempt += 1;
    }
  }

  if (lastError instanceof Error && lastError.name === "AbortError") {
    throw new JwError("JW_UNAVAILABLE", "请求超时，教务系统无响应");
  }

  throw new JwError("JW_UNAVAILABLE", `教务系统请求失败 (已重试 ${retries} 次)`);
}
