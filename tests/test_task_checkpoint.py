# -*- coding: utf-8 -*-
"""任务进度检查点测试:测活/检测试用的重启恢复与续跑数据。"""
from __future__ import annotations

import json
import threading

import pytest

from common import task_checkpoint


@pytest.fixture()
def ckpt_dir(tmp_path, monkeypatch):
    """每个测试独立检查点目录。"""
    target = tmp_path / "checkpoints"
    monkeypatch.setenv("TASK_CHECKPOINT_DIR", str(target))
    return target


@pytest.fixture()
def reset_health_state():
    import web_app

    with web_app._ACCOUNT_HEALTH_PROGRESS_LOCK:
        web_app._ACCOUNT_HEALTH_PROGRESS.update(
            running=False,
            interrupted=False,
            pending_keys=[],
            pending_count=0,
            message="",
        )
    web_app._ACCOUNT_HEALTH_CHECKPOINT_KEYS = []
    web_app._ACCOUNT_HEALTH_DONE_KEYS.clear()
    yield
    web_app._ACCOUNT_HEALTH_CHECKPOINT_KEYS = []
    web_app._ACCOUNT_HEALTH_DONE_KEYS.clear()


@pytest.fixture()
def reset_trial_state():
    from common.trial_eligibility_jobs import TRIAL_ELIGIBILITY_JOBS

    with TRIAL_ELIGIBILITY_JOBS.lock:
        TRIAL_ELIGIBILITY_JOBS.jobs.clear()
        TRIAL_ELIGIBILITY_JOBS.active_job_id = ""
    TRIAL_ELIGIBILITY_JOBS._checkpoint_done.clear()
    yield
    with TRIAL_ELIGIBILITY_JOBS.lock:
        TRIAL_ELIGIBILITY_JOBS.jobs.clear()
        TRIAL_ELIGIBILITY_JOBS.active_job_id = ""
    TRIAL_ELIGIBILITY_JOBS._checkpoint_done.clear()


class TestTaskCheckpointStore:
    def test_save_load_roundtrip(self, ckpt_dir):
        task_checkpoint.save_checkpoint(
            "demo", {"running": True, "keys": ["a", "b"], "done_keys": ["a"]}
        )
        loaded = task_checkpoint.load_checkpoint("demo")
        assert loaded == {"running": True, "keys": ["a", "b"], "done_keys": ["a"]}
        assert (ckpt_dir / "demo.json").is_file()

    def test_load_missing_returns_none(self, ckpt_dir):
        assert task_checkpoint.load_checkpoint("missing") is None

    def test_clear_removes_file(self, ckpt_dir):
        task_checkpoint.save_checkpoint("demo", {"running": True})
        task_checkpoint.clear_checkpoint("demo")
        assert task_checkpoint.load_checkpoint("demo") is None
        task_checkpoint.clear_checkpoint("demo")  # 幂等

    def test_corrupt_file_returns_none(self, ckpt_dir):
        ckpt_dir.mkdir(parents=True, exist_ok=True)
        (ckpt_dir / "bad.json").write_text("{not json", encoding="utf-8")
        assert task_checkpoint.load_checkpoint("bad") is None

    def test_flush_now_writes_latest_payload(self, ckpt_dir):
        state = {"done": 1}

        def payload():
            return {"running": True, **state}

        saver = task_checkpoint.DebouncedCheckpointSaver("debounce", interval=60)
        saver.submit(payload)
        state["done"] = 2
        saver.flush_now(payload)
        loaded = task_checkpoint.load_checkpoint("debounce")
        assert loaded["done"] == 2


class TestHealthCheckpoint:
    @pytest.fixture()
    def web_app_mod(self):
        import web_app

        return web_app

    def _write_checkpoint(self, ckpt_dir, payload):
        ckpt_dir.mkdir(parents=True, exist_ok=True)
        (ckpt_dir / "health_check.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def test_begin_persists_running_checkpoint(
        self, ckpt_dir, web_app_mod, reset_health_state
    ):
        web_app_mod._account_health_checkpoint_begin(["k1", "k2"], workers=5)
        ck = task_checkpoint.load_checkpoint("health_check")
        assert ck["running"] is True
        assert ck["params"]["keys"] == ["k1", "k2"]
        assert ck["done_keys"] == []

    def test_restore_interrupted(
        self, ckpt_dir, web_app_mod, reset_health_state
    ):
        self._write_checkpoint(
            ckpt_dir,
            {
                "version": 1,
                "task_type": "health_check",
                "running": True,
                "completed": False,
                "params": {"keys": ["k1", "k2", "k3"], "workers": 10},
                "done_keys": ["k1"],
                "snapshot": {
                    "running": True,
                    "requested": 3,
                    "total": 3,
                    "done": 1,
                    "checked": 1,
                    "alive": 1,
                    "updates": {},
                },
                "saved_at": "2026-09-05T00:00:00Z",
            },
        )
        web_app_mod._account_health_checkpoint_restore()
        snap = web_app_mod._account_health_progress_snapshot(include_updates=False)
        assert snap["running"] is False
        assert snap["interrupted"] is True
        assert snap["pending_keys"] == ["k2", "k3"]
        assert snap["pending_count"] == 2
        assert "服务重启" in snap["message"]

    def test_restore_skips_completed(
        self, ckpt_dir, web_app_mod, reset_health_state
    ):
        self._write_checkpoint(
            ckpt_dir,
            {"running": False, "completed": True, "params": {}, "done_keys": []},
        )
        web_app_mod._account_health_checkpoint_restore()
        snap = web_app_mod._account_health_progress_snapshot(include_updates=False)
        assert snap.get("interrupted") is False

    def test_finish_success_clears_checkpoint(
        self, ckpt_dir, web_app_mod, reset_health_state
    ):
        web_app_mod._account_health_checkpoint_begin(["k1", "k2"], workers=5)
        # 模拟被跳过的封号账号不触发回调:完成时 pending 仍必须为空
        web_app_mod._finish_account_health_progress(
            result={
                "requested": 2,
                "actual_total": 2,
                "checked": 2,
                "usable": 2,
                "unavailable": 0,
                "skipped_known_deactivated": 0,
                "deactivated": 0,
                "invalid_other": 0,
                "alive": 2,
                "limited": 0,
                "invalid": 0,
                "reauth_required": 0,
                "error": 0,
            }
        )
        assert task_checkpoint.load_checkpoint("health_check") is None
        snap = web_app_mod._account_health_progress_snapshot(include_updates=False)
        assert snap["pending_keys"] == []
        assert snap["pending_count"] == 0
        assert snap["interrupted"] is False

    def test_finish_cancel_keeps_pending_for_resume(
        self, ckpt_dir, web_app_mod, reset_health_state
    ):
        web_app_mod._account_health_checkpoint_begin(["k1", "k2", "k3"], workers=5)
        web_app_mod._ACCOUNT_HEALTH_DONE_KEYS.update({"k1"})
        web_app_mod._finish_account_health_progress(message="测活请求已取消")
        ck = task_checkpoint.load_checkpoint("health_check")
        assert ck is not None
        assert ck["running"] is False
        snap = web_app_mod._account_health_progress_snapshot(include_updates=False)
        assert snap["pending_keys"] == ["k2", "k3"]
        assert snap["pending_count"] == 2


