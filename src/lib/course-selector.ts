/**
 * 选课客户端 — 对接山东农业大学教务系统选课接口。
 *
 * 已实现：
 * - queryEnrolledCourses() — 查询已选课程（选课结果）
 *
 * 待实现（需要选课开放后抓包）：
 * - queryAvailableCourses() — 查询可选课程
 * - registerCourse() — 提交选课
 */

import { createHash } from "crypto";

import { JwError } from "./errors";
import { jwRequest } from "./http";
import { getTerm } from "./env";
import { logger } from "./logger";
import type { EnrolledCourse, EnrolledCourseResult, RegisterResult } from "../types";

// ============ 内置学期列表 ============

function buildTermList(): string[] {
  const terms: string[] = [];
  for (let start = 2029; start >= 2020; start -= 1) {
    terms.push(`${start}-${start + 1}-2`);
    terms.push(`${start}-${start + 1}-1`);
  }
  return terms;
}

function inferDefaultTerm(): string {
  return getTerm();
}

// ============ 选课结果查询 ============

/**
 * 查询已选课程（选课结果）。
 *
 * GET /xkgl/loadXsxkjgList?lx=xkrz&type=list&pageNum=1&pageSize=200&xnxqid={term}
 */
export async function queryEnrolledCourses(
  cookieHeader: string,
  term?: string
): Promise<EnrolledCourseResult> {
  const resolvedTerm = term || inferDefaultTerm();

  logger.info(`正在查询 ${resolvedTerm} 的选课结果...`);

  const params = new URLSearchParams({
    lx: "xkrz",
    type: "list",
    pageNum: "1",
    pageSize: "200",
    xnxqid: resolvedTerm,
  });

  const response = await jwRequest(`/xkgl/loadXsxkjgList?${params.toString()}`, {
    method: "GET",
    cookieHeader,
    accept: "application/json, text/javascript, */*; q=0.01",
  });

  // 检查 JSON 是否返回登录页面（会话失效）
  if (/登录|login|请先登录/i.test(response.text) && !response.text.startsWith("{")) {
    throw new JwError("UNAUTHORIZED", "登录状态已失效，请重新登录");
  }

  let payload: unknown;
  try {
    payload = JSON.parse(response.text);
  } catch {
    throw new JwError("JW_UNAVAILABLE", "选课结果接口返回格式异常，非 JSON 数据");
  }

  if (!payload || typeof payload !== "object") {
    throw new JwError("JW_UNAVAILABLE", "选课结果接口返回数据为空");
  }

  const root = payload as Record<string, unknown>;

  // 检查业务状态码
  if (root.code !== 0 && root.code !== 200) {
    throw new JwError("JW_UNAVAILABLE", `选课结果接口返回错误: ${root.msg ?? "未知错误"}`);
  }

  const rawData = root.data;
  if (!Array.isArray(rawData)) {
    throw new JwError("JW_UNAVAILABLE", "选课结果数据格式异常：data 字段不是数组");
  }

  const courses = rawData.map((item: Record<string, unknown>) =>
    parseEnrolledCourseItem(item, resolvedTerm)
  );

  // 统计信息
  const totalCredits = courses.reduce((sum, c) => sum + parseFloat(c.credit || "0"), 0);

  logger.success(`查询成功：共 ${courses.length} 门课程，${totalCredits.toFixed(1)} 学分`);

  return {
    term: resolvedTerm,
    courses,
    summary: {
      totalCourses: courses.length,
      totalCredits,
    },
  };
}

// ============ 单条课程解析 ============

const WEEKDAY_MAP: Record<string, number> = {
  "星期一": 1,
  "星期二": 2,
  "星期三": 3,
  "星期四": 4,
  "星期五": 5,
  "星期六": 6,
  "星期日": 7,
};

/** 节次代码转起始/结束节次： "0102" → { start: 1, end: 2 }， "05060708" → { start: 5, end: 8 } */
function parseSectionCode(code: string): { start: number; end: number } {
  const digits = code.replace(/节/g, "").trim();
  // 每两位一组解析（如 05060708 → [5, 6, 7, 8]）
  const groups: number[] = [];
  for (let i = 0; i < digits.length; i += 2) {
    const n = parseInt(digits.slice(i, i + 2), 10);
    if (Number.isFinite(n)) groups.push(n);
  }
  if (groups.length === 0) return { start: 1, end: 1 };
  return {
    start: groups[0],
    end: groups[groups.length - 1],
  };
}

