"""OAuth token 静态加密：Fernet 对称加密，密钥来自 SECRET_KEY。

token 仅在导出任务期间以密文落在 SQLite，任务终态后立即清列；
内存与日志中不出现明文。
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


class TokenCipher:
    def __init__(self, secret: str) -> None:
        if not secret:
            raise ValueError("SECRET_KEY is not set — generate one with `python -m app.genkey`")
        # 任意长度的 SECRET_KEY 派生出 Fernet key（32 字节 URL-safe base64）
        digest = hashlib.sha256(secret.encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("token 解密失败（SECRET_KEY 是否变更？）") from exc
