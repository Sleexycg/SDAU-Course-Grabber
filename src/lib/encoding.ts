export function encodeInp(input: string): string {
  return Buffer.from(input, "utf8").toString("base64");
}

/**
 * 构建教务系统登录的加密凭证。
 *
 * 算法：
 * 1. 将 account、password、空格分别做 base64 编码，用 "%%%" 拼接成 code
 * 2. 遍历 code 的前 55 个字符，根据 sxh 的数字决定在每字符后插入多少位 scode
 */
export function buildEncodedCredential(
  account: string,
  password: string,
  scodeSeed: string,
  sxh: string
): string {
  const accountEncoded = encodeInp(account);
  const passwordEncoded = encodeInp(password);
  const codeDogEncoded = encodeInp(" ");
  const code = `${accountEncoded}%%%${passwordEncoded}%%%${codeDogEncoded}`;

  let scode = scodeSeed;
  let encoded = "";

  for (let i = 0; i < code.length; i += 1) {
    if (i < 55) {
      const index = Number.parseInt(sxh.slice(i, i + 1), 10);
      const safeIndex = Number.isFinite(index) && index > 0 ? index : 0;
      encoded += `${code[i]}${scode.slice(0, safeIndex)}`;
      scode = scode.slice(safeIndex);
    } else {
      encoded += code.slice(i);
      break;
    }
  }

  return encoded;
}