/** 解析 sksj 字段："星期一 0102节\n星期三 0304节" → 课程时间列表 */
function parseScheduleTime(raw: string | undefined): Array<{
  weekday: number;
  startSection: number;
  endSection: number;
}> {
  if (!raw || !raw.trim()) return [];

  const results: Array<{ weekday: number; startSection: number; endSection: number }> = [];
  const lines = raw.replace(/<br\s*\/?>/gi, "\n").split("\n");

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) continue;

    // 匹配 "星期一 0102节" 或 "星期四 01020304节"
    const match = trimmed.match(/^(星期[一二三四五六日])\s*(\d+(?:节)?)$/);
    if (!match) continue;

    const weekday = WEEKDAY_MAP[match[1]];
    if (!weekday) continue;

    const sectionCode = match[2];
    const { start, end } = parseSectionCode(sectionCode);

    results.push({ weekday, startSection: start, endSection: end });
  }

  return results;
}

/** 解析 skdd 字段（多行教室） */
function parseLocations(raw: string | undefined): string[] {
  if (!raw || !raw.trim()) return [];
  return raw
    .replace(/<br\s*\/?>/gi, "\n")
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
}

function buildEnrolledCourseId(
  term: string,
  kch: string,
  jx0501id?: string
): string {
  const seed = `${term}-${kch}-${jx0501id ?? ""}`;
  return createHash("sha1").update(seed).digest("hex").slice(0, 16);
}

function parseEnrolledCourseItem(
  item: Record<string, unknown>,
  term: string
): EnrolledCourse {
  const name = String(item.kc_mc ?? "").trim();
  const code = String(item.kch ?? "").trim();
  const teacher = String(item.xm ?? "").trim();
  const credit = String(item.xf ?? "0").trim();
  const hours = String(item.zxs ?? "0").trim();
  const courseType = String(item.kclb_mc ?? "").trim(); // 必修/限选/任选
  const typeCode = String(item.kcxz_mc ?? "").trim();   // BK/XY/XR/XF/BS
  const classroom = String(item.skdd ?? "").trim();
  const scheduleRaw = String(item.sksj ?? "").trim();
  const college = String(item.yx_mc ?? "").trim();
  const classGroup = String(item.ktmc ?? "").trim();
  const jx0501id = String(item.jx0501id ?? "").trim();

  const scheduleItems = parseScheduleTime(scheduleRaw);
  const locations = parseLocations(classroom);

  // 该课程可能有多次上课时间（如周一3-4 + 周三1-2），
  // 返回第一个作为主时间，但完整信息放在 rawSchedule 中
  const primarySchedule = scheduleItems[0] ?? {
    weekday: 0,
    startSection: 0,
    endSection: 0,
  };

  return {
    id: buildEnrolledCourseId(term, code, jx0501id),
    name,
    code,
    teacher,
    credit,
    location: locations.length > 0 ? locations.join("/") : classroom.replace(/\n/g, "/").replace(/<br\s*\/?>/gi, "/"),
    courseType,
    typeCode,
    classGroup,
    college,
    totalHours: hours,
    weekday: primarySchedule.weekday,
    startSection: primarySchedule.startSection,
    endSection: primarySchedule.endSection,
    weeks: [], // 选课结果接口不返回周次信息；课表接口有
    term,
    jx0501id,
    // 保留原始明细
    rawSchedule: scheduleItems,
    rawLocations: locations,
  };
}

// ============ 格式化输出 ============

/** 计算字符串在终端中的可视宽度（中文=2，其他=1） */
function visualLen(s: string): number {
  let len = 0;
  for (const ch of s) {
    len += /[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]/.test(ch) ? 2 : 1;
  }
  return len;
}

/** 按可视宽度左对齐填充空格 */
function vpad(s: string, w: number): string {
  const cur = visualLen(s);
  return cur >= w ? s : s + " ".repeat(w - cur);
}

/** 全宽分隔线 ── */
const FULL_W = 76;
function fullDivider(char = "═"): string {
  return char.repeat(FULL_W);
}

/** 生成 section 分隔线，文本居中，两侧 ─ 填满 FULL_W */
function sectionLine(label: string, count?: number): string {
  const text = count !== undefined ? ` ${label} (${count}门) ` : ` ${label} `;
  const textW = visualLen(text);
  const innerW = FULL_W - 2; // 去掉开头的 2 空格缩进
  const dashesTotal = innerW - textW;
  const left = Math.floor(dashesTotal / 2);
  const right = dashesTotal - left;
  return `  ${"─".repeat(left)}${text}${"─".repeat(right)}`;
}

