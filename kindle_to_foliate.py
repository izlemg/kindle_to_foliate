from bs4 import BeautifulSoup
from ebooklib import epub
import ebooklib
import json
import re
import argparse
import os
import warnings
from datetime import datetime, timezone

warnings.filterwarnings("ignore", category=UserWarning, message=".*XML.*HTML.*")

try:
    from bs4 import XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except ImportError:
    pass

# Kindle CSS class to Foliate color name
COLOR_MAP = {
    "highlight_yellow": "yellow",
    "highlight_orange": "orange",
    "highlight_pink": "pink",
    "highlight_blue": "blue",
    "highlight_green": "green",
}

# Map Foliate color names to CSS colors for inline marking
CSS_COLOR = {
    "yellow": "#fff59d",
    "orange": "#ffcc80",
    "pink": "#f48fb1",
    "blue": "#90caf9",
    "green": "#a5d6a7",
}


def normalize(text: str) -> str:
    """Normalize text for matching - remove extra whitespace and normalize punctuation."""
    text = re.sub(r"\s+", " ", text).strip()
    # Normalize common punctuation differences
    text = text.replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2018", "'")
    text = text.replace("\u2014", "-").replace("\u2013", "-")
    # Normalize ALL spaces around dashes: "word - word" / "word- word" / "word -word" → "word-word"
    text = re.sub(r"\s*-\s*", "-", text)
    # Strip footnote reference numbers (1-3 digits) that appear:
    # - attached to a word before space: "Rawls3 rejects" → "Rawls rejects"
    # - between punctuation/spaces: ", 4 a" → ", a"
    # - before closing parens/brackets: "GPAI6)" → "GPAI)"
    # - at start/end of text (captured highlights often include footnotes)
    text = re.sub(r"(?<=\w)\d{1,3}(?=[\s,.);\]])", "", text)
    text = re.sub(r"(?<=[\s,.])\d{1,3}(?=[\s,.);\]])", "", text)
    text = re.sub(r"^\d{1,3}\s+", "", text)  # Leading footnote number
    text = re.sub(r"\s+\d{1,3}$", "", text)  # Trailing footnote number
    # Normalize bracket spacing (AFTER footnote strip to catch "word ]" left behind):
    # "[ I]" → "[I]", "] t" → "]t"
    text = re.sub(r"\[\s+", "[", text)
    text = re.sub(r"\s+\]", "]", text)
    text = re.sub(r"\]\s+(?=[a-zA-Z])", "]", text)
    # Normalize parenthetical spacing (AFTER footnote strip to catch "word )" left behind):
    # "( word)" → "(word)", "word )" → "word)"
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    # Normalize slash spacing: "other/ more" → "other/more"
    text = re.sub(r"/ ", "/", text)
    # Collapse extra whitespace again
    text = re.sub(r"\s+", " ", text).strip()
    return text



def extract_highlights(annotation_file):
    """Parse Kindle notebook HTML into normalized highlight entries with colors."""

    with open(annotation_file, encoding="utf-8") as f:
        soup = BeautifulSoup(f, "xml")

    highlights = []

    for heading in soup.select(".noteHeading"):
        text_el = heading.find_next_sibling("div", class_="noteText")
        if not text_el:
            continue

        raw_text = normalize(text_el.get_text())
        if len(raw_text) < 5:
            continue

        color = "yellow"
        color_span = heading.select_one("span[class^=highlight_]")
        if color_span and color_span.has_attr("class"):
            color_class = next((c for c in color_span["class"] if c.startswith("highlight_")), None)
            color = COLOR_MAP.get(color_class, "yellow")

        highlights.append({"text": raw_text, "color": color})

    # Deduplicate by highlight text, keep first color encountered
    unique = {}
    for h in highlights:
        unique.setdefault(h["text"], h)

    print("Highlights extracted:", len(unique))
    return list(unique.values())


def get_element_text(element):
    """Get element text, stripping footnote markers (<sup> tags) for better matching."""
    from bs4 import Tag
    import copy
    elem = copy.copy(element)
    for sup in elem.find_all("sup"):
        sup.decompose()
    return re.sub(r"\s+", " ", elem.get_text()).strip()


