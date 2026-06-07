/** 简易带时间戳的控制台日志工具 */

type LogLevel = "INFO" | "WARN" | "ERROR" | "SUCCESS";

function timestamp(): string {
  return new Date().toLocaleString("zh-CN", {
    hour12: false,
  });
}

function format(level: LogLevel, message: string): string {
  return `[${timestamp()}] [${level}] ${message}`;
}

export const logger = {
  info(message: string) {
    console.log(format("INFO", message));
  },

  warn(message: string) {
    console.warn(format("WARN", message));
  },

  error(message: string) {
    console.error(format("ERROR", message));
  },

  success(message: string) {
    console.log(format("SUCCESS", message));
  },
};
