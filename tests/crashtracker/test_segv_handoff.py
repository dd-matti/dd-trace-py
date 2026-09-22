import sys
from types import SimpleNamespace
from typing import Optional
from unittest.mock import MagicMock
from unittest.mock import call

import pytest

from ddtrace.internal import excepthook
from ddtrace.internal import forksafe
from ddtrace.internal.core import crashtracking
from ddtrace.internal.settings.crashtracker import config as crashtracker_config


def _patch_start_ready(
    monkeypatch: pytest.MonkeyPatch,
    stack_mod: SimpleNamespace,
    *,
    init: MagicMock,
    on_fork: Optional[MagicMock] = None,
    started: Optional[list[bool]] = None,
) -> None:
    started_flag: list[bool] = started if started is not None else [False]

    def _is_started() -> bool:
        return started_flag[0]

    def _mark_started(*_a: object, **_k: object) -> None:
        started_flag[0] = True

    init.side_effect = _mark_started
    monkeypatch.setitem(sys.modules, "ddtrace.internal.datadog.profiling.stack", stack_mod)
    monkeypatch.setattr(crashtracking, "is_available", True)
    monkeypatch.setattr(crashtracking, "is_started", _is_started)
    monkeypatch.setattr(crashtracker_config, "enabled", True)
    monkeypatch.setattr(
        crashtracking,
        "_get_args",
        lambda *_a, **_k: (MagicMock(), MagicMock(), MagicMock()),
    )
    monkeypatch.setattr(crashtracking, "crashtracker_init", init, raising=False)
    if on_fork is not None:
        monkeypatch.setattr(crashtracking, "crashtracker_on_fork", on_fork, raising=False)
    monkeypatch.setattr(excepthook, "register", MagicMock())
    monkeypatch.setattr(forksafe, "register", MagicMock())


@pytest.mark.skipif(sys.platform == "win32", reason="Signal handling not supported on Windows")
def test_crashtracker_start_pauses_then_uninstalls_when_we_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """crashtracking.start pauses, uninstalls, inits, then reinstalls when we own SIGSEGV."""
    order: list[str] = []

    def _record(name: str, result: object = None) -> object:
        order.append(name)
        return result

    stack_mod: SimpleNamespace = SimpleNamespace(
        pause_sampling=MagicMock(side_effect=lambda: _record("pause", True)),
        resume_sampling=MagicMock(side_effect=lambda: _record("resume")),
        segv_handler_installed=MagicMock(side_effect=lambda: _record("owned", True)),
        uninstall_segv_handler=MagicMock(side_effect=lambda: _record("uninstall")),
        reinstall_segv_handler=MagicMock(side_effect=lambda: _record("reinstall")),
    )
    init: MagicMock = MagicMock()
    _patch_start_ready(monkeypatch, stack_mod, init=init)

    started: bool = crashtracking.start()
    assert started
    assert order == ["pause", "owned", "uninstall", "reinstall", "resume"]
    init.assert_called_once()
    assert stack_mod.pause_sampling.mock_calls == [call()]
    assert stack_mod.resume_sampling.mock_calls == [call()]


@pytest.mark.skipif(sys.platform == "win32", reason="Signal handling not supported on Windows")
def test_crashtracker_start_skips_swap_on_pause_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pause timeout: do not uninstall or reinstall (cycle), but still init."""
    stack_mod: SimpleNamespace = SimpleNamespace(
        pause_sampling=MagicMock(return_value=None),
        resume_sampling=MagicMock(),
        segv_handler_installed=MagicMock(return_value=True),
        uninstall_segv_handler=MagicMock(),
        reinstall_segv_handler=MagicMock(),
    )
    init: MagicMock = MagicMock()
    _patch_start_ready(monkeypatch, stack_mod, init=init)

    started: bool = crashtracking.start()
    assert started

    stack_mod.pause_sampling.assert_called_once()
    stack_mod.segv_handler_installed.assert_not_called()
    stack_mod.uninstall_segv_handler.assert_not_called()
    stack_mod.reinstall_segv_handler.assert_not_called()
    stack_mod.resume_sampling.assert_not_called()
    init.assert_called_once()


@pytest.mark.skipif(sys.platform == "win32", reason="Signal handling not supported on Windows")
def test_crashtracker_start_skips_swap_when_foreign_owns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not reclaim after a real foreign (non-ddtrace) takeover (PROF-14568)."""
    stack_mod: SimpleNamespace = SimpleNamespace(
        pause_sampling=MagicMock(return_value=True),
        resume_sampling=MagicMock(),
        segv_handler_installed=MagicMock(return_value=False),
        uninstall_segv_handler=MagicMock(),
        reinstall_segv_handler=MagicMock(),
    )
    init: MagicMock = MagicMock()
    _patch_start_ready(monkeypatch, stack_mod, init=init)

    started: bool = crashtracking.start()
    assert started

    stack_mod.uninstall_segv_handler.assert_not_called()
    stack_mod.reinstall_segv_handler.assert_not_called()
    stack_mod.resume_sampling.assert_called_once()
    init.assert_called_once()


