import { getWebhookUrl, getWebhookMethod } from "./env";
import { logger } from "./logger";
import type { EnrolledCourseResult } from "../types";

interface NotifyPayload {
  type: "success" | "error" | "info";
  title: string;
  message: string;
  courseId?: string;
  courseName?: string;
  attemptCount?: number;
  elapsed?: string;
}

/**
 * 发送 webhook 通知。
 * 支持 GET（参数拼在 URL 上）和 POST（JSON body）两种模式。
 */
export async function sendWebhook(
  url: string,
  method: "GET" | "POST",
  payload: NotifyPayload
): Promise<void> {
  try {
    if (method === "GET") {
      const params = new URLSearchParams({
        title: payload.title,
        content: payload.message,
        ...(payload.courseName ? { course: payload.courseName } : {}),
      });
      const fullUrl = `${url}${url.includes("?") ? "&" : "?"}${params.toString()}`;
      await fetch(fullUrl, { method: "GET", cache: "no-store" });
    } else {
      await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        cache: "no-store",
      });
    }
    logger.info(`Webhook 推送成功 (${method} → ${url.slice(0, 48)}...)`);
  } catch (error) {
    logger.warn(`Webhook 推送失败: ${error instanceof Error ? error.message : String(error)}`);
  }
}

/**
 * 抢课成功的通知。
 */
export async function notifySuccess(
  webhookUrl: string | null,
  webhookMethod: "GET" | "POST",
  info: {
    courseName: string;
    attemptCount: number;
    elapsed: string;
  }
): Promise<void> {
  const payload: NotifyPayload = {
    type: "success",
    title: "🎉 抢课成功！",
    message: `课程：${info.courseName}\n尝试次数：${info.attemptCount}\n耗时：${info.elapsed}`,
    courseName: info.courseName,
    attemptCount: info.attemptCount,
    elapsed: info.elapsed,
  };

  logger.success(`\n╔══════════════════════════════════╗`);
  logger.success(`║        🎉 抢课成功！             ║`);
  logger.success(`║  课程：${info.courseName.padEnd(22)}║`);
  logger.success(`║  尝试：${String(info.attemptCount).padEnd(22)}║`);
  logger.success(`║  耗时：${info.elapsed.padEnd(22)}║`);
  logger.success(`╚══════════════════════════════════╝`);

  if (webhookUrl) {
    await sendWebhook(webhookUrl, webhookMethod, payload);
  }
}

/**
 * 抢课出错的通知。
 */
export async function notifyError(
  webhookUrl: string | null,
  webhookMethod: "GET" | "POST",
  info: {
    courseName: string;
    error: string;
    attemptCount: number;
  }
): Promise<void> {
  const payload: NotifyPayload = {
    type: "error",
    title: "❌ 抢课异常",
    message: `课程：${info.courseName}\n错误：${info.error}\n已尝试：${info.attemptCount} 次`,
    courseName: info.courseName,
    attemptCount: info.attemptCount,
  };

  logger.error(`抢课异常 [${info.courseName}]: ${info.error}`);

  if (webhookUrl) {
    await sendWebhook(webhookUrl, webhookMethod, payload);
  }
}

/**
 * 推送选课结果查询到 Webhook（ShowDoc 兼容格式）。
 */
export async function notifyQueryResult(
  webhookUrl: string | null,
  webhookMethod: "GET" | "POST",
  result: EnrolledCourseResult
): Promise<void> {
  if (!webhookUrl) return;

  const { term, courses, summary } = result;

  // 按课程类型分组
  const grouped = new Map<string, typeof courses>();
  for (const c of courses) {
    const type = c.courseType || "其他";
    if (!grouped.has(type)) grouped.set(type, []);
    grouped.get(type)!.push(c);
  }

  const lines: string[] = [];
  lines.push(`## 选课结果 · ${term}`);
  lines.push(``);
  lines.push(`共 **${summary.totalCourses}** 门课程，**${summary.totalCredits.toFixed(1)}** 学分`);
  lines.push(``);

  for (const [type, typedCourses] of grouped) {
    lines.push(`**${type}**（${typedCourses.length}门）：`);
    for (const c of typedCourses) {
      const timeInfo =
        c.weekday > 0
          ? `${["", "周一", "周二", "周三", "周四", "周五", "周六", "周日"][c.weekday] || ""} ${c.startSection}-${c.endSection}节`
          : "时间待定";
      const loc = c.location
        ? c.location.replace(/<br\s*\/?>/gi, "/").replace(/[;　]+/g, "/")
        : "";
      const locStr = loc ? ` @ ${loc}` : "";
      lines.push(`- ${c.name}（${c.credit}学分）${c.teacher} ${timeInfo}${locStr}`);
    }
    lines.push(``);
  }

  const message = lines.join("\n");
  const payload: NotifyPayload = {
    type: "info",
    title: `📋 选课结果 · ${term}`,
    message,
  };

  await sendWebhook(webhookUrl, webhookMethod, payload);
  logger.info(`选课结果已推送到 Webhook`);
}
