"""
AI 技术知识助手 Skill
====================
针对 AI/ML 技术文档（八股文）的场景化行为配置。

使用方式：
    from skills.ai_tech_assistant import SKILL_CONFIG
    然后在 workflow.py 的 generate 节点和 retrieve 节点中引用配置。

设计理由：
    系统默认是通用问答模式，不区分问题类型，所有问题用同一套检索参数和回答风格。
    Skill 让系统知道"我在处理什么领域的问题"，从而：
    1. 根据问题子类型调整检索参数（k 值）
    2. 给 generate 注入领域专属的回答规范（格式、术语、引用）
    3. 定义知识边界外的回答策略
"""


SKILL_CONFIG = {
    # ============================================================
    # 基本信息
    # ============================================================
    "name": "AI技术知识助手",
    "id": "ai_tech_assistant",
    "version": "1.0",
    "description": "针对 AI/ML 技术文档的检索策略和回答规范，"
                   "优化概念解释、技术对比、关系查询三类典型问题的回答质量。",

    # ============================================================
    # 检索策略配置
    # ============================================================
    "retrieval": {
        # 问题子类型 → 推荐的检索参数
        # classify 节点已经通过 Function Calling 判定了 intent（semantic/entity/hybrid）
        # 这里的 question_type 是更细粒度的分类，用于调整 k 值
        # question_type 由 generate 节点的 prompt 识别，不改 classify 逻辑
        "question_types": {
            "concept": {
                "description": "概念解释类：什么是X、X的原理、解释X",
                "keywords": ["什么是", "解释", "原理", "定义", "是什么", "概念"],
                "k_value": 3,
                "reason": "概念通常在单个 chunk 里完整呈现，3 个 chunk 足够覆盖"
            },
            "comparison": {
                "description": "技术对比类：A和B的区别、A vs B、对比A和B",
                "keywords": ["区别", "不同", "对比", "vs", "比较", "差异", "优缺点"],
                "k_value": 6,
                "reason": "对比需要覆盖两个实体各自的信息，k 要大一些防止只搜到一边"
            },
            "relationship": {
                "description": "关系查询类：A和B有什么关系、A用到了哪些B",
                "keywords": ["关系", "联系", "用到", "依赖", "基于", "属于", "包含"],
                "k_value": 5,
                "reason": "关系在图谱里有直接存储，向量检索做补充，默认 k 即可"
            },
            "listing": {
                "description": "列举类：X有哪些组件、列举X的特点、X的步骤",
                "keywords": ["有哪些", "列举", "包括", "组成", "步骤", "流程", "类型"],
                "k_value": 6,
                "reason": "列举内容可能跨 chunk 分布，多取一些保证覆盖"
            },
            "application": {
                "description": "应用场景类：X怎么用、X的实际应用、什么时候用X",
                "keywords": ["怎么用", "应用", "场景", "实践", "案例", "什么时候用"],
                "k_value": 4,
                "reason": "应用场景通常在概念解释附近，4 个 chunk 够"
            }
        },
        "default_k": 5,
    },

    # ============================================================
    # 回答格式配置（注入 generate prompt）
    # ============================================================
    "response_format": {
        # 通用回答结构规则
        "general_rules": [
            "先用一句话给出核心结论或定义",
            "再展开详细的原理、机制或细节说明",
            "如果涉及多个要点，用清晰的分点结构",
            
        ],

        # 按问题子类型的专属格式要求
        "type_specific": {
            "concept": {
                "structure": "一句话定义 → 核心原理展开 → 关键组件/步骤 → 应用场景",
                "length_hint": "200-400字",
            },
            "comparison": {
                "structure": "一句话概括核心差异 → 逐维度对比（用表格或分点） → 选择建议",
                "length_hint": "300-600字",
                "special": "对比类问题优先使用表格呈现异同点，表格列为对比维度，行为对比对象",
            },
            "relationship": {
                "structure": "一句话说明关系类型 → 具体关联方式 → 实际协作场景",
                "length_hint": "150-300字",
            },
            "listing": {
                "structure": "总数概述 → 逐项列举（每项一句话说明） → 补充说明",
                "length_hint": "按条目数量自适应，每条1-2句",
            },
            "application": {
                "structure": "适用场景概述 → 具体使用方式 → 注意事项或局限",
                "length_hint": "200-400字",
            },
        },
    },

    # ============================================================
    # 术语处理规则（注入 generate prompt）
    # ============================================================
    "terminology": {
        "first_mention_format": "{中文名}（{English Name}）",
        "first_mention_rule": "专业术语首次出现时给出中英文对照，"
                              "如果术语本身不常见则附一句话解释",
        "subsequent_rule": "后续出现直接使用中文名或通用缩写",
        "examples": [
            "检索增强生成（RAG, Retrieval-Augmented Generation）",
            "大语言模型（LLM, Large Language Model）",
            "向量数据库（Vector Database）",
            "注意力机制（Attention Mechanism）",
            "微调（Fine-tuning）",
        ],
    },

   

    # ============================================================
    # 回答边界规则（配合 verify 节点）
    # ============================================================
    "boundary": {
        # 知识库中完全没有相关信息时的回答模板
        "no_info_template": (
            "当前知识库中没有关于「{topic}」的直接信息。"
            "建议上传相关文档后重试，或尝试换一个角度提问。"
        ),
        # 知识库中信息不完整时的处理方式
        "partial_info_rule": "基于已有检索内容回答，不编造文档中没有的技术细节。"
                             "明确区分'文档中提到的'和'超出知识库范围的'内容。",
        # 绝对禁止的行为
        "forbidden": [
            "不编造知识库中没有的技术细节、数据或引用",
            "不猜测两个实体之间不存在于图谱中的关系",
            "不把不同技术的特性张冠李戴",
        ],
    },

    # ============================================================
    # 组装后的 generate system prompt（直接注入 generate 节点）
    # ============================================================
    "generate_system_prompt": """你是一个专业的 AI 技术知识助手。请基于提供的检索内容回答用户问题。

## 回答规范

### 结构要求
1. 先用一句话给出核心结论或定义
2. 再展开详细的原理、机制或细节说明
3. 如果涉及多个要点，用清晰的分点结构
4. 结尾给出实际应用场景或帮助记忆的要点总结

### 对比类问题
当用户问"A和B的区别""A vs B"等对比类问题时，使用表格呈现异同点。

### 术语处理
- 专业术语首次出现时给出中英文对照，格式：中文名（English Name）
- 如果术语不常见，附加一句话解释
- 后续出现直接使用中文名或通用缩写



### 严格限制
- 只使用检索内容中明确提到的信息
- 不编造技术细节、数据或引用
- 不猜测实体之间不存在于检索结果中的关系
- 如果检索内容不足以完整回答，明确说明哪些部分超出了当前知识库范围
""",
}


