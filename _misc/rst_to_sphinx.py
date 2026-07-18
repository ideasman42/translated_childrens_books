#!/usr/bin/env python3
"""
Build a PDF from a single reStructuredText file via a throw-away Sphinx project.

The input file is self-contained (its own title and substitutions); this script
wraps it in the minimal Sphinx project Sphinx needs, builds it inside a
temporary directory, moves the resulting PDF out, then discards everything else.
"""

__all__ = ("main",)

import argparse
import shutil
import subprocess
import sys
import tempfile

from pathlib import Path

# LaTeX engines Sphinx knows how to drive.
#
# NOTE(@ideasman42): default to `pdflatex`. The source is German (umlauts, sharp
# s) and uses non-breaking spaces for indentation, so a UTF-8-aware setup is
# needed -- but Sphinx already configures `inputenc`/`fontenc` for pdflatex, so
# it renders all of these correctly in practice (verified against the German
# original). pdflatex is also Sphinx's own default and the most broadly
# installed; xelatex/lualatex give nicer font handling but pull in support
# packages (xetex formats, luaotfload) that are not always present. So keep
# them selectable, and default to the one that just works.
ENGINES = ("pdflatex", "xelatex", "lualatex")
ENGINE_DEFAULT = "pdflatex"

# Body font size in points. Headings and relative sizes scale from this.
FONT_SIZE_DEFAULT = 9.0

# Adornment characters a title over-line/under-line may be drawn with.
_TITLE_ADORNMENT = frozenset("=-`:'\"~^_*+#<>.")

# Prefix marking a build setting embedded in the document, e.g. an RST comment
# `my-pdf:font-size=9.5`. It lives in a comment so it never renders.
_CONFIG_PREFIX = "my-pdf:"
# Settings the document is allowed to set; anything else is a typo worth
# flagging rather than silently ignoring.
_CONFIG_KEYS = frozenset({"font", "font-size"})


def _document_config(rst_text: str) -> dict[str, str]:
    """Return the ``my-pdf:key=value`` build settings embedded in the document."""
    config = {}
    for line in rst_text.splitlines():
        line = line.strip()
        if line.startswith(_CONFIG_PREFIX) and "=" in line:
            key, _, value = line[len(_CONFIG_PREFIX):].partition("=")
            config[key.strip()] = value.strip()
    return config


def _is_title_adornment(line: str) -> bool:
    """True if ``line`` is a title over-/under-line: a run of one adornment char."""
    line = line.rstrip()
    return len(line) >= 3 and len(set(line)) == 1 and set(line) <= _TITLE_ADORNMENT


def _document_title(rst_text: str, fallback: str) -> str:
    """Return the document title (the first over-/under-lined heading), or ``fallback``."""
    lines = rst_text.splitlines()
    for i, line in enumerate(lines[:-1]):
        text = line.strip()
        # The title is the first text line with an adornment under-line beneath
        # it. An over-line above it, when present, is itself adornment and is
        # skipped -- so both `Title\n=====` and `=====\nTitle\n=====` work.
        if text and not _is_title_adornment(text) and _is_title_adornment(lines[i + 1]):
            return text
    return fallback


