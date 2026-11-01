from cheatmaker.ui.tasks import TaskRunner


def test_async_runner_done_and_failed(qtbot):
    r = TaskRunner()
    got = []
    r.run(lambda: 41 + 1, got.append, got.append)
    qtbot.waitUntil(lambda: got == [42], timeout=3000)

    def boom():
        raise RuntimeError("x")

    r.run(boom, got.append, lambda exc: got.append(type(exc).__name__))
    qtbot.waitUntil(lambda: got == [42, "RuntimeError"], timeout=3000)
    assert not r.busy


def test_sync_runner_busy_signal(qtbot):
    r = TaskRunner(sync=True)
    states = []
    r.busy_changed.connect(states.append)
    r.run(lambda: 1, lambda _v: None, lambda _e: None)
    assert states == [True, False]
