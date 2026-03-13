"""
Academic Writing Assistant Bot
────────────────────────────────────────────────────────────────
Features:
  • Reads ANTHROPIC_API_KEY and MAILTO from .env
  • Accepts input as: plain text file (.txt), image (.png/.jpg/.webp), or inline text
  • Accepts an instructions file or inline flags to set:
      - Citation style  (apa | mla | chicago)
      - Pages required  (target word count derived: ~275 words/page)
      - Number of sources required
      - Humanize flag
      - Paper metadata (title, author, institution, course, instructor)
  • Finds real sources via CrossRef API
  • Verifies credibility with Claude
  • Formats citations exactly per style rules
  • Writes a fully-formatted Word document

Usage examples:
  python academic_bot.py --input essay.txt
  python academic_bot.py --input essay.txt --instructions my_instructions.txt
  python academic_bot.py --input photo.jpg
  python academic_bot.py --input essay.txt --style apa --pages 5 --sources 8 --humanize \\
      --title "Climate Change" --author "Jane Smith" --institution "UoN" \\
      --course "ENSC 301" --instructor "Prof. Kamau" --output my_paper.docx
"""

import os
import sys
import json
import re
import time
import base64
import argparse
import subprocess
import textwrap
from datetime import date
from pathlib import Path
from typing import Optional

import anthropic
import requests
from dotenv import load_dotenv

# ─────────────────────────────────────────────
# LOAD .env
# ─────────────────────────────────────────────
load_dotenv()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MAILTO            = os.getenv("MAILTO", "user@example.com")

if not ANTHROPIC_API_KEY:
    sys.exit("❌  ANTHROPIC_API_KEY not found. Add it to your .env file.")

CROSSREF_API = "https://api.crossref.org/works"
HEADERS      = {"User-Agent": f"AcademicBot/2.0 (mailto:{MAILTO})"}

WORDS_PER_PAGE = 275


# ══════════════════════════════════════════════
# SECTION 1 — INPUT PARSING
# ══════════════════════════════════════════════

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MIME_MAP   = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".png": "image/png",  ".webp": "image/webp", ".gif": "image/gif"}


def load_input(path: str, client: anthropic.Anthropic) -> str:
    """Load text from a .txt file OR extract text from an image using Claude vision."""
    p = Path(path)
    if not p.exists():
        sys.exit(f"❌  Input file not found: {path}")

    ext = p.suffix.lower()

    if ext in IMAGE_EXTS:
        print(f"🖼️  Image input detected — extracting text with Claude vision...")
        mime = MIME_MAP.get(ext, "image/jpeg")
        with open(p, "rb") as f:
            b64 = base64.standard_b64encode(f.read()).decode("utf-8")

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": mime, "data": b64}
                    },
                    {
                        "type": "text",
                        "text": (
                            "Transcribe ALL text visible in this image exactly as written. "
                            "Preserve paragraph breaks. If this is a handwritten document, "
                            "transcribe it faithfully. Return ONLY the transcribed text, "
                            "no commentary or explanation."
                        )
                    }
                ]
            }]
        )
        text = response.content[0].text.strip()
        print(f"   Extracted {len(text.split())} words from image.")
        return text

    elif ext == ".txt":
        return p.read_text(encoding="utf-8")

    else:
        sys.exit(f"❌  Unsupported file type: {ext}. Use .txt, .jpg, .jpeg, .png, or .webp")


# ══════════════════════════════════════════════
# SECTION 2 — INSTRUCTIONS PARSING
# ══════════════════════════════════════════════

INSTRUCTION_DEFAULTS = {
    "style":       "apa",
    "pages":       None,
    "sources":     None,
    "humanize":    False,
    "title":       "Academic Paper",
    "author":      "Author Name",
    "institution": "Institution Name",
    "course":      "",
    "instructor":  "",
    "output":      "output.docx",
}

