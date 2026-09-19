"""重建默认知识库「AI面试题」。

日常增删文档走前端的「知识库管理」页（上传 / 删除会自动切块、抽实体关系、
重建索引），不用再跑这个脚本。只有想把整个源文档目录整体重灌一遍时才用它。

切块 / 抽实体关系 / 建 SQLite + FAISS 的逻辑都在 kb_store.py 里。
"""

import kb_store

# 源文档目录（.md / .pdf 都会被收录）
FOLDER = r"E:\ailearning\ai-agent-interview-guide-main\ai-agent-interview-guide-main\docs\01-面试八股文"

if __name__ == "__main__":
    print(f"重建知识库「{kb_store.DEFAULT_KB_NAME}」（{kb_store.DEFAULT_KB_ID}）")
    print(f"源目录：{FOLDER}\n")
    kb_store.rebuild_kb_from_folder(
        kb_store.DEFAULT_KB_ID,
        FOLDER,
        name=kb_store.DEFAULT_KB_NAME,
        verbose=True,
    )
    print("\n完成。重启后端即可（索引缓存靠文件 mtime 自动失效，通常不用重启）。")