def find_containing_element(element, search_text, norm_fn=None):
    """Find the smallest element that contains the search text.
    If norm_fn is provided, normalize both sides before comparison.
    Returns (element, depth, child_index) or None.
    """
    from bs4 import Tag
    
    elem_text = get_element_text(element)
    compare_elem = norm_fn(elem_text) if norm_fn else elem_text
    compare_search = norm_fn(search_text) if norm_fn else search_text
    
    if compare_search not in compare_elem:
        return None
    
    # Try to find a more specific child that contains it
    child_idx = 0
    for child in element.children:
        if isinstance(child, Tag):
            child_idx += 1
            child_text = get_element_text(child)
            compare_child = norm_fn(child_text) if norm_fn else child_text
            if compare_search in compare_child:
                deeper = find_containing_element(child, search_text, norm_fn)
                if deeper:
                    elem, depth, idx = deeper
                    return (elem, depth + 1, child_idx)
                else:
                    return (child, 1, child_idx)
    
    # Text is in this element but not in a single child
    return (element, 0, 0)


def get_element_cfi_path(element, body):
    """Get the CFI path from body to element."""
    from bs4 import Tag
    
    path = []
    current = element
    
    while current and current != body:
        parent = current.parent
        if not parent:
            break
            
        # Count this element's position among siblings
        idx = 0
        for sibling in parent.children:
            if isinstance(sibling, Tag):
                idx += 1
                if sibling == current:
                    path.insert(0, idx * 2)
                    break
        
        current = parent
    
    return path


def locate_highlights(book, highlights):
    """Match highlights to EPUB documents and emit Foliate-style annotations with proper range CFIs."""
    
    print("Parsing EPUB documents...")
    # Build spine index mapping
    spine_index = {}
    spine_items = {}
    for idx, (idref, _) in enumerate(book.spine):
        itm = book.get_item_with_id(idref)
        if itm:
            spine_index[itm.file_name] = idx
            spine_items[itm.file_name] = itm

    annotations = []
    matched_texts = set()
    
    # Process each document once -  parse with lxml for speed
    for doc_idx, (href, item) in enumerate(spine_items.items(), 1):
        print(f"  Processing document {doc_idx}/{len(spine_items)}: {href}")
        raw_html = item.get_content().decode()
        soup = BeautifulSoup(raw_html, "lxml")  # lxml is much faster than html.parser
        doc_text = get_element_text(soup)
        doc_text_normalized = normalize(doc_text)
        
        # Check all highlights against this document
        for hl in highlights:
            search_text = hl["text"]
            
            if hl["text"] in matched_texts:
                continue  # Already matched
            
            # Check if this text appears in the document
            # Always use normalized comparison to handle unicode, hyphens, footnotes
            hl_norm = normalize(search_text)
            if hl_norm not in doc_text_normalized:
                continue  # Not in this document
            
            # Find the body element
            body = soup.find("body")
            if not body:
                continue
            
            # Find the smallest element containing this text (with normalization)
            result = find_containing_element(body, search_text, normalize)
            
            if result:
                    containing_elem, depth, child_idx = result
                    
                    # Get the CFI path to this element
                    elem_path = get_element_cfi_path(containing_elem, body)
                    
                    idx = spine_index.get(href, 0)
                    cfi_spine_slot = 6 + 2 * idx
                    
                    # Build CFI path: /6/spine!/4/2/elem_path
                    # /4 is html, /4/2 is body
                    path_str = "/4/2" + "".join(f"/{p}" for p in elem_path)
                    
                    # Get the text content of this element and find offset
                    elem_text = get_element_text(containing_elem)
                    norm_elem = normalize(elem_text)
                    norm_search = normalize(search_text)
                    start_offset = norm_elem.find(norm_search)
                    if start_offset == -1:
                        start_offset = 0
                    end_offset = start_offset + len(norm_search)
                    
                    # Create a simple range CFI pointing to the element
                    cfi = f"epubcfi(/6/{cfi_spine_slot}!{path_str},/1:{start_offset},/1:{end_offset})"
                    
                    annotations.append({
                        "value": cfi,
                        "color": hl["color"],
                        "text": hl["text"],  # Use original text from Kindle
                        "note": "",
                        "created": datetime.now(timezone.utc).isoformat(),
                        "modified": ""
                    })
                    
                    matched_texts.add(hl["text"])
    
    # Find which highlights weren't matched
    not_found = []
    for hl in highlights:
        if hl["text"] not in matched_texts:
            not_found.append(hl["text"][:80] + "..." if len(hl["text"]) > 80 else hl["text"])

    if not_found:
        print(f"\nWarning: {len(not_found)} highlights not matched to EPUB:")
        for txt in not_found:
            print(f"  - {txt}")

    return annotations
