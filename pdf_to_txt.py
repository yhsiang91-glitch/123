#!/usr/bin/env python3
"""Convert PDF files to plain text (UTF-8 by default).

    pdf-to-txt report.pdf                  # -> report.txt next to the PDF
    pdf-to-txt a.pdf b.pdf -o out/         # -> out/a.txt, out/b.txt
    pdf-to-txt ./pdfs -o ./txts            # whole directory tree, mirrored
    pdf-to-txt report.pdf -o - | less      # text to stdout

Importable too: ``pdf_to_text(path)`` returns the text of one PDF and
``main(argv)`` runs the command line and returns the exit status.
"""

from __future__ import annotations

import argparse
import codecs
import glob
import logging
import os
import re
import signal
import sys
import uuid
import warnings
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

__version__ = "0.2.0"

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_NO_PYPDF, EXIT_INTERRUPTED = 0, 1, 2, 3, 130

MODES = ("auto", "plain", "layout")
PAGE_SEPARATORS = ("blank", "ff", "marker", "none")

MIN_CHUNK_PAGES = 32  # smaller chunks cost more (re-opening the PDF) than they save
MAX_AUTO_JOBS = 8
MAX_JOBS = 61  # Windows cannot wait on more worker handles than this

PageRange = Tuple[int, Optional[int]]  # 1-based, inclusive; None = through the last page
PageResult = Tuple[int, str, Optional[str]]  # (page number, text, warning)


class ConversionError(Exception):
    """A PDF could not be converted; the message is meant for the user."""


class PlanError(Exception):
    """The command-line inputs/outputs do not make sense together."""


# --------------------------------------------------------------------------
# Text extraction
# --------------------------------------------------------------------------

_CJK = "⺀-〿぀-ヿ㄀-ㄯ㐀-䶿一-鿿豈-﫿＀-￯"
_CJK_GAP = re.compile(f"(?<=[{_CJK}]) +(?=[{_CJK}])")
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # keeps \t and \n
_SURROGATE = re.compile("[\ud800-\udfff]")


def _load_pypdf():
    import pypdf

    logging.getLogger("pypdf").setLevel(logging.ERROR)  # it warns about every quirk
    return pypdf


def _layout_text(page) -> str:
    text = page.extract_text(extraction_mode="layout", layout_mode_strip_rotated=False)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)  # layout mode pads with blank lines
    return _CJK_GAP.sub("", text)  # ...and spaces between letter-spaced CJK characters


def _is_fragmented(text: str) -> bool:
    """True when a page came out as (mostly) one character per line."""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    return len(lines) >= 8 and sum(len(line) == 1 for line in lines) / len(lines) > 0.6


def extract_page_text(page, mode: str = "auto") -> str:
    """Extract one pypdf page. ``auto`` = plain order, but switch to layout
    mode for pages whose text is shattered into single characters (common
    with CJK PDFs that draw every glyph in its own text block)."""
    if mode == "layout":
        return _layout_text(page)
    text = page.extract_text()
    if mode == "auto" and _is_fragmented(text):
        return _layout_text(page)
    return text


def _clean(raw: str) -> Tuple[str, Optional[str]]:
    text = _SURROGATE.sub("�", raw)  # lone surrogates cannot be encoded
    stripped = _CONTROL.sub("", text)
    lost = len(text) - len(stripped)
    warning = None
    if lost >= 8 and lost > 0.2 * len(text):
        warning = "text layer looks unmapped (mostly control characters); the PDF may need OCR"
    return stripped.strip("\n"), warning


def _describe(exc: BaseException) -> str:
    if isinstance(exc, BrokenProcessPool):
        return "a worker process died (out of memory?)"
    if type(exc).__name__ == "DependencyError":
        return f"{exc} (try: pip install 'pypdf[crypto]')"
    return f"{type(exc).__name__}: {exc}"


