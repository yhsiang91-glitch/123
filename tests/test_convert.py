import os
import subprocess
import sys
from pathlib import Path

import pytest

import pdf_to_txt as m
from pdf_to_txt import ConversionError, main


def read(path):
    return Path(path).read_text(encoding="utf-8")


# ---- basic conversion ------------------------------------------------------


def test_single_file_writes_txt_next_to_pdf(make_pdf, capsys):
    pdf = make_pdf("a.pdf", ["Hello", "World"])
    assert main([str(pdf)]) == 0
    assert read(pdf.with_suffix(".txt")) == "Hello\n\nWorld\n"
    assert "a.pdf -> " in capsys.readouterr().err


def test_chinese_text_and_filename(make_pdf):
    pdf = make_pdf("報告.pdf", ["你好，世界", "繁體中文測試"], cjk=True)
    assert main([str(pdf)]) == 0
    assert read(tmp := pdf.with_suffix(".txt")) == "你好，世界\n\n繁體中文測試\n", tmp


def test_library_api(make_pdf):
    pdf = make_pdf("a.pdf", ["one", "two", "three"])
    assert m.pdf_to_text(pdf) == "one\n\ntwo\n\nthree"
    assert m.pdf_to_text(pdf, pages="2-", page_sep="none") == "two\nthree"
    with pytest.raises(ConversionError):
        m.pdf_to_text(pdf, pages="9")


def test_quiet_hides_progress_but_not_errors(make_pdf, tmp_path, capsys):
    good = make_pdf("a.pdf")
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"nope")
    assert main(["-q", str(good), str(bad)]) == 1
    err = capsys.readouterr().err
    assert "->" not in err and "Done:" not in err
    assert "error: " in err and "bad.pdf" in err


# ---- page handling ---------------------------------------------------------


@pytest.mark.parametrize(
    "spec, expected",
    [("5", [(5, 5)]), ("1-3,5", [(1, 3), (5, 5)]), ("8-", [(8, None)]), ("-3", [(1, 3)]), (" 2 - 4 ", [(2, 4)])],
)
def test_parse_page_spec(spec, expected):
    assert m.parse_page_spec(spec) == expected


@pytest.mark.parametrize("spec", ["", "0", "3-1", "a", "1,,2", "-", "1-2-3"])
def test_parse_page_spec_rejects(spec):
    with pytest.raises(ValueError):
        m.parse_page_spec(spec)


def test_select_pages_clips_and_warns():
    assert m.select_pages([(2, 3), (9, None)], 4) == ([1, 2], ["the PDF has only 4 page(s); pages beyond that were ignored"])
    assert m.select_pages([(2, None)], 4) == ([1, 2, 3], [])
    assert m.select_pages(None, 3) == ([0, 1, 2], [])


def test_pages_option_and_bad_spec(make_pdf, capsys):
    pdf = make_pdf("a.pdf", ["one", "two", "three"])
    assert main(["-p", "1,3", str(pdf)]) == 0
    assert read(pdf.with_suffix(".txt")) == "one\n\nthree\n"
    with pytest.raises(SystemExit) as exc:
        main(["-p", "x", str(pdf)])
    assert exc.value.code == 2


def test_page_selection_matching_nothing_fails(make_pdf, capsys):
    pdf = make_pdf("a.pdf", ["one"])
    assert main(["-p", "5", str(pdf)]) == 1
    assert "matches no pages" in capsys.readouterr().err
    assert not pdf.with_suffix(".txt").exists()


def test_page_separators(make_pdf):
    pdf = make_pdf("a.pdf", ["one", "", "three"])  # middle page is blank

    def convert(sep):
        assert main(["-q", "--page-sep", sep, str(pdf)]) == 0
        return read(pdf.with_suffix(".txt"))

    assert convert("blank") == "one\n\nthree\n"
    assert convert("none") == "one\nthree\n"
    assert convert("ff") == "one\f\fthree\n"  # blank page keeps its slot
    assert convert("marker") == "===== Page 1 =====\none\n\n===== Page 2 =====\n\n\n===== Page 3 =====\nthree\n"


