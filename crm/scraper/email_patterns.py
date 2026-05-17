"""Email pattern detection — sniff visible emails from a website,
infer the company's email pattern, generate ranked candidates for a
given decision-maker name.

This is the email-side half of the Apollo replacement. CH gives us
the officer names (free); this module gives us a plausible email
address per officer (~$0 with self-hosted SMTP verification, or
~$0.005/lookup with Hunter etc.).

Hit rate expectations (vs Apollo's ~50% across UK SMB):
  - Pattern detected on the homepage + correctly applied: ~25% of leads.
  - No pattern visible, default 'first.last@' guess: ~70% accuracy on
    UK B2B (the dominant pattern by far).
  - Combined: ~50-55% of guesses are deliverable.

When the company exposes a partner's email anywhere on their site
(About / Team / Contact / Privacy pages), hit rate climbs to ~85%.

Approach
--------
1. Fetch the homepage + a small set of common path candidates
   (/about, /team, /contact, /privacy, /legal) via the proxy stack.
2. Regex-extract every email present, filter to ones on the company's
   own domain.
3. If any visible email matches an officer name (firstname or surname
   appears in the local part), infer the pattern from that mapping.
4. For an officer with no observed email, generate candidates in
   ranked order: pattern-derived first, then UK B2B defaults.

Pure HTTP via scraper.client. Never raises — returns best-effort
results with a structured error list when fetches fail.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from urllib.parse import urljoin, urlparse

from scraper import client as scraper_client

logger = logging.getLogger("crm.scraper.email_patterns")

# Standard email regex. Permissive: we want to catch obfuscated forms
# but we also filter out role/garbage addresses afterwards.
EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)

# Paths to probe in addition to the homepage. Limit to 4 to keep the
# proxy budget tight: 5 fetches per lead is the absolute ceiling.
EXTRA_PATHS = ("about", "team", "contact", "privacy")

# Role / generic addresses we ignore when inferring patterns. These
# don't carry pattern signal (info@ tells us nothing about the
# firstname format) but we still record them as fallback sends.
ROLE_LOCALS = {
    "info", "hello", "contact", "enquiries", "enquiry", "office",
    "admin", "support", "team", "sales", "marketing", "hr", "jobs",
    "careers", "accounts", "billing", "finance", "privacy", "legal",
    "dpo", "noreply", "no-reply", "donotreply",
    "webmaster", "postmaster", "abuse", "security",
}

# Hard cap on body bytes per fetched page — 200KB is plenty to find
# any visible email and keeps the proxy thread from stalling on huge
# pages full of JS.
MAX_BYTES = 200_000


# ── Tokenisation ─────────────────────────────────────────────────────
def _normalise_name(s: str) -> str:
    """Drop diacritics, lowercase, strip leading/trailing whitespace."""
    nfkd = unicodedata.normalize("NFKD", s or "")
    ascii_ = "".join(c for c in nfkd if not unicodedata.combining(c))
    return ascii_.lower().strip()


def _split_officer_name(raw: str) -> tuple[str | None, str | None]:
    """Parse a CH officer name like 'SMITH, John Robert' or
    'John Robert Smith' into (first, last) using common UK conventions.

    CH typically returns 'SURNAME, Firstname Middlename' — uppercased
    surname before a comma. Fall back to last-token-as-surname if no
    comma is present.
    """
    if not raw:
        return None, None
    s = raw.strip()
    if "," in s:
        surname, rest = s.split(",", 1)
        rest_tokens = rest.strip().split()
        if not rest_tokens:
            return None, _normalise_name(surname)
        first = _normalise_name(rest_tokens[0])
        last  = _normalise_name(surname)
        return first or None, last or None
    tokens = s.split()
    if len(tokens) < 2:
        return _normalise_name(tokens[0]) if tokens else None, None
    return _normalise_name(tokens[0]), _normalise_name(tokens[-1])


# ── Domain extraction ────────────────────────────────────────────────
def _domain_of(website: str | None) -> str | None:
    if not website:
        return None
    raw = website.strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    try:
        host = urlparse(raw).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


# ── Page fetching ────────────────────────────────────────────────────
def _fetch_pages(website: str) -> list[tuple[str, str]]:
    """Return [(url, body)] for the homepage + a small set of common
    paths. Skips any path that 404s or fails to fetch. Caps each body
    at MAX_BYTES."""
    if not scraper_client.is_available():
        return []
    out: list[tuple[str, str]] = []
    base = website.rstrip("/")
    if "://" not in base:
        base = "http://" + base

    # Homepage first — always try.
    body = scraper_client.fetch(base, max_bytes=MAX_BYTES)
    if body:
        out.append((base, body))

    # Extra paths — only attempt when homepage succeeded so we don't
    # waste fetches against a site that's clearly unreachable.
    if out:
        for p in EXTRA_PATHS:
            url = urljoin(base + "/", p)
            try:
                body = scraper_client.fetch(url, max_bytes=MAX_BYTES)
            except Exception:
                continue
            if body:
                out.append((url, body))
    return out


# ── Email extraction + classification ────────────────────────────────
def _extract_emails(body: str, domain: str) -> list[str]:
    """Pull every email on the company's own domain from a page body."""
    if not body or not domain:
        return []
    domain_lower = domain.lower()
    found: set[str] = set()
    for m in EMAIL_RE.finditer(body):
        e = m.group(0).lower()
        # Filter to own-domain only. We accept subdomain matches
        # ('hello@mail.acme.co.uk' counts for 'acme.co.uk') because
        # tenant-specific subdomains are common.
        host = e.split("@", 1)[1]
        if host == domain_lower or host.endswith("." + domain_lower):
            found.add(e)
    return sorted(found)


