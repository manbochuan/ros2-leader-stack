#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AMP 日志转发器 —— 把本地 amp_*.jsonl 转发到平台 Kafka 的 amp.* 主题

为什么需要它
------------
ACPs 的 AMP 协议要求：智能体只负责把心跳/指标/访问等日志「写到本地文件」，
再由一个 Forwarder 把它们转发到 Kafka。心跳是平台判断「智能体是否在线」的
唯一依据 —— 心跳送不到平台，发现查询按「只要在线」筛选时就找不到你，
即使业务进程活得好好的。

官方实现用 Fluent Bit（见 acps-infra 的 amp_forwarder 角色）。
本脚本用纯 Python 实现等价的转发逻辑，便于在没有 Fluent Bit 的环境里使用。

路由规则（与官方 fluent-bit.conf 一致）
--------------------------------------
    amp_heartbeat_*.jsonl  ->  amp.heartbeat
    amp_metrics_*.jsonl    ->  amp.metrics
    amp_access_*.jsonl     ->  amp.access
    amp_message_*.jsonl    ->  amp.message
    amp_system_*.jsonl     ->  amp.system
    amp_audit_*.jsonl      ->  amp.audit     （额外补 _fb_ts，对齐官方 timestamp_key）

用法
----
    python forwarder.py --probe        # 只测 Kafka 连通性，测完退出
    python forwarder.py                # 常驻转发

环境变量见 .env.example
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def load_dotenv(path: str) -> None:
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv(os.environ.get("ENV_FILE", ".env"))

LOG_DIR = Path(env("AMP_LOG_DIR", "/opt/acps-agent/acps-community/demo-partner/logs"))
STATE_DIR = Path(env("STATE_DIR", "/var/lib/amp-forwarder"))
BROKERS = [b.strip() for b in env("KAFKA_BROKERS", "wt.ioa.pub:19092").split(",") if b.strip()]
SECURITY = env("KAFKA_SECURITY_PROTOCOL", "PLAINTEXT").upper()
START_FROM = env("START_FROM", "tail").lower()        # tail=只发新产生的；head=从头补发
POLL_INTERVAL = float(env("POLL_INTERVAL", "1.0"))
REPORT_INTERVAL = float(env("REPORT_INTERVAL", "20"))
LOG_LEVEL = env("LOG_LEVEL", "INFO").upper()
RESEND_AUDIT_TS = env("RESEND_AUDIT_TS", "1") == "1"

# --probe 探测时临时覆盖 ssl_check_hostname（None 表示用 .env 里的值）
KAFKA_SSL_CHECK_HOSTNAME_OVERRIDE: bool | None = None

TOPICS = {
    "heartbeat": "amp.heartbeat",
    "metrics": "amp.metrics",
    "access": "amp.access",
    "message": "amp.message",
    "system": "amp.system",
    "audit": "amp.audit",
}

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("amp-forwarder")


# ---------------------------------------------------------------------------
# Kafka
# ---------------------------------------------------------------------------
def producer_kwargs() -> dict:
    """构造 Kafka 配置，自动丢弃当前 kafka-python 版本不支持的参数。

    不同大版本的 kafka-python 支持的名字不一样（例如
    api_version_auto_timeout_ms 在 3.x 已被移除），所以这里用
    DEFAULT_CONFIG 过滤一遍，避免 "Unrecognized configs" 报错。
    """
    from kafka import KafkaProducer

    supported = set(KafkaProducer.DEFAULT_CONFIG.keys())

    desired: dict = {
        "bootstrap_servers": BROKERS,
        "acks": 1,
        "retries": 5,
        "linger_ms": 200,
        "request_timeout_ms": 20000,
        "api_version_auto_timeout_ms": 15000,
        "security_protocol": "PLAINTEXT",
    }

    if SECURITY == "SSL":
        desired["security_protocol"] = "SSL"
        for envname, opt in (
            ("KAFKA_SSL_CAFILE", "ssl_cafile"),
            ("KAFKA_SSL_CERTFILE", "ssl_certfile"),
            ("KAFKA_SSL_KEYFILE", "ssl_keyfile"),
        ):
            v = env(envname, "")
            if v:
                desired[opt] = v
        check = KAFKA_SSL_CHECK_HOSTNAME_OVERRIDE
        if check is None:
            check = env("KAFKA_SSL_CHECK_HOSTNAME", "1") != "0"
        if not check:
            desired["ssl_check_hostname"] = False
    elif SECURITY in ("SASL_SSL", "SASL_PLAINTEXT"):
        desired.update(
            security_protocol=SECURITY,
            sasl_mechanism=env("KAFKA_SASL_MECHANISM", "PLAIN"),
            sasl_plain_username=env("KAFKA_SASL_USERNAME", ""),
            sasl_plain_password=env("KAFKA_SASL_PASSWORD", ""),
        )

    dropped = sorted(k for k in desired if k not in supported and k != "bootstrap_servers")
    if dropped:
        log.debug("当前 kafka-python 忽略这些参数：%s", dropped)

    kwargs = {k: v for k, v in desired.items() if k in supported or k == "bootstrap_servers"}
    log.debug("实际使用的 Kafka 参数：%s", sorted(kwargs))
    return kwargs


