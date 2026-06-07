import * as dotenv from "dotenv";
import { existsSync, readFileSync } from "fs";
import { resolve } from "path";
import { createInterface } from "readline/promises";
import { emitKeypressEvents } from "readline";

let rl = createInterface({ input: process.stdin, output: process.stdout });

// 优先加载 .env.local，其次 .env
dotenv.config({ path: resolve(process.cwd(), ".env.local") });
dotenv.config({ path: resolve(process.cwd(), ".env") });

import {
  getStudentId,
  getPassword,
  getTargetCourseIds,
  getTargetCourseNames,
  getWebhookUrl,
  getWebhookMethod,
  getPollIntervalMs,
  getMinIntervalMs,
  getMaxIntervalMs,
  getJwBaseUrl,
  getTerm,
  isSetupDone,
  isExeBinary,
} from "./lib/env";
import { logger } from "./lib/logger";
import { login } from "./lib/session";
import { jwRequest } from "./lib/http";
import { startTask, stopTask, listTaskStatuses, setTargetCount } from "./lib/grabber";
import {
  queryEnrolledCourses,
  formatEnrolledResult,
} from "./lib/course-selector";
import { encryptSecret } from "./lib/crypto";
import { notifyQueryResult } from "./lib/notifier";
import type { GrabTaskConfig } from "./types";

// ==================== 交互式学期选择器 ====================

interface TermParts {
  start: number;
  end: number;
  num: number;
}

function parseTerm(term: string): TermParts | null {
  const m = term.match(/^(\d{4})-(\d{4})-(\d)$/);
  if (!m) return null;
  return { start: parseInt(m[1]), end: parseInt(m[2]), num: parseInt(m[3]) };
}

function formatTerm(p: TermParts): string {
  return `${p.start}-${p.end}-${p.num}`;
}

function prevTerm(term: string): string {
  const p = parseTerm(term);
  if (!p) return term;
  if (p.num === 2) return formatTerm({ start: p.start, end: p.end, num: 1 });
  return formatTerm({ start: p.start - 1, end: p.end - 1, num: 2 });
}

function nextTerm(term: string): string {
  const p = parseTerm(term);
  if (!p) return term;
  if (p.num === 1) return formatTerm({ start: p.start, end: p.end, num: 2 });
  return formatTerm({ start: p.start + 1, end: p.end + 1, num: 1 });
}

/** 交互式学期翻滚选择 — 按 W/S 或 ↑/↓ 即时切换，Enter 确认 */
async function selectTermInteractive(defaultTerm: string, boxTitle: string = "查询选课结果"): Promise<string> {
  // 临时切换到 raw mode 捕获按键，但不关闭 rl（避免 readline 状态损坏）
  const stdin = process.stdin;
  const wasRaw = stdin.isRaw;
  if (stdin.isTTY) stdin.setRawMode(true);
  emitKeypressEvents(stdin);

  return new Promise<string>((resolve) => {
    let current = defaultTerm;

    function onKeypress(_str: string, key: { name: string; ctrl: boolean }) {
      if (key.ctrl && (key.name === "c" || key.name === "d")) {
        cleanup();
        process.exit(0);
        return;
      }

      if (key.name === "w" || key.name === "up") {
        current = prevTerm(current);
      } else if (key.name === "s" || key.name === "down") {
        current = nextTerm(current);
      } else if (key.name === "return" || key.name === "enter") {
        cleanup();
        resolve(current);
        return;
      }

      render();
    }

    function render() {
      console.clear();
      printBox(boxTitle);
      const prefix = boxTitle === "抢课模式" ? "新学期" : "当前学期";
      console.log(`\n${prefix}：${current}（W/S键切换学期）`);
    }

    function cleanup() {
      stdin.removeListener("keypress", onKeypress);
      if (stdin.isTTY) stdin.setRawMode(wasRaw ?? false);
    }

    stdin.on("keypress", onKeypress);
    render();
  });
}

// ==================== UI 工具 ====================

const BOX_W = 36;

function visualWidth(s: string): number {
  let w = 0;
  for (const ch of s) {
    w += /[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]/.test(ch) ? 2 : 1;
  }
  return w;
}

function boxLine(text: string): string {
  const inner = BOX_W - 2;
  const tw = visualWidth(text);
  const pad = inner - tw;
  const left = Math.floor(pad / 2);
  const right = pad - left;
  return `║${" ".repeat(left)}${text}${" ".repeat(right)}║`;
}

