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


def load_raw_text(path: str, client: anthropic.Anthropic) -> str:
    """Load raw text from a .txt file OR transcribe an image with Claude vision.
    Returns the full raw content — caller then decides what is instructions vs essay."""
    p = Path(path)
    if not p.exists():
        sys.exit(f"❌  Input file not found: {path}")

    ext = p.suffix.lower()

    if ext in IMAGE_EXTS:
        print(f"🖼️  Image detected — transcribing with Claude vision...")
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
                            "Preserve paragraph breaks and any section headers or labels. "
                            "If this is a handwritten document, transcribe it faithfully. "
                            "Return ONLY the transcribed text, no commentary."
                        )
                    }
                ]
            }]
        )
        text = response.content[0].text.strip()
        print(f"   Transcribed {len(text.split())} words from image.")
        return text

    elif ext == ".txt":
        return p.read_text(encoding="utf-8")

    else:
        sys.exit(f"❌  Unsupported file type: {ext}. Use .txt, .jpg, .jpeg, .png, or .webp")


def separate_instructions_from_content(raw: str, client: anthropic.Anthropic) -> tuple[dict, str]:
    """Use Claude to detect if the raw text mixes instructions with essay content.

    Handles cases like:
      - Student typed the assignment brief at the top of their essay file
      - Scanned assignment sheet with both the instructions and a draft
      - File starts with 'Write 5 pages in APA on...' then the essay body below
      - Image of a professor's handwritten brief + the student's draft

    Returns: (extracted_settings_dict, clean_essay_text)
    The caller merges extracted_settings with lower priority than CLI flags.
    """
    print("\n🔍 Checking input for embedded instructions...")

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": f"""You are parsing a document that may contain BOTH writing instructions/requirements AND actual essay/paper content.

Instructions/requirements look like:
- "Write a 5-page paper on...", "Research the topic of...", "Topic:"
- "Use APA format", "cite at least 8 sources", "minimum 10 references"
- "Author: Jane Smith", "Course: ENSC 301", "Student name:"
- Assignment briefs, rubrics, word count requirements, formatting rules
- Directives from a professor or assignment sheet

Essay/paper content looks like:
- Actual paragraphs making arguments or presenting factual information
- Content that would need academic citations
- Introduction, body paragraphs, conclusion sections

Analyze this document carefully and return ONLY a JSON object — no markdown:
{{
  "has_embedded_instructions": true or false,
  "essay_text": "<the actual essay/paper content only — empty string if no essay found>",
  "extracted_settings": {{
    "style":       "<apa|mla|chicago or null if not mentioned>",
    "pages":       <integer or null>,
    "sources":     <integer or null>,
    "humanize":    <true or false>,
    "title":       "<paper title or null>",
    "author":      "<student/author name or null>",
    "institution": "<university name or null>",
    "course":      "<course name/number or null>",
    "instructor":  "<instructor/professor name or null>",
    "output":      "<output filename.docx or null>"
  }},
  "summary": "<one sentence describing what was found>"
}}

