"""The PowerShell scripts must stay plain ASCII.

Windows PowerShell 5.1 — which is what Task Scheduler and a plain `powershell`
prompt still use — reads a .ps1 file with no byte-order mark as Windows-1252,
not UTF-8. An em-dash written as UTF-8 is the three bytes E2 80 94, and 0x94 in
Windows-1252 is a RIGHT DOUBLE QUOTATION MARK. PowerShell accepts smart quotes
as string delimiters, so every em-dash in a comment silently opened a string and
the rest of the file parsed as nonsense:

    Missing closing '}' in statement block or type definition.
    The string is missing the terminator: ".

Nothing about that error points at the real cause, and it costs an evening. It
had also been sitting in run.ps1 — the scheduled-task wrapper — for long enough
to stop the nightly scrape without anyone connecting the two.

A byte-order mark would also fix it, but ASCII cannot be got wrong by an editor
that helpfully converts a hyphen into a dash, so that is the rule: no character
above 0x7F in a .ps1 file. Use "-", not "—".
"""
import unittest
from pathlib import Path

SCHEDULE = Path(__file__).resolve().parent.parent.parent / "schedule"


def scripts():
    return sorted(SCHEDULE.glob("*.ps1")) if SCHEDULE.is_dir() else []


class TestPowerShellScriptsAreAscii(unittest.TestCase):
    def setUp(self):
        if not scripts():
            # schedule/ is not copied into the Pages repo, so this is expected
            # to find nothing when the suite runs on GitHub.
            self.skipTest(f"no PowerShell scripts under {SCHEDULE}")

    def test_no_byte_above_7f(self):
        for path in scripts():
            with self.subTest(script=path.name):
                data = path.read_bytes()
                offenders = {}
                for i, byte in enumerate(data):
                    if byte > 0x7F:
                        line = data[:i].count(b"\n") + 1
                        offenders.setdefault(line, set()).add(hex(byte))
                self.assertEqual(
                    offenders, {},
                    f"{path.name} has non-ASCII bytes at lines "
                    f"{sorted(offenders)} — PowerShell 5.1 will read these as "
                    f"Windows-1252 and 0x94 becomes a quote character. "
                    f"Replace the dashes and curly quotes with ASCII.")

    def test_they_still_decode_as_windows_1252_identically(self):
        """The actual property that matters: a 5.1 read and a 7 read agree."""
        for path in scripts():
            with self.subTest(script=path.name):
                data = path.read_bytes()
                self.assertEqual(data.decode("cp1252"), data.decode("utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