# ============================================================
# 辅助函数：根据用户问题判断问题子类型
# ============================================================
def detect_question_type(question: str) -> str:
    """
    根据用户问题中的关键词判断问题子类型。
    返回值：concept / comparison / relationship / listing / application
    
    这个函数不替代 classify 节点的 intent 判断（semantic/entity/hybrid），
    而是更细粒度的分类，用于：
    1. 动态调整检索的 k 值
    2. 在 generate prompt 中附加类型专属的格式要求
    """
    question_lower = question.lower()
    
    type_configs = SKILL_CONFIG["retrieval"]["question_types"]
    
    for qtype, config in type_configs.items():
        for keyword in config["keywords"]:
            if keyword in question_lower:
                return qtype
    
    # 没有命中任何关键词，默认 concept
    return "concept"


def get_k_value(question: str) -> int:
    """根据问题子类型返回推荐的 k 值。"""
    qtype = detect_question_type(question)
    type_config = SKILL_CONFIG["retrieval"]["question_types"].get(qtype)
    if type_config:
        return type_config["k_value"]
    return SKILL_CONFIG["retrieval"]["default_k"]


def get_format_hint(question: str) -> str:
    """
    根据问题子类型返回格式提示，附加到 generate prompt 末尾。
    让 LLM 知道这类问题该用什么结构回答。
    """
    qtype = detect_question_type(question)
    type_format = SKILL_CONFIG["response_format"]["type_specific"].get(qtype)
    
    if not type_format:
        return ""
    
    hint = f"\n\n## 本次回答的格式要求\n"
    hint += f"问题类型：{qtype}\n"
    hint += f"推荐结构：{type_format['structure']}\n"
    hint += f"参考长度：{type_format['length_hint']}\n"
    
    if "special" in type_format:
        hint += f"特殊要求：{type_format['special']}\n"
    
    return hint


def build_generate_prompt(question: str, context: str, is_retry: bool = False) -> str:
    """
    组装完整的 generate prompt。
    
    参数：
        question: 用户问题
        context: 检索到的内容（向量 + 图谱拼接后的文本）
        is_retry: 是否是 verify 打回后的重试
    
    返回：
        完整的 prompt 字符串，包含 system prompt + 检索内容 + 问题 + 格式提示
    """
    system_prompt = SKILL_CONFIG["generate_system_prompt"]
    format_hint = get_format_hint(question)
    
    prompt = f"""{system_prompt}
{format_hint}

## 检索到的内容
{context}

## 用户问题
{question}
"""
    
    if is_retry:
        prompt += """
## ⚠️ 重试约束
这是验证未通过后的重试。请严格只使用上方检索内容中的信息回答，
不要添加任何检索内容中没有提到的细节。
"""
    
    return prompt
