"""v6.4.8 网络异常诊断（utils.network）测试。

覆盖 GitHub issue #56 / #57 的真实异常形态：本机 DNS 解析不了 Agnes 输出文件域名
`cos-platform-outputs.agnes-ai.cn`，tenacity RetryError 包着 requests ConnectionError
包着 socket.gaierror(11004)。要求：
- 这类故障翻译成人话（含域名 + DNS 自助步骤），不再只抛 RetryError[...]；
- 429 / 5xx / 超时等偶发故障**不得**被误判成本地网络问题（否则引导文案会误导用户）。
"""

import socket
from concurrent.futures import Future

import pytest
import requests
from tenacity import RetryError

from utils.network import describe_network_error, is_network_infra_error

# issue #57 里的原始异常文本（截断保留关键部分）
_DNS_TEXT = (
    "HTTPSConnectionPool(host='cos-platform-outputs.agnes-ai.cn', port=443): "
    "Max retries exceeded with url: /videos/agnes-video-v2.0/video_ca26a36d.mp4 "
    "(Caused by NameResolutionError(\"HTTPSConnection(host='cos-platform-outputs.agnes-ai.cn', "
    "port=443): Failed to resolve 'cos-platform-outputs.agnes-ai.cn' "
    "([Errno 11004] getaddrinfo failed)\"))"
)


def _dns_retry_error() -> RetryError:
    """复刻 issue #56/#57 的异常链：tenacity → requests → urllib3 → socket.gaierror。"""
    gai = socket.gaierror(11004, "getaddrinfo failed")
    conn_error = requests.exceptions.ConnectionError(_DNS_TEXT)
    conn_error.__cause__ = gai
    future: Future = Future()
    future.set_exception(conn_error)
    return RetryError(future)


def test_dns_failure_is_recognized_as_local_network_issue():
    exc = _dns_retry_error()
    assert is_network_infra_error(exc) is True


def test_dns_failure_message_is_actionable_and_names_host():
    message = describe_network_error(_dns_retry_error())
    assert message, "DNS 解析失败必须给出可读提示，不能回退成 RetryError[...]"
    assert "cos-platform-outputs.agnes-ai.cn" in message, "提示应包含解析失败的域名"
    assert "DNS" in message
    assert "223.5.5.5" in message, "应给出可切换的 DNS 建议"
    assert "重试任务" in message, "应引导网络恢复后从失败环节续传"


def test_connection_refused_is_recognized():
    exc = requests.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='api.agnes-ai.cn', port=443): "
        "Max retries exceeded with url: /v1/videos (Caused by "
        "NewConnectionError('<urllib3.connection.HTTPSConnection object>: "
        "Failed to establish a new connection: [Errno 111] Connection refused'))"
    )
    message = describe_network_error(exc)
    assert "无法连接" in message
    assert "api.agnes-ai.cn" in message


@pytest.mark.parametrize(
    "text",
    [
        "HTTP 429: Too Many Requests",
        "HTTP 502: Bad Gateway",
        "HTTPSConnectionPool(host='apihub.agnes-ai.com', port=443): "
        "Read timed out. (read timeout=30)",
        "Video generation failed: content policy violation",
    ],
)
def test_transient_and_deterministic_errors_are_not_mislabeled(text):
    """限流 / 5xx / 超时 / 内容审核不能被归成本地网络故障（引导会完全错误）。"""
    exc = requests.exceptions.HTTPError(text)
    assert is_network_infra_error(exc) is False
    assert describe_network_error(exc) == ""


def test_plain_exception_returns_empty_for_caller_fallback():
    assert describe_network_error(ValueError("bad prompt")) == ""
    assert is_network_infra_error(ValueError("bad prompt")) is False


def test_host_extraction_falls_back_to_url():
    exc = requests.exceptions.ConnectionError(
        "Failed to resolve https://platform-outputs.agnes-ai.space/images/x.png via DNS"
    )
    message = describe_network_error(exc)
    assert "platform-outputs.agnes-ai.space" in message