_PATTERNS = [
    (r'\bstyle\s*[:\-=]\s*(\w+)',                "style"),
    (r'\bformat\s*[:\-=]\s*(\w+)',               "style"),
    (r'\bcitation\s+style\s*[:\-=]\s*(\w+)',     "style"),
    (r'\b(\d+)\s+pages?\b',                      "pages"),
    (r'\bpages?\s*[:\-=]\s*(\d+)',               "pages"),
    (r'\bword\s+count\s*[:\-=]\s*(\d+)',         "word_count"),
    (r'\b(\d+)\s+sources?\b',                    "sources"),
    (r'\bsources?\s*[:\-=]\s*(\d+)',             "sources"),
    (r'\breferences?\s*[:\-=]\s*(\d+)',          "sources"),
    (r'\bhumanize[:\-=\s]*(yes|true|1)?\b',     "humanize"),
    (r'\btitle\s*[:\-=]\s*(.+)',                 "title"),
    (r'\bauthor\s*[:\-=]\s*(.+)',                "author"),
    (r'\bname\s*[:\-=]\s*(.+)',                  "author"),
    (r'\binstitution\s*[:\-=]\s*(.+)',           "institution"),
    (r'\buniversity\s*[:\-=]\s*(.+)',            "institution"),
    (r'\bcourse\s*[:\-=]\s*(.+)',                "course"),
    (r'\bclass\s*[:\-=]\s*(.+)',                 "course"),
    (r'\binstructor\s*[:\-=]\s*(.+)',            "instructor"),
    (r'\bprofessor\s*[:\-=]\s*(.+)',             "instructor"),
    (r'\boutput\s*[:\-=]\s*(\S+\.docx)',         "output"),
]


def parse_instructions_file(path: str) -> dict:
    """Parse a plain-English instructions .txt file into a settings dict."""
    orig_text = Path(path).read_text(encoding="utf-8")
    lower     = orig_text.lower()
    settings  = {}

    for pattern, key in _PATTERNS:
        m = re.search(pattern, lower, re.IGNORECASE)
        if not m:
            continue
        val = m.group(1).strip() if m.lastindex else True

        if key == "style":
            val = val.lower()
            if "chicago" in val:   val = "chicago"
            elif "mla" in val:     val = "mla"
            else:                  val = "apa"
            settings["style"] = val

        elif key == "pages":
            try:    settings["pages"] = int(val)
            except: pass

        elif key == "word_count":
            try:    settings["pages"] = max(1, int(val) // WORDS_PER_PAGE)
            except: pass

        elif key == "sources":
            try:    settings["sources"] = int(val)
            except: pass

        elif key == "humanize":
            settings["humanize"] = True

        elif key in ("title", "author", "institution", "course", "instructor", "output"):
            # Restore original casing
            orig_m = re.search(pattern, orig_text, re.IGNORECASE)
            settings[key] = orig_m.group(1).strip() if orig_m and orig_m.lastindex else str(val)

    return settings


def merge_settings(file_settings: dict, cli_overrides: dict) -> dict:
    result = dict(INSTRUCTION_DEFAULTS)
    result.update({k: v for k, v in file_settings.items() if v is not None})
    result.update({k: v for k, v in cli_overrides.items() if v is not None})
    return result


def print_settings(s: dict):
    print(f"\n{'─'*55}")
    print(f"  Settings")
    print(f"{'─'*55}")
    print(f"  Style       : {s['style'].upper()}")
    print(f"  Pages       : {s['pages'] or 'no target'}")
    print(f"  Sources     : {s['sources'] or 'as many as needed'}")
    print(f"  Humanize    : {'yes' if s['humanize'] else 'no'}")
    print(f"  Title       : {s['title']}")
    print(f"  Author      : {s['author']}")
    print(f"  Institution : {s['institution']}")
    if s['course']:     print(f"  Course      : {s['course']}")
    if s['instructor']: print(f"  Instructor  : {s['instructor']}")
    print(f"  Output      : {s['output']}")
    print(f"{'─'*55}")


# ══════════════════════════════════════════════
# SECTION 3 — TEXT EXPANSION (page target)
# ══════════════════════════════════════════════

def expand_text_to_pages(text: str, pages: int, style: str, client: anthropic.Anthropic) -> str:
    """If a page target is set and the text is shorter, ask Claude to expand it."""
    target_words  = pages * WORDS_PER_PAGE
    current_words = len(text.split())
    if current_words >= target_words * 0.9:
        return text

    print(f"\n📝 Expanding text to ~{target_words} words ({pages} pages)...")
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8000,
        messages=[{
            "role": "user",
            "content": f"""You are an academic writer. Expand the following text to approximately {target_words} words 
while maintaining the exact same topic, argument, and academic tone ({style.upper()} style).

Rules:
- Keep all original sentences — only ADD content, never remove or change existing text
- Add supporting details, explanations, examples, and transitions between ideas
- Do not invent citations — leave placeholders where more evidence is needed
- Maintain consistent academic register throughout
- Return ONLY the expanded text, no commentary

Current text ({current_words} words):
{text}"""
        }]
    )
    expanded  = response.content[0].text.strip()
    new_count = len(expanded.split())
    print(f"   Expanded: {current_words} → {new_count} words (~{new_count // WORDS_PER_PAGE} pages)")
    return expanded


