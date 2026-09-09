"""A slug off the source's own HTML must not choose where we write.

BEA builds its PDF filename from the detail-page slug. os.path.join with a
value containing "/" or ".." writes outside pdf_dir — and these pipelines run
under a systemd timer on the ingest box.
"""
import os

import pytest

from bea_ingest import pipeline


@pytest.mark.parametrize("hostile", [
    "../../../../etc/cron.d/pwn",
    "/etc/cron.d/pwn",
    "a/b/c",
    "..",
    ".",
    "....//....//evil",
    "with spaces and ; rm -rf /",
])
def test_a_hostile_slug_stays_inside_the_pdf_directory(tmp_path, hostile):
    name = pipeline._safe_filename(hostile)
    assert os.sep not in name
    assert "/" not in name and "\\" not in name
    assert not name.startswith(".")

    dest = os.path.join(str(tmp_path), name + ".pdf")
    assert os.path.realpath(dest).startswith(os.path.realpath(str(tmp_path)) + os.sep)


def test_an_ordinary_slug_is_left_recognisable():
    # The guard must not mangle the normal case: these filenames are how an
    # operator finds a report on disk.
    name = pipeline._safe_filename("accident-to-the-glider-ask13-f-cecy-2018-08-18")
    assert name == "accident-to-the-glider-ask13-f-cecy-2018-08-18"


def test_an_empty_identifier_still_produces_a_filename():
    assert pipeline._safe_filename("") == "unnamed"
    assert pipeline._safe_filename(None) == "unnamed"


def test_a_very_long_identifier_is_bounded():
    # ext4 caps a filename at 255 bytes; an unbounded slug would raise
    # ENAMETOOLONG at download time and look like a fetch failure.
    assert len(pipeline._safe_filename("x" * 5000)) <= 120