def _build_html_pattern(text: str) -> str:
    """Build a regex pattern that matches highlight text in raw HTML,
    allowing for inline tags, unicode punctuation differences, 
    hyphen-space wrapping, and footnote markers."""
    # Work character by character from the normalized kindle text
    parts = []
    # Allow optional inline tags between any characters
    tag_gap = r"(?:<[^>]*>)*"
    # Optional whitespace + tags
    ws_tag_gap = r"(?:\s|<[^>]*>)*"
    
    i = 0
    while i < len(text):
        ch = text[i]
        
        if ch == ' ':
            # Whitespace: allow flexible whitespace + optional inline tags + optional footnote digits
            # (footnote numbers may have been stripped during normalization, so digits can appear at space boundaries)
            parts.append(r"(?:\s|<[^>]*>|\d)+")
            i += 1
        elif ch == "'":
            # Match straight or curly apostrophe
            parts.append(r"['\u2018\u2019]")
            i += 1
        elif ch == '"':
            # Match straight or curly quotes
            parts.append(r'["\u201c\u201d]')
            i += 1
        elif ch == '-':
            # Match optional space/tags, then hyphen/en-dash/em-dash, then optional space/tags
            parts.append(ws_tag_gap + r"[\-\u2013\u2014]" + ws_tag_gap)
            i += 1
        elif ch == '[':
            # Match opening bracket + optional whitespace/tags
            parts.append(r"\[" + ws_tag_gap)
            i += 1
        elif ch == ']':
            # Match optional whitespace/tags/digits + closing bracket + optional whitespace/tags
            parts.append(r"(?:\s|<[^>]*>|\d)*\]" + ws_tag_gap)
            i += 1
        elif ch == '(':
            # Match opening paren + optional whitespace/tags
            parts.append(r"\(" + ws_tag_gap)
            i += 1
        elif ch == ')':
            # Match optional whitespace/tags/digits + closing paren (handles stripped footnote years)
            parts.append(r"(?:\s|<[^>]*>|\d)*\)")
            i += 1
        elif ch in r'\.^$*+?{}|':
            parts.append(re.escape(ch))
            i += 1
        else:
            # Regular character - collect a run of plain chars for efficiency
            run = [ch]
            j = i + 1
            while j < len(text) and text[j] not in " '\"-[]()}" and text[j] not in r'\.^$*+?{}|' and not text[j].isdigit():
                run.append(text[j])
                j += 1
            if ch.isdigit():
                # Digit sequence: collect all consecutive digits, allow matching any digit run
                # (handles footnote numbers stripped during normalization: "1" matches "1980")
                while j < len(text) and text[j].isdigit():
                    j += 1
                parts.append(tag_gap + r"\d+" + tag_gap)
            else:
                # Allow optional tags between characters in the run
                escaped = [re.escape(c) for c in run]
                parts.append(tag_gap.join(escaped))
            i = j
    
    return "".join(parts)


