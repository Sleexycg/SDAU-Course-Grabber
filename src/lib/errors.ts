export type CourseGrabErrorCode =
  | "JW_UNAVAILABLE"
  | "INVALID_CREDENTIALS"
  | "UNAUTHORIZED"
  | "BAD_REQUEST"
  | "COURSE_FULL"
  | "TIME_CONFLICT"
  | "REG_CLOSED"
  | "NOT_OPEN_YET"
  | "UNKNOWN";

export class JwError extends Error {
  readonly code: CourseGrabErrorCode;

  constructor(code: CourseGrabErrorCode, message: string) {
    super(message);
    this.code = code;
    this.name = "JwError";
  }
}
