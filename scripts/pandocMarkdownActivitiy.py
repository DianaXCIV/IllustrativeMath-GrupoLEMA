#!/usr/bin/env python3
"""
Enrique Acosta, 2025
Convert an act-*.ptx, warm-*.ptx, cool-*.ptx, files into a single Pandoc-Markdown
file that renders neatly as docx Word, but it may also partially work for Reveal.js
slides, PDF (Beamer), or PowerPoint.

╭─────────────────────────────  What the script does  ───────────────────────────╮
│ 1. Reads a *.ptx* file that contains one `<activity>` element (or corresponding│
│    warm and cool tags).                                                        │
│ 2. Resolves all `<custom ref="…"/>` placeholders using                         │
│       ../meta/customizations/textos-constantes-v1.ptx                          │
│    so, for example, `<custom ref="recommended-time-titulo"/>` becomes          │
│    “Tiempo recomendado”.                                                       │
│ 3. Resolves all `<xi:include href="…"/>` directives using XInclude.            │
│    The script uses `lxml.etree` to expand includes before processing content.  │
│ 4. Builds slides in this order (unless flags change it):                       │
│       • Statement  (title “Enunciado”)                                         │
│       • Solution   (title “Solución”)                                          │
│       • All teacher-note <dl> items in <prelude>                               │
│       • All teacher-note <dl> items in <postlude>                              │
│ 5. Each teacher-note `<li>` becomes one ## markdown section.                   │
│ 6. Supported inline conversions:                                               │
│       <m> … </m>        →  $…$   (inline LaTeX)                                │
│       <me> … </me>      →  $$ … $$ (display math)                              │
│       <image>           →  `![](../assets/foo.png){width=…}`                   │
│                          Width is taken from @width (e.g., "45%") on the image,│
│                          or inherited from an enclosing <sidebyside>. It is    │
│                          converted to inches unless --keep-percentage-widths   │
│                          is used.                                              │
│       <q> … </q>        →  « … »                                               │
│ 7. `<sidebyside>` is **flattened**: the first “column” is emitted, a blank     │
│    line is inserted, then the second column, etc. `<stack>` wrappers are       │
│    ignored; only their children appear.                                        │
│ 8. Lists keep proper numbering: 1. → a. → 1. for nesting levels 0/1/2+.        │
│ 9. The resulting Markdown is written to the output file.                       │
╰────────────────────────────────────────────────────────────────────────────────╯

Command-line flags
──────────────────
--prelude-first      Place all <prelude> slides **before** the statement slide
--no-solution        Do not include the <solution> slide
--minimal            Within every teacher-note slide:
                       · keep **only** <p> or <li> elements that contain a <q>
                       · drop the entire slide if nothing remains after pruning
--keep-percentage-widths  Keep image widths as percentages (e.g., 45%) instead of
                          converting them to absolute inches.
--convert-svg        Convert extensionless <image> sources from their .pdf version to
                     a high-res .png. Width is calculated from the .svg version.
                     Requires ImageMagick to be installed.

Usage
─────
  python pandocMarkdownActivitiy.py <path to activity.ptx>
  python pandocMarkdownActivitiy.py <path to activity.ptx> --prelude-first
  python pandocMarkdownActivitiy.py <path to activity.ptx> --no-solution
  python pandocMarkdownActivitiy.py <path to activity.ptx> --minimal

Typical Pandoc step afterwards
──────────────────────────────
  pandoc activity.md -o activity.docx                                 # Word
  pandoc activity.md -t revealjs -s -o activity.html                  # slides
  pandoc activity.md -o activity.pdf --pdf-engine=xelatex             # Beamer PDF
  pandoc activity.md -o activity.pptx --reference-doc template5.pptx  # PowerPoint

Limitations
───────────
* Images are **not automatically centred** in DOCX/PPTX due to a Pandoc
  limitation; centre manually or via template if needed.
* Column widths and multi-column layouts in DOCX are not preserved; the
  script flattens side-by-side content vertically instead.
* Image width is inherited from the enclosing <sidebyside> widths attribute
  if not explicitly set, but layout remains vertical.
* Only the subset of PTX tags listed above is handled. Unknown tags fall
  through as plain text.
"""
from __future__ import annotations
import sys, argparse, textwrap, string, re, copy
from pathlib import Path
import subprocess

from lxml import etree as ET

# ─────────────────────────── CONSTANTS ──────────────────────────────────
INDENT = "    "
DEFAULT_DOC_WIDTH_INCHES = 6.5
SVG_CONVERSION_DPI = 300

# ─────────────────────── STANDALONE UTILITIES ───────────────────────────

# === String and XML Helpers ===
def strip_ns(tag: str | None) -> str:
    """Removes the namespace from an XML tag."""
    if not isinstance(tag, str): return ""
    return tag.split("}", 1)[-1]

