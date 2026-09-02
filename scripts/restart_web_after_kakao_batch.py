#!/usr/bin/env python3
"""Archive the active Kakao job and reload the web service after it finishes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
import yaml


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:8787"
SERVICE = "gpt-register-fp-8787.service"
ARCHIVE = ROOT / "data" / "kakao_link_jobs.json"
LOG = ROOT / "data" / "logs" / "restart_after_kakao_batch.log"
TERMINAL = {"success", "partial", "failed", "stopped"}


def write_log(message: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def credentials() -> tuple[str, str]:
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    web = config.get("web") if isinstance(config.get("web"), dict) else {}
    return str(web.get("username") or "admin"), str(web.get("password") or "")


def center_payload() -> dict:
    username, password = credentials()
    session = requests.Session()
    response = session.post(
        f"{BASE_URL}/api/auth/login",
        json={"username": username, "password": password},
        timeout=30,
    )
    response.raise_for_status()
    response = session.get(f"{BASE_URL}/api/link-center", timeout=60)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def archive_jobs(jobs: list[dict]) -> None:
    finished = [item for item in jobs if isinstance(item, dict) and item.get("status") in TERMINAL]
    ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{ARCHIVE.name}.", suffix=".tmp", dir=str(ARCHIVE.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "jobs": finished}, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, ARCHIVE)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def main() -> int:
    target_job_id = str(sys.argv[1] if len(sys.argv) > 1 else "").strip()
    if not target_job_id:
        write_log("缺少目标 job_id，监视器退出")
        return 2
    write_log(f"开始等待批次 {target_job_id}")
    while True:
        try:
            payload = center_payload()
            active = payload.get("active") if isinstance(payload.get("active"), dict) else None
            if active and str(active.get("job_id") or "") == target_job_id:
                write_log(f"批次运行中 {active.get('done', 0)}/{active.get('total', 0)}")
                time.sleep(30)
                continue
            jobs = payload.get("jobs") if isinstance(payload.get("jobs"), list) else []
            target = next((item for item in jobs if str(item.get("job_id") or "") == target_job_id), None)
            if not target or target.get("status") not in TERMINAL:
                write_log("目标批次尚未进入终态，继续等待")
                time.sleep(30)
                continue
            archive_jobs(jobs)
            write_log(f"批次已归档，状态={target.get('status')}；重载 {SERVICE}")
            subprocess.run(["systemctl", "restart", SERVICE], check=True, timeout=30)
            for _ in range(30):
                try:
                    if requests.get(f"{BASE_URL}/api/health", timeout=5).ok:
                        write_log("服务重载完成，新代码已生效")
                        return 0
                except requests.RequestException:
                    pass
                time.sleep(2)
            write_log("服务重载后健康检查超时")
            return 1
        except Exception as error:
            write_log(f"轮询异常：{type(error).__name__}: {error}")
            time.sleep(30)


if __name__ == "__main__":
    raise SystemExit(main())

