# -*- coding: utf-8 -*-
"""批任务进度检查点:服务重启后恢复进度并支持续跑。

任务(测活/检测试用/接码)的运行态平时只存在内存里,服务一重启就全丢。
本模块把每个任务的进度按类型落盘到 data/task_checkpoints/<task>.json:
- 任务启动时写入完整参数(keys/emails + 配置);
- 运行中由防抖保存器周期刷新已完成集合;
- 正常完成清除检查点;取消/异常保留(running=false)供续跑;
- 服务启动时发现 running=true 的检查点 => 任务被重启打断,
  恢复为 interrupted 状态并给出剩余账号列表。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Callable

_DEFAULT_DIR = Path(__file__).resolve().parent.parent / "data" / "task_checkpoints"


def _checkpoint_dir() -> Path:
    override = os.environ.get("TASK_CHECKPOINT_DIR", "").strip()
    return Path(override) if override else _DEFAULT_DIR


def checkpoint_path(task_type: str) -> Path:
    return _checkpoint_dir() / f"{task_type}.json"


def save_checkpoint(task_type: str, payload: dict[str, Any]) -> None:
    """原子写检查点(临时文件 + rename),失败静默(进度落盘不能影响任务)。"""
    try:
        path = checkpoint_path(task_type)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(tmp, path)
    except Exception:
        pass


def load_checkpoint(task_type: str) -> dict[str, Any] | None:
    try:
        path = checkpoint_path(task_type)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def clear_checkpoint(task_type: str) -> None:
    try:
        checkpoint_path(task_type).unlink(missing_ok=True)
    except Exception:
        pass


class DebouncedCheckpointSaver:
    """高频进度更新的防抖落盘。

    submit() 只安排一个延迟 timer(已有挂起则跳过),到点后调用
    payload_fn() 取最新快照落盘——两次 flush 之间的更新自动合并。
    """

    def __init__(self, task_type: str, interval: float = 2.0) -> None:
        self.task_type = task_type
        self.interval = max(0.2, float(interval))
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def submit(self, payload_fn: Callable[[], dict[str, Any]]) -> None:
        with self._lock:
            if self._timer is not None:
                return
            timer = threading.Timer(self.interval, self._flush)
            timer.daemon = True
            self._timer = timer
        timer.start()

    def _flush(self) -> None:
        with self._lock:
            self._timer = None
        try:
            save_checkpoint(self.task_type, payload_fn())
        except Exception:
            pass

    def flush_now(self, payload_fn: Callable[[], dict[str, Any]]) -> None:
        """立即落盘(任务结束路径),取消挂起的防抖 timer。"""
        with self._lock:
            timer = self._timer
            self._timer = None
        if timer is not None:
            timer.cancel()
        try:
            save_checkpoint(self.task_type, payload_fn())
        except Exception:
            pass