@contextmanager
def _conversion_errors() -> Iterator[None]:
    """Turn any failure into a ConversionError with a short message.
    (A plain-string exception also pickles cleanly out of worker processes.)"""
    try:
        yield
    except (ConversionError, KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except BaseException as exc:  # includes pyo3 PanicException
        raise ConversionError(_describe(exc)) from None


def _open_reader(fh, password: Optional[str]):
    pypdf = _load_pypdf()
    reader = pypdf.PdfReader(fh)
    if reader.is_encrypted:
        try:
            len(reader.pages)  # PDFs with an empty user password open by themselves
        except pypdf.errors.FileNotDecryptedError:
            if not password:
                raise ConversionError("encrypted PDF: a password is required (--password)") from None
            if not reader.decrypt(password):
                raise ConversionError("wrong password") from None
    return reader


def _count_pages(path: Path, password: Optional[str]) -> int:
    with _conversion_errors(), open(path, "rb") as fh:
        return len(_open_reader(fh, password).pages)


def _extract_chunk(path: Path, password: Optional[str], indices: List[int], mode: str) -> List[PageResult]:
    """Extract the given 0-based pages. Runs in worker processes, so it only
    takes and returns plain data. A page that fails costs only that page."""
    results: List[PageResult] = []
    with _conversion_errors(), open(path, "rb") as fh:  # a handle, not a path: pypdf would copy the whole file
        reader = _open_reader(fh, password)
        for i in indices:
            try:
                text, warning = _clean(extract_page_text(reader.pages[i], mode))
            except (KeyboardInterrupt, SystemExit, GeneratorExit):
                raise
            except BaseException as exc:
                if type(exc).__name__ == "DependencyError":
                    raise  # a missing package affects the whole file, not just this page
                text, warning = "", f"text extraction failed ({_describe(exc)})"
            results.append((i + 1, text, warning))
    return results


def _init_worker() -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl-C is handled (once) by the parent


# --------------------------------------------------------------------------
# Page selection and output assembly
# --------------------------------------------------------------------------


def parse_page_spec(spec: str) -> List[PageRange]:
    """Parse '1-3,5,8-' (1-based; '-3' = first three; '8-' = page 8 onward)."""
    ranges: List[PageRange] = []
    for part in spec.split(","):
        m = re.fullmatch(r"\s*(\d+)?\s*(?:(-)\s*(\d+)?)?\s*", part)
        if not m or (m.group(1) is None and m.group(3) is None):
            raise ValueError(f"invalid page range {part.strip()!r} (expected e.g. 1-3,5,8-)")
        start = int(m.group(1)) if m.group(1) else 1
        if m.group(2) is None:
            end: Optional[int] = start
        else:
            end = int(m.group(3)) if m.group(3) else None
        if start < 1 or (end is not None and end < start):
            raise ValueError(f"invalid page range {part.strip()!r}")
        ranges.append((start, end))
    return ranges


def select_pages(ranges: Optional[Sequence[PageRange]], total: int) -> Tuple[List[int], List[str]]:
    """0-based page indices for ``ranges`` in a PDF of ``total`` pages, plus warnings."""
    if ranges is None:
        return list(range(total)), []
    chosen = set()
    clipped = False
    for start, end in ranges:
        last = total if end is None else end
        clipped = clipped or start > total or last > total
        chosen.update(range(start - 1, min(last, total)))
    warnings = [f"the PDF has only {total} page(s); pages beyond that were ignored"] if clipped else []
    return sorted(chosen), warnings


def join_pages(pages: Sequence[Tuple[int, str]], sep: str = "blank") -> str:
    """Join (page number, text) pairs. ``ff`` keeps empty pages as empty slots
    so that page N is always the N-th form-feed-separated chunk."""
    if sep == "ff":
        return "\f".join(text for _, text in pages)
    if sep == "marker":
        return "\n\n".join(f"===== Page {n} =====\n{text}" for n, text in pages)
    glue = "\n" if sep == "none" else "\n\n"
    return glue.join(text for _, text in pages if text)


def pdf_to_text(
    path,
    *,
    mode: str = "auto",
    pages: Optional[str] = None,
    password: Optional[str] = None,
    page_sep: str = "blank",
) -> str:
    """Return the text of one PDF (serial, in-process). Raises ConversionError."""
    path = Path(path)
    indices, notes = _prepare(path, password, parse_page_spec(pages) if pages else None)
    results = _extract_chunk(path, password, indices, mode)
    for note in notes:
        warnings.warn(f"{path}: {note}", stacklevel=2)
    for n, _, warning in results:
        if warning:
            warnings.warn(f"{path}: page {n}: {warning}", stacklevel=2)
    if not any(text for _, text, _ in results):
        raise ConversionError("no extractable text (scanned images? try OCR)")
    return join_pages([(n, text) for n, text, _ in results], page_sep)


def _prepare(path: Path, password: Optional[str], ranges: Optional[Sequence[PageRange]]):
    total = _count_pages(path, password)
    if total == 0:
        raise ConversionError("the PDF has no pages")
    indices, warnings = select_pages(ranges, total)
    if not indices:
        raise ConversionError(f"the page selection matches no pages (the PDF has {total})")
    return indices, warnings


def _split(indices: List[int], parts: int) -> List[List[int]]:
    parts = max(1, min(parts, len(indices) // MIN_CHUNK_PAGES))
    step = -(-len(indices) // parts)
    return [indices[i : i + step] for i in range(0, len(indices), step)]


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def _encode(text: str, encoding: str) -> Tuple[bytes, bool]:
    """Encode text; characters the encoding lacks become '?' (second item True)."""
    try:
        return text.encode(encoding), False
    except UnicodeEncodeError:
        return text.encode(encoding, errors="replace"), True


def write_atomic(dst: Path, data: bytes) -> None:
    """Write via a temp file + rename so that a failure never leaves a
    truncated file behind or destroys the previous output."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{os.getpid()}-{uuid.uuid4().hex[:8]}.part")  # short: dst.name may be near the limit
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dst)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# Planning: which PDF goes where
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Task:
    src: Path
    dst: Optional[Path]  # None = stdout


def _txt_name(pdf: Path) -> str:
    return pdf.stem + ".txt" if pdf.suffix.lower() == ".pdf" else pdf.name + ".txt"


def _iter_pdfs(root: Path, recursive: bool) -> Iterator[Path]:
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))  # .git, .venv, ...
            for name in sorted(filenames):
                path = Path(dirpath, name)
                if name.lower().endswith(".pdf") and path.is_file():
                    yield path
    else:
        for path in sorted(root.iterdir()):
            if path.suffix.lower() == ".pdf" and path.is_file():
                yield path


def _expand_inputs(inputs: Sequence[str]) -> Tuple[List[Path], List[str]]:
    """Expand wildcards ourselves (Windows shells do not) and report missing paths."""
    paths: List[Path] = []
    problems: List[str] = []
    for item in inputs:
        if os.path.exists(item):
            paths.append(Path(item))
            continue
        matches = sorted(glob.glob(item)) if any(c in item for c in "*?[") else []
        if matches:
            paths.extend(Path(m) for m in matches)
        else:
            problems.append(f"{item}: no such file or directory")
    return paths, problems


def _real(path: Path) -> str:
    """Identity of a path for collision checks: symlinks resolved (realpath never
    raises on loops), case folded where the filesystem usually is insensitive."""
    real = os.path.realpath(path)
    return real.casefold() if sys.platform in ("win32", "darwin") else real


def plan_tasks(inputs: Sequence[str], output: Optional[str], recursive: bool = True) -> Tuple[List[Task], List[str]]:
    """Map inputs to (source, destination) pairs. Returns the tasks and a list
    of problems (missing inputs, refused destinations); raises PlanError for
    unusable argument combinations."""
    paths, problems = _expand_inputs(inputs)
    to_stdout = output == "-"
    out = None if output is None or to_stdout else Path(output)

    files, dirs = [], []
    for p in paths:  # the same path given twice (or matched by a glob too) counts once
        group = files if p.is_file() else dirs if p.is_dir() else None
        if group is not None and all(os.path.abspath(p) != os.path.abspath(q) for q in group):
            group.append(p)
    for p in paths:
        if not p.is_file() and not p.is_dir():
            problems.append(f"{p}: not a regular file or directory")

    # With exactly one file input, OUT names the output file unless it is (or
    # looks like) a directory; in every other case OUT is a directory.
    out_is_file = (
        out is not None
        and len(files) == 1
        and not dirs
        and not out.is_dir()
        and not str(output).endswith(("/", os.sep))
    )

    pairs: List[Tuple[Path, Path]] = []
    for f in files:
        if out is None:
            dst = f.with_name(_txt_name(f))
        elif out_is_file:
            dst = out
        else:
            dst = out / _txt_name(f)
        pairs.append((f, dst))
    for d in dirs:
        base = d if out is None else (out / Path(os.path.realpath(d)).name if len(paths) > 1 else out)
        for pdf in _iter_pdfs(d, recursive):
            pairs.append((pdf, base / pdf.relative_to(d).parent / _txt_name(pdf)))

    if to_stdout:
        if len(pairs) != 1:
            raise PlanError(f"'-o -' (stdout) needs exactly one PDF, but {len(pairs)} were found")
        return [Task(pairs[0][0], None)], problems

    source_ids = {_real(s) for s, _ in pairs}
    tasks: List[Task] = []
    seen_src, seen_dst = set(), {}
    for src, dst in pairs:
        key = os.path.abspath(src)  # not realpath: a symlink to a PDF is its own input
        if key in seen_src:
            continue  # the same PDF named twice (e.g. a file and its directory)
        seen_src.add(key)
        if dst.suffix.lower() == ".pdf":
            problems.append(f"{src}: refusing to write {dst} (output must not be a .pdf file)")
        elif dst.is_dir():
            problems.append(f"{src}: output {dst} is a directory")
        elif _real(dst) in source_ids:
            problems.append(f"{src}: refusing to overwrite input file {dst}")
        elif _real(dst) in seen_dst:
            problems.append(f"{src}: same output {dst} as {seen_dst[_real(dst)]}")
        else:
            seen_dst[_real(dst)] = src
            tasks.append(Task(src, dst))
    return tasks, problems


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------


@dataclass
class Options:
    mode: str = "auto"
    pages: Optional[List[PageRange]] = None
    password: Optional[str] = None
    page_sep: str = "blank"
    encoding: str = "utf-8"
    jobs: int = 1
    skip_existing: bool = False


@dataclass
class Summary:
    converted: int = 0
    skipped: int = 0
    failed: int = 0


class _Log:
    """Diagnostics go to stderr so stdout stays clean for '-o -'."""

    def __init__(self, quiet: bool = False) -> None:
        self.quiet = quiet

    def info(self, msg: str) -> None:
        if not self.quiet:
            print(msg, file=sys.stderr)

    def warn(self, msg: str) -> None:
        print(f"warning: {msg}", file=sys.stderr)

    def error(self, msg: str) -> None:
        print(f"error: {msg}", file=sys.stderr)


class _SerialExecutor:
    """The slice of ProcessPoolExecutor we use, running in-process."""

    def submit(self, fn, *args) -> Future:
        fut: Future = Future()
        try:
            fut.set_result(fn(*args))
        except Exception as exc:
            fut.set_exception(exc)
        return fut

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        pass


@dataclass
class _InFlight:
    task: Task
    warnings: List[str]
    remaining: int
    results: Dict[int, List[PageResult]] = field(default_factory=dict)
    error: Optional[str] = None


def _abort(executor) -> None:
    """Drop queued work and stop running workers (never raises: it runs while
    another exception, e.g. Ctrl-C, is already being handled)."""
    try:
        procs = list((getattr(executor, "_processes", None) or {}).values())  # shutdown() clears this
        executor.shutdown(wait=False, cancel_futures=True)
        for proc in procs:
            proc.terminate()
    except Exception:
        pass


def run(tasks: Sequence[Task], opts: Options, log: Optional[_Log] = None) -> Summary:
    """Convert every task. One bad file never stops the others.

    Files go to a process pool, largest first; a single big PDF is split into
    contiguous page chunks so that several workers share it. Only a few files
    are in flight at once, which bounds memory on large batches."""
    log = log or _Log()
    summary = Summary()
    total = len(tasks)
    if not total:
        return summary
    workers = min(opts.jobs or min(os.cpu_count() or 1, MAX_AUTO_JOBS), MAX_JOBS)
    queue = list(tasks)
    if workers > 1:
        queue.sort(key=lambda t: _size(t.src), reverse=True)  # keep the pool busy to the end
    todo = iter(queue)

    executor = None
    futures: Dict[Future, Tuple[_InFlight, int]] = {}
    active: List[_InFlight] = []  # files with chunks in flight
    broken = False  # a worker died; the pool must be replaced
    finished = 0

    def fail(task: Task, message: str) -> None:
        nonlocal finished
        finished += 1
        summary.failed += 1
        log.error(f"{task.src}: {message}")

    def finish(state: _InFlight) -> None:
        nonlocal finished
        if state in active:
            active.remove(state)
        task = state.task
        if state.error:
            return fail(task, state.error)
        for message in state.warnings:
            log.warn(f"{task.src}: {message}")
        pages = [r for k in sorted(state.results) for r in state.results[k]]
        for n, _, warning in pages:
            if warning:
                log.warn(f"{task.src}: page {n}: {warning}")
        if not any(text for _, text, _ in pages):
            return fail(task, "no extractable text (scanned images? try OCR, e.g. ocrmypdf -l chi_tra)")
        text = join_pages([(n, t) for n, t, _ in pages], opts.page_sep) + "\n"
        data, lossy = _encode(text, opts.encoding)
        if lossy:
            log.warn(f"{task.src}: some characters cannot be encoded as {opts.encoding}; written as '?'")
        try:
            if task.dst is None:
                if sys.stdout is None:
                    raise OSError("stdout is closed")
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            else:
                write_atomic(task.dst, data)
        except (OSError, ValueError) as exc:  # ValueError: stdout object already closed
            return fail(task, f"cannot write output: {exc}")
        finished += 1
        summary.converted += 1
        plural = "" if len(pages) == 1 else "s"
        log.info(f"[{finished}/{total}] {task.src} -> {task.dst or 'stdout'} ({len(pages)} page{plural})")

    def start(task: Task) -> None:
        nonlocal executor, broken, finished
        if opts.skip_existing and task.dst is not None and task.dst.exists():
            finished += 1
            summary.skipped += 1
            log.info(f"[{finished}/{total}] {task.src}: skipped, {task.dst} exists")
            return
        try:
            indices, notes = _prepare(task.src, opts.password, opts.pages)
        except ConversionError as exc:
            return fail(task, str(exc))
        chunks = _split(indices, workers)
        if broken and not futures:  # replace a pool whose worker died
            _abort(executor)
            executor, broken = None, False
        if executor is None:
            parallel = workers > 1 and (total > 1 or len(chunks) > 1)
            executor = ProcessPoolExecutor(workers, initializer=_init_worker) if parallel else _SerialExecutor()
        state = _InFlight(task, notes, remaining=len(chunks))
        active.append(state)
        try:
            for k, chunk in enumerate(chunks):
                futures[executor.submit(_extract_chunk, task.src, opts.password, chunk, opts.mode)] = (state, k)
        except BrokenProcessPool:  # a worker died since the last check
            broken = True
            for lost in active:  # every file in flight, this one included, lost its results
                fail(lost.task, _describe(BrokenProcessPool()))
            active.clear()
            futures.clear()

    def collect(fut: Future) -> None:
        nonlocal broken
        state, k = futures.pop(fut)
        exc = fut.exception()
        broken = broken or isinstance(exc, BrokenProcessPool)
        if exc is not None:
            state.error = state.error or _describe(exc)
        else:
            state.results[k] = fut.result()
        state.remaining -= 1
        if state.remaining == 0:
            finish(state)

    try:
        exhausted = False
        while True:
            window = 2 * workers if isinstance(executor, ProcessPoolExecutor) else 1
            while not exhausted and len(active) < window:
                task = next(todo, None)
                if task is None:
                    exhausted = True
                else:
                    start(task)
            if not futures:
                if exhausted:
                    break
                continue
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            for fut in done:
                collect(fut)
    except BaseException:
        if executor is not None:
            _abort(executor)
        raise
    if executor is not None:
        executor.shutdown()
    return summary


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def _pages_arg(value: str) -> List[PageRange]:
    try:
        return parse_page_spec(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _encoding_arg(value: str) -> str:
    try:
        info = codecs.lookup(value)
    except LookupError:
        raise argparse.ArgumentTypeError(f"unknown encoding {value!r}") from None
    if not getattr(info, "_is_text_encoding", True):
        raise argparse.ArgumentTypeError(f"{value!r} is not a text encoding")
    return info.name


def _version_text() -> str:
    try:
        pypdf_version = metadata.version("pypdf")
    except metadata.PackageNotFoundError:
        pypdf_version = "not installed"
    return f"%(prog)s {__version__} (pypdf {pypdf_version}, Python {sys.version.split()[0]})"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf-to-txt",
        description="Convert PDF files to plain text. Directories are searched recursively for *.pdf.",
        epilog=(
            "examples:\n"
            "  pdf-to-txt report.pdf                 -> report.txt next to the PDF\n"
            "  pdf-to-txt a.pdf b.pdf -o out/        -> out/a.txt, out/b.txt\n"
            "  pdf-to-txt ./pdfs -o ./txts           -> mirror the tree under ./txts\n"
            "  pdf-to-txt book.pdf -p 1-3,10- -o -   -> selected pages to stdout\n"
            "\nexit status: 0 ok, 1 a file failed, 2 usage error, 3 pypdf unusable, 130 interrupted"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("inputs", nargs="*", metavar="INPUT", help="PDF files and/or directories")
    parser.add_argument(
        "-o", "--output", metavar="OUT",
        help="output file (single PDF only), output directory, or '-' for stdout "
        "(default: write NAME.txt next to each PDF)",
    )
    parser.add_argument("-p", "--pages", type=_pages_arg, metavar="SPEC",
                        help="pages to convert, 1-based, e.g. 1-3,5,8- (default: all)")
    parser.add_argument(
        "--mode", choices=MODES, default="auto",
        help="auto: reading order of the PDF, falling back to layout mode for pages shattered into "
        "single characters; plain: always reading order; layout: keep the visual layout (default: auto)",
    )
    parser.add_argument(
        "--page-sep", choices=PAGE_SEPARATORS, default="blank",
        help="between pages: blank line, ff (form feed, keeps empty pages), marker ('===== Page N ====='), "
        "none (default: blank)",
    )
    parser.add_argument("--password", default=os.environ.get("PDF_PASSWORD"),
                        help="password for encrypted PDFs (default: $PDF_PASSWORD)")
    parser.add_argument("-e", "--encoding", type=_encoding_arg, default="utf-8",
                        help="output encoding, e.g. utf-8-sig, big5, gb18030 (default: utf-8)")
    parser.add_argument("-j", "--jobs", type=int, default=0, metavar="N",
                        help=f"worker processes (default: CPU count, at most {MAX_AUTO_JOBS}; 1 = single process)")
    parser.add_argument("--skip-existing", action="store_true", help="do not convert PDFs whose output already exists")
    parser.add_argument("--no-recursive", action="store_true", help="do not descend into subdirectories")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print warnings and errors")
    parser.add_argument("-V", "--version", action="version", version=_version_text())
    return parser


def _tolerate_unencodable_output() -> None:
    """A CJK file name must not crash a status message on a cp1252/ASCII console."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    _tolerate_unencodable_output()
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    after_dashes: List[str] = []
    if "--" in argv:  # argparse's intermixed mode cannot handle '--', so split there ourselves
        cut = argv.index("--")
        argv, after_dashes = argv[:cut], argv[cut + 1 :]
    args = parser.parse_intermixed_args(argv)
    args.inputs += after_dashes
    if not args.inputs:
        parser.error("the following arguments are required: INPUT")
    if args.jobs < 0:
        parser.error("--jobs must be 0 (auto) or greater")
    log = _Log(args.quiet)

    try:
        _load_pypdf()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # a broken 'cryptography' raises pyo3's PanicException, a BaseException
        log.error(f"cannot use pypdf ({type(exc).__name__}: {exc}); install it with: pip install 'pypdf[crypto]'")
        return EXIT_NO_PYPDF

    try:
        tasks, problems = plan_tasks(args.inputs, args.output, recursive=not args.no_recursive)
    except PlanError as exc:
        parser.error(str(exc))
    for problem in problems:
        log.error(problem)
    if not tasks and not problems:
        log.error("no PDF files found")
        return EXIT_FAILED

    opts = Options(
        mode=args.mode, pages=args.pages, password=args.password or None, page_sep=args.page_sep,
        encoding=args.encoding, jobs=args.jobs, skip_existing=args.skip_existing,
    )
    try:
        summary = run(tasks, opts, log)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return EXIT_INTERRUPTED
    summary.failed += len(problems)
    if len(tasks) + len(problems) > 1:
        log.info(f"Done: {summary.converted} converted, {summary.skipped} skipped, {summary.failed} failed")
    return EXIT_FAILED if summary.failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
