"""Website signal extractor — fetches a lead's homepage + likely
sub-pages, hands the combined HTML to Anthropic Haiku, returns a
structured dict of signals the drafter can use.

Why LLM-extract rather than regex/BeautifulSoup parse
-----------------------------------------------------
Per-site parsers rot the moment a target redesigns. UK SMB websites
are all over the place (Wix, Squarespace, hand-rolled WordPress,
30-year-old static HTML). A single regex / CSS-selector pipeline
won't generalise.

A small LLM call (Haiku, ~£0.001 per page) reads any HTML and
returns the same schema regardless of source markup. Cost per pipeline
run at typical volume (200 leads × 1-3 pages × £0.001) is ~£0.60 —
the cheapest part of the stack.

Schema returned (all optional):
  summary:         1-2 sentence "what they do" plain text
  services:        list of service-line strings
  team_size_hint:  'solo' | 'small' (2-10) | 'medium' (10-50) | 'large' (50+) | None
  recency_hint:    'fresh' (recent activity/copyright) | 'stale' (old) | None
  contact_form:    bool — is there a contact form on the homepage / contact page?
  pricing_visible: bool — do they publish prices anywhere?
  client_logos:    list of mentioned client/partner names (max 5)

Fallback
--------
- No Anthropic key → returns {summary: None, ...} with all fields blank.
- Fetch failures → enricher records status='blocked'/'timeout'.
- Parse failure → status='parse_failed', no signals.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable

from scraper import client as scraper_client

logger = logging.getLogger("crm.scraper.extractor")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

# Common sub-paths that UK SMB sites use for "who we are" and "what we
# offer" content. Tried after the homepage; first one to return content
# is appended to the prompt.
SUBPAGE_CANDIDATES = (
    "/about", "/about-us", "/who-we-are",
    "/services", "/what-we-do", "/our-services",
)


SYSTEM_PROMPT = """You extract structured signals from B2B website HTML.

Output strictly as JSON with this exact schema (all fields optional —
omit a field rather than guess):

{
  "summary":          "1-2 sentence plain-English description of what they do",
  "services":         ["service one", "service two", "..."],
  "team_size_hint":   "solo" | "small" | "medium" | "large",
  "recency_hint":     "fresh" | "stale",
  "contact_form":     true | false,
  "pricing_visible":  true | false,
  "client_logos":     ["Client A", "Client B"]
}

Rules:
- summary: extract the actual proposition from their words. No marketing
  fluff, no "as their website states". 30-50 words max.
- services: extract the named service lines, not headings like "Our
  approach". Max 8 items. UK English spellings.
- team_size_hint: "solo" if a single named founder/consultant, "small"
  if 2-10 implied, "medium" if 10-50, "large" if 50+. Base on team
  pages, "about us" copy, "we are a team of X" lines. Omit if unclear.
- recency_hint: "fresh" if you see a 2024+ copyright, recent blog post,
  or recent client work. "stale" if copyright pre-2022, no dated content,
  obvious neglect markers. Omit if neutral.
- contact_form: true if the HTML contains <form> with email/name inputs.
- pricing_visible: true if specific prices or pricing tiers are quoted.
- client_logos: named clients/partners visibly listed. Max 5. Omit if none.

