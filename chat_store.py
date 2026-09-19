"""聊天历史持久化 + 对话上下文管理。

历史：每个对话一个 JSON 文件放 chat_history/<chat_id>.json：
    {
      "id", "created_at", "updated_at", "title"(第一条用户消息前 20 字), "kb_id",
      "messages": [
        {"role": "user"|"assistant", "content", "timestamp", "verify_status"?},
        {"role": "divider", "type": "context_cleared", "timestamp"}   # 清空上下文的分隔线
      ]
    }
后端在每轮问答完成后自动追加消息（app.py 的 /api/chat/stream 里），所以
关页面 / 刷新 / 开新对话都不会丢已完成的对话。

上下文：内存里维护 chat_id -> 最近若干条消息，喂给 generate 节点理解指代。
「清空上下文」= 清空这份内存 + 往历史文件里写一条 divider（重载后仍然是清空状态）。
"""

import json
import logging
import os
import re
import threading
from datetime import datetime

logger = logging.getLogger("kg_rag.chat_store")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_DIR = os.path.join(BASE_DIR, "chat_history")

# 最近多少条消息进上下文（8 条 ≈ 4 轮问答）
MAX_CONTEXT_MSGS = 8
TITLE_LEN = 20

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_lock = threading.RLock()
_contexts = {}  # chat_id -> [{"role","content"}]


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _valid(chat_id):
    return bool(chat_id) and bool(_ID_RE.match(str(chat_id)))


def _path(chat_id):
    return os.path.join(HISTORY_DIR, f"{chat_id}.json")


def _read(chat_id):
    p = _path(chat_id)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.exception("读取对话失败 %s", chat_id)
        return None


def _write(chat):
    os.makedirs(HISTORY_DIR, exist_ok=True)
    p = _path(chat["id"])
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(chat, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


# ==================== 历史：查询 / 删除 ====================

def load_chat(chat_id):
    if not _valid(chat_id):
        return None
    return _read(chat_id)


def list_chats():
    """所有历史对话的摘要，按最后更新时间倒序。"""
    out = []
    if not os.path.isdir(HISTORY_DIR):
        return out
    try:
        import kb_store
        kb_names = {k["id"]: k["name"] for k in kb_store.list_kbs()}
    except Exception:
        kb_names = {}
    for fn in os.listdir(HISTORY_DIR):
        if not fn.endswith(".json"):
            continue
        chat = _read(fn[:-5])
        if not chat or not chat.get("id"):
            continue
        real = [m for m in chat.get("messages", []) if m.get("role") in ("user", "assistant")]
        kb_id = chat.get("kb_id")
        out.append({
            "id": chat["id"],
            "title": chat.get("title") or "(无标题对话)",
            "created_at": chat.get("created_at", ""),
            "updated_at": chat.get("updated_at") or chat.get("created_at", ""),
            "kb_id": kb_id,
            "kb_name": kb_names.get(kb_id, kb_id or "—"),
            "message_count": len(real),
        })
    out.sort(key=lambda c: c["updated_at"], reverse=True)
    return out


def delete_chat(chat_id):
    if not _valid(chat_id):
        raise ValueError("非法对话 ID")
    with _lock:
        _contexts.pop(chat_id, None)
        p = _path(chat_id)
        if os.path.isfile(p):
            os.remove(p)


# ==================== 历史：追加 ====================

def _append_messages(chat_id, kb_id, new_msgs, title_from=None):
    with _lock:
        chat = _read(chat_id)
        if chat is None:
            chat = {
                "id": chat_id,
                "created_at": _now(),
                "kb_id": kb_id,
                "title": "",
                "messages": [],
            }
        chat.setdefault("messages", []).extend(new_msgs)
        chat["updated_at"] = _now()
        if kb_id:
            chat["kb_id"] = kb_id
        if not chat.get("title") and title_from:
            chat["title"] = str(title_from).strip()[:TITLE_LEN]
        _write(chat)
        return chat


def append_turn(chat_id, kb_id, user_content, assistant_content, verified):
    """一轮问答完成后调用：把用户消息 + 助手回答写进历史文件。"""
    if not _valid(chat_id):
        return
    ts = _now()
    _append_messages(
        chat_id, kb_id,
        [
            {"role": "user", "content": user_content, "timestamp": ts},
            {"role": "assistant", "content": assistant_content,
             "timestamp": ts, "verify_status": verified},
        ],
        title_from=user_content,
    )


def _insert_divider(chat_id):
    ts = _now()
    div = {"role": "divider", "type": "context_cleared", "timestamp": ts}
    # 只给已经有内容的对话写分隔线
    if os.path.isfile(_path(chat_id)):
        _append_messages(chat_id, None, [div])
    return div


# ==================== 上下文（内存） ====================

def _rehydrate(chat_id):
    """从历史文件恢复上下文：取最后一条 divider 之后的 user/assistant 消息。"""
    chat = _read(chat_id)
    msgs = []
    if chat:
        for m in chat.get("messages", []):
            role = m.get("role")
            if role == "divider":
                msgs = []
            elif role in ("user", "assistant"):
                msgs.append({"role": role, "content": m.get("content", "")})
    return msgs[-MAX_CONTEXT_MSGS:]


def get_context(chat_id):
    """返回该对话当前的上下文消息列表（副本）。"""
    if not _valid(chat_id):
        return []
    with _lock:
        if chat_id not in _contexts:
            _contexts[chat_id] = _rehydrate(chat_id)
        return list(_contexts[chat_id])


def record_context(chat_id, user_content, assistant_content):
    if not _valid(chat_id):
        return
    with _lock:
        ctx = _contexts.setdefault(chat_id, [])
        ctx.append({"role": "user", "content": user_content})
        ctx.append({"role": "assistant", "content": assistant_content})
        if len(ctx) > MAX_CONTEXT_MSGS:
            del ctx[:-MAX_CONTEXT_MSGS]


def clear_context(chat_id):
    """清空内存里的上下文，并往历史文件写一条 divider。返回 divider 消息。"""
    if not _valid(chat_id):
        return None
    with _lock:
        _contexts[chat_id] = []
        return _insert_divider(chat_id)