# ══════════════════════════════════════════════
# SECTION 4 — CITATION PIPELINE
# ══════════════════════════════════════════════

def safe_parse_json(raw: str) -> dict | list:
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw   = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = re.search(r'(\{[\s\S]*\}|\[[\s\S]*\])', raw)
    if m:
        try:   return json.loads(m.group(1))
        except: pass
    cleaned = re.sub(r'(?<=[^\w])\*{1,2}(?=[^\w])|(?<=[^\w])\*{1,2}$', '', raw)
    try:   return json.loads(cleaned)
    except: pass
    raise ValueError(f"Could not parse JSON:\n{raw[:300]}")


def extract_claims(text: str, sources_needed: Optional[int], client: anthropic.Anthropic) -> list[dict]:
    print("\n📖 Step 1: Extracting claims that need citations...")
    limit_note = f"\nIdentify AT LEAST {sources_needed} claims." if sources_needed else ""
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": f"""You are an academic writing assistant.

Read the following text and identify every factual or empirical claim that requires an academic citation.
For each claim, create a short search query (5-8 words) suitable for Google Scholar.{limit_note}

Return ONLY a JSON array — no markdown, no explanation:
[
  {{"claim": "original sentence from text", "query": "search query for scholar"}},
  ...
]

TEXT:
{text}"""
        }]
    )
    claims = safe_parse_json(response.content[0].text.strip())
    print(f"   Found {len(claims)} claims to verify.")
    return claims


def search_crossref(query: str, max_results: int = 5) -> list[dict]:
    try:
        resp = requests.get(
            CROSSREF_API,
            params={
                "query":  query,
                "rows":   max_results,
                "select": "DOI,title,author,published,container-title,volume,issue,page,publisher,type"
            },
            headers=HEADERS,
            timeout=10
        )
        resp.raise_for_status()
        results = []
        for item in resp.json().get("message", {}).get("items", []):
            authors     = item.get("author", [])
            author_list = [f"{a.get('family','')} {a.get('given','')[:1]}".strip() for a in authors[:6]]
            date_parts  = item.get("published", {}).get("date-parts", [[None]])[0]
            year        = date_parts[0] if date_parts else None
            results.append({
                "doi":       item.get("DOI", ""),
                "title":     (item.get("title") or [""])[0],
                "authors":   author_list,
                "year":      year,
                "journal":   (item.get("container-title") or [""])[0],
                "volume":    item.get("volume"),
                "issue":     item.get("issue"),
                "pages":     item.get("page"),
                "publisher": item.get("publisher", ""),
                "type":      item.get("type", "journal-article"),
                "url":       f"https://doi.org/{item['DOI']}" if item.get("DOI") else ""
            })
        return results
    except Exception as e:
        print(f"   ⚠️  CrossRef error for '{query}': {e}")
        return []


def verify_sources(claim: str, sources: list[dict], client: anthropic.Anthropic) -> Optional[dict]:
    if not sources:
        return None
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": f"""Pick the BEST academic source for this claim.

Claim: "{claim}"

Candidates:
{json.dumps(sources, indent=2)}

Rank by: (1) relevance, (2) peer-reviewed journal preferred, (3) recency, (4) reputable publisher.

