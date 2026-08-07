"""生成旧版登录表单要求的凭据字段。"""

from __future__ import annotations

import base64


def _base64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def build_encoded_credential(
    account: str,
    password: str,
    scode_seed: str,
    sxh: str,
) -> str:
    """复现登录页 JavaScript 生成的 ``encoded`` 字段。"""

    code = f"{_base64(account)}%%%{_base64(password)}%%%{_base64(' ')}"
    scode = scode_seed
    output: list[str] = []

    for index, character in enumerate(code):
        if index >= 55:
            output.append(code[index:])
            break
        digit = sxh[index : index + 1]
        take = int(digit) if digit.isdigit() and digit != "0" else 0
        output.extend((character, scode[:take]))
        scode = scode[take:]

    return "".join(output)


__all__ = ["build_encoded_credential"]