function boxBorder(char: "╔" | "╚"): string {
  return `${char}${"═".repeat(BOX_W - 2)}${char === "╔" ? "╗" : "╝"}`;
}

function printBox(title: string) {
  console.log(boxBorder("╔"));
  console.log(boxLine(title));
  console.log(boxBorder("╚"));
}

async function showMenu() {
  console.clear();

  if (!isSetupDone()) {
    console.log("检测到尚未配置账号信息，请先设置环境变量\n");
    await rl.question("  按回车键开始配置...");
    await runConfig();
    return;
  }

  printBox("SDAU-Course-Grabber");
  console.log(``);
  console.log(`  1. 查询选课结果`);
  console.log(`  2. 启动抢课`);
  if (!isExeBinary()) {
    console.log(`  3. 设置环境变量`);
    console.log(`  4. 导出 exe`);
  } else {
    console.log(`  3. 查看信息`);
  }
  console.log(`  0. 退出`);
  console.log(``);

  const maxChoice = isExeBinary() ? "3" : "4";
  const answer = await rl.question(`  请选择 (0-${maxChoice}): `);
  const choice = answer.trim();

  switch (choice) {
    case "1":
      await runQueryInteractive();
      break;
    case "2":
      await runGrabInteractive();
      break;
    case "3":
      if (isExeBinary()) {
        await runInfo();
      } else {
        await runConfig();
      }
      break;
    case "4":
      if (!isExeBinary()) {
        await runExportExe();
      } else {
        await waitKey();
      }
      break;
    case "0":
      console.log("再见！");
      process.exit(0);
    default:
      console.log("无效选择，请按回车键继续...");
      await rl.question("");
      await showMenu();
  }
}

// ==================== 查询选课结果 ====================

async function runQueryInteractive() {
  const studentId = getStudentId();
  const password = getPassword();

  if (!studentId || !password) {
    logger.error("请先设置学号和密码（菜单 → 3. 设置环境变量）");
    await waitKey();
    return;
  }

  const defaultTerm = getTerm();
  const term = await selectTermInteractive(defaultTerm);

  logger.info("正在登录教务系统...");
  let cookieHeader: string;
  try {
    cookieHeader = await login(studentId, password);
    logger.success("登录成功 ✅\n");
  } catch (error) {
    logger.error(`登录失败: ${error instanceof Error ? error.message : String(error)}`);
    await waitKey();
    return;
  }

  try {
    const result = await queryEnrolledCourses(cookieHeader, term);
    console.log("\n" + formatEnrolledResult(result) + "\n");

    if (!isExeBinary()) {
      const whUrl = getWebhookUrl();
      if (whUrl) {
        const divider = "─".repeat(76);
        console.log(`  ${divider}`);
        const pushAns = (await rl.question("\n  是否推送选课结果到 Webhook？(y/N): ")).trim().toLowerCase();
        if (pushAns === "y" || pushAns === "yes") {
          logger.info("正在推送选课结果...");
          await notifyQueryResult(whUrl, getWebhookMethod(), result);
        }
      }
    }
  } catch (error) {
    logger.error(`查询失败: ${error instanceof Error ? error.message : String(error)}`);
  }

  await waitKey();
}

// ==================== 课程列表加载 ====================

interface CourseItem {
  id: string;
  name: string;
}

const COURSE_LIST_FILE = "courses.json";

/** 加载 courses.json 中的课程列表 */
function loadCourseList(): CourseItem[] {
  try {
    if (!existsSync(COURSE_LIST_FILE)) return [];
    const raw = readFileSync(COURSE_LIST_FILE, "utf8");
    const list: unknown = JSON.parse(raw);
    if (!Array.isArray(list)) return [];
    return list.filter((item): item is CourseItem =>
      typeof item === "object" && item !== null && typeof item.id === "string" && typeof item.name === "string"
    );
  } catch {
    return [];
  }
}

/** 交互式从课程列表中选课 — 返回课程ID和名称 */
async function selectCoursesFromList(): Promise<Array<{ id: string; name: string }>> {
  const courseList = loadCourseList();
  if (courseList.length === 0) return [];

  console.clear();
  printBox("选择课程");
  console.log("");
  console.log("  可选课程：");
  console.log("");
  const maxLen = String(courseList.length).length;
  for (let i = 0; i < courseList.length; i++) {
    console.log(`  ${String(i + 1).padStart(maxLen)}. ${courseList[i].name}（${courseList[i].id}）`);
  }
  console.log("");

  const ans = await rl.question("  请选择课程序号（多个用逗号/空格分隔）: ");
  const indices = ans
    .split(/[,，\s]+/)
    .map((s) => parseInt(s.trim(), 10))
    .filter((n) => Number.isFinite(n) && n >= 1 && n <= courseList.length);

  if (indices.length === 0) return [];

  return [...new Set(indices)].map((i) => ({ ...courseList[i - 1] }));
}