# ---- directories, outputs --------------------------------------------------


def test_directory_is_recursive_case_insensitive_and_skips_hidden(make_pdf, tmp_path):
    root = tmp_path / "docs"
    make_pdf("lower.pdf", parent=root)
    make_pdf("UPPER.PDF", parent=root)
    make_pdf("deep.pdf", parent=root / "a" / "b")
    make_pdf("hidden.pdf", parent=root / ".git")
    assert main(["-q", str(root)]) == 0
    found = sorted(p.relative_to(root).as_posix() for p in root.rglob("*.txt"))
    assert found == ["UPPER.txt", "a/b/deep.txt", "lower.txt"]


def test_no_recursive(make_pdf, tmp_path):
    root = tmp_path / "docs"
    make_pdf("top.pdf", parent=root)
    make_pdf("deep.pdf", parent=root / "sub")
    assert main(["-q", "--no-recursive", str(root)]) == 0
    assert [p.name for p in root.rglob("*.txt")] == ["top.txt"]


def test_directory_mirrored_into_output_dir(make_pdf, tmp_path):
    root, out = tmp_path / "docs", tmp_path / "txts"
    make_pdf("a.pdf", parent=root)
    make_pdf("b.pdf", parent=root / "sub")
    assert main(["-q", str(root), "-o", str(out)]) == 0
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*.txt")) == ["a.txt", "sub/b.txt"]
    assert not list(root.rglob("*.txt"))


def test_several_inputs_go_into_output_dir(make_pdf, tmp_path):
    f = make_pdf("f.pdf")
    root = tmp_path / "docs"
    make_pdf("d.pdf", parent=root)
    out = tmp_path / "out"
    assert main(["-q", str(f), str(root), "-o", str(out)]) == 0
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*.txt")) == ["docs/d.txt", "f.txt"]


def test_output_file_versus_directory(make_pdf, tmp_path):
    pdf = make_pdf("a.pdf")
    assert main(["-q", str(pdf), "-o", str(tmp_path / "x" / "renamed.txt")]) == 0
    assert (tmp_path / "x" / "renamed.txt").is_file()
    assert main(["-q", str(pdf), "-o", str(tmp_path / "newdir") + os.sep]) == 0  # trailing slash = directory
    assert (tmp_path / "newdir" / "a.txt").is_file()
    assert main(["-q", str(pdf), "-o", str(tmp_path / "x")]) == 0  # existing directory
    assert (tmp_path / "x" / "a.txt").is_file()


def test_options_may_follow_inputs(make_pdf, tmp_path):
    a, b = make_pdf("a.pdf"), make_pdf("b.pdf")
    assert main([str(a), "-o", str(tmp_path / "o"), str(b)]) == 0
    assert sorted(p.name for p in (tmp_path / "o").iterdir()) == ["a.txt", "b.txt"]


def test_wildcards_are_expanded_by_us(make_pdf, tmp_path):
    make_pdf("a1.pdf")
    make_pdf("a2.pdf")
    make_pdf("b.pdf")
    assert main(["-q", str(tmp_path / "a*.pdf")]) == 0
    assert sorted(p.name for p in tmp_path.glob("*.txt")) == ["a1.txt", "a2.txt"]


# ---- safety ---------------------------------------------------------------


def test_never_overwrites_a_pdf(make_pdf, capsys):
    pdf = make_pdf("a.pdf")
    original = pdf.read_bytes()
    assert main([str(pdf), "-o", str(pdf)]) == 1
    assert pdf.read_bytes() == original
    assert ".pdf" in capsys.readouterr().err


def test_second_pdf_is_not_mistaken_for_output(make_pdf):
    a, b = make_pdf("a.pdf", ["A"]), make_pdf("b.pdf", ["B"])
    original = b.read_bytes()
    assert main(["-q", str(a), str(b)]) == 0
    assert b.read_bytes() == original and read(b.with_suffix(".txt")) == "B\n"


