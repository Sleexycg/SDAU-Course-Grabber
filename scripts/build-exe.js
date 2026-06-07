/**
 * 构建 exe — 将 .env.local 的配置打包进 exe，无需外部配置文件即可运行。
 *
 * 用法: node scripts/build-exe.js
 *
 * 流程:
 * 1. 读取 .env.local 中的环境变量
 * 2. 密码如果是加密的，自动解密为明文（确保在其他电脑也能用）
 * 3. 生成 src/env-builtin.ts（编译内置的默认值）
 * 4. 执行 bun build --compile
 * 5. 清理临时文件
 */
const fs = require("fs");
const path = require("path");
const { execSync } = require("child_process");

// 解析 .env.local 文件
function parseEnvFile(filePath) {
  const content = fs.readFileSync(filePath, "utf8");
  const env = {};
  for (const line of content.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eqIdx = trimmed.indexOf("=");
    if (eqIdx === -1) continue;
    const key = trimmed.slice(0, eqIdx).trim();
    const val = trimmed.slice(eqIdx + 1).trim();
    if (key) env[key] = val;
  }
  return env;
}

// 尝试解密密码（使用项目自身的 crypto 模块）
function tryDecryptPassword(encrypted) {
  // 检查是否是加密格式（三段 base64url 用 . 连接）
  if (!/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(encrypted)) {
    return encrypted; // 已经是明文
  }
  try {
    // 用项目中的解密函数
    const { decryptSecret } = require("../dist/lib/crypto");
    const decrypted = decryptSecret(encrypted);
    if (decrypted !== null) return decrypted;
  } catch {}
  // 解密失败，保留原值（让运行时去处理错误）
  console.warn("  ⚠️ 密码解密失败，将保留加密值嵌入 exe（仅在本机能用）");
  return encrypted;
}

// 要打包进 exe 的变量列表（只包含有值的）
// ⚠️ WEBHOOK_URL / WEBHOOK_METHOD 不内置，由使用者在 exe 同目录放 .env.local 配置
const INJECT_KEYS = [
  "STUDENT_ID",
  "PASSWORD",
  "TARGET_COURSE_IDS",
  "TARGET_COURSE_NAMES",
  "TERM",
  "JW_BASE_URL",
  "POLL_INTERVAL_MS",
  "MIN_INTERVAL_MS",
  "MAX_INTERVAL_MS",
  "JW_TIMEOUT_MS",
  "JW_RETRY_COUNT",
  "SETUP_DONE",
];

async function main() {
  console.log("=== 读取 .env.local ===");
  const envPath = path.resolve(".env.local");
  if (!fs.existsSync(envPath)) {
    console.error("  ✘ 找不到 .env.local，请先配置环境变量");
    process.exit(1);
  }

  const env = parseEnvFile(envPath);

  // 确保先编译 TS → JS（解密需要 dist/lib/crypto.js）
  console.log("\n=== 编译 TypeScript ===");
  execSync("npx tsc --declaration false --declarationMap false", {
    stdio: "inherit",
    shell: true,
  });

  // 解密密码
  if (env.PASSWORD) {
    const decrypted = tryDecryptPassword(env.PASSWORD);
    env.PASSWORD = decrypted;
  }

  // 过滤出要注入的变量
  const injectEnv = {};
  for (const key of INJECT_KEYS) {
    if (env[key] !== undefined) {
      injectEnv[key] = env[key];
    }
  }

  // 生成 src/env-builtin.ts
  console.log("\n=== 生成 src/env-builtin.ts ===");
  const lines = [
    "// ⚠️ 自动生成 — 运行 npm run build:exe 时写入",
    "// 修改请编辑 .env.local 后重新构建",
    "",
    "export const BUILTIN_ENV: Record<string, string> = {",
  ];
  for (const [key, val] of Object.entries(injectEnv)) {
    // 对值做 JS 字符串转义（处理引号、换行等）
    const escaped = JSON.stringify(val);
    lines.push(`  ${JSON.stringify(key)}: ${escaped},`);
  }
  lines.push("};");
  lines.push("");

  fs.writeFileSync("src/env-builtin.ts", lines.join("\n"), "utf8");
  console.log(`  ✔ 已注入 ${Object.keys(injectEnv).length} 个变量`);

  // 以学号命名输出文件
  const studentId = env.STUDENT_ID || "course-grabber";
  const outName = path.resolve(`${studentId}.exe`);

  // 执行 bun build --compile
  console.log("\n=== 执行 bun build --compile ===");
  // bun 可能不在 PATH 中，尝试常见路径
  const bunCmd = fs.existsSync("C:\\Users\\LENOVO\\.bun\\bin\\bun.exe")
    ? "C:\\Users\\LENOVO\\.bun\\bin\\bun.exe"
    : "bun";
  execSync(`"${bunCmd}" build src/index.ts --compile --outfile=${outName}`, {
    stdio: "inherit",
    shell: true,
  });

  // 替换图标（如果项目根目录有 favicon.ico）
  const icoPath = path.resolve("favicon.ico");
  if (fs.existsSync(icoPath)) {
    try {
      const { rcedit } = require("rcedit");
      await rcedit(path.resolve(outName), { icon: icoPath });
      console.log("  ✔ 图标已替换");
    } catch (e) {
      console.log("  - 图标替换失败（可忽略）:", e.message);
    }
  }

  const size = fs.statSync(outName).size;
  console.log(`\n✅ 构建完成！`);
  console.log(`  输出: ${outName} (${(size / 1024 / 1024).toFixed(1)} MB)`);
  console.log(`  💡 如需更改配置，编辑 .env.local 后重新构建`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
