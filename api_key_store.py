"""DeepSeek API key 的唯一来源：用户通过网页提交的 key（存内存，不落盘，重启即丢）
> 环境变量 DEEPSEEK_API_KEY > 无。

kb_store.py / workflow.py / app.py 都从这里取 key，不再各自硬编码。
这个模块不 import 项目里任何其它模块，避免循环引用。
"""

import os
import threading

_lock = threading.Lock()
_user_key = None


def set_user_key(key):
    """网页 POST /api/settings/apikey 提交的 key，只存进程内存。"""
    global _user_key
    with _lock:
        _user_key = (key or "").strip() or None


def get_key():
    """用户提交的 key 优先，其次环境变量，都没有则 None。"""
    with _lock:
        if _user_key:
            return _user_key
    return os.environ.get("DEEPSEEK_API_KEY") or None


def has_key():
    return bool(get_key())


def status():
    """给 GET /api/settings/apikey/status 用：不返回完整 key，只返回尾 4 位。"""
    with _lock:
        if _user_key:
            return {"configured": True, "source": "user", "last4": _user_key[-4:]}
    env_key = os.environ.get("DEEPSEEK_API_KEY")
    if env_key:
        return {"configured": True, "source": "env", "last4": env_key[-4:]}
    return {"configured": False, "source": "none", "last4": None}