Return ONLY JSON — no markdown:
{{"chosen_index": <int>, "reason": "<one sentence>"}}"""
        }]
    )
    result  = safe_parse_json(response.content[0].text.strip())
    idx     = result.get("chosen_index", 0)
    chosen  = sources[idx] if 0 <= idx < len(sources) else sources[0]
    chosen["verification_reason"] = result.get("reason", "")
    return chosen


def format_citation(source: dict, style: str, client: anthropic.Anthropic) -> dict:
    style_rules = {
        "apa":     "APA 7th edition: Author, A. A., & Author, B. B. (Year). Title of article. Journal Name, volume(issue), page-page. https://doi.org/xxxxx",
        "mla":     "MLA 9th edition: Author Last, First. \"Article Title.\" Journal Name, vol. X, no. Y, Year, pp. XX-XX. DOI.",
        "chicago": "Chicago 17th (author-date): Author Last, First. Year. \"Article Title.\" Journal Name volume (issue): pages. https://doi.org/xxxxx"
    }
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=600,
        messages=[{
            "role": "user",
            "content": f"""Format this source as a citation.

Style: {style_rules.get(style, style_rules['apa'])}

Source:
{json.dumps(source, indent=2)}

Return ONLY valid JSON — no markdown, no asterisks for italics, escape internal quotes with backslash:
{{"reference_list": "<full reference in plain text>", "in_text": "<e.g. (Smith & Jones, 2021)>"}}

Rules:
- Use "et al." after 6+ authors (APA) or 1st author only (MLA/Chicago)
- Omit missing fields cleanly — do not write n.d. unless year is truly unknown
- DOIs formatted as https://doi.org/xxxxx"""
        }]
    )
    raw = response.content[0].text.strip()
    try:
        return safe_parse_json(raw)
    except ValueError:
        authors = source.get("authors", ["Unknown"])
        year    = source.get("year", "n.d.")
        title   = source.get("title", "Untitled")
        journal = source.get("journal", "")
        doi     = source.get("url", "")
        first   = authors[0].split()[0] if authors else "Unknown"
        et_al   = " et al." if len(authors) > 1 else ""
        return {
            "reference_list": f"{', '.join(authors[:6])} ({year}). {title}. {journal}. {doi}".strip(". "),
            "in_text":        f"({first}{et_al}, {year})"
        }


def humanize_text(text: str, client: anthropic.Anthropic) -> str:
    print("\n✍️  Humanizing text...")
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8000,
        messages=[{
            "role": "user",
            "content": f"""You are an academic editor. Rewrite this text to:
- Sound natural and human-written (vary sentence length, avoid robotic phrasing)
- Maintain formal academic tone
- Preserve ALL in-text citations EXACTLY as written — do not move, change, or remove them
- Keep all facts identical; do not add new claims

Return ONLY the rewritten text, no commentary.

TEXT:
{text}"""
        }]
    )
    return response.content[0].text.strip()


# ══════════════════════════════════════════════
# SECTION 5 — WORD DOCUMENT GENERATION
# ══════════════════════════════════════════════

def write_docx(final_text: str, references: list[str], settings: dict):
    raw_paragraphs = [p.strip() for p in final_text.split('\n') if p.strip()]
    payload = {
        "title":       settings["title"],
        "author":      settings["author"],
        "institution": settings["institution"],
        "course":      settings.get("course", ""),
        "instructor":  settings.get("instructor", ""),
        "date":        date.today().strftime("%B %d, %Y"),
        "paragraphs":  raw_paragraphs,
        "references":  references,
        "style":       settings["style"],
        "output_path": settings["output"],
    }
    script = Path(__file__).parent / "generate_apa_docx.js"
    result = subprocess.run(
        ["node", str(script)],
        input=json.dumps(payload),
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"   ⚠️  docx error:\n{result.stderr}")
    else:
        print(result.stdout.strip())


# ══════════════════════════════════════════════
# SECTION 6 — MAIN PIPELINE
# ══════════════════════════════════════════════