def test_input_without_pdf_extension_keeps_its_name(make_pdf, tmp_path):
    src = make_pdf("scan.pdf", ["data"]).rename(tmp_path / "scan.dat")
    assert main(["-q", str(src)]) == 0
    assert read(tmp_path / "scan.dat.txt") == "data\n"
    assert src.read_bytes().startswith(b"%PDF")


def test_two_inputs_with_the_same_output_name(make_pdf, tmp_path, capsys):
    make_pdf("a.pdf", ["first"], parent=tmp_path / "x")
    make_pdf("a.pdf", ["second"], parent=tmp_path / "y")
    out = tmp_path / "out"
    assert main(["-q", str(tmp_path / "x" / "a.pdf"), str(tmp_path / "y" / "a.pdf"), "-o", str(out)]) == 1
    assert "same output" in capsys.readouterr().err
    assert read(out / "a.txt") == "first\n"


def test_same_file_twice_is_converted_once(make_pdf, capsys):
    pdf = make_pdf("a.pdf")
    assert main([str(pdf), str(pdf)]) == 0
    assert capsys.readouterr().err.count("->") == 1


def test_skip_existing_and_default_overwrite(make_pdf):
    pdf = make_pdf("a.pdf", ["new"])
    txt = pdf.with_suffix(".txt")
    txt.write_text("old", encoding="utf-8")
    assert main(["-q", "--skip-existing", str(pdf)]) == 0
    assert read(txt) == "old"
    assert main(["-q", str(pdf)]) == 0
    assert read(txt) == "new\n"


def test_write_is_atomic(make_pdf, monkeypatch):
    pdf = make_pdf("a.pdf", ["new"])
    txt = pdf.with_suffix(".txt")
    txt.write_text("previous good output", encoding="utf-8")

    def boom(*args):
        raise OSError("disk full")

    monkeypatch.setattr(m.os, "replace", boom)
    assert main(["-q", str(pdf)]) == 1
    assert read(txt) == "previous good output"
    assert [p.name for p in pdf.parent.iterdir() if p.name.endswith(".part")] == []


# ---- failures stay local ---------------------------------------------------