class TestTrialCheckpoint:
    @staticmethod
    def _record(key: str, email: str, **extra):
        return {
            "email": email,
            "account_id": email.split("@")[0],
            "access_token": f"at-{key}",
            "plan_type": "free",
            **extra,
        }

    def test_start_writes_checkpoint(self, ckpt_dir, reset_trial_state, monkeypatch):
        import common.trial_eligibility_jobs as tej

        release = threading.Event()

        def fake_check(record, proxy="", direct=False):
            release.wait(timeout=10)  # 阻塞保证检查点在运行中被读取
            raise RuntimeError("stopped in test")

        monkeypatch.setattr(tej, "check_trial_eligibility", fake_check)
        records = [self._record("k1", "a@x.com"), self._record("k2", "b@x.com")]
        expected_keys = [tej.account_delete_key(record) for record in records]
        job = tej.TRIAL_ELIGIBILITY_JOBS.start(records, concurrency=2)
        try:
            ck = task_checkpoint.load_checkpoint("trial_eligibility")
            assert ck["running"] is True
            assert ck["keys"] == expected_keys
            assert ck["emails"] == ["a@x.com", "b@x.com"]
            assert ck["done_keys"] == []
        finally:
            release.set()
            tej.TRIAL_ELIGIBILITY_JOBS.wait(job["job_id"], 10)

    def test_restore_interrupted_job(self, ckpt_dir, reset_trial_state):
        ckpt_dir.mkdir(parents=True, exist_ok=True)
        (ckpt_dir / "trial_eligibility.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "running": True,
                    "completed": False,
                    "keys": ["k1", "k2", "k3"],
                    "emails": ["a@x.com", "b@x.com", "c@x.com"],
                    "done_keys": ["k1"],
                    "snapshot": {
                        "total": 3,
                        "done": 1,
                        "eligible": 1,
                        "status": "running",
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        import common.trial_eligibility_jobs as tej

        tej._restore_trial_eligibility_checkpoint()
        job = tej.TRIAL_ELIGIBILITY_JOBS.active()
        assert job["status"] == "interrupted"
        assert job["interrupted"] is True
        assert [item["key"] for item in job["items"]] == ["k2", "k3"]
        assert job["pending_keys"] == ["k2", "k3"]
        assert [item["email"] for item in job["items"]] == ["b@x.com", "c@x.com"]

    def test_interrupted_job_does_not_block_start(
        self, ckpt_dir, reset_trial_state, monkeypatch
    ):
        ckpt_dir.mkdir(parents=True, exist_ok=True)
        (ckpt_dir / "trial_eligibility.json").write_text(
            json.dumps(
                {
                    "running": True,
                    "completed": False,
                    "keys": ["k1"],
                    "emails": ["a@x.com"],
                    "done_keys": [],
                    "snapshot": {"total": 1, "done": 0, "status": "running"},
                }
            ),
            encoding="utf-8",
        )
        import common.trial_eligibility_jobs as tej

        release = threading.Event()
        monkeypatch.setattr(
            tej,
            "check_trial_eligibility",
            lambda record, proxy="", direct=False: (
                release.wait(timeout=10) or (_ for _ in ()).throw(RuntimeError("stop"))
            ),
        )
        tej._restore_trial_eligibility_checkpoint()
        records = [self._record("k9", "z@x.com")]
        job = tej.TRIAL_ELIGIBILITY_JOBS.start(records, concurrency=1)  # 不应抛 RuntimeError
        try:
            assert job["total"] == 1
        finally:
            release.set()
            tej.TRIAL_ELIGIBILITY_JOBS.wait(job["job_id"], 10)


# 接码不再提供断点续跑：一个账号只能绑定一次手机号，中断后由操作者手动重新发起。
