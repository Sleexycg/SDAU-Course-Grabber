import { logger } from "./logger";
import { registerCourse } from "./course-selector";
import type { RegisterResult } from "../types";
import { notifySuccess, notifyError } from "./notifier";
import { isExeBinary } from "./env";
import type { GrabTaskConfig, GrabTaskState } from "../types";

// ============ 任务存储 ============

const taskMap = new Map<string, GrabTaskState>();
let abortControllerMap = new Map<string, AbortController>();
/** 全局轮次计数器，用于不同尝试轮次之间加空行 */
let lastLoggedRound = 0;
/** 全局已成功抢到的课程数 */
let globalSuccessCount = 0;
/** 全局已设置的抢课目标数（由 GrabTaskConfig.targetSuccessCount 设定） */
let globalTargetCount = 0;

/** 设置全局抢课目标数 */
export function setTargetCount(n: number): void {
  globalTargetCount = n;
  globalSuccessCount = 0;
}

/** 停止指定任务之外的所有其他任务 */
function stopOtherTasks(successTaskId: string): void {
  for (const [id, controller] of abortControllerMap) {
    if (id !== successTaskId) {
      controller.abort();
      abortControllerMap.delete(id);
      const state = taskMap.get(id);
      if (state && state.status === "running") {
        state.status = "stopped";
        state.endedAt = new Date().toISOString();
        state.lastMessage = "⏹️ 其他课程已抢到，本任务自动停止";
      }
    }
  }
}

// ============ 工具函数 ============

function elapsedSeconds(startedAt: string): string {
  const ms = Date.now() - new Date(startedAt).getTime();
  const totalSec = Math.floor(ms / 1000);
  const min = Math.floor(totalSec / 60);
  const sec = totalSec % 60;
  return min > 0 ? `${min}分${sec}秒` : `${sec}秒`;
}

function addLog(state: GrabTaskState, message: string): void {
  const line = `[${new Date().toLocaleTimeString("zh-CN", { hour12: false })}] ${message}`;
  state.recentLogs.push(line);
  if (state.recentLogs.length > 20) {
    state.recentLogs.shift();
  }
  state.lastMessage = message;
}

function adaptInterval(
  currentMs: number,
  resultCode: string | undefined,
  minMs: number,
  maxMs: number,
  remainingSlots?: number
): number {
  // 剩余名额 1-5 且课程不是已满/冲突时，直接加速到最低间隔
  if (
    remainingSlots !== undefined &&
    remainingSlots >= 1 &&
    remainingSlots <= 5 &&
    resultCode !== "COURSE_FULL" &&
    resultCode !== "TIME_CONFLICT"
  ) {
    return minMs;
  }

  switch (resultCode) {
    case "NOT_OPEN_YET":
      // 未到选课时间 → 退避，减少无效请求
      return Math.min(Math.round(currentMs * 1.5), maxMs);
    case "COURSE_FULL":
      // 课程已满 → 加速轮询，抢退选释放的名额
      return Math.max(Math.round(currentMs / 1.2), minMs);
    case "TIME_CONFLICT":
      // 时间冲突 → 不可能成功，退避降低干扰
      return Math.min(Math.round(currentMs * 2), maxMs);
    case "UNKNOWN":
    default:
      // 网络错误 / 未知响应 → 轻量退避
      return Math.min(Math.round(currentMs * 1.3), maxMs);
  }
}

// ============ 任务管理 ============

/**
 * 生成简短任务 ID。
 */
function generateTaskId(): string {
  return `grab_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 6)}`;
}

/**
 * 启动一个抢课任务。
 *
 * 返回 taskId，可通过 getTaskStatus 查询状态。
 */
export async function startTask(
  cookieHeader: string,
  studentId: string,
  password: string,
  config: GrabTaskConfig
): Promise<string> {
  const taskId = generateTaskId();
  const minMs = config.minIntervalMs ?? 300;
  const maxMs = config.maxIntervalMs ?? 5000;
  const initialMs = config.initialIntervalMs ?? 800;

  const state: GrabTaskState = {
    taskId,
    courseId: config.courseId,
    courseName: config.courseName || config.courseId,
    status: "running",
    currentIntervalMs: initialMs,
    attemptCount: 0,
    startedAt: new Date().toISOString(),
    recentLogs: [],
  };

  taskMap.set(taskId, state);
  const abortController = new AbortController();
  abortControllerMap.set(taskId, abortController);

  // 异步运行，不阻塞调用方
  runTaskLoop(taskId, cookieHeader, studentId, password, config, state, abortController.signal).catch(
    (err) => {
      logger.error(`任务 ${taskId} 异常退出: ${err instanceof Error ? err.message : String(err)}`);
      state.status = "error";
      state.endedAt = new Date().toISOString();
      addLog(state, `❌ 任务异常: ${err instanceof Error ? err.message : String(err)}`);
    }
  );

  logger.info(`🚀 启动抢课任务 [${taskId}] → ${config.courseName} (初始间隔 ${initialMs}ms)`);
  return taskId;
}