// ==================== 抢课 ====================

async function runGrabInteractive() {
  const studentId = getStudentId();
  const password = getPassword();

  if (!studentId || !password) {
    console.clear();
    printBox("抢课模式");
    console.log("");
    logger.error("请先设置学号和密码（菜单 → 3. 设置环境变量）");
    await waitKey();
    return;
  }

  const currentTerm = getTerm();
  const defaultGrabTerm = nextTerm(currentTerm);

  let term: string;
  if (isExeBinary()) {
    // exe 模式：抢课学期锁死为当前学期的下一学期
    term = defaultGrabTerm;
  } else {
    term = await selectTermInteractive(defaultGrabTerm, "抢课模式");

    if (term !== defaultGrabTerm) {
      console.log("");
      logger.warn(`⚠️  注意：您选择的学期（${term}）不是当前学期的下一学期（${defaultGrabTerm}），请确认该学期已开放选课`);
      const confirmTerm = await rl.question("  确认使用该学期继续？(Y/n): ");
      if (confirmTerm.trim().toLowerCase() === "n") {
        logger.info("已取消");
        await waitKey();
        return;
      }
    }
  }

  // 统一输出学期
  console.clear();
  printBox("抢课模式");
  console.log(`目标学期：${term}`);

  let courseIds = getTargetCourseIds();
  let courseName = "";

  // 课程名称优先从 TARGET_COURSE_NAMES 取（bat 来自 env，exe 来自内置）
  const names = getTargetCourseNames();
  if (names.length === courseIds.length) {
    courseName = names.join("、");
  }

  if (courseIds.length === 0 && !isExeBinary()) {
    // 有 courses.json 则从列表选，跳过手动输入课程 ID 和课程名称
    const selected = await selectCoursesFromList();
    if (selected.length > 0) {
      courseIds = selected.map((c) => c.id);
      courseName = selected.map((c) => c.name).join("、");
    }
  }

  if (courseIds.length === 0) {
    const input = await rl.question(
      "  请输入要抢的课程 ID（多个用逗号分隔）: "
    );
    courseIds = input
      .split(/[,，]/)
      .map((s) => s.trim())
      .filter(Boolean);
  }

  if (courseIds.length === 0) {
    logger.error("未输入任何课程 ID");
    await waitKey();
    return;
  }

  if (!courseName) {
    courseName = await rl.question(
      `  课程名称（用于日志显示，可留空）: `
    );
  }

  const displayName = courseName || courseIds.join(", ");
  if (isExeBinary()) {
    console.log(`目标课程: ${displayName}`);
  } else {
    console.log(`\n目标课程: ${displayName}`);
  }
  console.log(`轮询间隔: ${getPollIntervalMs()}ms`);
  if (!isExeBinary()) {
    logger.info(`Webhook: ${getWebhookUrl() ?? "未配置"}`);
  }
  console.log("");
  const totalSelected = courseIds.length;

  // 询问要抢几门课
  let targetCount: number;
  const maxCount = Math.min(7, totalSelected);
  while (true) {
    const ans = await rl.question(
      `  请输入抢课数（最高为${maxCount}）：`
    );
    const trimmed = ans.trim();
    if (trimmed === "") {
      targetCount = 1;
      break;
    }
    const n = parseInt(trimmed, 10);
    if (isNaN(n) || n < 1 || n > maxCount || String(n) !== trimmed) {
      logger.warn(`  请输入 1-${maxCount} 之间的数字`);
      continue;
    }
    targetCount = n;
    break;
  }

  const confirm = await rl.question("  确认启动抢课? (Y/n): ");
  if (confirm.trim().toLowerCase() !== "y") {
    logger.info("已取消");
    await waitKey();
    return;
  }

  logger.info("正在登录教务系统...");
  let cookieHeader: string;
  try {
    cookieHeader = await login(studentId, password);
    logger.success("登录成功 ✅\n");
  } catch (error) {
    logger.error(`登录失败: ${error instanceof Error ? error.message : String(error)}`);
    await waitKey();
    return;
  }

  const commonConfig: Partial<GrabTaskConfig> = {
    webhookUrl: getWebhookUrl() ?? undefined,
    webhookMethod: getWebhookMethod(),
    initialIntervalMs: getPollIntervalMs(),
    minIntervalMs: getMinIntervalMs(),
    maxIntervalMs: getMaxIntervalMs(),
    targetSuccessCount: targetCount,
  };

  const taskIds: string[] = [];
  const courseNames = getTargetCourseNames();
  // 设置全局抢课目标
  setTargetCount(targetCount);
  for (let i = 0; i < courseIds.length; i++) {
    const courseId = courseIds[i];
    // 优先用 env 中对应的课程名称，其次用共享名称，最后用课程 ID
    const perName = i < courseNames.length ? courseNames[i] : "";
    const taskId = await startTask(cookieHeader, studentId, password, {
      courseId,
      courseName: perName || courseName || courseId,
      ...commonConfig,
    });
    taskIds.push(taskId);
  }

  logger.info("\n🎯 抢课进行中... 按回车键返回菜单\n");
  await rl.question("");

  logger.info("正在停止所有任务...");
  for (const taskId of taskIds) {
    stopTask(taskId);
  }

  const tasks = listTaskStatuses();
  for (let i = 0; i < tasks.length; i++) {
    const task = tasks[i];
    const icon = task.status === "success" ? "✅" : "⏹️";
    logger.info(
      `${icon} [${task.courseName}] ${task.status === "success" ? "抢课成功!" : "已停止"} | 尝试 ${task.attemptCount} 次`
    );
    if (task.lastMessage) logger.info(`   最后消息: ${task.lastMessage}`);
    if (i < tasks.length - 1) console.log("");
  }

  await waitKey();
}