def run_pipeline(input_path: str, settings: dict):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    print(f"\n{'='*55}")
    print(f"  Academic Writing Assistant  |  Style: {settings['style'].upper()}")
    print(f"{'='*55}")
    print_settings(settings)

    text = load_input(input_path, client)
    print(f"\n   Input: {len(text.split())} words loaded.")

    if settings["pages"]:
        text = expand_text_to_pages(text, settings["pages"], settings["style"], client)

    claims = extract_claims(text, settings["sources"], client)
    if not claims:
        print("No citeable claims found.")
        return

    print("\n🔍 Step 2-4: Searching, verifying, and formatting citations...")
    citation_map   = {}
    references     = []
    sources_so_far = 0
    sources_limit  = settings["sources"]

    for i, item in enumerate(claims, 1):
        if sources_limit and sources_so_far >= sources_limit:
            print(f"\n   Reached requested source limit ({sources_limit}). Stopping search.")
            break

        claim = item["claim"]
        query = item["query"]
        short = claim[:67] + "..." if len(claim) > 70 else claim
        print(f"\n   [{i}/{len(claims)}] \"{short}\"")
        print(f"   🔎 {query}")

        sources = search_crossref(query, max_results=5)
        if not sources:
            print("   ❌ No sources found — skipping.")
            continue

        best = verify_sources(claim, sources, client)
        if not best:
            continue

        print(f"   ✅ {best.get('title','?')[:60]}... ({best.get('year','?')})")
        print(f"      {best.get('verification_reason','')}")

        citation = format_citation(best, settings["style"], client)
        citation_map[claim] = citation
        references.append(citation["reference_list"])
        sources_so_far += 1
        time.sleep(0.5)

    # Insert in-text citations
    print("\n📎 Inserting in-text citations...")
    cited_text = text
    for claim, citation in citation_map.items():
        in_text = citation.get("in_text", "")
        if claim in cited_text and in_text:
            idx = cited_text.find(claim)
            end = idx + len(claim)
            cited_text = cited_text[:end] + f" {in_text}" + cited_text[end:]

    final_text = humanize_text(cited_text, client) if settings["humanize"] else cited_text

    # Terminal preview
    print(f"\n{'='*55}")
    print("  FINAL TEXT (preview)")
    print(f"{'='*55}")
    preview = final_text[:800] + ("\n\n[...see output .docx for full text...]" if len(final_text) > 800 else "")
    print(preview)

    if references:
        print(f"\n{'='*55}")
        print(f"  REFERENCES ({settings['style'].upper()}) — {len(set(references))} sources")
        print(f"{'='*55}")
        seen = set()
        for i, ref in enumerate(references, 1):
            if ref not in seen:
                print(f"{i}. {ref}\n")
                seen.add(ref)

    print(f"\n📄 Writing Word document → {settings['output']}")
    write_docx(final_text, references, settings)

    return {"text": final_text, "references": references}


# ══════════════════════════════════════════════
# SECTION 7 — CLI
# ══════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Academic Writing Assistant",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
        Examples:
          python academic_bot.py --input essay.txt
          python academic_bot.py --input photo.jpg --instructions brief.txt
          python academic_bot.py --input essay.txt --style apa --pages 5 --sources 8 --humanize
          python academic_bot.py --input essay.txt --instructions inst.txt --author "Jane Smith"
        """)
    )

    parser.add_argument("--input",        required=True, help=".txt OR image (.jpg/.png/.webp)")
    parser.add_argument("--instructions", type=str,      help="Plain-English instructions .txt file")

    parser.add_argument("--style",       choices=["apa","mla","chicago"], help="Citation style")
    parser.add_argument("--pages",       type=int,  help="Target page count (~275 words/page)")
    parser.add_argument("--sources",     type=int,  help="Number of sources required")
    parser.add_argument("--humanize",    action="store_true", default=None, help="Rewrite to sound natural")
    parser.add_argument("--title",       type=str,  help="Paper title")
    parser.add_argument("--author",      type=str,  help="Author name")
    parser.add_argument("--institution", type=str,  help="University / institution")
    parser.add_argument("--course",      type=str,  help="Course name and number")
    parser.add_argument("--instructor",  type=str,  help="Instructor name")
    parser.add_argument("--output",      type=str,  help="Output .docx filename (default: output.docx)")

    args = parser.parse_args()

    file_settings = parse_instructions_file(args.instructions) if args.instructions else {}
    cli_overrides = {k: v for k, v in vars(args).items()
                     if k not in ("input", "instructions") and v is not None}
    if "--humanize" not in sys.argv:
        cli_overrides.pop("humanize", None)

    settings = merge_settings(file_settings, cli_overrides)
    run_pipeline(args.input, settings)


if __name__ == "__main__":
    main()