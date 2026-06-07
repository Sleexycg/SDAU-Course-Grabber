export interface GrabTaskConfig {
  /** 目标课程 ID（教务系统中的课程标识） */
  courseId: string;
  /** 课程名称（仅用于日志/通知展示） */
  courseName?: string;
  /** Webhook 推送 URL（选填） */
  webhookUrl?: string;
  /** Webhook 推送方式 */
  webhookMethod?: "GET" | "POST";
  /** 初始轮询间隔（毫秒） */
  initialIntervalMs?: number;
  /** 最小轮询间隔（毫秒） */
  minIntervalMs?: number;
  /** 最大轮询间隔（毫秒） */
  maxIntervalMs?: number;
  /** 需要抢到的课程数量（达到此数后停止其他任务） */
  targetSuccessCount?: number;
}

export interface GrabTaskState {
  /** 任务唯一 ID */
  taskId: string;
  /** 目标课程 ID */
  courseId: string;
  /** 课程名称 */
  courseName: string;
  /** 任务状态 */
  status: "running" | "success" | "stopped" | "error";
  /** 当前轮询间隔（毫秒） */
  currentIntervalMs: number;
  /** 已尝试次数 */
  attemptCount: number;
  /** 开始时间 */
  startedAt: string;
  /** 结束时间（成功/停止时设置） */
  endedAt?: string;
  /** 最近结果消息 */
  lastMessage?: string;
  /** 近期的日志行（最多保留 20 条） */
  recentLogs: string[];
  /** 最终结果详情（成功时） */
  result?: RegisterResult;
}

export interface RegisterResult {
  success: boolean;
  message: string;
  code?: "SUCCESS" | "COURSE_FULL" | "TIME_CONFLICT" | "NOT_OPEN_YET" | "UNKNOWN";
  /** 剩余名额（可选，接口返回后用于加速轮询） */
  remainingSlots?: number;
}

// ============ 选课结果查询 ============

/** 已选课程（选课结果） */
export interface EnrolledCourse {
  /** 唯一标识 */
  id: string;
  /** 课程代码 */
  code: string;
  /** 课程名称 */
  name: string;
  /** 授课教师 */
  teacher: string;
  /** 上课地点（多地点时用分号连接） */
  location: string;
  /** 学分 */
  credit: string;
  /** 总学时 */
  totalHours?: string;
  /** 课程属性（必修/限选/任选） */
  courseType?: string;
  /** 课程类型代码（BK/XY/XR/XF/BS） */
  typeCode?: string;
  /** 教学班名称 */
  classGroup?: string;
  /** 开课学院 */
  college?: string;
  /** 星期几 (1-7)，取该课程第一次上课时间 */
  weekday: number;
  /** 开始节次 */
  startSection: number;
  /** 结束节次 */
  endSection: number;
  /** 上课周次（选课结果接口不含此信息） */
  weeks: number[];
  /** 所属学期 */
  term: string;
  /** 教务内部 ID */
  jx0501id?: string;
  /** 完整上课时间明细 */
  rawSchedule?: Array<{ weekday: number; startSection: number; endSection: number }>;
  /** 完整上课地点列表 */
  rawLocations?: string[];
}

/** 选课结果查询响应 */
export interface EnrolledCourseResult {
  term: string;
  /** 已选课程列表 */
  courses: EnrolledCourse[];
  /** 统计信息 */
  summary: {
    totalCourses: number;
    totalCredits: number;
  };
}