async function runTaskLoop(
  taskId: string,
  cookieHeader: string,
  studentId: string,
  password: string,
  config: GrabTaskConfig,
  state: GrabTaskState,
  signal: AbortSignal
): Promise<void> {
  const minMs = config.minIntervalMs ?? 300;
  const maxMs = config.maxIntervalMs ?? 5000;

  while (!signal.aborted && state.status === "running") {
    state.attemptCount += 1;
    const attempt = state.attemptCount;

    // 新轮次的第一条日志前加空行
    if (attempt > 1 && attempt > lastLoggedRound) {
      console.log("");
      lastLoggedRound = attempt;
    }

    addLog(state, `🔄 第 ${attempt} 次尝试 (间隔 ${state.currentIntervalMs}ms)`);
    logger.info(`[${state.courseName}] 第 ${attempt} 次提交选课...`);

    try {
      const result = await registerCourse(cookieHeader, config.courseId);

      if (result.success) {
        // 🎉 抢课成功！
        state.status = "success";
        state.result = result;
        state.endedAt = new Date().toISOString();
        addLog(state, `✅ 抢课成功！${result.message}`);

        // 检查是否达到目标数量
        globalSuccessCount += 1;
        if (globalTargetCount > 1 && globalSuccessCount >= globalTargetCount) {
          logger.info(`🎯 已抢到 ${globalSuccessCount} 门课程，达到目标，停止剩余任务`);
          stopOtherTasks("");
        } else if (globalTargetCount > 1) {
          logger.info(`📊 当前进度: ${globalSuccessCount}/${globalTargetCount}`);
        }

        if (!isExeBinary()) {
          await notifySuccess(config.webhookUrl ?? null, config.webhookMethod ?? "GET", {
            courseName: state.courseName,
            attemptCount: attempt,
            elapsed: elapsedSeconds(state.startedAt),
          });
        }
        return;
      }

      // 选课失败，记录日志并自适应调整间隔
      addLog(state, `❌ ${result.message} (${result.code ?? "UNKNOWN"})`);
      logger.info(`[${state.courseName}] ${result.message}`);

      state.currentIntervalMs = adaptInterval(state.currentIntervalMs, result.code, minMs, maxMs, result.remainingSlots);

      if (
        result.remainingSlots !== undefined &&
        result.remainingSlots <= 5 &&
        result.remainingSlots > 0
      ) {
        logger.info(`[${state.courseName}] 仅剩 ${result.remainingSlots} 个名额，加速轮询间隔至 ${state.currentIntervalMs}ms`);
      }

      // 如果是时间冲突（不可恢复），提前停止
      if (result.code === "TIME_CONFLICT") {
        state.status = "error";
        state.endedAt = new Date().toISOString();
        addLog(state, `⛔ 课程时间冲突，无法选课，任务终止`);
        logger.warn(`[${state.courseName}] 课程时间冲突，任务终止`);
        return;
      }
    } catch (error) {
      const errorMsg = error instanceof Error ? error.message : String(error);
      addLog(state, `⚠️ 请求异常: ${errorMsg}`);
      logger.warn(`[${state.courseName}] 请求异常: ${errorMsg}`);

      // 网络错误时退避
      state.currentIntervalMs = Math.min(Math.round(state.currentIntervalMs * 2), maxMs);
    }

    // 等待间隔后继续
    await sleep(state.currentIntervalMs, signal);
    if (signal.aborted) break;
  }

  // 被停止的情况
  if (state.status !== "success") {
    state.status = "stopped";
    state.endedAt = new Date().toISOString();
    addLog(state, `⏹️ 任务已手动停止`);
  }
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    const onAbort = () => {
      clearTimeout(timer);
      resolve();
    };
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

/**
 * 停止一个抢课任务。
 */
export function stopTask(taskId: string): boolean {
  const abortController = abortControllerMap.get(taskId);
  if (!abortController) return false;

  abortController.abort();
  abortControllerMap.delete(taskId);

  const state = taskMap.get(taskId);
  if (state && state.status === "running") {
    state.status = "stopped";
    state.endedAt = new Date().toISOString();
  }

  logger.info(`⏹️ 已停止任务 ${taskId}`);
  return true;
}

/**
 * 查询单个任务状态。
 */
export function getTaskStatus(taskId: string): GrabTaskState | null {
  return taskMap.get(taskId) ?? null;
}

/**
 * 查询所有任务状态。
 */
export function listTaskStatuses(): GrabTaskState[] {
  return Array.from(taskMap.values());
}