// ==================== 配置环境变量 ====================

async function runConfig() {
  console.clear();
  printBox("设置环境变量");
  console.log("");

  const currentStudentId = process.env.STUDENT_ID?.trim() || "";
  const currentPasswordRaw = process.env.PASSWORD?.trim() || "";
  const currentWebhook = process.env.WEBHOOK_URL?.trim() || "";
  const currentTerm = process.env.TERM?.trim() || getTerm();
  const currentInterval = Number(process.env.POLL_INTERVAL_MS) || 800;

  console.log("  留空则保持当前值不变\n");

  const sid = (await rl.question(`  学号 [${currentStudentId}]: `)).trim();
  const pwd = (await rl.question(`  密码 [${currentPasswordRaw ? "已设置" : "未设置"}]: `)).trim();
  const term = (await rl.question(`  学期 [${currentTerm}]: `)).trim();
  const webhook = (await rl.question(`  Webhook 地址（可为空） [${currentWebhook || "未设置"}]: `)).trim();
  const interval = (await rl.question(`  轮询间隔(ms) [${currentInterval}]: `)).trim();

  // 选择课程（有 courses.json 时直接展示）
  let courseIds: string[];
  let courseNames: string[];
  const courseList = loadCourseList();
  if (courseList.length > 0) {
    console.clear();
    printBox("设置环境变量 — 选择课程");
    console.log("");
    console.log("  可选课程（留空则保持当前值不变）：");
    console.log("");
    const maxLen = String(courseList.length).length;
    for (let i = 0; i < courseList.length; i++) {
      console.log(`  ${String(i + 1).padStart(maxLen)}. ${courseList[i].name}（${courseList[i].id}）`);
    }
    console.log("");
    const sel = (await rl.question("  请选择课程序号（多个用逗号/空格分隔）: ")).trim();
    if (sel) {
      const indices = sel
        .split(/[,，\s]+/)
        .map((s) => parseInt(s.trim(), 10))
        .filter((n) => Number.isFinite(n) && n >= 1 && n <= courseList.length);
      if (indices.length > 0) {
        const picked = [...new Set(indices)].map((i) => ({ ...courseList[i - 1] }));
        courseIds = picked.map((c) => c.id);
        courseNames = picked.map((c) => c.name);
      } else {
        courseIds = getTargetCourseIds();
        courseNames = getTargetCourseNames();
      }
    } else {
      courseIds = getTargetCourseIds();
      courseNames = getTargetCourseNames();
    }
  } else {
    courseIds = getTargetCourseIds();
    courseNames = getTargetCourseNames();
  }

  const fs = await import("fs");
  const newPwd = pwd || currentPasswordRaw;
  const encryptedPwd = newPwd && !/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(newPwd)
    ? encryptSecret(newPwd)
    : newPwd;
  const newWebhook = webhook || currentWebhook;
  const newInterval = interval || String(currentInterval);

  const content = [
    `# 生成时间: ${new Date().toLocaleString("zh-CN")}`,
    "",
    `JW_BASE_URL=${getJwBaseUrl()}`,
    `STUDENT_ID=${sid || currentStudentId}`,
    `PASSWORD=${encryptedPwd}`,
    `TERM=${term || currentTerm}`,
    `TARGET_COURSE_IDS=${courseIds.join(",")}`,
    `TARGET_COURSE_NAMES=${courseNames.join(",")}`,
    `WEBHOOK_URL=${newWebhook}`,
    `WEBHOOK_METHOD=${getWebhookMethod()}`,
    `POLL_INTERVAL_MS=${newInterval}`,
    `MIN_INTERVAL_MS=${getMinIntervalMs()}`,
    `MAX_INTERVAL_MS=${getMaxIntervalMs()}`,
    `JW_TIMEOUT_MS=12000`,
    `JW_RETRY_COUNT=2`,
    `SETUP_DONE=true`,
    "",
  ].join("\n");

  fs.writeFileSync(
    resolve(process.cwd(), ".env.local"),
    content,
    "utf8"
  );

  dotenv.config({ path: resolve(process.cwd(), ".env.local"), override: true });

  logger.success("配置已保存 ✅（密码已加密，仅本机可用）");
  await waitKey();
}

