"""
邮箱验证码模块（可选功能）
=========================
通过环境变量 REQUIRE_EMAIL_VERIFY=true 启用。
默认关闭——不影响现有注册流程和转化率。

启用后注册需要：先获取验证码 → 填验证码才能注册。
需配置 SMTP 环境变量（见下方）才能真正发邮件。

环境变量：
- REQUIRE_EMAIL_VERIFY: "true" 启用邮箱验证（默认 false）
- SMTP_HOST: SMTP 服务器（如 smtp.qq.com / smtp.163.com）
- SMTP_PORT: 端口（QQ邮箱 465）
- SMTP_USER: 发信邮箱账号
- SMTP_PASS: 邮箱授权码（不是登录密码，是邮箱设置里申请的授权码）
- SMTP_FROM: 发信人显示名（可选）
"""
import os
import ssl
import smtplib
import random
import time
import logging
from email.mime.text import MIMEText
from email.header import Header

logger = logging.getLogger("geo.email")

# 验证码内存存储：{email: (code, expire_ts, send_ts)}
# 生产环境量大可换 Redis，初期内存够用
_codes = {}


def is_email_verify_enabled() -> bool:
    """是否启用邮箱验证。默认关闭。"""
    return os.getenv("REQUIRE_EMAIL_VERIFY", "").lower() == "true"


def _smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_USER") and os.getenv("SMTP_PASS"))


def can_send(email: str) -> tuple[bool, str]:
    """防刷：同一邮箱60秒内只能发一次。"""
    rec = _codes.get(email)
    if rec and time.time() - rec[2] < 60:
        wait = int(60 - (time.time() - rec[2]))
        return False, f"请 {wait} 秒后再获取验证码"
    return True, ""


def send_code(email: str) -> tuple[bool, str]:
    """生成并发送验证码。返回 (成功, 消息)。"""
    ok, msg = can_send(email)
    if not ok:
        return False, msg

    code = f"{random.randint(0, 999999):06d}"
    expire_ts = time.time() + 600  # 10分钟有效
    _codes[email] = (code, expire_ts, time.time())

    # 未配置 SMTP 时，开发模式：把验证码记到日志（生产必须配 SMTP）
    if not _smtp_configured():
        logger.warning("⚠️ 未配置 SMTP，验证码仅记录到日志（开发模式）: %s -> %s", email, code)
        return True, "dev"  # 开发模式标记

    try:
        host = os.getenv("SMTP_HOST")
        port = int(os.getenv("SMTP_PORT", "465"))
        user = os.getenv("SMTP_USER")
        password = os.getenv("SMTP_PASS")
        from_name = os.getenv("SMTP_FROM", "见微")

        subject = "见微 - 注册验证码"
        body = (
            f"您的验证码是：{code}\n\n"
            f"验证码 10 分钟内有效，请勿泄露给他人。\n"
            f"如非本人操作，请忽略此邮件。\n\n"
            f"—— 见微 · 让 AI 主动推荐你的品牌"
        )
        msg_obj = MIMEText(body, "plain", "utf-8")
        msg_obj["Subject"] = Header(subject, "utf-8")
        msg_obj["From"] = f"{from_name} <{user}>"
        msg_obj["To"] = email

        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=10) as server:
                server.login(user, password)
                server.sendmail(user, [email], msg_obj.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=10) as server:
                server.starttls(context=context)
                server.login(user, password)
                server.sendmail(user, [email], msg_obj.as_string())
        return True, "验证码已发送，请查收邮件"
    except Exception as e:
        logger.error("发送验证码失败: %s", e)
        # 发送失败时删除已存的码，避免用户拿不到码却占用
        _codes.pop(email, None)
        return False, "验证码发送失败，请稍后重试或联系客服"


def verify_code(email: str, code: str) -> bool:
    """校验验证码。成功后即失效（一次性）。"""
    rec = _codes.get(email)
    if not rec:
        return False
    stored_code, expire_ts, _ = rec
    if time.time() > expire_ts:
        _codes.pop(email, None)
        return False
    if (code or "").strip() != stored_code:
        return False
    # 验证成功，立即失效，防重放
    _codes.pop(email, None)
    return True