DOCUMENT:
{raw}"""
        }]
    )

    result   = safe_parse_json(response.content[0].text.strip())
    has_mixed = result.get("has_embedded_instructions", False)
    essay     = result.get("essay_text", "").strip()
    extracted = result.get("extracted_settings", {})
    summary   = result.get("summary", "")

    # Drop None values so merge_settings falls back to defaults/CLI properly
    extracted = {k: v for k, v in extracted.items() if v is not None}

    if has_mixed:
        print(f"   ⚠️  Embedded instructions found: {summary}")
        if not essay:
            print("   ℹ️  No essay body found — topic/instructions only.")
            print("      Will write the essay from scratch.")
            # Store the raw text so run_pipeline can use it as the topic prompt
            extracted["_raw_instructions"] = raw
        else:
            print(f"   📄 Essay content isolated: {len(essay.split())} words.")
        if {k: v for k, v in extracted.items() if not k.startswith("_")}:
            nice = ", ".join(f"{k}={repr(v)}" for k, v in extracted.items() if not k.startswith("_"))
            print(f"   ⚙️  Settings extracted from file: {nice}")
    else:
        print("   ✅ No embedded instructions — treating entire input as essay text.")
        essay = raw

    return extracted, essay


# ══════════════════════════════════════════════
# SECTION 2 — INSTRUCTIONS PARSING
# ══════════════════════════════════════════════

INSTRUCTION_DEFAULTS = {
    "style":       "apa",
    "pages":       None,
    "sources":     None,
    "humanize":    True,
    "min_year":    None,   # None = auto (current_year - 5)
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
    from datetime import datetime
    auto_year = datetime.now().year - 5
    year_display = s.get("min_year") or f"{auto_year} (auto)"
    print(f"  Style       : {s['style'].upper()}")
    print(f"  Pages       : {s['pages'] or 'no target'}")
    print(f"  Sources     : {s['sources'] or 'as many as needed'}")
    print(f"  Sources from: {year_display}+")
    print(f"  Humanize    : {'yes' if s['humanize'] else 'no'}")
    print(f"  Title       : {s['title']}")
    print(f"  Author      : {s['author']}")
    print(f"  Institution : {s['institution']}")
    if s['course']:     print(f"  Course      : {s['course']}")
    if s['instructor']: print(f"  Instructor  : {s['instructor']}")
    print(f"  Output      : {s['output']}")
    print(f"{'─'*55}")


# ══════════════════════════════════════════════
# SECTION 3 — ESSAY WRITING
# ══════════════════════════════════════════════

def write_essay_from_scratch(topic: str, pages: int, sources: int, style: str,
                              client: anthropic.Anthropic) -> str:
    """Write a complete essay from a topic/instructions string.

    Crucially: every factual claim is written as a BARE STATEMENT with no citation
    marker at all. Citations are inserted later by the citation pipeline after real
    sources have been found. This eliminates placeholder citations entirely.
    """
    target_words = (pages or 3) * WORDS_PER_PAGE
    num_sources  = sources or 6
    print(f"\n✏️  Writing essay from scratch (~{target_words} words, {num_sources} sources needed)...")

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8000,
        messages=[{
            "role": "user",
            "content": f"""You are an academic writer. Write a complete, well-structured academic essay on the following topic.

TOPIC / INSTRUCTIONS:
{topic}

REQUIREMENTS:
- Length: approximately {target_words} words ({pages or 3} pages at 275 words/page)
- Style: {style.upper()}
- The essay needs approximately {num_sources} citeable factual or empirical claims
- Structure: introduction, clearly developed body paragraphs, conclusion
- Tone: formal academic prose

CRITICAL CITATION RULE — read carefully:
- Do NOT write any citation markers, brackets, or placeholders anywhere in the text
- Do NOT write things like (Author, Year), [citation needed], (placeholder), or any reference markers
- Write EVERY factual claim as a clean, confident statement with NO marker attached
- Example of what to write: "Adolescents who use social media for more than three hours daily show elevated rates of depression."
- Example of what NOT to write: "Adolescents who use social media for more than three hours daily show elevated rates of depression (Smith, 2021)." or "...depression [citation needed]."
- Citations will be inserted automatically after real sources are found — your job is only to write the claims cleanly

Return ONLY the essay text. No title, no author line, no references section — just the body paragraphs."""
        }]
    )
    essay = response.content[0].text.strip()
    # Safety: strip any placeholder patterns the model may have snuck in anyway
    essay = _strip_placeholders(essay)
    word_count = len(essay.split())
    print(f"   ✅ Essay written: {word_count} words (~{word_count // WORDS_PER_PAGE} pages)")
    return essay


def expand_existing_essay(text: str, pages: int, style: str, client: anthropic.Anthropic) -> str:
    """Expand an existing essay draft to hit a page target.
    Never writes placeholder citations — any new content is written claim-only."""
    target_words  = pages * WORDS_PER_PAGE
    current_words = len(text.split())
    if current_words >= target_words * 0.9:
        return text

    print(f"\n📝 Expanding essay to ~{target_words} words ({pages} pages)...")
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8000,
        messages=[{
            "role": "user",
            "content": f"""You are an academic writer. Expand the following essay to approximately {target_words} words.

Rules:
- Keep all original sentences — only ADD content, never remove or reword existing text
- Add supporting details, explanations, examples, and transitions
- CRITICAL: Do NOT write any citation markers in new content — no (Author, Year), no [citation needed],
  no placeholders of any kind. Write all new factual claims as bare statements only.
  Citations already in the text like (Smith, 2021) must be preserved exactly.
- Maintain the same academic register throughout
- Return ONLY the expanded text, no commentary