def test_bad_files_do_not_stop_the_batch(make_pdf, tmp_path, capsys):
    good = make_pdf("good.pdf", ["fine"])
    (tmp_path / "bad.pdf").write_bytes(b"this is not a pdf")
    (tmp_path / "empty.pdf").write_bytes(b"")
    assert main([str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "bad.pdf" in err and "empty.pdf" in err and "Done: 1 converted, 0 skipped, 2 failed" in err
    assert read(good.with_suffix(".txt")) == "fine\n"
    assert not (tmp_path / "bad.txt").exists()


def test_missing_input_is_reported_but_others_convert(make_pdf, tmp_path, capsys):
    good = make_pdf("good.pdf")
    assert main([str(tmp_path / "nope.pdf"), str(good)]) == 1
    assert "nope.pdf: no such file" in capsys.readouterr().err
    assert good.with_suffix(".txt").exists()


def test_no_pdfs_found(tmp_path, capsys):
    assert main([str(tmp_path)]) == 1
    assert "no PDF files found" in capsys.readouterr().err


def test_pdf_without_text_is_reported_and_keeps_old_output(make_pdf, capsys):
    pdf = make_pdf("scan.pdf", ["", ""])
    txt = pdf.with_suffix(".txt")
    txt.write_text("old", encoding="utf-8")
    assert main([str(pdf)]) == 1
    assert "no extractable text" in capsys.readouterr().err
    assert read(txt) == "old"


def test_failing_page_costs_only_that_page(make_pdf, monkeypatch, capsys):
    pdf = make_pdf("a.pdf", ["one", "two", "three"])
    real = m.extract_page_text
    calls = []

    def flaky(page, mode="auto"):
        calls.append(1)
        if len(calls) == 2:
            raise ValueError("broken content stream")
        return real(page, mode)

    monkeypatch.setattr(m, "extract_page_text", flaky)
    assert main(["-j", "1", str(pdf)]) == 0
    assert read(pdf.with_suffix(".txt")) == "one\n\nthree\n"
    assert "page 2: text extraction failed (ValueError: broken content stream)" in capsys.readouterr().err


# ---- encryption ------------------------------------------------------------


def test_empty_user_password_needs_no_password(make_pdf, encrypt):
    enc = encrypt(make_pdf("p.pdf", ["secret text"]), user_pw="")
    assert main(["-q", str(enc)]) == 0
    assert read(enc.with_suffix(".txt")) == "secret text\n"


@pytest.mark.parametrize("algorithm", ["AES-256", "RC4-128"])
def test_password_protected(make_pdf, encrypt, capsys, algorithm):
    enc = encrypt(make_pdf("p.pdf", ["secret text"]), algorithm=algorithm)
    assert main([str(enc)]) == 1
    assert "password is required" in capsys.readouterr().err
    assert main(["--password", "wrong", str(enc)]) == 1
    assert "wrong password" in capsys.readouterr().err
    assert main(["-q", "--password", "secret", str(enc)]) == 0
    assert read(enc.with_suffix(".txt")) == "secret text\n"


def test_password_from_environment(make_pdf, encrypt, monkeypatch):
    enc = encrypt(make_pdf("p.pdf", ["secret text"]))
    monkeypatch.setenv("PDF_PASSWORD", "secret")
    assert main(["-q", str(enc)]) == 0


# ---- stdout and encodings --------------------------------------------------


def test_stdout_output(make_pdf, capsysbinary):
    pdf = make_pdf("a.pdf", ["你好", "world"], cjk=True)
    assert main(["-o", "-", str(pdf)]) == 0
    captured = capsysbinary.readouterr()
    assert captured.out.decode("utf-8") == "你好\n\nworld\n"
    assert b"->" in captured.err  # status stays out of the data stream
    assert not pdf.with_suffix(".txt").exists()


def test_stdout_needs_exactly_one_pdf(make_pdf):
    a, b = make_pdf("a.pdf"), make_pdf("b.pdf")
    with pytest.raises(SystemExit) as exc:
        main(["-o", "-", str(a), str(b)])
    assert exc.value.code == 2


def test_encodings(make_pdf, capsys):
    pdf = make_pdf("a.pdf", ["你好"], cjk=True)
    txt = pdf.with_suffix(".txt")
    assert main(["-q", "-e", "utf-8-sig", str(pdf)]) == 0
    assert txt.read_bytes().startswith(b"\xef\xbb\xbf")
    assert main(["-q", "-e", "big5", str(pdf)]) == 0
    assert txt.read_bytes().decode("big5") == "你好\n"
    ascii_pdf = make_pdf("b.pdf", ["你好 ok"], cjk=True)
    assert main(["-e", "ascii", str(ascii_pdf)]) == 0
    assert read(ascii_pdf.with_suffix(".txt")) == "?? ok\n"
    assert "cannot be encoded as ascii" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["-e", "no-such-codec", str(pdf)])


# ---- extraction details ----------------------------------------------------


class _Page:
    def __init__(self, plain, layout):
        self.plain, self.layout = plain, layout

    def extract_text(self, extraction_mode="plain", **_):
        return self.layout if extraction_mode == "layout" else self.plain


def test_auto_mode_switches_to_layout_for_shattered_text():
    shattered = "\n".join("你好世界中文測試")  # one character per line
    page = _Page(shattered, "你 好 世 界 中 文 測 試\n\n\n\n\n下 一 行")
    assert m.extract_page_text(page, "plain") == shattered
    assert m.extract_page_text(page, "auto") == "你好世界中文測試\n\n下一行"
    sane = _Page("Hello\nWorld", "unused")
    assert m.extract_page_text(sane, "auto") == "Hello\nWorld"


def test_is_fragmented_thresholds():
    assert not m._is_fragmented("a\nb\nc")  # too few lines to judge
    assert not m._is_fragmented("\n".join(["a long line of text"] * 9 + ["x"]))
    assert m._is_fragmented("\n".join("abcdefgh"))


def test_clean_strips_control_characters_and_surrogates():
    assert m._clean("a\x00b\x0cc\r\n\td\x85") == ("abc\n\td", None)
    assert m._clean("x\ud800y") == ("x�y", None)
    garbage = "".join(chr(i) for i in range(1, 9)) + "\x0e\x0f\x10\x11" + "ok"
    text, warning = m._clean(garbage)
    assert text == "ok" and "OCR" in warning


# ---- parallelism -----------------------------------------------------------


def test_parallel_output_matches_serial(make_pdf, tmp_path):
    for i in range(4):
        make_pdf(f"f{i}.pdf", [f"file {i} page {p}" for p in range(1, 4)], parent=tmp_path / "in")
    assert main(["-q", "-j", "1", str(tmp_path / "in"), "-o", str(tmp_path / "serial")]) == 0
    assert main(["-q", "-j", "3", str(tmp_path / "in"), "-o", str(tmp_path / "pool")]) == 0
    names = sorted(p.name for p in (tmp_path / "serial").iterdir())
    assert names == ["f0.txt", "f1.txt", "f2.txt", "f3.txt"]
    for name in names:
        assert read(tmp_path / "pool" / name) == read(tmp_path / "serial" / name)


def test_one_big_pdf_is_split_across_workers_in_order(make_pdf, tmp_path):
    pdf = make_pdf("big.pdf", [f"page number {n}" for n in range(1, 131)])
    assert main(["-q", "-j", "1", str(pdf), "-o", str(tmp_path / "serial.txt")]) == 0
    assert main(["-q", "-j", "4", str(pdf), "-o", str(tmp_path / "pool.txt")]) == 0
    text = read(tmp_path / "pool.txt")
    assert text == read(tmp_path / "serial.txt")
    assert text.startswith("page number 1\n\npage number 2\n") and text.endswith("page number 130\n")


def test_split_makes_contiguous_chunks_of_at_least_the_minimum():
    assert m._split(list(range(10)), 8) == [list(range(10))]
    assert m._split(list(range(1000)), 4) == [list(range(i, i + 250)) for i in range(0, 1000, 250)]
    chunks = m._split(list(range(70)), 8)
    assert len(chunks) == 2 and sum(chunks, []) == list(range(70))


def test_bad_file_in_a_parallel_batch(make_pdf, tmp_path, capsys):
    for i in range(3):
        make_pdf(f"ok{i}.pdf", parent=tmp_path / "in")
    (tmp_path / "in" / "bad.pdf").write_bytes(b"junk")
    assert main(["-j", "2", str(tmp_path / "in")]) == 1
    assert "Done: 3 converted, 0 skipped, 1 failed" in capsys.readouterr().err


# ---- command line plumbing -------------------------------------------------


def test_keyboard_interrupt_exit_status(make_pdf, monkeypatch, capsys):
    pdf = make_pdf("a.pdf")

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(m, "run", interrupted)
    assert main([str(pdf)]) == 130
    assert "Interrupted" in capsys.readouterr().err


def test_script_runs_as_a_program(make_pdf, tmp_path):
    pdf = make_pdf("a.pdf", ["via subprocess"])
    script = Path(m.__file__)
    done = subprocess.run([sys.executable, str(script), "-q", "-o", "-", str(pdf)], capture_output=True)
    assert done.returncode == 0 and done.stdout == b"via subprocess\n"
    version = subprocess.run([sys.executable, str(script), "--version"], capture_output=True, text=True)
    assert version.stdout.startswith("pdf-to-txt " + m.__version__)