def build_producer():
    from kafka import KafkaProducer  # 延迟导入，--probe 时可以给出更清楚的报错

    return KafkaProducer(**producer_kwargs())


def probe() -> int:
    """只测连通性：按几种常见组合依次试，报告哪个能通"""
    try:
        import kafka
        from kafka import KafkaProducer  # noqa: F401
    except ImportError:
        log.error("缺少 kafka-python，请先执行： pip install kafka-python")
        return 1

    log.info("kafka-python 版本：%s", getattr(kafka, "__version__", "未知"))

    global SECURITY, KAFKA_SSL_CHECK_HOSTNAME_OVERRIDE
    original = SECURITY

    attempts = [
        ("PLAINTEXT", "PLAINTEXT", False),
        ("SSL", "SSL", True),
        ("SSL（不校验主机名）", "SSL", False),
        ("SASL_SSL", "SASL_SSL", True),
    ]

    for label, proto, check_hostname in attempts:
        SECURITY = proto
        KAFKA_SSL_CHECK_HOSTNAME_OVERRIDE = check_hostname
        log.info("尝试 %s 连接 %s ...", label, BROKERS)
        try:
            p = KafkaProducer(**producer_kwargs())
            parts = p.partitions_for("amp.heartbeat")
            log.info("  [成功] 协议=%s   amp.heartbeat 分区=%s",
                     proto, sorted(parts) if parts else "未知（主题可能还没建）")
            try:
                names = sorted(p.topics()) if hasattr(p, "topics") else []
                if names:
                    log.info("  可见主题（前 20 个）：%s", names[:20])
            except Exception:  # noqa: BLE001
                pass
            p.close(timeout=5)
            log.info("")
            log.info("==> 建议把 .env 里的 KAFKA_SECURITY_PROTOCOL 设为 %s", proto)
            if proto == "SSL" and not check_hostname:
                log.info("==> 并且加上 KAFKA_SSL_CHECK_HOSTNAME=0")
            return 0
        except Exception as exc:  # noqa: BLE001
            log.warning("  [失败] %s", repr(exc)[:300])

    SECURITY = original
    KAFKA_SSL_CHECK_HOSTNAME_OVERRIDE = None
    log.error("以上组合都连不上 Kafka。可能是：网络/端口不通、需要用户名密码、或服务端证书要求特定 CA。")
    return 1


# ---------------------------------------------------------------------------
# 文件追踪
# ---------------------------------------------------------------------------
def topic_for(path: Path) -> str | None:
    name = path.name
    if not name.startswith("amp_") or not name.endswith(".jsonl"):
        return None
    middle = name[len("amp_"): -len(".jsonl")]
    kind = middle.split("_", 1)[0]
    return TOPICS.get(kind)


