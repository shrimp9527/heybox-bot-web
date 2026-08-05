"""
原子 JSON 读写工具
写文件先落 .tmp 再 os.replace，避免写入中断产生半截文件；
读取时主文件损坏自动回退 .bak 备份。
"""

import copy
import json
import os


def atomic_write_json(path: str, data):
    """
    原子写入 JSON 文件：先写 path + ".tmp"，再 os.replace 到 path。
    写入成功后顺带把同内容写到 path + ".bak" 作为备份。
    任何异常都会抛出，由调用方处理。
    """
    tmp_path = path + ".tmp"
    bak_path = path + ".bak"
    text = json.dumps(data, ensure_ascii=False, indent=2)
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp_path, path)
    # 主文件写成功后顺带更新备份（备份失败不影响主流程）
    try:
        with open(bak_path, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass


def load_json_with_backup(path: str, default):
    """
    读取 JSON 文件，主文件失败时尝试 path + ".bak" 备份。
    都失败则返回 default 的深拷贝。
    """
    for candidate in (path, path + ".bak"):
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return copy.deepcopy(default)
