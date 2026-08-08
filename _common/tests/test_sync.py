# The vendoring contract.
#
# Sources stay self-contained — each package keeps a real pdf.py/text.py it can
# be copied away with, exactly as the README promises. What changes is that
# those files are now GENERATED from one canonical copy, and this test fails
# the build if any of them stops matching. That is the whole mechanism: no
# runtime dependency, no 40 venvs to update, and no silent drift either.
import pytest

from _common import sync


class TestRendering:
    def test_the_source_code_is_substituted(self):
        out = sync.render("slug = 'crash-{{SOURCE}}'\n", source="ovv", module="text")
        assert out == "slug = 'crash-ovv'\n"

    def test_the_module_name_is_substituted(self):
        out = sync.render("# {{SOURCE}}_ingest/{{MODULE}}.py\n", source="ahac", module="pdf")
        assert out == "# ahac_ingest/pdf.py\n"

    def test_a_canon_with_no_tokens_is_copied_verbatim(self):
        assert sync.render("x = 1\n", source="ovv", module="pdf") == "x = 1\n"

    def test_an_unknown_token_is_a_hard_error(self):
        # A typo like {{SORUCE}} would otherwise ship to every package.
        with pytest.raises(ValueError, match="SORUCE"):
            sync.render("# {{SORUCE}}\n", source="ovv", module="pdf")


class TestTheRealCanon:
    """The canon files must actually render for every package that takes them."""

    def test_every_vendored_pair_renders_and_is_valid_python(self):
        import ast
        for source, modules in sync.VENDORED.items():
            for canon_module, target_module in modules.items():
                text = sync.expected_text(source, canon_module, target_module)
                ast.parse(text)  # raises SyntaxError if the template broke it
                assert "{{" not in text, f"{source}/{target_module} kept a token"

    def test_a_package_can_land_the_canon_under_another_name(self):
        # nsib's text.py owns Nigerian registration parsing on top of the
        # shared three functions, so the canon lands as _textbase.py and is
        # re-exported. Without this the sync would delete real logic.
        assert sync.VENDORED["nsib"]["text"] == "_textbase"
        text = sync.expected_text("nsib", "text", "_textbase")
        assert text.startswith("# nsib_ingest/_textbase.py")
        assert "crash-nsib" in text

    def test_the_vendored_header_names_the_owning_package(self):
        text = sync.render(sync.read_canon("text"), source="sacaa", module="text")
        assert text.startswith("# sacaa_ingest/text.py")

    def test_it_tells_the_next_reader_not_to_edit_it(self):
        text = sync.render(sync.read_canon("pdf"), source="ovv", module="pdf")
        head = text[:600]
        assert "VENDORED" in head
        assert "_common/pdf.py" in head


class TestDriftGate:
    """The gate itself: what is on disk must equal what the canon renders."""

    def test_no_vendored_file_has_drifted(self):
        drifted = sync.find_drift()
        assert drifted == [], (
            "vendored copies no longer match the canon:\n  "
            + "\n  ".join(drifted)
            + "\nRun `python -m _common.sync` after changing a canonical file."
        )

    def test_the_gate_notices_a_changed_byte(self, tmp_path, monkeypatch):
        # A gate that cannot fail is not a gate. Prove it fails.
        target = sync.vendor_path("ovv", "text")
        original = target.read_text(encoding="utf-8")
        try:
            target.write_text(original + "# sneaky\n", encoding="utf-8")
            assert "ovv/text" in " ".join(sync.find_drift())
        finally:
            target.write_text(original, encoding="utf-8")
        assert sync.find_drift() == []
