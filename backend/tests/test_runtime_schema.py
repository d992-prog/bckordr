from app.schemas.runtime import WorkerTaskStatusResponse


def test_worker_task_status_response_carries_live_planned_rps():
    payload = WorkerTaskStatusResponse(task_id=7, status="running", stop_reason=None, planned_rps=12.5)

    assert payload.planned_rps == 12.5
