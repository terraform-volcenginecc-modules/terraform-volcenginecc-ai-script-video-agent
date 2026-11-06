from datetime import datetime, timedelta

SCRIPT_MAX_RETRIES = 2
SCRIPT_TIMEOUT_SECONDS = 600


def script_deadline(job):
    # Recovery gets a fresh budget without rewriting the task's creation history.
    started = job.snapshot.get("_script_started_at")
    start = datetime.fromisoformat(started) if started else job.created_at
    return start + timedelta(seconds=SCRIPT_TIMEOUT_SECONDS)


def timeout_script(job):
    job.status = "failed"
    job.phase = "timeout"
    job.error = "剧本任务超过 10 分钟总时限，已停止重试；请调整设置后重新生成"
    job.error_details = None
    job.owner = None
    job.lease_until = None
