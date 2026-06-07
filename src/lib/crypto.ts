import { createCipheriv, createDecipheriv, createHash, randomBytes } from "crypto";
import { hostname } from "os";

/**
 * 从机器特征派生 AES-256 密钥。
 * 不同机器生成的密钥不同，.env.local 被复制到其他机器也无法解密。
 */
function deriveKey(): Buffer {
  const salt = "WeSDAU@2026!SecureConfig#SDAU";
  const seed = `${hostname()}-${salt}`;
  return createHash("sha256").update(seed).digest();
}

/** 加密格式：base64url(iv).base64url(authTag).base64url(ciphertext) */
export function encryptSecret(plaintext: string): string {
  const iv = randomBytes(12);
  const key = deriveKey();
  const cipher = createCipheriv("aes-256-gcm", key, iv);
  const encrypted = Buffer.concat([cipher.update(Buffer.from(plaintext, "utf8")), cipher.final()]);
  const authTag = cipher.getAuthTag();
  return [iv.toString("base64url"), authTag.toString("base64url"), encrypted.toString("base64url")].join(".");
}

/** 解密，失败时返回 null（密钥不匹配或数据损坏） */
export function decryptSecret(token: string): string | null {
  try {
    const parts = token.split(".");
    if (parts.length !== 3) return null;
    const [ivRaw, authTagRaw, ciphertextRaw] = parts;
    const iv = Buffer.from(ivRaw, "base64url");
    const authTag = Buffer.from(authTagRaw, "base64url");
    const ciphertext = Buffer.from(ciphertextRaw, "base64url");
    const decipher = createDecipheriv("aes-256-gcm", deriveKey(), iv);
    decipher.setAuthTag(authTag);
    return Buffer.concat([decipher.update(ciphertext), decipher.final()]).toString("utf8");
  } catch {
    return null;
  }
}

/** 判断一段文本是否已加密（匹配 三段 base64url 用 . 连接的格式） */
export function isEncrypted(value: string): boolean {
  return /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(value);
}
