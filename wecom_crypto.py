# -*- coding: utf-8 -*-
"""
企业微信回调消息加解密（自建应用「接收消息」）。
参考官方加解密方案：AES-256-CBC，PKCS7(块长32)，签名 sha1。
"""
import base64
import hashlib
import os
import struct
import random
import string
from Crypto.Cipher import AES


class WXBizMsgCrypt:
    def __init__(self, token: str, encoding_aes_key: str, corp_id: str):
        self.token = token
        self.corp_id = corp_id
        # EncodingAESKey 为 43 字符，补 '=' 后 base64 解码得到 32 字节密钥
        self.aes_key = base64.b64decode(encoding_aes_key + "=")
        self.iv = self.aes_key[:16]

    # ---------- 基础工具 ----------
    def _sha1(self, *parts):
        s = "".join(sorted(parts))
        return hashlib.sha1(s.encode("utf-8")).hexdigest()

    @staticmethod
    def _pkcs7_pad(data: bytes, block: int = 32) -> bytes:
        pad = block - (len(data) % block)
        if pad == 0:
            pad = block
        return data + bytes([pad]) * pad

    @staticmethod
    def _pkcs7_unpad(data: bytes) -> bytes:
        pad = data[-1]
        return data[:-pad]

    # ---------- 解密 ----------
    def _decrypt(self, encrypt_b64: str) -> str:
        cipher = AES.new(self.aes_key, AES.MODE_CBC, self.iv)
        plain = cipher.decrypt(base64.b64decode(encrypt_b64))
        plain = self._pkcs7_unpad(plain)
        # 结构：random(16) + msg_len(4) + msg + corpid
        msg_len = struct.unpack(">I", plain[16:20])[0]
        msg = plain[20:20 + msg_len]
        return msg.decode("utf-8")

    # ---------- 加密（被动回复用） ----------
    def _encrypt(self, msg: str) -> str:
        random16 = "".join(random.choices(string.ascii_letters + string.digits, k=16)).encode()
        msg_bytes = msg.encode("utf-8")
        body = random16 + struct.pack(">I", len(msg_bytes)) + msg_bytes + self.corp_id.encode()
        body = self._pkcs7_pad(body)
        cipher = AES.new(self.aes_key, AES.MODE_CBC, self.iv)
        return base64.b64encode(cipher.encrypt(body)).decode()

    # ---------- 对外接口 ----------
    def verify_url(self, msg_signature: str, timestamp: str, nonce: str, echostr: str):
        """首次配置回调 URL 时的校验，返回解密后的 echostr 明文"""
        if self._sha1(self.token, timestamp, nonce, echostr) != msg_signature:
            return None
        return self._decrypt(echostr)

    def decrypt_msg(self, msg_signature: str, timestamp: str, nonce: str, encrypt: str):
        """校验并解密主动推送的消息，返回明文 XML"""
        if self._sha1(self.token, timestamp, nonce, encrypt) != msg_signature:
            return None
        return self._decrypt(encrypt)

    def encrypt_reply(self, reply_xml: str, nonce: str, timestamp: str) -> str:
        """把被动回复的 XML 加密，组装成企业微信要求的返回包"""
        enc = self._encrypt(reply_xml)
        sig = self._sha1(self.token, timestamp, nonce, enc)
        return (
            "<xml>"
            f"<Encrypt><![CDATA[{enc}]]></Encrypt>"
            f"<MsgSignature><![CDATA[{sig}]]></MsgSignature>"
            f"<TimeStamp>{timestamp}</TimeStamp>"
            f"<Nonce><![CDATA[{nonce}]]></Nonce>"
            "</xml>"
        )
