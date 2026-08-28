"""Taking the listening socket from systemd. See docs/deployment.md."""

import os

from psych_ingestor.cli import _activated_fd


def test_no_socket_when_systemd_did_not_start_us(monkeypatch):
    monkeypatch.delenv("LISTEN_PID", raising=False)
    monkeypatch.delenv("LISTEN_FDS", raising=False)
    assert _activated_fd() is None


def test_takes_the_socket_systemd_passed(monkeypatch):
    monkeypatch.setenv("LISTEN_PID", str(os.getpid()))
    monkeypatch.setenv("LISTEN_FDS", "1")
    assert _activated_fd() == 3


def test_ignores_variables_meant_for_another_process(monkeypatch):
    """They're inherited, so a process systemd didn't name must not believe them."""
    monkeypatch.setenv("LISTEN_PID", str(os.getpid() + 1))
    monkeypatch.setenv("LISTEN_FDS", "1")
    assert _activated_fd() is None


def test_ignores_a_count_that_is_not_a_number(monkeypatch):
    monkeypatch.setenv("LISTEN_PID", str(os.getpid()))
    monkeypatch.setenv("LISTEN_FDS", "")
    assert _activated_fd() is None
