import os
import subprocess
import sys
import threading

import pytest

from pyedit.fdin import read_fd


def test_reads_a_pipe_to_eof():
    r, w = os.pipe()
    os.write(w, b"payload\n")
    os.close(w)
    assert read_fd(r) == "payload\n"
    os.close(r)


def test_reads_past_the_first_chunk():
    payload = "x" * (1 << 17) + "tail"
    r, w = os.pipe()

    # os.write blocks past the 64KiB pipe buffer, so the writer
    # runs while read_fd drains
    def feed():
        os.write(w, payload.encode())
        os.close(w)

    threading.Thread(target=feed).start()
    assert read_fd(r) == payload
    os.close(r)


def test_a_closed_fd_is_named_in_the_error():
    r, w = os.pipe()
    os.close(r)
    os.close(w)
    with pytest.raises(OSError) as raised:
        read_fd(r)
    assert f"read_fd({r})" in str(raised.value)
    assert "N<<'EOF'" in str(raised.value)


def test_heredocs_pair_with_redirections_in_order(project):
    old = "def greet():\n    '''Say hi.'''\n    print(\"Hi\")\n"
    new = "def greet(name=\"world\"):\n    '''Say hi.'''\n    print(f\"Hi, {name}\")\n"
    (project / "greet.py").write_text(old)
    command = (
        f"{sys.executable} -m pyedit -s - 3<<'OLD' 4<<'NEW' <<'PY'\n"
        f"{old}"
        "OLD\n"
        f"{new}"
        "NEW\n"
        'pyedit.edit("greet.py", pyedit.read_fd(3), pyedit.read_fd(4))\n'
        "PY\n"
    )
    tmp = project / "tmp"
    tmp.mkdir()
    result = subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        text=True,
        cwd=project,
        env={
            **os.environ,
            "TMPDIR": str(tmp),
            "XDG_STATE_HOME": str(project / "state"),
        },
    )
    assert result.returncode == 0, result.stderr
    assert '+def greet(name="world"):' in result.stdout
    assert (project / "greet.py").read_text() == old
