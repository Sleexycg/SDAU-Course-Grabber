/**
 * 构建空白 exe — 不内置任何配置，用户第一次运行时会进入配置界面自行填写。
 *
 * 用法: node scripts/build-blank-exe.js
 *
 * 打包成独立 exe 方便分发。
 */
const fs = require("fs");
const path = require("path");
const { execSync } = require("child_process");

async function main() {
  // 生成空的 env-builtin.ts
  console.log("=== 生成空的 src/env-builtin.ts ===");
  const content = [
    "// ⚠️ 自动生成 — 空白 exe 构建",
    "// 所有配置由用户第一次运行时填写",
    "",
    "export const BUILTIN_ENV: Record<string, string> = {};",
    "",
  ].join("\n");
  fs.writeFileSync("src/env-builtin.ts", content, "utf8");
  console.log("  ✔ 已生成空白内置配置");

  // 执行 bun build --compile
  console.log("\n=== 执行 bun build --compile ===");
  const bunCmd = fs.existsSync("C:\\Users\\LENOVO\\.bun\\bin\\bun.exe")
    ? "C:\\Users\\LENOVO\\.bun\\bin\\bun.exe"
    : "bun";
  execSync(`"${bunCmd}" build src/index.ts --compile --outfile=SDAU-Course-Grabber.exe`, {
    stdio: "inherit",
    shell: true,
  });

  // 替换图标（如果项目根目录有 favicon.ico）
  const icoPath = path.resolve("favicon.ico");
  if (fs.existsSync(icoPath)) {
    try {
      const { rcedit } = require("rcedit");
      await rcedit(path.resolve("SDAU-Course-Grabber.exe"), { icon: icoPath });
      console.log("  ✔ 图标已替换");
    } catch (e) {
      console.log("  - 图标替换失败（可忽略）:", e.message);
    }
  }

  const size = fs.statSync("SDAU-Course-Grabber.exe").size;
  console.log(`\n✅ 构建完成！`);
  console.log(`  输出: SDAU-Course-Grabber.exe (${(size / 1024 / 1024).toFixed(1)} MB)`);
  console.log(`  📝 在 exe 同目录下创建 .env.local 文件写入配置后即可使用`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