No markdown, no commentary, no code fences. Return the JSON object only."""


def _api_key() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY")


def _call_anthropic(prompt: str, timeout: int = 12) -> str | None:
    if not _api_key():
        return None
    payload = json.dumps({
        "model": ANTHROPIC_MODEL,
        "max_tokens": 700,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        ANTHROPIC_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": _api_key() or "",
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for b in data.get("content") or []:
                if b.get("type") == "text":
                    return (b.get("text") or "").strip()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return None
    return None


# Strip <script>, <style>, HTML comments, and inline event handlers
# before sending to Haiku. Aggressive trim — we don't need JS / CSS
# for "what does this business do" extraction, and removing it cuts
# token cost ~3-5x.
_STRIP_TAGS = re.compile(r"<(script|style|svg|noscript|iframe)\b[^>]*>.*?</\1>",
                         re.IGNORECASE | re.DOTALL)
_STRIP_COMMENTS = re.compile(r"<!--.*?-->", re.DOTALL)
_COLLAPSE_WS = re.compile(r"\s+")
# Try to find a <main>, <article>, or <body> for content. Heuristic only —
# if missing, we fall back to the full document.
_CONTENT_RE = re.compile(r"<(main|article|body)\b[^>]*>(.*?)</\1>",
                         re.IGNORECASE | re.DOTALL)


def _clean_html(html: str, max_chars: int = 12_000) -> str:
    """Trim HTML to the content-bearing parts within a token budget."""
    no_scripts = _STRIP_TAGS.sub("", html)
    no_comments = _STRIP_COMMENTS.sub("", no_scripts)

    # Pull out <main> / <article> / <body> if we can.
    m = _CONTENT_RE.search(no_comments)
    chunk = m.group(2) if m else no_comments

    # Collapse whitespace but keep angle brackets — Haiku can read raw HTML.
    chunk = _COLLAPSE_WS.sub(" ", chunk).strip()
    if len(chunk) > max_chars:
        return chunk[:max_chars]
    return chunk


def _normalise_root(url: str) -> str | None:
    """Resolve a (maybe partial) website URL to https://host/."""
    if not url:
        return None
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urllib.parse.urlparse(url)
    if not parsed.netloc:
        return None
    return f"https://{parsed.netloc}/"


def _candidate_urls(root: str) -> Iterable[str]:
    yield root
    for path in SUBPAGE_CANDIDATES:
        yield urllib.parse.urljoin(root, path)


def _parse_haiku_json(raw: str) -> dict | None:
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").lstrip("json").strip()
    try:
        out = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(out, dict):
        return None
    return out


# ── Public entry ────────────────────────────────────────────────────
def extract(website_url: str) -> dict:
    """Fetch a lead's website and return the structured-signals dict.

    Returns: {status, signals, pages_fetched, errors}.
      status:   'ok' | 'no_website' | 'blocked' | 'timeout' | 'parse_failed' | 'disabled'
      signals:  the schema dict from SYSTEM_PROMPT, or empty {}
      pages_fetched: list of URLs we actually got HTML back from
      errors:   short string per failure, for debugging
    """
    result: dict = {"status": "ok", "signals": {},
                    "pages_fetched": [], "errors": []}

    root = _normalise_root(website_url)
    if not root:
        result["status"] = "no_website"
        return result

    if not scraper_client.is_available():
        result["status"] = "disabled"
        result["errors"].append("scraper client unavailable (wreq missing or pool empty)")
        return result

    # Fetch homepage + at most one matching sub-page (stops on first hit
    # to keep cost predictable at ~1-2 pages per lead).
    chunks: list[tuple[str, str]] = []  # (url, cleaned_html)
    homepage_body = scraper_client.fetch(root)
    if homepage_body:
        chunks.append((root, _clean_html(homepage_body)))
        result["pages_fetched"].append(root)
    else:
        result["status"] = "blocked"
        result["errors"].append(f"homepage fetch failed: {root}")
        return result

    for sub in SUBPAGE_CANDIDATES:
        url = urllib.parse.urljoin(root, sub)
        body = scraper_client.fetch(url)
        if body and len(body) > 500:
            chunks.append((url, _clean_html(body)))
            result["pages_fetched"].append(url)
            break  # one sub-page is enough for the LLM context

    # Stitch context — the LLM gets a labelled concatenation.
    context = "\n\n".join(
        f"### {url}\n{html}" for url, html in chunks
    )

    if not _api_key():
        # No LLM available — return blank signals but flag the success.
        result["errors"].append("no ANTHROPIC_API_KEY — signals not extracted")
        return result

    prompt = (
        "Extract structured signals from this website's HTML. Apply the "
        "schema strictly.\n\n" + context + "\n\nReturn the JSON object only."
    )
    raw = _call_anthropic(prompt)
    if not raw:
        result["status"] = "parse_failed"
        result["errors"].append("Anthropic call failed or empty response")
        return result

    parsed = _parse_haiku_json(raw)
    if parsed is None:
        result["status"] = "parse_failed"
        result["errors"].append("response was not valid JSON")
        return result

    result["signals"] = parsed
    return result
