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


def test_eta_text():
    from cheatmaker.ui.find_wizard import eta_text

    assert eta_text(0.01, 10) == "%p%"
    assert eta_text(0.5, 30) == "%p% · about 30 s left"
    assert eta_text(0.25, 40) == "%p% · about 2 min left"
    assert eta_text(1.0, 100) == "%p%"