@pytest.mark.skipif(sys.platform == "win32", reason="Signal handling not supported on Windows")
def test_crashtracker_double_start_skips_second_init(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second start() is a true no-op: native init is Once-guarded; Python must not swap again."""
    stack_mod: SimpleNamespace = SimpleNamespace(
        pause_sampling=MagicMock(return_value=False),
        resume_sampling=MagicMock(),
        segv_handler_installed=MagicMock(return_value=True),
        uninstall_segv_handler=MagicMock(),
        reinstall_segv_handler=MagicMock(),
    )
    init: MagicMock = MagicMock()
    started_flag: list[bool] = [False]
    _patch_start_ready(monkeypatch, stack_mod, init=init, started=started_flag)

    first: bool = crashtracking.start()
    assert first
    init.assert_called_once()
    stack_mod.uninstall_segv_handler.assert_called_once()

    second: bool = crashtracking.start()
    assert second
    init.assert_called_once()
    stack_mod.uninstall_segv_handler.assert_called_once()
    stack_mod.reinstall_segv_handler.assert_called_once()
    stack_mod.pause_sampling.assert_called_once()


@pytest.mark.skipif(sys.platform == "win32", reason="Signal handling not supported on Windows")
def test_crashtracker_fork_handoff_reinstalls(monkeypatch: pytest.MonkeyPatch) -> None:
    """crashtracker_on_fork uses the same pause/uninstall/reinstall sequence."""
    order: list[str] = []

    def _record(name: str, result: object = None) -> object:
        order.append(name)
        return result

    stack_mod: SimpleNamespace = SimpleNamespace(
        pause_sampling=MagicMock(side_effect=lambda: _record("pause", False)),
        resume_sampling=MagicMock(side_effect=lambda: _record("resume")),
        segv_handler_installed=MagicMock(side_effect=lambda: _record("owned", True)),
        uninstall_segv_handler=MagicMock(side_effect=lambda: _record("uninstall")),
        reinstall_segv_handler=MagicMock(side_effect=lambda: _record("reinstall")),
    )
    fork_handler: list[object] = []
    on_fork: MagicMock = MagicMock()
    init: MagicMock = MagicMock()
    _patch_start_ready(monkeypatch, stack_mod, init=init, on_fork=on_fork)
    monkeypatch.setattr(forksafe, "register", lambda fn: fork_handler.append(fn))

    started: bool = crashtracking.start()
    assert started
    assert fork_handler

    order.clear()
    handler: object = fork_handler[0]
    assert callable(handler)
    handler()
    assert order == ["pause", "owned", "uninstall", "reinstall"]
    on_fork.assert_called_once()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
@pytest.mark.subprocess(
    env={
        "_DD_PROFILING_STACK_FAST_COPY": "1",
        "_DD_PROFILING_STACK_ADAPTIVE_SAMPLING_ENABLED": "0",
    },
    err=lambda s: "falling back to syscall" not in s,
)
def test_warmup_crashtracker_start_keeps_us_on_top() -> None:
    """Warmup + start() (including a second start) keeps us on both signals, crashtracker previous."""
    import os
    import signal
    import time
    from typing import Optional

    from ddtrace.internal.core import crashtracking
    from ddtrace.internal.datadog.profiling import ddup
    from ddtrace.internal.datadog.profiling import stack
    from ddtrace.internal.datadog.profiling.stack import _stack
    from ddtrace.internal.settings.crashtracker import config as crashtracker_config

    assert crashtracking.is_available
    assert stack.is_available

    ddup.config(env="test", service="test", version="0.0.0")
    ddup.start()
    _stack._set_fast_copy_warmup_seconds(30.0)
    stack.set_adaptive_sampling(False)
    started: bool = stack.start()
    assert started

    try:
        saw_warmup: bool = False
        warmup_deadline: float = time.monotonic() + 10
        while time.monotonic() < warmup_deadline:
            if _stack.fast_copy_memory_active() is False and stack.segv_handler_installed():
                saw_warmup = True
                break
            time.sleep(0.05)
        assert saw_warmup, "sampler never entered warmup with handlers still installed"

        crashtracker_config.debug_url = "http://127.0.0.1:9"
        crashtracker_config._stacktrace_resolver = "safe"
        first: bool = crashtracking.start()
        second: bool = crashtracking.start()
        assert first
        assert second
        assert crashtracking.is_started()
        assert stack.segv_handler_installed() is True

        def _is_native(path: Optional[str]) -> bool:
            if path is None:
                return False
            base: str = os.path.basename(path)
            return base == "_native" or base.startswith("_native.") or base.startswith("_native-")

        pause_result: Optional[bool] = stack.pause_sampling()
        assert pause_result is not None, "sampler pause timed out before inspecting previous"
        try:
            stack.uninstall_segv_handler()
            assert stack.segv_handler_installed() is False
            segv_fname: Optional[str] = _stack._signal_handler_dli_fname(int(signal.SIGSEGV))
            bus_fname: Optional[str] = _stack._signal_handler_dli_fname(int(signal.SIGBUS))
            assert _is_native(segv_fname), segv_fname
            assert _is_native(bus_fname), bus_fname
            stack.reinstall_segv_handler()
            assert stack.segv_handler_installed() is True
        finally:
            if pause_result is True:
                stack.resume_sampling()
    finally:
        stack.stop()