def xml_str(el: ET.Element) -> str:
    """Returns the XML element as a clean string."""
    return ET.tostring(el, encoding="unicode").strip()

def bullet_label(num: int, lvl: int) -> str:
    """Generates a bullet label for a list item based on its level."""
    if lvl == 0: return f"{num}."
    if lvl == 1:
        i, j = divmod(num - 1, 26)
        return (string.ascii_lowercase[i - 1] if i else "") + string.ascii_lowercase[j] + "."
    return f"{num}."

# === File and System Helpers ===
def load_constants(base_path: Path) -> dict[str, str]:
    """Loads text constants from the specified PTX customizations file."""
    const_path = (base_path.parent / "../meta/customizations/textos-constantes-v1.ptx").resolve()
    constants = {}
    if not const_path.exists():
        return constants
    try:
        root = ET.parse(str(const_path)).getroot()
        for c in root.findall(".//custom"):
            if c.get("name") and c.text:
                constants[c.get("name")] = c.text.strip()
    except Exception as e:
        print(f"Warning: Could not load constants from {const_path}: {e}", file=sys.stderr)
    return constants

def load_and_prepare_xml(ptx_path: Path) -> ET._ElementTree:
    """Loads, parses, and resolves XIncludes for a PTX file."""
    parser = ET.XMLParser(load_dtd=True, recover=True)
    tree = ET.parse(str(ptx_path), parser)
    tree.xinclude()
    return tree

# === Image Conversion Helpers ===
def get_svg_pixel_width(svg_path: Path) -> float:
    """Parses an SVG file to find its intrinsic pixel width."""
    try:
        svg_tree = ET.parse(str(svg_path))
        svg_root = svg_tree.getroot()
        if width_str := svg_root.get("width", ""):
            return float(re.sub(r"[a-zA-Z%]+", "", width_str))
        if viewBox := svg_root.get("viewBox"):
            return float(viewBox.split()[2])
    except Exception as e:
        print(f"Warning: Could not parse SVG dimensions from {svg_path.name}: {e}", file=sys.stderr)
    return 0.0