def main():
    parser = argparse.ArgumentParser(
        description="Convert Kindle highlights to Foliate-compatible annotations",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s book.epub book.html
  %(prog)s mybook.epub mybook.html -o mybook_annotations.json
        """
    )
    parser.add_argument("epub", help="Input EPUB file")
    parser.add_argument("html", help="Kindle notebook HTML file")
    parser.add_argument(
        "-o", "--output",
        help="Output JSON file (default: <epub_name>_annotations.json)"
    )
    parser.add_argument(
        "-e", "--epub-output",
        help="Output highlighted EPUB file (default: <epub_name>_highlighted.epub)"
    )
    
    args = parser.parse_args()
    
    # Generate default output filenames based on input EPUB name
    epub_base = os.path.splitext(args.epub)[0]
    output_json = args.output or f"{epub_base}_annotations.json"
    output_epub = args.epub_output or f"{epub_base}_highlighted.epub"
    
    # Check input files exist
    if not os.path.exists(args.epub):
        print(f"Error: EPUB file not found: {args.epub}")
        return 1
    if not os.path.exists(args.html):
        print(f"Error: HTML file not found: {args.html}")
        return 1
    
    print(f"Reading highlights from: {args.html}")
    highlights = extract_highlights(args.html)
    
    print(f"Reading EPUB: {args.epub}")
    book = epub.read_epub(args.epub)
    annotations = locate_highlights(book, highlights)

    # Foliate expects top-level metadata/progress/lastLocation/annotations
    dc_meta = book.metadata.get("http://purl.org/dc/elements/1.1/", {})
    title_list = dc_meta.get("title", [])
    metadata = {
        "identifier": dc_meta.get("identifier", [("", {})])[0][0],
        "title": title_list[0][0] if title_list else "Unknown Title",
        "subtitle": title_list[1][0] if len(title_list) > 1 else "",
        "language": dc_meta.get("language", [("", {})])[0][0],
        "publisher": dc_meta.get("publisher", [("", {})])[0][0],
        "published": dc_meta.get("date", [("", {})])[0][0],
        "modified": datetime.now(timezone.utc).isoformat(),
        "rights": dc_meta.get("rights", [("", {})])[0][0],
        "author": {
            "name": dc_meta.get("creator", [("Unknown", {})])[0][0],
            "role": "aut"
        }
    }

    progress = [0, len(book.spine)]
    last_location = annotations[0]["value"] if annotations else ""

    foliate_payload = {
        "metadata": metadata,
        "progress": progress,
        "lastLocation": last_location,
        "annotations": annotations,
    }

    print(f"Writing annotations to: {output_json}")
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(foliate_payload, f, indent=2, ensure_ascii=False)

    print(f"Annotations generated: {len(annotations)}")

    # Also produce an EPUB with inline <mark> tags so highlights are visible
    # even if CFIs are imperfect.
    # We copy the original EPUB and only modify xhtml files in-place to
    # preserve the original structure, TOC, and formatting.
    print(f"Creating highlighted EPUB: {output_epub}")
    try:
        import shutil
        import zipfile
        
        # Copy original EPUB
        shutil.copy2(args.epub, output_epub)
        
        # Build spine href → file path mapping from the original book
        xhtml_paths = {}
        for item in book.get_items():
            if item.get_type() == ebooklib.ITEM_DOCUMENT:
                xhtml_paths[item.file_name] = item
        
        # Build a list of highlights with CSS colors
        # Sort by length descending to avoid marking substrings first
        matched_highlights = [(ann["text"], ann["color"]) for ann in annotations]
        matched_highlights.sort(key=lambda x: len(x[0]), reverse=True)
        
        # Pre-compile regex patterns for each highlight
        highlight_patterns = []
        for text, color in matched_highlights:
            color_code = CSS_COLOR.get(color, "#fff59d")
            pattern_str = _build_html_pattern(text)
            try:
                pat = re.compile(pattern_str, re.DOTALL)
                highlight_patterns.append((pat, color_code, text))
            except re.error:
                pass
        
        # Find the actual paths inside the zip (may have OEBPS/ or EPUB/ prefix)
        inserted = 0
        with zipfile.ZipFile(output_epub, 'r') as zin:
            zip_names = zin.namelist()
        
        # Map spine file_name to actual zip path
        zip_path_map = {}
        for fname in xhtml_paths:
            for zn in zip_names:
                if zn.endswith(fname):
                    zip_path_map[fname] = zn
                    break
        
        # Read, modify, and rewrite each xhtml file
        modified_files = {}
        inserted_texts = set()
        with zipfile.ZipFile(output_epub, 'r') as zin:
            for fname, zpath in zip_path_map.items():
                html = zin.read(zpath).decode('utf-8')
                changed = False
                for pat, color_code, text in highlight_patterns:
                    m = pat.search(html)
                    if m:
                        matched_html = m.group(0)
                        replacement = f'<mark style="background:{color_code}">{matched_html}</mark>'
                        html = html[:m.start()] + replacement + html[m.end():]
                        inserted += 1
                        inserted_texts.add(text)
                        changed = True
                if changed:
                    modified_files[zpath] = html.encode('utf-8')
        
        # Rewrite the zip with modified files
        if modified_files:
            import tempfile
            tmp_path = output_epub + '.tmp'
            with zipfile.ZipFile(output_epub, 'r') as zin:
                with zipfile.ZipFile(tmp_path, 'w') as zout:
                    for item in zin.infolist():
                        if item.filename in modified_files:
                            zout.writestr(item, modified_files[item.filename])
                        else:
                            zout.writestr(item, zin.read(item.filename))
            os.replace(tmp_path, output_epub)
        
        print(f"Inline highlights inserted: {inserted}")
        
        # Report annotations not inserted inline
        not_inserted = [text for text, color in matched_highlights if text not in inserted_texts]
        if not_inserted:
            print(f"\nWarning: {len(not_inserted)} annotations not inserted inline:")
            for txt in not_inserted:
                print(f"  - {txt[:80]}..." if len(txt) > 80 else f"  - {txt}")
        
        print(f"\nSuccess! Created:")
        print(f"  - {output_json} (import into Foliate)")
        print(f"  - {output_epub} (open directly in Foliate with visible highlights)")
    except Exception as exc:  # best-effort; don't block JSON output
        import traceback
        print(f"Warning: Inline EPUB generation failed: {exc}")
        traceback.print_exc()
        print(f"\nCreated: {output_json}")
    
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