Current essay ({current_words} words):
{text}"""
        }]
    )
    expanded  = response.content[0].text.strip()
    expanded  = _strip_placeholders(expanded)
    new_count = len(expanded.split())
    print(f"   Expanded: {current_words} → {new_count} words (~{new_count // WORDS_PER_PAGE} pages)")
    return expanded


def _strip_placeholders(text: str) -> str:
    """Remove any placeholder citation patterns the model may have written."""
    patterns = [
        r'\(placeholder[^)]*\)',
        r'\[placeholder[^\]]*\]',
        r'\(citation needed\)',
        r'\[citation needed\]',
        r'\(citation\)',
        r'\[citation\]',
        r'\(source needed\)',
        r'\[source needed\]',
        r'\(insert citation\)',
        r'\[insert citation\]',
        r'\(needs citation\)',
        r'\[needs citation\]',
    ]
    for p in patterns:
        text = re.sub(p, '', text, flags=re.IGNORECASE)
    # Clean up any double spaces left behind
    text = re.sub(r'  +', ' ', text)
    text = re.sub(r' ([.,;])', r'\1', text)
    return text.strip()


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


def search_crossref(query: str, max_results: int = 8, min_year: int = None) -> list[dict]:
    """Search CrossRef. Fetches extra results then sorts by recency so recent papers surface first.
    min_year: if set, filters out anything older. Defaults to (current_year - 5)."""
    from datetime import datetime
    cutoff = min_year if min_year else (datetime.now().year - 5)
    try:
        resp = requests.get(
            CROSSREF_API,
            params={
                "query":  query,
                "rows":   max_results,
                "select": "DOI,title,author,published,container-title,volume,issue,page,publisher,type",
                "filter": f"from-pub-date:{cutoff}"   # CrossRef native date filter
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
            # Skip items with no year or below cutoff (belt-and-suspenders)
            if year and year < cutoff:
                continue
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
        # Sort: newest first
        results.sort(key=lambda x: x.get("year") or 0, reverse=True)

        # Fallback: if the date filter returned nothing, retry without it
        if not results:
            resp2 = requests.get(
                CROSSREF_API,
                params={"query": query, "rows": 5,
                        "select": "DOI,title,author,published,container-title,volume,issue,page,publisher,type"},
                headers=HEADERS, timeout=10
            )
            resp2.raise_for_status()
            for item in resp2.json().get("message", {}).get("items", []):
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
            if results:
                print(f"   ⚠️  No sources from {cutoff}+ found — using best available (oldest: {min(r['year'] or 9999 for r in results)})")
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


def check_ai_probability(text: str) -> Optional[float]:
    """Returns the probability (0-100) that the text is AI-generated, or None if the API fails."""
    try:
        # We take the first 500 words to avoid payload size limits
        preview = " ".join(text.split()[:500])
        response = requests.post(
            "https://api-inference.huggingface.co/models/roberta-base-openai-detector",
            json={"inputs": preview, "options": {"wait_for_model": True}},
            timeout=15
        )
        if response.status_code == 200:
            results = response.json()
            if isinstance(results, list) and isinstance(results[0], list):
                for score_dict in results[0]:
                    if score_dict.get("label") == "Fake":
                        return score_dict.get("score", 0.0) * 100
            elif isinstance(results, list) and isinstance(results[0], dict):
                for score_dict in results:
                    if score_dict.get("label") == "Fake":
                        return score_dict.get("score", 0.0) * 100
    except Exception as e:
        print(f"   ⚠️  AI Detection API error: {e}")
    return None


def zerogpt_bypass(text: str) -> str:
    """Inject zero-width spaces into random long words to break AI detection tokenization."""
    import random
    zws = '\u200B'
    words = text.split(' ')
    bypassed_words = []
    for word in words:
        if len(word) > 4 and random.random() < 0.4:
            idx = len(word) // 2
            word = word[:idx] + zws + word[idx:]
        bypassed_words.append(word)
    return ' '.join(bypassed_words)


def aggressive_humanize_pass(text: str, client: anthropic.Anthropic) -> str:
    """Pass targeting burstiness, perplexity, and AI-predictability."""
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8000,
        messages=[{
            "role": "user",
            "content": f"""The following academic text still reads too much like AI. We need to introduce burstiness and perplexity.

RULES:
1. Preserve ALL in-text citations exactly — (Author, Year) tags must not change.
2. Keep all factual content identical.
3. ENFORCE BURSTINESS: Mix extremely short sentences (3-5 words) with much longer, complex ones. Do not let sentence lengths remain uniform.
4. INJECT PERPLEXITY: Replace highly predictable word choices with less common, academically valid synonyms.
5. ADD MINOR IMPERFECTIONS: Introduce occasional slight conversational pivots or slightly less formal phrasing that real students use.
6. Return ONLY the rewritten text — no commentary.