function waitKey(): Promise<void> {
  return rl.question("\n  按回车键返回菜单...").then(() => showMenu());
}

// ==================== 导出 exe ====================

/** 运行构建脚本导出 exe */
async function runExportExe() {
  console.clear();
  printBox("导出 exe");
  console.log("");
  console.log("  请选择导出类型：\n");
  console.log("  1. 配置版 exe — 内置当前 .env.local 信息，开箱即用");
  console.log("  2. 空白版 exe — 不内置配置，用户首次运行自行填写");
  console.log("");

  const ans = (await rl.question("  请选择 (1/2): ")).trim();
  const buildScript =
    ans === "2"
      ? resolve("scripts/build-blank-exe.js")
      : resolve("scripts/build-exe.js");

  if (!existsSync(buildScript)) {
    logger.error(`未找到构建脚本: ${buildScript}`);
    await waitKey();
    return;
  }

  logger.info("正在构建 exe，请稍候...\n");
  try {
    const { spawnSync } = await import("child_process");
    const result = spawnSync("node", [buildScript], {
      stdio: "inherit",
      shell: true,
    });
    if (result.status !== 0 && result.status !== null) {
      throw new Error(`构建进程退出码: ${result.status}`);
    }
    logger.success("\n导出完成 ✅");
  } catch (err) {
    logger.error(`导出失败: ${err instanceof Error ? err.message : String(err)}`);
  }

  console.log("");
  await waitKey();
}

// ==================== exe 信息查看 ====================

