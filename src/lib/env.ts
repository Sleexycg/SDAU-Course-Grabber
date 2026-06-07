import { decryptSecret, isEncrypted } from "./crypto";
import { BUILTIN_ENV } from "../env-builtin";

export function getEnvString(name: string, fallback?: string): string {
  const value = process.env[name];
  if (value !== undefined) return value;
  // 检查编译内置的默认值（exe 打包时注入）
  const builtin = BUILTIN_ENV[name];
  if (builtin !== undefined) return builtin;
  if (fallback !== undefined) return fallback;
  throw new Error(`Missing required environment variable: ${name}`);
}

export function getEnvNumber(name: string, fallback?: number): number {
  const raw = process.env[name] ?? BUILTIN_ENV[name] ?? String(fallback ?? "");
  const value = Number.parseInt(raw, 10);
  if (Number.isFinite(value)) return value;
  if (fallback !== undefined) return fallback;
  throw new Error(`Missing or invalid numeric environment variable: ${name}`);
}

export function getJwBaseUrl(): string {
  return getEnvString("JW_BASE_URL", "https://jw.sdau.edu.cn").replace(/\/$/, "");
}

export function getJwTimeoutMs(): number {
  return getEnvNumber("JW_TIMEOUT_MS", 12000);
}

export function getJwRetryCount(): number {
  return getEnvNumber("JW_RETRY_COUNT", 2);
}

export function getJwUserAgent(): string {
  return getEnvString(
    "JW_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
  );
}

/** 首次运行标识：检查是否已配置 */
export function isSetupDone(): boolean {
  return process.env.SETUP_DONE === "true" || BUILTIN_ENV.SETUP_DONE === "true";
}

/** 当前是否运行在静态编译 exe 且包含内置配置（非空白 exe） */
export function isBuiltForExe(): boolean {
  try {
    const isExe = process.execPath.endsWith(".exe") && !process.execPath.endsWith("node.exe");
    // 只有内置了有效配置才算"exe 模式"，空白 exe 行为同 CLI
    return isExe && !!BUILTIN_ENV.STUDENT_ID;
  } catch {
    return false;
  }
}

/** 是否运行在 bun 编译的 exe 进程中（无论是否有内置配置） */
export function isExeBinary(): boolean {
  try {
    return process.execPath.endsWith(".exe") && !process.execPath.endsWith("node.exe");
  } catch {
    return false;
  }
}

export function getStudentId(): string {
  return getEnvString("STUDENT_ID");
}

export function getPassword(): string {
  const raw = getEnvString("PASSWORD");
  if (isEncrypted(raw)) {
    const decrypted = decryptSecret(raw);
    if (decrypted !== null) return decrypted;
    // 解密失败，可能是换了机器或配置损坏
    throw new Error("密码解密失败，请重新设置密码（菜单 → 3. 设置环境变量）");
  }
  return raw;
}

export function getTargetCourseIds(): string[] {
  const raw = getEnvString("TARGET_COURSE_IDS", "");
  if (!raw.trim()) return [];
  return raw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

/** 课程名称列表，与 TARGET_COURSE_IDS 一一对应 */
export function getTargetCourseNames(): string[] {
  const raw = process.env.TARGET_COURSE_NAMES?.trim() || BUILTIN_ENV.TARGET_COURSE_NAMES?.trim();
  if (!raw) return [];
  return raw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

export function getWebhookUrl(): string | null {
  const url = process.env.WEBHOOK_URL?.trim() || BUILTIN_ENV.WEBHOOK_URL?.trim();
  return url || null;
}

export function getWebhookMethod(): "GET" | "POST" {
  const method = (process.env.WEBHOOK_METHOD ?? BUILTIN_ENV.WEBHOOK_METHOD ?? "GET").trim().toUpperCase();
  return method === "POST" ? "POST" : "GET";
}

export function getPollIntervalMs(): number {
  return getEnvNumber("POLL_INTERVAL_MS", 800);
}

export function getMinIntervalMs(): number {
  return getEnvNumber("MIN_INTERVAL_MS", 300);
}

export function getMaxIntervalMs(): number {
  return getEnvNumber("MAX_INTERVAL_MS", 5000);
}

/** 自动推断当前学期 */
export function inferCurrentTerm(): string {
  const now = new Date();
  const month = now.getMonth() + 1;
  const startYear = month >= 8 ? now.getFullYear() : now.getFullYear() - 1;
  const endYear = startYear + 1;
  const termNo = month >= 2 && month <= 7 ? 2 : 1;
  return `${startYear}-${endYear}-${termNo}`;
}

/**
 * 读取 TERM 环境变量，未设置时自动推断当前学期。
 * 返回格式如 "2025-2026-1"
 */
export function getTerm(): string {
  const raw = process.env.TERM?.trim() || BUILTIN_ENV.TERM?.trim();
  if (raw && /^\d{4}-\d{4}-\d$/.test(raw)) return raw;
  return inferCurrentTerm();
}