TEXT:
{text}"""
        }]
    )
    return response.content[0].text.strip()


def humanize_text(text: str, client: anthropic.Anthropic) -> str:
    """Multi-pass humanization targeting the specific patterns AI detectors flag.

    Pass 1 — Structural disruption: break the AI essay formula
    Pass 2 — Lexical variation: replace AI-preferred words and phrases
    Pass 3 — Rhythm and flow: vary sentence length and add natural imperfections
    """
    print("\n✍️  Humanizing — Pass 1/3: Breaking AI structure patterns...")

    # ── Pass 1: Structural disruption ────────────────
    # AI text has a rigid formula: topic sentence → evidence → analysis → transition.
    # Break this by splitting paragraphs, merging short ones, and adding conversational
    # asides and qualifications that real writers use.
    p1 = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8000,
        messages=[{
            "role": "user",
            "content": f"""You are rewriting academic text to evade AI detection. Your goal is to disrupt the predictable AI essay structure.

RULES — follow every one precisely:
1. Preserve ALL in-text citations exactly — do not move or remove (Author, Year) tags
2. Keep every factual claim — do not add or remove information
3. Break the "topic sentence → evidence → analysis → transition" formula:
   - Split long structured paragraphs into two shorter ones occasionally
   - Merge a short paragraph into the one before or after it occasionally
   - Start some paragraphs mid-thought rather than with a clean topic sentence
   - End some paragraphs abruptly without a tidy conclusion sentence
4. Remove ALL of these AI-signature transition phrases (replace with nothing or something more direct):
   "Furthermore", "Moreover", "Additionally", "It is important to note",
   "It is worth noting", "In conclusion", "In summary", "This highlights",
   "This underscores", "This demonstrates", "Notably", "Significantly",
   "It is evident that", "As previously mentioned", "In light of this"
5. Add occasional hedging that real writers use: "at least in part", "to some degree",
   "the evidence suggests", "arguably", "it seems", "in many cases"
6. Return ONLY the rewritten text — no commentary, no labels

TEXT:
{text}"""
        }]
    ).content[0].text.strip()

    print("   ✍️  Humanizing — Pass 2/3: Replacing AI vocabulary...")

    # ── Pass 2: Lexical substitution ─────────────────
    # Claude has preferred words it reaches for automatically. Replace them with
    # the more varied, less-polished vocabulary real students use.
    p2 = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8000,
        messages=[{
            "role": "user",
            "content": f"""You are editing academic text to make it sound like a real student wrote it, not an AI.

RULES:
1. Preserve ALL in-text citations exactly — (Author, Year) tags must not change
2. Keep all factual content identical
3. Replace these overused AI words with more natural alternatives (do not use the same replacement every time — vary them):
   - "utilize" → use, apply, employ, work with
   - "leverage" → use, draw on, rely on, take advantage of
   - "crucial" / "vital" / "paramount" → important, key, central, necessary
   - "delve" → look at, examine, explore, dig into
   - "underscore" / "highlight" → show, suggest, point to, make clear
   - "multifaceted" → complex, varied, multi-layered
   - "robust" → strong, reliable, solid, substantial
   - "holistic" → broad, overall, comprehensive, wide-ranging
   - "facilitate" → help, support, make possible, enable
   - "demonstrate" → show, indicate, reveal, suggest
   - "comprehensive" → thorough, detailed, wide-ranging, broad
   - "implications" → effects, consequences, meaning, what this means
4. Where a sentence uses passive voice unnecessarily, switch to active
5. Replace all em-dashes (—) used as clause separators with commas or restructured sentences
6. Return ONLY the rewritten text — no commentary

TEXT:
{p1}"""
        }]
    ).content[0].text.strip()

    print("   ✍️  Humanizing — Pass 3/3: Varying rhythm and sentence length...")

    # ── Pass 3: Rhythm variation ──────────────────────
    # AI text has suspiciously even sentence lengths. Real writing mixes very short
    # punchy sentences with longer ones. Also add minor authentic imperfections.
    p3 = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8000,
        messages=[{
            "role": "user",
            "content": f"""Final editing pass on this academic text. Your job is sentence rhythm and natural flow.

RULES:
1. Preserve ALL in-text citations exactly — (Author, Year) tags must not change
2. Keep all factual content identical
3. Vary sentence length aggressively:
   - After 2-3 long sentences, add one very short sentence (under 10 words) that makes a sharp point
   - Break some long compound sentences into two shorter ones
   - Occasionally combine two short sentences into one flowing one
4. Add natural connective tissue that students actually write:
   - "That said, ..." / "Even so, ..." / "Still, ..." (instead of "However")
   - "This matters because..." / "The reason is simple:" (instead of "This is significant because")
   - "Put differently, ..." (instead of "In other words")
   - "What this means in practice is..." (instead of "Therefore")