async function runInfo() {
  console.clear();
  printBox("个人信息");
  console.log("");

  const sid = getStudentId();
  const pwd = getPassword();

  if (!sid || !pwd) {
    logger.error("未检测到学号或密码，请重新构建 exe");
    await waitKey();
    return;
  }

  logger.info("正在登录教务系统...");
  let cookieHeader: string;
  try {
    cookieHeader = await login(sid, pwd);
    logger.success("登录成功 ✅\n");
  } catch (error) {
    logger.error(`登录失败: ${error instanceof Error ? error.message : String(error)}`);
    await waitKey();
    return;
  }

  // 抓取个人信息页面
  let college = "";
  let className = "";
  try {
    const response = await jwRequest("/framework/xsMainV_new.htmlx?t1=1", {
      cookieHeader,
    });
    // 去除空白和 &nbsp; 后提取学院和专业
    const compact = response.text.replace(/\s+/g, "").replace(/&nbsp;|&#160;/g, "");
    const collegeMatch = compact.match(/qz-ellipse[^<]*?学院：([^<]+?)</);
    if (collegeMatch) college = collegeMatch[1].trim();
    const classMatch = compact.match(/qz-ellipse[^<]*?班级：([^<]+?)</);
    if (classMatch) className = classMatch[1].trim();
  } catch (error) {
    logger.warn(`个人信息抓取失败: ${error instanceof Error ? error.message : String(error)}`);
  }

  // 输出基本信息
  console.log(`  ${"学号：".padEnd(10)}${sid}`);
  if (college) console.log(`  ${"学院：".padEnd(10)}${college}`);
  if (className) console.log(`  ${"班级：".padEnd(10)}${className}`);

  // 输出课程信息
  const courseIds = getTargetCourseIds();
  const courseNames = getTargetCourseNames();
  if (courseIds.length > 0) {
    console.log(`  ${"目标课程：".padEnd(10)}`);
    for (let i = 0; i < courseIds.length; i++) {
      const name = i < courseNames.length ? courseNames[i] : "";
      const label = name ? `${name}（${courseIds[i]}）` : courseIds[i];
      console.log(`    ${i + 1}. ${label}`);
    }
  } else {
    console.log(`  ${"目标课程：".padEnd(10)}（未设置）`);
  }

  console.log("");
  await waitKey();
}

// ==================== 入口 ====================

async function main() {
  const command = process.argv[2]?.trim().toLowerCase();

  if (command) {
    const studentId = getStudentId();
    const password = getPassword();

    if (!studentId || !password) {
      logger.error("请设置 STUDENT_ID 和 PASSWORD 环境变量");
      process.exit(1);
    }

    switch (command) {
      case "query": {
        const termArg = process.argv[3]?.trim();
        const term = termArg && /^\d{4}-\d{4}-\d$/.test(termArg) ? termArg : getTerm();

        logger.info("正在登录教务系统...");
        let cookieHeader: string;
        try {
          cookieHeader = await login(studentId, password);
          logger.success("登录成功 ✅\n");
        } catch (error) {
          logger.error(`登录失败: ${error instanceof Error ? error.message : String(error)}`);
          process.exit(1);
        }

        const result = await queryEnrolledCourses(cookieHeader, term);
        console.log("\n" + formatEnrolledResult(result) + "\n");

        const whUrl = getWebhookUrl();
        if (whUrl) {
          logger.info("正在推送选课结果...");
          await notifyQueryResult(whUrl, getWebhookMethod(), result);
        }
        process.exit(0);
      }

      case "grab": {
        const courseIds = getTargetCourseIds();
        if (courseIds.length === 0) {
          logger.error("请设置 TARGET_COURSE_IDS 环境变量");
          process.exit(1);
        }

        const targetCount = courseIds.length;

        logger.info("正在登录教务系统...");
        let cookieHeader: string;
        try {
          cookieHeader = await login(studentId, password);
          logger.success("登录成功 ✅");
        } catch (error) {
          logger.error(`登录失败: ${error instanceof Error ? error.message : String(error)}`);
          process.exit(1);
        }

        const commonConfig: Partial<GrabTaskConfig> = {
          webhookUrl: getWebhookUrl() ?? undefined,
          webhookMethod: getWebhookMethod(),
          initialIntervalMs: getPollIntervalMs(),
          minIntervalMs: getMinIntervalMs(),
          maxIntervalMs: getMaxIntervalMs(),
          targetSuccessCount: targetCount,
        };

        const taskIds: string[] = [];
        setTargetCount(targetCount);
        for (const courseId of courseIds) {
          const taskId = await startTask(cookieHeader, studentId, password, {
            courseId,
            courseName: courseId,
            ...commonConfig,
          });
          taskIds.push(taskId);
        }

        logger.info("\n所有任务已启动。按 Ctrl+C 停止所有任务。\n");

        const statusInterval = setInterval(() => {
          const tasks = listTaskStatuses();
          for (const task of tasks) {
            if (task.status === "running") {
              logger.info(
                `[${task.courseName}] 已尝试 ${task.attemptCount} 次 | 间隔 ${task.currentIntervalMs}ms`
              );
            }
          }
        }, 10000);

        const shutdown = () => {
          clearInterval(statusInterval);
          for (const id of taskIds) stopTask(id);
          process.exit(0);
        };
        process.on("SIGINT", shutdown);
        process.on("SIGTERM", shutdown);
        await new Promise(() => {});
        return;
      }

      default:
        logger.error(`未知命令: ${command}，可用命令: query, grab`);
        process.exit(1);
    }
  }

  await showMenu();
}

main().catch((err) => {
  logger.error(`Fatal: ${err instanceof Error ? err.message : String(err)}`);
  process.exit(1);
});
