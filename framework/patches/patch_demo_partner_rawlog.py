#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo-partner 补丁 #2：打印完整的原始 JSON-RPC 请求

问题
----
梧桐（叮当）发来的任务里，框架提取出的文本只有编排描述：
    "调用智能体「...」\n\nDAG 指定的上游产物摘要：\n叮当已完成协作规划并开始按 DAG 派发。"
看不到用户真正的问题。需要确认：问题**根本没发**，还是**发了但在别的字段里**。

改动
----
在 partners/main.py 的 /rpc 端点里，把完整的 RpcRequest 打印到日志。
日志可通过 journalctl -u acps-agent 查看。

回滚
----
    cp partners/main.py.bak2 partners/main.py
"""

from __future__ import annotations

import py_compile
import shutil
import sys
from pathlib import Path

TARGET = Path("/opt/acps-agent/acps-community/demo-partner/partners/main.py")

R_OLD = """        command = rpc_request.params.command
        request_bytes = len(rpc_request.model_dump_json().encode("utf-8"))
"""

R_NEW = """        command = rpc_request.params.command
        # [本地补丁#2] 打印完整原始请求，用于排查 Leader 到底发了什么
        try:
            _raw = rpc_request.model_dump_json()
            logger.info("RAW_RPC_REQUEST", raw_len=len(_raw), raw=_raw)
        except Exception:
            logger.warning("RAW_RPC_REQUEST dump failed", exc_info=True)
        request_bytes = len(rpc_request.model_dump_json().encode("utf-8"))
"""


def main() -> int:
    if not TARGET.exists():
        print(f"[失败] 找不到文件：{TARGET}")
        return 1

    source = TARGET.read_text(encoding="utf-8")

    if "RAW_RPC_REQUEST" in source:
        print("[跳过] 这个文件已经打过补丁#2了。")
        return 0

    hits = source.count(R_OLD)
    if hits != 1:
        print(f"[失败] 锚点匹配到 {hits} 处（应该是 1 处），未做任何修改。")
        print("请把这段输出贴给助手，重新生成补丁。")
        return 1

    patched = source.replace(R_OLD, R_NEW, 1)

    backup = TARGET.with_name(TARGET.name + ".bak2")
    shutil.copy2(TARGET, backup)
    TARGET.write_text(patched, encoding="utf-8")

    try:
        py_compile.compile(str(TARGET), doraise=True)
    except py_compile.PyCompileError as exc:
        shutil.copy2(backup, TARGET)
        print(f"[失败] 语法检查不通过，已还原。错误：{exc}")
        return 1

    print("[成功] 补丁#2 已应用，语法检查通过。")
    print(f"       原文件备份为 {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