class Tailer:
    """按偏移量读取一个 NDJSON 文件的新增行"""

    def __init__(self, path: Path, topic: str, start_from: str) -> None:
        self.path = path
        self.topic = topic
        self.is_audit = topic == TOPICS["audit"]
        self.state_file = STATE_DIR / (path.name + ".offset")
        self.offset = self._initial_offset(start_from)
        self.sent = 0

    def _initial_offset(self, start_from: str) -> int:
        if self.state_file.exists():
            try:
                return int(self.state_file.read_text().strip())
            except (OSError, ValueError):
                pass
        if start_from == "head":
            return 0
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def _save_offset(self) -> None:
        try:
            tmp = self.state_file.with_name(self.state_file.name + ".tmp")
            tmp.write_text(str(self.offset), encoding="utf-8")
            tmp.replace(self.state_file)
        except OSError as exc:
            log.warning("保存偏移失败 %s：%s", self.state_file, exc)

    def read_new(self) -> list[bytes]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.offset:
            log.warning("%s 被截断/轮转，从头开始", self.path.name)
            self.offset = 0
        if size == self.offset:
            return []

        lines: list[bytes] = []
        try:
            with self.path.open("rb") as fh:
                fh.seek(self.offset)
                while True:
                    raw = fh.readline()
                    if not raw:
                        break
                    if not raw.endswith(b"\n"):
                        break  # 半行，等下次读
                    self.offset += len(raw)
                    stripped = raw.strip()
                    if stripped:
                        lines.append(stripped)
        except OSError as exc:
            log.warning("读 %s 失败：%s", self.path.name, exc)
            return []

        if lines:
            self._save_offset()
        return lines


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
def send_one(producer, topic: str, payload: bytes, is_audit: bool) -> None:
    if is_audit and RESEND_AUDIT_TS:
        try:
            obj = json.loads(payload)
            obj.setdefault("_fb_ts", time.time())
            payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        except (ValueError, TypeError):
            pass
    producer.send(topic, payload)


def run() -> int:
    if not LOG_DIR.is_dir():
        log.error("日志目录不存在：%s", LOG_DIR)
        return 1
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    log.info("=" * 60)
    log.info("日志目录   : %s", LOG_DIR)
    log.info("Kafka      : %s", ",".join(BROKERS))
    log.info("安全协议   : %s", SECURITY)
    log.info("起始位置   : %s", START_FROM)
    log.info("状态目录   : %s", STATE_DIR)
    log.info("=" * 60)

    try:
        producer = build_producer()
        parts = producer.partitions_for("amp.heartbeat")
        log.info("Kafka 连接正常（amp.heartbeat 分区=%s）", sorted(parts) if parts else "未知")
    except Exception as exc:  # noqa: BLE001
        log.error("Kafka 连接失败：%s", repr(exc)[:400])
        log.error("先跑一次： python forwarder.py --probe")
        return 1

    tailers: dict[str, Tailer] = {}
    last_report = 0.0
    total_sent = 0
    total_err = 0

    while True:
        # 发现（或新增）日志文件
        for path in sorted(LOG_DIR.glob("amp_*.jsonl")):
            topic = topic_for(path)
            if topic is None or path.name in tailers:
                continue
            tailers[path.name] = Tailer(path, topic, START_FROM)
            log.info("开始跟踪 %s  ->  %s", path.name, topic)

        for name, tailer in tailers.items():
            for line in tailer.read_new():
                try:
                    send_one(producer, tailer.topic, line, tailer.is_audit)
                    tailer.sent += 1
                    total_sent += 1
                    if tailer.sent == 1:
                        log.info("首次发送成功  %s  ->  %s", name, tailer.topic)
                except Exception as exc:  # noqa: BLE001
                    total_err += 1
                    log.warning("发送失败（%s）：%s", name, repr(exc)[:200])

        try:
            producer.flush(timeout=5)
        except Exception as exc:  # noqa: BLE001
            total_err += 1
            log.warning("flush 失败：%s", repr(exc)[:200])

        now = time.time()
        if now - last_report >= REPORT_INTERVAL:
            detail = "  ".join(f"{n}={t.sent}" for n, t in tailers.items())
            log.info("累计已发=%d 失败=%d   %s", total_sent, total_err, detail)
            last_report = now

        time.sleep(POLL_INTERVAL)


def main() -> int:
    parser = argparse.ArgumentParser(description="AMP 日志转发器")
    parser.add_argument("--probe", action="store_true", help="只测 Kafka 连通性后退出")
    args = parser.parse_args()
    if args.probe:
        return probe()
    try:
        return run()
    except KeyboardInterrupt:
        log.info("收到中断，退出")
        return 0


if __name__ == "__main__":
    sys.exit(main())
