#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo-partner 本地补丁

问题
----
partners/main.py 在 lifespan 里无条件创建并启动 GroupHandler（群组模式 / RabbitMQ）。
GroupHandler._start_shared_inbox_consumer() 只捕获 (AMQPError, OSError)，
但服务端在没有 mTLS 证书时会抛 RuntimeError:

    RuntimeError: ssl_context is required for AMQPS + EXTERNAL authentication

这个 RuntimeError 没有被捕获，冒泡到 lifespan，导致 Application startup failed。

修复
----
ACS 的 capabilities.messageQueue 为空数组时（即不声明群组能力），
不创建也不启动 GroupHandler。

改动三处，改完全部可回滚（自动备份为 partners/main.py.bak）。

用法
----
    cd /opt/acps-agent/acps-community/demo-partner
    python3 /path/to/patch_demo_partner.py
"""

from __future__ import annotations

import py_compile
import shutil
import sys
from pathlib import Path

TARGET = Path("/opt/acps-agent/acps-community/demo-partner/partners/main.py")

# ---- 改动 1：读能力位时，把 messageQueue 也算出来 ----
R1_OLD = """        capabilities = acs_data.get("capabilities", {})
        streaming_enabled = bool(capabilities.get("streaming", False))
        notification_enabled = bool(capabilities.get("notification", False))
"""
R1_NEW = """        capabilities = acs_data.get("capabilities", {})
        streaming_enabled = bool(capabilities.get("streaming", False))
        notification_enabled = bool(capabilities.get("notification", False))
        message_queue_enabled = bool(capabilities.get("messageQueue") or [])
"""

# ---- 改动 2：读 acs.json 失败时的兜底分支也要有这个变量 ----
R2_OLD = """    except Exception:
        config = {}
        streaming_enabled = False
        notification_enabled = False
        acs_data = {}
"""
R2_NEW = """    except Exception:
        config = {}
        streaming_enabled = False
        notification_enabled = False
        message_queue_enabled = False
        acs_data = {}
"""

# ---- 改动 3：只有声明了群组能力才启动 GroupHandler ----
R3_OLD = """        rabbitmq_cfg = config.get("rabbitmq", {})
        mq_ssl_context = build_rabbitmq_ssl_context(agent_path, server_cfg)
        runner = GenericRunner(agent_name, agent_path)
        group_handler = GroupHandler(
            agent_name,
            runner,
            rabbitmq_config=rabbitmq_cfg,
            ssl_context=mq_ssl_context,
            identity_binding_enabled=identity_binding_enabled,
        )
        await group_handler.start()
        runner.start_heartbeat()
"""
R3_NEW = """        runner = GenericRunner(agent_name, agent_path)
        # [本地补丁] 未声明 messageQueue 能力时不启动群组模式。
        # 否则 GroupHandler 会去连 RabbitMQ，失败时抛 RuntimeError（未被其内部捕获）
        # 导致 Application startup failed。
        if message_queue_enabled:
            rabbitmq_cfg = config.get("rabbitmq", {})
            mq_ssl_context = build_rabbitmq_ssl_context(agent_path, server_cfg)
            group_handler = GroupHandler(
                agent_name,
                runner,
                rabbitmq_config=rabbitmq_cfg,
                ssl_context=mq_ssl_context,
                identity_binding_enabled=identity_binding_enabled,
            )
            await group_handler.start()
        runner.start_heartbeat()
"""

PATCHES = (
    ("能力位读取", R1_OLD, R1_NEW),
    ("异常兜底分支", R2_OLD, R2_NEW),
    ("lifespan 启动逻辑", R3_OLD, R3_NEW),
)


def main() -> int:
    if not TARGET.exists():
        print(f"[失败] 找不到文件：{TARGET}")
        return 1

    source = TARGET.read_text(encoding="utf-8")

    if "message_queue_enabled" in source:
        print("[跳过] 这个文件已经打过补丁了，无需重复。")
        return 0

    # 先全部校验，任何一个不匹配就整体不写，保证不会改出半成品
    for label, old, _new in PATCHES:
        hits = source.count(old)
        if hits != 1:
            print(f"[失败] 补丁「{label}」匹配到 {hits} 处（应该是 1 处），未做任何修改。")
            print("请把这一整段输出贴给助手，重新生成补丁。")
            return 1

    patched = source
    for _label, old, new in PATCHES:
        patched = patched.replace(old, new, 1)

    backup = TARGET.with_name(TARGET.name + ".bak")
    shutil.copy2(TARGET, backup)
    TARGET.write_text(patched, encoding="utf-8")

    try:
        py_compile.compile(str(TARGET), doraise=True)
    except py_compile.PyCompileError as exc:
        shutil.copy2(backup, TARGET)
        print(f"[失败] 打补丁后语法检查不通过，已还原原文件。错误：{exc}")
        return 1

    print("[成功] 补丁已应用，语法检查通过。")
    print(f"       原文件已备份为 {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
