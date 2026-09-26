from datetime import timedelta

from scripts import run_data_readiness_scheduler as supervisor


def test_supervisor_recovers_stale_runs_before_claiming(monkeypatch):
    events = []

    class Session:
        def __enter__(self):
            events.append("session_enter")
            return self

        def __exit__(self, *_args):
            events.append("session_exit")

        def commit(self):
            events.append("commit")

    class Executor:
        def __init__(self, **_kwargs):
            events.append("executor")

        def run_once(self):
            events.append("claim")
            return None

    def recover(session, *, stale_after, retry_policy):
        assert isinstance(session, Session)
        assert stale_after == timedelta(minutes=2)
        assert isinstance(retry_policy, supervisor.RetryPolicy)
        events.append("recover")
        return ()

    monkeypatch.setattr(supervisor, "SessionLocal", Session)
    monkeypatch.setattr(supervisor, "recover_stale_runs", recover)
    monkeypatch.setattr(supervisor, "WorkerExecutor", Executor)

    assert supervisor.run_workers(object(), max_jobs=1) == []
    assert events.index("recover") < events.index("commit") < events.index("claim")
