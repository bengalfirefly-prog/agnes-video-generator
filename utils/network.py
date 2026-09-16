"""utils.network — 网络层异常的人话诊断（v6.4.8）

把 ``requests`` / ``urllib3`` / ``socket`` 的底层网络异常（域名解析失败、连接被拒、
代理不通）翻译成用户能自助排查的提示。

背景（GitHub issue #56 / #57）：用户本机 DNS 解析不了 Agnes 的输出文件域名
``cos-platform-outputs.agnes-ai.cn``（腾讯云 COS），视频在服务端**已经生成成功**，
只差最后一步下载。此前这类失败只抛出 ``RetryError[<Future ... raised ConnectionError>]``，
用户无从判断是自己机器的网络问题还是服务故障，于是反复点「重试任务」而无效。

设计约束：
- 只翻译**能确定归因**的两类（DNS 解析失败 / 连接无法建立），其余返回空串由调用方
  回退到 ``str(exc)``；绝不猜测性改写，避免把 429 / 5xx / 超时也套上网络文案。
- 返回值面向终端用户，会同时进入进度消息、失败面板与诊断报告，因此自包含：
  现象 + 影响 + 自助步骤 + 续传指引。
"""

import re
import socket
from typing import List

__all__ = ["describe_network_error", "is_network_infra_error"]

# 异常链遍历深度上限（tenacity → requests → urllib3 → socket 一般 4~5 层）
_MAX_CHAIN_DEPTH = 12

# 目标主机提取：urllib3 的 host='xxx' 优先，其次异常文本里的完整 URL
_HOST_PATTERNS = (
    re.compile(r"host='([^']+)'"),
    re.compile(r"host=([^,\s]+)"),
    re.compile(r"https?://([^/:\s]+)"),
)

# DNS 解析失败（Windows 11004 / Linux -2、-3 / Temporary failure）
_DNS_MARKERS = (
    "getaddrinfo failed",
    "failed to resolve",
    "name or service not known",
    "temporary failure in name resolution",
    "nodename nor servname provided",
    "no address associated with hostname",
    "nameresolution",
    "gaierror",
    "11004",
)

# 连接被拒 / 无法建立连接（不含 timeout：超时多为服务端拥塞，不归因到本地环境）
_CONNECT_MARKERS = (
    "connection refused",
    "connection aborted",
    "cannot connect to proxy",
    "tunnel connection failed",
    "newconnectionerror",
    "host unreachable",
    "network is unreachable",
)


def _chain_exceptions(exc: BaseException) -> List[BaseException]:
    """展开异常链（``__cause__`` / ``__context__`` + tenacity ``last_attempt``）。

    Args:
        exc: 顶层异常。

    Returns:
        链上的异常对象列表，按广度优先、去重、限深。
    """
    chain: List[BaseException] = []
    seen: set[int] = set()
    pending: List[BaseException] = [exc]
    while pending and len(chain) < _MAX_CHAIN_DEPTH:
        current = pending.pop(0)
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        inner = _retryerror_cause(current)
        if inner is not None:
            pending.append(inner)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return chain


def _retryerror_cause(exc: BaseException) -> "BaseException | None":
    """取出 tenacity ``RetryError.last_attempt`` 内包着的真实异常（非 RetryError 返回 None）。"""
    last_attempt = getattr(exc, "last_attempt", None)
    if last_attempt is None:
        return None
    try:
        inner = last_attempt.exception()
    except Exception:
        return None
    return inner if isinstance(inner, BaseException) else None


def _chain_text(exc: BaseException) -> str:
    """拼接异常链的类型名与消息（小写），供关键词匹配。"""
    parts = [f"{type(item).__name__}: {item}" for item in _chain_exceptions(exc)]
    return "\n".join(parts).lower()


def _extract_host(text: str) -> str:
    """从异常文本里提取失败的目标主机名（提不到返回空串）。"""
    for pattern in _HOST_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return ""


def _has_gaierror(exc: BaseException) -> bool:
    """异常链里是否存在 socket.gaierror（DNS 失败的类型级证据）。"""
    return any(isinstance(item, socket.gaierror) for item in _chain_exceptions(exc))


def is_network_infra_error(exc: BaseException) -> bool:
    """判断异常是否属于「本机网络 / 域名解析」这一类环境故障。

    Args:
        exc: 待判定的异常。

    Returns:
        True 表示可归因为本地网络环境（DNS 解析失败或连接无法建立）。
    """
    text = _chain_text(exc)
    if _has_gaierror(exc):
        return True
    return any(marker in text for marker in _DNS_MARKERS + _CONNECT_MARKERS)


def describe_network_error(exc: BaseException) -> str:
    """把网络层异常翻译成用户可自助排查的中文提示。

    Args:
        exc: 流水线捕获到的原始异常（可能是 tenacity ``RetryError`` 包装）。

    Returns:
        用户可读提示；无法确定归因时返回空串，调用方应回退到原始异常文本。
    """
    text = _chain_text(exc)
    is_dns = _has_gaierror(exc) or any(marker in text for marker in _DNS_MARKERS)
    is_connect = any(marker in text for marker in _CONNECT_MARKERS)
    if not (is_dns or is_connect):
        return ""

    host = _extract_host(text)
    target = f"`{host}`" if host else "Agnes 服务域名"
    if is_dns:
        return (
            f"网络诊断：本机无法解析域名 {target}（DNS 解析失败）。"
            "服务端任务通常已经完成，只是本机取不回结果文件。"
            "请依次检查：1) 换用能解析该域名的 DNS（国内推荐 223.5.5.5 或 119.29.29.29）；"
            "2) 关闭代理/VPN 的 DNS 劫持，检查安全软件与 hosts 是否拦截了该域名；"
            "3) Windows 执行 ipconfig /flushdns 后重新打开本页；"
            "4) 恢复后点「重试任务」从失败环节续传，已生成的视频不会重复提交。"
        )
    return (
        f"网络诊断：本机无法连接到 {target}（连接被拒绝或被拦截）。"
        "请检查代理、VPN、防火墙或安全软件是否拦截了该地址，"
        "放行后点「重试任务」从失败环节续传，已生成的视频不会重复提交。"
    )
