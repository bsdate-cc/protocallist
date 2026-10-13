"""The port of the panel: which one a user gets, and who holds one that is taken. The account and the table
of listeners are the real ones of this computer - they are only read."""
import socket
import subprocess
import sys
from pathlib import Path

from protocallist.panel import port


def test_every_account_has_ports_of_its_own_and_they_never_meet_those_of_another():
    assert port.candidates(1001) == [40701, 41701, 42701, 43701, 44701]
    assert port.candidates(1002)[0] == 40702 and port.candidates(51234)[0] == 40934      # the last three digits
    mine, theirs = set(port.candidates(1001)), set(port.candidates(1002))
    assert not mine & theirs                                             # whatever shift each of them had to take
    assert all(1024 < number < 49152 for number in port.candidates(1999))               # below the ports Windows hands out itself


def test_the_account_is_the_one_windows_names():
    said = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True).stdout
    sid = said.strip().rsplit(",", 1)[-1].strip('"')
    assert sid.startswith("S-1-") and port.account() == int(sid.rsplit("-", 1)[1])
    assert port.candidates() == port.candidates(port.account())


def test_the_program_that_holds_a_port_is_named():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    try:
        taken = listener.getsockname()[1]
        assert port.holder(taken).lower() == Path(sys.executable).name.lower()     # this very test
    finally:
        listener.close()
    assert port.holder(taken) == ""                                      # nobody listens there any more