def run_pdf_to_png_conversion(pdf_path: Path, png_path: Path):
    """If the target PNG doesn't exist, converts the PDF using ImageMagick."""
    if not pdf_path.exists():
        print(f"Warning: PDF source file not found at {pdf_path}", file=sys.stderr)
        return
    if png_path.exists():
        return

    print(f"Converting PDF: {pdf_path.name} -> {png_path.name}")
    command = [
        "magick", "convert",
        "-density", str(SVG_CONVERSION_DPI),
        "-background", "none",
        str(pdf_path),
        str(png_path)
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        print("FATAL ERROR: `magick` command not found. Please install ImageMagick.", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"FATAL ERROR: ImageMagick failed to convert {pdf_path.name}.", file=sys.stderr)
        print(f"Stderr: {e.stderr}", file=sys.stderr)
        sys.exit(1)


# ───────────────────── CORE CONVERSION LOGIC ────────────────────────────

class PtxToMarkdownConverter:
    """
    Encapsulates the logic for converting a PTX file to Pandoc Markdown.
    """
    def __init__(self, root: ET.Element, const: dict, args: argparse.Namespace, input_dir: Path):
        self.root = root
        self.const = const
        self.args = args
        self.input_dir = input_dir

    def convert(self) -> str:
        """
        Orchestrates the conversion process and returns the final Markdown string.
        """
        title = self.root.findtext("./title", "(sin título)").replace('"', r"\"")
        md = ["---", 'title: "IM+LEMA: Actividad editable"', "slideLevel: 2", "---", "", f"# {title}", ""]

        slide_order = []
        if self.args.prelude_first:
            slide_order.extend(self._process_section("prelude"))

        if (stmt := self.root.find("./statement")) is not None:
            body = self._process_block_children(stmt)
            slide_order.append(self._format_slide("Enunciado", body))

        if not self.args.no_solution:
            if (sol := self.root.find("./solution")) is not None:
                body = self._process_block_children(sol)
                slide_order.append(self._format_slide("Solución", body))

        if not self.args.prelude_first:
            slide_order.extend(self._process_section("prelude"))

        slide_order.extend(self._process_section("postlude"))

        md.extend(line for slide_content in slide_order for line in slide_content)
        return "\n".join(md)

    def _process_section(self, section_name: str) -> list[list[str]]:
        """Processes all teacher-note items within a section (e.g., prelude).

        Each note is an <li> of the section's <dl>.  The "dl/li" step keeps the
        items of a <ul> nested inside a note from counting as notes themselves.
        """
        slides = []
        sections = self.root.findall(f".//{section_name}")
        for para in [li for sec in sections for li in sec.findall(".//dl/li")]:
            work_para = para
            if self.args.minimal:
                if not (work_para := self._prune_for_minimal(para)):
                    continue

            h_el = work_para.find("./title")
            heading = self._inline_md(h_el) if h_el is not None else "(sin título)"
            
            body_elements = [ch for ch in work_para if strip_ns(ch.tag) != "title"]
            body = self._process_block_children(body_elements)
            slides.append(self._format_slide(heading, body))
        return slides

    def _process_block_children(self, elements: list[ET.Element]) -> list[str]:
        """Converts a list of block-level XML elements to Markdown lines."""
        return [line for elem in elements for line in self._block_md(elem)]

    # === Minimal Mode Helpers ===
    def _has_q(self, elem: ET.Element) -> bool:
        if strip_ns(elem.tag) == "q": return True
        return any(self._has_q(child) for child in elem)

    def _prune_for_minimal(self, paragraphs: ET.Element) -> ET.Element | None:
        new_para = copy.copy(paragraphs); new_para.clear()
        for child in paragraphs:
            tag = strip_ns(child.tag)
            if tag == "title":
                new_para.append(child)
            elif tag == "p" and self._has_q(child):
                new_para.append(child)
            elif tag in ("ol", "ul"):
                new_list = copy.copy(child); new_list.clear()
                for li in child.findall("./li"):
                    if self._has_q(li): new_list.append(li)
                if len(new_list): new_para.append(new_list)
        
        # Keep the slide only if it has content other than the title
        if any(strip_ns(c.tag) != "title" for c in new_para):
            return new_para
        return None

    # === Markdown Generation Methods ===
    def _format_slide(self, title: str, body: list[str]) -> list[str]:
        return [f"## {title}", ""] + body

    def _inline_md(self, el: ET.Element) -> str:
        """Converts an inline XML element and its children to a Markdown string."""
        tag = strip_ns(el.tag)
        if tag == "m": return f"${el.text}$"
        if tag == "me": return f"\n$${el.text}$$\n"
        if tag == "image": return self._process_image_tag(el)
        if tag == "custom":
            ref = el.get("ref")
            return self.const.get(ref, el.text.strip() if el.text else xml_str(el))
        if tag == "q":
            inner = "".join(self._inline_md(c) for c in el)
            return f"«{(el.text or '')}{inner}»"

        parts = [el.text or ""]
        for c in el:
            child_tag = strip_ns(c.tag)
            if child_tag in ("sidebyside", "ol", "ul"):
                parts.append("\n\n" + "\n".join(self._block_md(c)).strip() + "\n\n")
            else:
                parts.append(self._inline_md(c))
            if c.tail: parts.append(c.tail)
        return "".join(parts)

    def _block_md(self, el: ET.Element, lvl: int = 0) -> list[str]:
        """Converts a block-level XML element to a list of Markdown lines."""
        tag = strip_ns(el.tag)
        if tag == "p":
            content = self._inline_md(el).strip()
            return content.splitlines() + [""] if content else []
        if tag == "line":
            lines = [self._inline_md(el).rstrip()]
            if el.tail and el.tail.strip(): lines.append(el.tail.strip())
            return lines
        if tag in ("ol", "ul"):
            lines = []
            for i, li in enumerate(el.findall("./li"), start=1):
                lines.extend(self._block_md_li(li, lvl, i if tag == "ol" else None))
            lines.append("")
            return lines
        if tag == "sidebyside":
            return self._sidebyside_md(el, lvl)
        if tag == "xi:include":
            return ["```xml", xml_str(el), "```", ""]
        
        # Default fallback for unknown block tags
        content = self._inline_md(el).strip()
        return [content, ""] if content else []

    def _block_md_li(self, li: ET.Element, lvl: int, num: int | None) -> list[str]:
        """Formats a list item `<li>` with correct indentation and bullet."""
        bullet = bullet_label(num, lvl) if num is not None else "*"
        prefix = INDENT * lvl + bullet + " "
        content = self._inline_md(li).strip()
        lines = content.split('\n')
        if not lines or not lines[0].strip(): return []

        out = [prefix + lines[0].lstrip()]
        indent_prefix = ' ' * len(prefix)
        for line in lines[1:]:
            out.append(indent_prefix + line if line.strip() else "")
        return out

    def _sidebyside_md(self, node: ET.Element, lvl: int) -> list[str]:
        """Flattens a <sidebyside> element into a vertical sequence of its columns."""
        out, first_col = [], True
        for col in node:
            if not first_col: out.append("")
            first_col = False
            
            if strip_ns(col.tag) in ("stack", "div", "col"):
                out.extend(self._process_block_children(list(col)))
            else:
                out.extend(self._block_md(col, lvl))
        return out
        
    def _process_image_tag(self, el: ET.Element) -> str:
        """Handles all logic for an <image> tag, returning a Markdown string."""
        src = el.get("source", "").strip()
        width = self._get_image_width(el)

        # Handle SVG conversion if specified
        if self.args.convert_svg and not Path(src).suffix:
            src, width = self._handle_svg_conversion(src, width)
        elif not Path(src).suffix:
            src += ".png" # Default to png if no extension
        
        # Normalize path and encode spaces
        if not src.startswith("../assets/"):
            src = f"../assets/{src}"
        src = src.replace(" ", "%20")
        
        # Format final width string
        if not width:
            final_width_str = "4in"
        elif self.args.keep_percentage_widths:
            final_width_str = width
        else: # Convert percentage to inches
            try:
                percentage = float(width.strip('%'))
                final_width_str = f"{(percentage / 100.0) * DEFAULT_DOC_WIDTH_INCHES:.2f}in"
            except (ValueError, TypeError):
                final_width_str = "4in" # Fallback

        return f'![]({src}){{width="{final_width_str}"}}'

    def _get_image_width(self, el: ET.Element) -> str:
        """Finds image width, inheriting from <sidebyside> if necessary."""
        if width := el.get("width", "").strip():
            return width
        
        parent = el.getparent()
        while parent is not None:
            if strip_ns(parent.tag) == "sidebyside":
                widths_attr = parent.get("widths") or parent.getparent().get("widths")
                if widths_attr:
                    widths = widths_attr.split()
                    # Find which column this image is in
                    column_el = el
                    while column_el.getparent() is not None and column_el.getparent() != parent:
                        column_el = column_el.getparent()
                    
                    try:
                        siblings = [s for s in parent if isinstance(s.tag, str)]
                        idx = siblings.index(column_el)
                        if idx < len(widths):
                            return widths[idx].strip()
                    except ValueError:
                        pass # Should not happen if structure is valid
                break
            parent = parent.getparent()
        return ""

    def _handle_svg_conversion(self, src: str, width: str) -> tuple[str, str]:
        """Manages the PDF->PNG conversion process and returns new src and width."""
        svg_path = (self.input_dir / f"../assets/{src}").with_suffix('.svg').resolve()
        pdf_path = svg_path.with_suffix('.pdf')
        png_path = svg_path.with_suffix('.png')
        
        run_pdf_to_png_conversion(pdf_path, png_path)
        
        # If width was not explicitly set, calculate it from the SVG
        if not width and svg_path.exists():
            pixel_width = get_svg_pixel_width(svg_path)
            if pixel_width > 0:
                physical_width_in = pixel_width / SVG_CONVERSION_DPI
                percentage = (physical_width_in / DEFAULT_DOC_WIDTH_INCHES) * 100
                width = f"{percentage:.0f}%"

        return src + ".png", width

# ───────────────────────── MAIN EXECUTION ───────────────────────────────
def main() -> None:
    """Parses arguments, orchestrates the conversion, and writes the output file."""
    ap = argparse.ArgumentParser(
        description="Convert PTX to Pandoc Markdown slides.",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=__doc__.split('Limitations\n───────────')[-1] # Show limitations in help
    )
    ap.add_argument("input", help="input .ptx file")
    ap.add_argument("--prelude-first", action="store_true", help="render prelude slides before statement slide")
    ap.add_argument("--no-solution", action="store_true", help="omit the solution slide")
    ap.add_argument("--minimal", action="store_true", help="within <paragraphs> keep only <p>/<li> containing <q>")
    ap.add_argument("--keep-percentage-widths", action="store_true", help="keep image widths as percentages instead of converting to inches")
    ap.add_argument("--convert-svg", action="store_true", help="Convert extensionless <image> sources from .pdf to .png")
    args = ap.parse_args()

    in_file = Path(args.input)
    if not in_file.exists():
        print(f"FATAL ERROR: Input file not found at {in_file}", file=sys.stderr)
        sys.exit(1)
        
    out_file = in_file.with_suffix(".md")
    input_dir = in_file.parent

    # 1. Load data
    constants = load_constants(in_file)
    tree = load_and_prepare_xml(in_file)
    
    # 2. Instantiate converter and run the conversion
    converter = PtxToMarkdownConverter(
        root=tree.getroot(),
        const=constants,
        args=args,
        input_dir=input_dir
    )
    markdown_output = converter.convert()

    # 3. Write output
    try:
        out_file.write_text(markdown_output, encoding="utf-8")
        print(f"Wrote {out_file}")
    except IOError as e:
        print(f"FATAL ERROR: Could not write to output file {out_file}: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()