def _write_project(
    src_dir: Path,
    doc_stem: str,
    title: str,
    engine: str,
    font: str | None = None,
    font_size: float = FONT_SIZE_DEFAULT,
) -> None:
    """Write the conf.py and marker files that turn ``src_dir`` into a Sphinx project."""
    # LaTeX preamble additions, each on its own line:
    # - secnumdepth: drop section numbers, so a heading reads "Page 15", not
    #   "15 Page 15".
    # - fontsize: set a true `font_size`pt body. The `fontsize` package is used
    #   rather than the class `pointsize` option (article only offers 10/11/12pt)
    #   or a bare \fontsize (which reverts wherever \normalsize is called); it
    #   redefines \normalsize and the relative sizes so headings scale too.
    # - filbreak before each \section: keep a section on one page when it fits,
    #   letting several short sections share a page, and start a fresh page only
    #   when the section would otherwise span the page break. A section taller
    #   than a page still has to span -- that is unavoidable.
    preamble = (
        "\\setcounter{secnumdepth}{-2}\n"
        f"\\usepackage[fontsize={font_size:g}pt]{{fontsize}}\n"
        "\\usepackage{etoolbox}\n"
        "\\pretocmd{\\section}{\\filbreak}{}{}\n"
        # Restyle the section title (the "Page N" heading): centered, at body
        # size (\normalsize), and in the body font, bold and black (\rmfamily is
        # the main font set by fontpkg) rather than Sphinx's coloured sans
        # header. titlesec is already loaded by Sphinx, so just re-issue
        # \titleformat.
        "\\titleformat{\\section}[block]"
        "{\\centering\\normalsize\\rmfamily\\bfseries}{}{0pt}{}\n"
        # Tighten the vertical space above/below the heading; the class default
        # (~3.5ex above, ~2.3ex below) leaves too much blank around it.
        "\\titlespacing*{\\section}{0pt}{4pt}{2pt}\n"
        # Running head: the document title, small and centered in the top margin
        # of every page, with the page number at the foot. Overrides Sphinx's
        # `normal` page style (which sets only the footer); \@title needs
        # \makeatletter. This wins because the preamble follows Sphinx's styles.
        "\\makeatletter\n"
        "\\fancypagestyle{normal}{%\n"
        "  \\fancyhf{}%\n"
        "  \\fancyhead[C]{\\small\\@title}%\n"
        "  \\fancyfoot[C]{\\small\\thepage}%\n"
        "  \\renewcommand{\\headrulewidth}{0pt}%\n"
        "  \\renewcommand{\\footrulewidth}{0pt}%\n"
        "}\n"
        "\\makeatother\n"
        # Remove the space Sphinx puts around a line block. Its DUlineblock
        # environment sets \partopsep to a full \baselineskip (~11pt), which
        # -- because every paragraph here is a line block -- lands above each
        # section title and, combining via \addvspace (a max, not a sum),
        # swallows the \titlespacing above. Zero partopsep so the title spacing
        # is what actually shows. itemsep is kept so the body line spacing is
        # unchanged.
        "\\renewenvironment{DUlineblock}[1]{%\n"
        "  \\list{}{\\setlength{\\partopsep}{0pt}\\setlength{\\topsep}{0pt}%\n"
        "          \\setlength{\\itemsep}{0.15\\baselineskip}\\setlength{\\parsep}{0pt}%\n"
        "          \\setlength{\\leftmargin}{#1}}\\raggedright}{\\endlist}\n"
    )

    # Present the story as plain running text: no title page, no table of
    # contents, and the `howto` theme so the top-level headings stay sections
    # rather than becoming numbered "Chapter N" pages (which the `manual` theme
    # would force).
    elements = {
        "papersize": "a4paper",
        "pointsize": "10pt",
        "maketitle": "",
        "tableofcontents": "",
        "preamble": preamble,
        # Tighten the space around a transition rule; Sphinx defaults to
        # \bigskip either side, which is too airy at this font size.
        "transition": "\n\n\\smallskip\\hrule\\smallskip\n\n",
        # Halve Sphinx's default 1in page margins. marginpar is zeroed since
        # there are no margin notes to reserve space for.
        "sphinxsetup": "hmargin=0.5in,vmargin=0.5in,marginpar=0pt",
        # Place the running head and page-number foot a small, matched distance
        # (~6pt, about half the body margin) from the page edges. Left to itself
        # the head is jammed against the top edge; includeheadfoot instead insets
        # both a full 0.5in, which is too much. Setting headsep/footskip
        # explicitly gives a small symmetric inset. Sphinx's own \geometry only
        # sets hmargin/vmargin/marginpar, so these persist and the body keeps its
        # 0.5in margin.
        "geometry": "\\usepackage[headheight=10pt,headsep=21pt,footskip=28pt]{geometry}",
    }
    if font is not None:
        # Set the body font by its fontconfig family name via fontspec. This
        # only works with a system-font engine (xelatex/lualatex), which the
        # caller has already ensured. fontspec picks up the bold/italic faces
        # from the family, so a plain name is enough for a well-formed family.
        elements["fontpkg"] = f"\\usepackage{{fontspec}}\n\\setmainfont{{{font}}}\n"

    # The document is its own root; no index/toctree is needed for a single file.
    conf = (
        "# Generated by rst_to_sphinx.py -- do not edit.\n"
        f"project = {title!r}\n"
        "author = ''\n"
        "release = ''\n"
        "extensions = []\n"
        f"root_doc = {doc_stem!r}\n"
        f"latex_engine = {engine!r}\n"
        # (startdocname, targetname, title, author, theme).
        f"latex_documents = [({doc_stem!r}, {doc_stem + '.tex'!r}, {title!r}, '', 'howto')]\n"
        f"latex_elements = {elements!r}\n"
    )
    (src_dir / "conf.py").write_text(conf, encoding="utf-8")


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run ``cmd``, capturing combined output; on failure print the tail and raise."""
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        # LaTeX/Sphinx logs are long; the useful part is at the end.
        sys.stderr.write("\n".join(proc.stdout.splitlines()[-40:]) + "\n")
        raise subprocess.CalledProcessError(proc.returncode, cmd)
    return proc


def _build_pdf(src_dir: Path, build_dir: Path, doc_stem: str, engine: str) -> Path:
    """Build ``src_dir`` to a PDF under ``build_dir`` and return its path."""
    sphinx = [sys.executable, "-m", "sphinx"]

    if shutil.which("latexmk") is not None:
        # Preferred path: Sphinx's `latexpdf` maker runs latexmk, which reruns
        # the engine until cross-references and the table of contents settle.
        _run([*sphinx, "-M", "latexpdf", str(src_dir), str(build_dir), "-q"])
    else:
        # Fallback when latexmk is absent: emit the .tex, then drive the engine
        # directly. Two passes resolve the TOC and label references for a
        # single-file document; more would only matter if pagination cascaded,
        # which it does not here.
        _run([*sphinx, "-M", "latex", str(src_dir), str(build_dir), "-q"])
        latex_dir = build_dir / "latex"
        for _ in range(2):
            _run([engine, "-interaction=nonstopmode", f"{doc_stem}.tex"], cwd=latex_dir)

    pdf = build_dir / "latex" / f"{doc_stem}.pdf"
    if not pdf.exists():
        raise FileNotFoundError(f"expected PDF was not produced: {pdf}")
    return pdf


def _rst_to_pdf(
    input_rst: Path,
    output_pdf: Path,
    engine: str = ENGINE_DEFAULT,
    font: str | None = None,
    font_size: float | None = None,
) -> Path:
    """Build ``input_rst`` into ``output_pdf`` and return the output path.

    ``engine`` must be one of ``ENGINES``. ``font`` is a fontconfig family name
    for the body text; it requires a system-font engine, so pdflatex is upgraded
    to xelatex when a font is used. ``font_size`` is the body size in points.
    Both ``font`` and ``font_size``, when None, are taken from the document's
    `my-pdf:font` / `my-pdf:font-size` settings (``font_size`` falling back to
    ``FONT_SIZE_DEFAULT``). The build happens in a temporary directory that is
    removed on success and kept on failure so its LaTeX log can be inspected.
    """
    input_rst = input_rst.resolve()
    rst_text = input_rst.read_text(encoding="utf-8")
    title = _document_title(rst_text, fallback=input_rst.stem)
    config = _document_config(rst_text)

    # A misspelled key would otherwise be dropped and the wrong default used.
    for key in config.keys() - _CONFIG_KEYS:
        sys.stderr.write(f"warning: {input_rst.name}: unknown my-pdf:{key} (ignored)\n")

    # For font and size an explicit argument wins; otherwise take the document's
    # my-pdf: setting, else the default (no font override / FONT_SIZE_DEFAULT).
    if font is None:
        font = config.get("font")
    if font_size is None:
        embedded = config.get("font-size")
        if embedded is None:
            font_size = FONT_SIZE_DEFAULT
        else:
            try:
                font_size = float(embedded)
            except ValueError:
                raise ValueError(
                    f"{input_rst.name}: my-pdf:font-size is not a number: {embedded!r}"
                ) from None
    if font_size <= 0:
        raise ValueError(f"font size must be positive: {font_size:g}")

    # System fonts are only reachable through xelatex/lualatex; pdflatex uses its
    # own bundled fonts. Rather than fail, upgrade the default engine.
    if font is not None and engine == "pdflatex":
        engine = "xelatex"

    # The Sphinx source-document name is derived from the input's stem so the
    # built PDF and .tex share it; avoid a leading underscore or dot upsetting it.
    doc_stem = input_rst.stem.lstrip("_.") or "document"

    # Build in a temporary directory. Remove it on success, but keep it on
    # failure: the LaTeX log lives inside (the path Sphinx prints points there),
    # and it is the only way to diagnose a build error.
    tmp_path = Path(tempfile.mkdtemp(prefix="rst_to_sphinx_"))
    try:
        src_dir = tmp_path / "src"
        build_dir = tmp_path / "build"
        src_dir.mkdir()

        # Assemble the project: the document plus a generated conf.py.
        shutil.copyfile(input_rst, src_dir / f"{doc_stem}.rst")
        _write_project(src_dir, doc_stem, title, engine, font, font_size)

        pdf = _build_pdf(src_dir, build_dir, doc_stem, engine)

        # Move the PDF out before the temporary directory is removed.
        output_pdf.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(pdf, output_pdf)
    except BaseException:
        sys.stderr.write(f"note: build files kept for inspection: {tmp_path}\n")
        raise
    # Reached only when the build succeeded (the except re-raises otherwise).
    shutil.rmtree(tmp_path, ignore_errors=True)

    return output_pdf


def main() -> int:
    """Parse command-line arguments and build the requested PDF."""
    parser = argparse.ArgumentParser(
        description="Build a PDF from a reStructuredText file using a temporary Sphinx project.",
    )
    parser.add_argument("input", type=Path, help="the reStructuredText file to build")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="the PDF to write (default: input with a .pdf suffix, alongside the input)",
    )
    parser.add_argument(
        "--engine",
        choices=ENGINES,
        default=ENGINE_DEFAULT,
        help=f"the LaTeX engine (default: {ENGINE_DEFAULT})",
    )
    parser.add_argument(
        "--font",
        default=None,
        metavar="FAMILY",
        help="body font by fontconfig family name, e.g. 'ITC Souvenir Std' "
        "(implies xelatex); overrides the document's my-pdf:font",
    )
    parser.add_argument(
        "--font-size",
        type=float,
        default=None,
        metavar="PT",
        help="body font size in points; overrides the document's "
        f"my-pdf:font-size (default: {FONT_SIZE_DEFAULT:g})",
    )
    args = parser.parse_args()

    if not args.input.is_file():
        parser.error(f"input file not found: {args.input}")

    output = args.output if args.output is not None else args.input.with_suffix(".pdf")

    try:
        result = _rst_to_pdf(
            args.input,
            output,
            engine=args.engine,
            font=args.font,
            font_size=args.font_size,
        )
    except subprocess.CalledProcessError:
        # _run already wrote the useful log tail; the kept build dir was noted.
        sys.stderr.write("error: build failed\n")
        return 1
    except (FileNotFoundError, ValueError) as ex:
        sys.stderr.write(f"error: {ex}\n")
        return 1

    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
