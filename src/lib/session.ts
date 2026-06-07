import { JwError } from "./errors";
import { buildEncodedCredential } from "./encoding";
import { jwRequest } from "./http";

/** 从登录页面 HTML 中提取加密种子 */
function extractLoginSeed(html: string): { scode: string; sxh: string } {
  const scode = html.match(/var\s+scode\s*=\s*"([^"]+)";/)?.[1]?.trim();
  const sxh = html.match(/var\s+sxh\s*=\s*"([^"]+)";/)?.[1]?.trim();

  if (!scode || !sxh) {
    throw new JwError("JW_UNAVAILABLE", "登录混淆参数提取失败，教务页面可能已改版");
  }

  return { scode, sxh };
}

/** 检查 HTML 是否为登录页面 */
function isLoginPage(html: string): boolean {
  return /name=["']loginForm["']|欢迎登录教务系统|请先登录系统/i.test(html);
}

/** 从响应中提取登录失败消息 */
function parseLoginMessage(html: string): string {
  const match = html.match(/id=["']showMsg["'][^>]*>([^<]*)</i);
  return match?.[1]?.trim() ?? "";
}

/**
 * 登录教务系统，返回会话 Cookie Header。
 *
 * 流程：
 * 1. GET / 获取登录页，提取 scode 和 sxh
 * 2. 构建加密凭证
 * 3. POST /xk/LoginToXk 提交登录
 * 4. 验证登录结果
 */
export async function login(studentId: string, password: string): Promise<string> {
  const loginPage = await jwRequest("/");
  const { scode, sxh } = extractLoginSeed(loginPage.text);

  const encoded = buildEncodedCredential(studentId, password, scode, sxh);
  const body = new URLSearchParams({
    loginMethod: "LoginToXk",
    userlanguage: "0",
    userAccount: studentId,
    userPassword: "",
    encoded,
  });

  const loginResult = await jwRequest("/xk/LoginToXk", {
    method: "POST",
    body,
    cookieHeader: loginPage.cookieHeader,
    referer: loginPage.finalUrl,
  });

  const loginMessage = parseLoginMessage(loginResult.text);
  if (loginMessage && !/请先登录系统/i.test(loginMessage)) {
    throw new JwError("INVALID_CREDENTIALS", loginMessage);
  }

  if (isLoginPage(loginResult.text)) {
    throw new JwError("INVALID_CREDENTIALS", "学号或密码错误，或账号当前不可登录");
  }

  return loginResult.cookieHeader;
}

/**
 * 验证会话是否仍然有效。
 * 如果失效，自动重新登录。
 */
export async function ensureSession(
  cookieHeader: string | null,
  studentId: string,
  password: string
): Promise<string> {
  if (!cookieHeader) {
    return login(studentId, password);
  }

  // 尝试访问一个需要登录的页面验证会话
  try {
    const response = await jwRequest("/framework/xsMainV_new.htmlx?t1=1", {
      cookieHeader,
    });

    if (!isLoginPage(response.text)) {
      return cookieHeader; // 会话仍有效
    }

    // 会话已失效，重新登录
    return login(studentId, password);
  } catch {
    return login(studentId, password);
  }
}