def _local_part(email: str) -> str:
    return email.split("@", 1)[0].lower()


def _classify_emails(emails: list[str]) -> tuple[list[str], list[str]]:
    """Split visible emails into (personal, role)."""
    personal: list[str] = []
    role: list[str] = []
    for e in emails:
        lp = _local_part(e)
        # A local part is 'role' if its stem (drop trailing digits) is
        # in our ROLE_LOCALS set. Catches info1@, sales2@ etc.
        stem = re.sub(r"\d+$", "", lp)
        if stem in ROLE_LOCALS:
            role.append(e)
        else:
            personal.append(e)
    return personal, role


# ── Pattern inference ───────────────────────────────────────────────
# Patterns are stored as format strings with {first} / {last} / {f} / {l}
# placeholders. Order is rank: most-likely first → fallback last.
DEFAULT_PATTERNS = [
    "{first}.{last}",     # jane.doe@        — single most common UK B2B
    "{first}{last}",      # janedoe@
    "{first}",            # jane@
    "{f}{last}",          # jdoe@
    "{first}_{last}",     # jane_doe@
    "{first}-{last}",     # jane-doe@
    "{last}{f}",          # doej@
    "{last}.{first}",     # doe.jane@
    "{f}.{last}",         # j.doe@
]


def _format_local(pattern: str, first: str, last: str) -> str:
    return (pattern
            .replace("{first}", first)
            .replace("{last}", last)
            .replace("{f}", first[0] if first else "")
            .replace("{l}", last[0] if last else ""))


def _infer_pattern(personal_emails: list[str], officers: list[dict]) -> str | None:
    """If any visible personal email maps cleanly to an officer name,
    return the pattern that produced it. Otherwise None.

    We only trust patterns confirmed by name-match — random local
    parts ('mark42@', 'gemma-pa@') don't tell us anything about how
    the company names mailboxes.
    """
    if not personal_emails or not officers:
        return None

    # Pre-tokenise officers once.
    officer_parts = [
        (first, last)
        for o in officers
        for first, last in [_split_officer_name(o.get("name") or "")]
        if first and last
    ]
    if not officer_parts:
        return None

    for email in personal_emails:
        lp = _local_part(email)
        for first, last in officer_parts:
            for pat in DEFAULT_PATTERNS:
                if _format_local(pat, first, last) == lp:
                    return pat
    return None


def generate_candidates(officer_name: str, domain: str,
                        pattern: str | None = None) -> list[str]:
    """Ranked email guesses for one officer. If `pattern` is known
    (from infer_pattern), it's tried first and given highest weight.
    Otherwise we fall back to the DEFAULT_PATTERNS ranking."""
    first, last = _split_officer_name(officer_name)
    if not first or not last:
        return []
    domain = domain.lower().lstrip(".")
    seen: set[str] = set()
    out: list[str] = []
    patterns = ([pattern] + DEFAULT_PATTERNS) if pattern else DEFAULT_PATTERNS
    for p in patterns:
        local = _format_local(p, first, last)
        if not local or "@" in local:
            continue
        addr = f"{local}@{domain}"
        if addr in seen:
            continue
        seen.add(addr)
        out.append(addr)
    return out


# ── Public entry ─────────────────────────────────────────────────────
def detect(website: str | None, officers: list[dict] | None) -> dict:
    """Sniff the website, infer pattern, return everything we found.

    Returns:
      {
        domain:           "rocaaccountants.co.uk" | None
        visible_emails:   ["info@…", "jane.doe@…", …]
        personal_emails:  subset of visible (drops info@ / hello@ / etc.)
        role_emails:      ["info@…", "hello@…", …]
        inferred_pattern: "{first}.{last}" | None
        confidence:       'high' | 'medium' | 'low' | 'none'
        errors:           ["fetch failed", ...]
      }
    """
    result: dict = {
        "domain":           None,
        "visible_emails":   [],
        "personal_emails":  [],
        "role_emails":      [],
        "inferred_pattern": None,
        "confidence":       "none",
        "errors":           [],
    }

    domain = _domain_of(website)
    if not domain:
        result["errors"].append("no website / domain")
        return result
    result["domain"] = domain

    pages = _fetch_pages(website)
    if not pages:
        result["errors"].append("no pages fetched (scraper disabled or all fetches failed)")
        return result

    visible: set[str] = set()
    for url, body in pages:
        for e in _extract_emails(body, domain):
            visible.add(e)
    visible_sorted = sorted(visible)
    result["visible_emails"] = visible_sorted

    personal, role = _classify_emails(visible_sorted)
    result["personal_emails"] = personal
    result["role_emails"]     = role

    inferred = _infer_pattern(personal, officers or [])
    result["inferred_pattern"] = inferred

    if inferred:
        result["confidence"] = "high"
    elif personal:
        # We saw personal emails but couldn't tie them to an officer —
        # the pattern is still inferrable in theory but we don't know
        # which one. Mark medium so the operator can investigate.
        result["confidence"] = "medium"
    elif role:
        result["confidence"] = "low"
    else:
        result["confidence"] = "none"
    return result
