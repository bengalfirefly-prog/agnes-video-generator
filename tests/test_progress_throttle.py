"""2.4 进度写盘节流测试。

验证 _emit 对进度类字段的写盘节流：0.5s 阈值内多次 emit 合并为一次落盘，
超过阈值后再次落盘；关键状态（不经 _emit）不受影响。
"""
import asyncio
from types import SimpleNamespace

import pytest

from core.pipelines import BasePipeline


class _DummyPipeline(BasePipeline):
    async def run(self, state):
        return None


def _make_pipeline(update_state) -> BasePipeline:
    p = object.__new__(_DummyPipeline)  # 绕过 __init__
    p.task_manager = SimpleNamespace(update_state=update_state)
    p.progress_callback = None
    p._state = SimpleNamespace(
        current_step="", current_status="", current_progress=0.0, current_message="",
    )
    return p


async def test_emit_throttles_rapid_progress_writes():
    """0.5s 内多次 emit → 只落盘一次；超过阈值后再次落盘。"""
    calls = []
    p = _make_pipeline(lambda **kw: calls.append(kw))

    for i in range(5):
        await p._emit("video_gen", "running", f"msg{i}", i / 10)
    assert len(calls) == 1, "0.5s 内的连续进度更新应合并为一次写盘"

    await asyncio.sleep(0.6)
    await p._emit("video_gen", "running", "msg-late", 0.6)
    assert len(calls) == 2, "超过节流阈值后应再次落盘"
    assert calls[-1]["current_progress"] == 0.6


async def test_emit_always_updates_memory_state():
    """节流只合并写盘，内存 state 每次 emit 都更新（前端读内存不滞后）。"""
    calls = []
    p = _make_pipeline(lambda **kw: calls.append(kw))

    for i in range(4):
        await p._emit("video_gen", "running", f"msg{i}", i / 10)
    # 内存始终是最新值
    assert p._state.current_message == "msg3"
    assert p._state.current_progress == 0.3


async def test_terminal_status_bypasses_throttle():
    """v6.4.8：终态（completed/failed）紧跟进度写入时也必须落盘。

    此前终态与其他进度更新一样被 0.5s 节流合并，紧跟在上一次写入之后的
    completed/failed 事件会整条丢弃，盘上的 current_step / current_message
    停留在很久以前的旧值，诊断报告因此归因失真（issue #56/#57）。
    """
    calls = []
    p = _make_pipeline(lambda **kw: calls.append(kw))

    await p._emit("video_gen", "running", "正在下载", 0.5)
    await p._emit("video_gen", "completed", "视频生成完成", 0.8)
    assert len(calls) == 2, "终态事件不应被写盘节流丢弃"
    assert calls[-1]["current_step"] == "video_gen"
    assert calls[-1]["current_message"] == "视频生成完成"


async def test_error_emit_preserves_real_step_name():
    """v6.4.8：失败终态用 preserve_step 保留真实环节名，只更新状态与消息。"""
    calls = []
    p = _make_pipeline(lambda **kw: calls.append(kw))

    await p._emit("video_gen", "running", "网络诊断：稍后重试", 0.5)
    await p._emit("error", "failed", "网络诊断：本机无法解析域名", 0.0, preserve_step=True)

    assert p._state.current_step == "video_gen", "error 事件不能把环节名冲成 error"
    assert p._state.current_status == "failed"
    last = calls[-1]
    assert "current_step" not in last, "preserve_step 时不应写盘覆盖 current_step"
    assert last["current_status"] == "failed"


def test_save_removes_indent(tmp_path, monkeypatch):
    """2.4：task_state.json 不再使用 indent=2（体积减半）。"""
    import json

    from core.task_manager import TaskManager
    from models.task import SimpleVideoTask

    monkeypatch.setattr("core.task_manager.get_working_dir", lambda: str(tmp_path))
    tm = TaskManager("t_indent", dir_name="d_indent")
    tm.create(SimpleVideoTask(task_id="t_indent", prompt="测试"))
    raw = (tmp_path / "d_indent" / "task_state.json").read_text(encoding="utf-8")
    # 无缩进：不包含 "\n  " 结构（紧凑 JSON 单行）
    assert "\n" not in raw, "紧凑写盘不应含换行"
    data = json.loads(raw)
    assert data["task_id"] == "t_indent"
