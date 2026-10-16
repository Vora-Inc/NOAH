"""Test isolation for the secret backstop (noah.secretscrub).

An entry point registers the endpoints it was given and wraps sys.stdout/sys.stderr. Left in place
between tests, a registered test endpoint would silently scrub a later test's output and hide a real
leak from it; and with the backstop active a missing site fix is masked, because the backstop hides
the same text. So every test starts and ends with an empty registry and the original streams, and
``activate`` is OFF by default: site tests exercise the site fixes alone. A test that is about the
backstop asks for it with ``@pytest.mark.usefixtures("backstop")``.
"""

import sys

import pytest


def _restore_streams():
    from noah import secretscrub

    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        while isinstance(stream, secretscrub.ScrubbingStream):
            stream = stream._stream
            setattr(sys, name, stream)
    sys.excepthook = sys.__excepthook__


def _forget_installs():
    """Whether the backstop was installed by an earlier test is process state; clear it so a test
    run in one process after another behaves as it does alone (the harness does not depend on
    ``secretscrub.reset()`` clearing it)."""
    from noah import secretscrub

    secretscrub._installed = False
    secretscrub._log_factory_installed = False


@pytest.fixture(autouse=True)
def _isolate_secretscrub():
    from noah import secretscrub

    secretscrub.reset()
    _forget_installs()
    secretscrub.set_enabled(False)
    yield
    secretscrub.reset()
    _forget_installs()
    secretscrub.set_enabled(True)
    _restore_streams()


@pytest.fixture
def backstop():
    """Switch the backstop on for one test."""
    from noah import secretscrub

    secretscrub.set_enabled(True)
    yield
    secretscrub.set_enabled(False)
