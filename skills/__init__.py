"""
Skills 模块
==========
管理 RAG 系统的场景化 Skill 配置。

每个 Skill 是一个 Python 模块，导出 SKILL_CONFIG 字典和辅助函数。
Skill 不改变 workflow 的流程结构（节点和路由不变），
只改变每个节点内部的行为参数（k 值、prompt 内容、回答格式）。

使用方式：
    from skills import load_skill, get_active_skill
    
    # 启动时加载默认 Skill
    load_skill("ai_tech_assistant")
    
    # 在 workflow 节点中获取当前 Skill
    skill = get_active_skill()
    k = skill["get_k_value"](question)
    prompt = skill["build_generate_prompt"](question, context)
"""

import importlib
from typing import Optional

# 当前激活的 Skill
_active_skill: Optional[dict] = None

# 已注册的 Skill 列表（skill_id → module_name）
SKILL_REGISTRY = {
    "ai_tech_assistant": "skills.ai_tech_assistant",
    # 以后加新 Skill 在这里注册：
    # "academic_paper": "skills.academic_paper",
    # "code_review": "skills.code_review",
}


def load_skill(skill_id: str) -> dict:
    """
    加载指定 Skill，设为当前激活。
    
    返回一个字典，包含：
    - config: SKILL_CONFIG 原始配置
    - get_k_value: 根据问题返回 k 值的函数
    - get_format_hint: 根据问题返回格式提示的函数
    - build_generate_prompt: 组装 generate prompt 的函数
    - detect_question_type: 判断问题子类型的函数
    """
    global _active_skill
    
    if skill_id not in SKILL_REGISTRY:
        raise ValueError(
            f"未知的 Skill: {skill_id}。"
            f"可用: {list(SKILL_REGISTRY.keys())}"
        )
    
    module = importlib.import_module(SKILL_REGISTRY[skill_id])
    
    _active_skill = {
        "id": skill_id,
        "config": module.SKILL_CONFIG,
        "get_k_value": module.get_k_value,
        "get_format_hint": module.get_format_hint,
        "build_generate_prompt": module.build_generate_prompt,
        "detect_question_type": module.detect_question_type,
    }
    
    print(f"[Skill] 已加载: {module.SKILL_CONFIG['name']} v{module.SKILL_CONFIG['version']}")
    return _active_skill


def get_active_skill() -> Optional[dict]:
    """获取当前激活的 Skill，未加载则返回 None。"""
    return _active_skill


def list_skills() -> list[dict]:
    """列出所有已注册的 Skill（不加载，只读元信息）。"""
    result = []
    for skill_id, module_name in SKILL_REGISTRY.items():
        try:
            module = importlib.import_module(module_name)
            config = module.SKILL_CONFIG
            result.append({
                "id": skill_id,
                "name": config["name"],
                "version": config["version"],
                "description": config["description"],
                "active": _active_skill and _active_skill["id"] == skill_id,
            })
        except Exception as e:
            result.append({
                "id": skill_id,
                "name": skill_id,
                "error": str(e),
            })
    return result