/** 课程类型代码 → 中文名 */
export function typeCodeToLabel(typeCode: string): string {
  const map: Record<string, string> = {
    BK: "必修", XY: "任选", XR: "任选", XF: "限选",
    BS: "实践", XZ: "选修", XG: "选修", XT: "选修", ZY: "专业",
  };
  return map[typeCode] ?? typeCode;
}

/** 星期数字 → 中文 */
export function weekdayLabel(w: number): string {
  const map = ["", "周一", "周二", "周三", "周四", "周五", "周六", "周日"];
  return map[w] ?? `周${w}`;
}

// 每列可视宽度（左对齐）
const W_NAME    = 24;  // 课程名（最长约 12 个汉字）
const W_CREDIT  = 6;   // 学分如 "4.0分"
const W_TEACHER = 10;  // 教师名（最长约 4-5 汉字）
const W_TIME    = 16;  // 时间如 "周五 3-4节"

/** 将 EnrolledCourseResult 格式化为可读文本 */
export function formatEnrolledResult(result: EnrolledCourseResult): string {
  const lines: string[] = [];
  let { courses } = result;

  // 筛重：同名同教师的课程，保留有时间安排的，删除时间待定的
  const seen = new Map<string, EnrolledCourse>();
  for (const c of courses) {
    const key = `${c.name}||${c.teacher}`;
    const existing = seen.get(key);
    if (!existing) {
      seen.set(key, c);
    } else if (existing.weekday === 0 && c.weekday > 0) {
      // 已有的是时间待定，当前有时间 → 替换
      seen.set(key, c);
    }
    // 两者都有时间或都待定 → 保留已有的
  }
  courses = Array.from(seen.values());

  // 按课程类型分组，同时统计各类型的学分
  const grouped = new Map<string, EnrolledCourse[]>();
  const creditByType = new Map<string, number>();
  for (const c of courses) {
    const type = c.courseType || "其他";
    if (!grouped.has(type)) grouped.set(type, []);
    grouped.get(type)!.push(c);
    creditByType.set(type, (creditByType.get(type) || 0) + parseFloat(c.credit || "0"));
  }

  // 指定输出顺序：必修 → 限选 → 任选 → 其他
  const order = ["必修", "限选", "任选"];
  const sortedTypes = order.filter((t) => grouped.has(t));
  for (const t of grouped.keys()) {
    if (!sortedTypes.includes(t)) sortedTypes.push(t);
  }

  for (let i = 0; i < sortedTypes.length; i++) {
    const type = sortedTypes[i];
    const typedCourses = grouped.get(type)!;
    const credits = creditByType.get(type) ?? 0;
    lines.push(sectionLine(`${type}（${typedCourses.length}门-${credits.toFixed(1)}学分）`));
    for (const c of typedCourses) {
      const creditStr = `${parseFloat(c.credit || "0").toFixed(1)}分`;
      const timeInfo =
        c.weekday > 0
          ? `${weekdayLabel(c.weekday)} ${c.startSection}-${c.endSection}节`
          : "时间待定";
      const loc = c.location
        ? c.location.replace(/<br\s*\/?>/gi, "/").replace(/[;　]+/g, "/")
        : "";
      lines.push(
        `  ${vpad(c.name, W_NAME)}  ${vpad(creditStr, W_CREDIT)}  ${vpad(c.teacher, W_TEACHER)}  ${vpad(timeInfo, W_TIME)}  ${loc}`
      );
    }
    // 组之间加空行，末尾不加
    if (i < sortedTypes.length - 1) {
      lines.push("");
    }
  }

  return lines.join("\n");
}

// ============ 待实现 ============

export async function queryAvailableCourses(
  cookieHeader: string,
  _params?: { term?: string; keyword?: string }
): Promise<never> {
  logger.warn("queryAvailableCourses: 尚未实现，需选课开放后抓包");
  throw new JwError("JW_UNAVAILABLE", "可选课程查询接口尚未实现");
}

export async function registerCourse(
  cookieHeader: string,
  _courseId: string,
  _extraParams?: Record<string, string>
): Promise<RegisterResult> {
  logger.warn("registerCourse: 尚未实现，需选课开放后抓包");
  throw new JwError("JW_UNAVAILABLE", "选课提交接口尚未实现");
}
