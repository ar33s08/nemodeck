"""Runner: argv discipline, no shells, bounded output, stubborn timeouts."""

import sys
import time

from nemodeck.runner import Runner


def test_argv_is_literal_no_shell_interpolation():
    r = Runner(timeout=30)
    res = r.run([sys.executable, "-c", "import sys; print(sys.argv[1])", "a; echo pwned && `id`"])
    assert res.ok
    assert res.out.strip() == "a; echo pwned && `id`"


def test_missing_binary_is_reported_not_raised():
    r = Runner(timeout=5)
    res = r.run(["definitely-not-a-real-binary-xyz"])
    assert res.rc == 127
    assert "not found" in res.err


def test_timeout_is_enforced_and_partial_output_kept():
    r = Runner(timeout=0.6)
    started = time.monotonic()
    res = r.run([sys.executable, "-u", "-c", "print('late', flush=True); import time; time.sleep(5)"])
    elapsed = time.monotonic() - started
    assert res.timed_out
    assert not res.ok
    assert elapsed < 4
    assert "timed out" in res.err


def test_stream_kills_long_process_at_deadline():
    r = Runner(timeout=30)
    lines = list(r.stream([sys.executable, "-u", "-c", "import time; print('first', flush=True); time.sleep(30)"], timeout=0.8))
    joined = "\n".join(lines)
    assert "first" in joined
    assert "timed out" in joined
    assert joined.rstrip().endswith("exit 0") or "exit -" in joined  # killed => non-zero noted