5. Ensure the first sentence of the paper does NOT start with "In recent years", "Over the past",
   or any other classic AI essay opener — rewrite it if needed
6. The final paragraph should NOT start with "In conclusion" or "In summary" — remove or rephrase
7. Return ONLY the final rewritten text — no commentary, no labels

TEXT:
{p2}"""
        }]
    ).content[0].text.strip()
    current_text = p3
    max_retries = 3

    print("\n🧐  Step: Verifying AI Detectability...")
    for attempt in range(max_retries):
        ai_prob = check_ai_probability(current_text)
        if ai_prob is None:
            print("   ⚠️  AI Detection service unavailable. Applying aggressive humanization pass to ensure safety...")
            current_text = aggressive_humanize_pass(current_text, client)
            break

        print(f"   📊 AI Probability: {ai_prob:.1f}%")

        if ai_prob <= 20.0:
            print("   ✅ Text passes humanization threshold!")
            break

        print(f"   🔄 AI signature too high. Running aggressive humanization pass (Attempt {attempt + 1}/{max_retries})...")
        current_text = aggressive_humanize_pass(current_text, client)

    print("\n🕵️  Step: Applying Extreme Humanization (Token-Breaker)...")
    current_text = zerogpt_bypass(current_text)

    print(f"   ✅ Humanization complete.")
    return current_text


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

    # ── Step 0: Load and separate instructions from content ──
    raw = load_raw_text(input_path, client)
    file_extracted, text = separate_instructions_from_content(raw, client)

    # Merge: defaults < instructions file < embedded file instructions < CLI flags
    # (file_extracted has LOWER priority than the passed-in settings which already
    #  merged defaults + instructions file + CLI — so we only fill gaps)
    for key, val in file_extracted.items():
        if key not in settings or settings[key] == INSTRUCTION_DEFAULTS.get(key):
            settings[key] = val

    print(f"\n{'='*55}")
    print(f"  Academic Writing Assistant  |  Style: {settings['style'].upper()}")
    print(f"{'='*55}")
    print_settings(settings)

    # ── Determine mode: write from scratch OR work with existing essay ──
    if not text:
        # No essay body found — only instructions/topic. Write from scratch.
        print("\n✏️  No essay body found — writing from scratch based on topic/instructions...")
        topic_context = file_extracted.get("_raw_instructions", raw)
        text = write_essay_from_scratch(
            topic=topic_context,
            pages=settings["pages"] or 3,
            sources=settings["sources"] or 6,
            style=settings["style"],
            client=client
        )
    else:
        print(f"\n   Input: {len(text.split())} words of existing essay content.")
        # Strip any placeholders that may already be in the submitted essay
        text = _strip_placeholders(text)
        if settings["pages"]:
            text = expand_existing_essay(text, settings["pages"], settings["style"], client)

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

        sources = search_crossref(query, max_results=8, min_year=settings.get("min_year"))
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

    # Final safety pass — strip any placeholder citations that slipped through
    cited_text = _strip_placeholders(cited_text)
    placeholders_found = cited_text.count("placeholder") + cited_text.count("citation needed")
    if placeholders_found == 0:
        print("   ✅ No placeholder citations detected.")
    else:
        print(f"   ⚠️  {placeholders_found} placeholder(s) still found after stripping — check output.")

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
    parser.add_argument("--no-humanize", action="store_false", dest="humanize", default=None, help="Disable rewriting to sound natural")
    parser.add_argument("--humanize",    action="store_true", dest="humanize", default=None, help="Rewrite to sound natural (enabled by default)")
    parser.add_argument("--title",       type=str,  help="Paper title")
    parser.add_argument("--author",      type=str,  help="Author name")
    parser.add_argument("--institution", type=str,  help="University / institution")
    parser.add_argument("--course",      type=str,  help="Course name and number")
    parser.add_argument("--instructor",  type=str,  help="Instructor name")
    parser.add_argument("--output",      type=str,  help="Output .docx filename (default: output.docx)")
    parser.add_argument("--min-year",    type=int,  dest="min_year", help="Oldest source year allowed (default: current year - 5)")

    args = parser.parse_args()

    file_settings = parse_instructions_file(args.instructions) if args.instructions else {}
    cli_overrides = {k: v for k, v in vars(args).items()
                     if k not in ("input", "instructions") and v is not None}
    if "--humanize" not in sys.argv and "--no-humanize" not in sys.argv:
        cli_overrides.pop("humanize", None)

    settings = merge_settings(file_settings, cli_overrides)
    run_pipeline(args.input, settings)


if __name__ == "__main__":
    main()