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

import json
import logging
import re
import time
import unicodedata
from urllib.parse import urljoin, urlparse

from scraper import client as scraper_client

logger = logging.getLogger("crm.scraper.email_patterns")

# Standard email regex. Permissive: we want to catch obfuscated forms
# but we also filter out role/garbage addresses afterwards.
EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)

# LinkedIn personal-profile URL — /in/<slug>. Slug allows lowercase
# alphanumerics, hyphens, and percent-encoded chars. We strip the
# trailing slash + any query/fragment.
LINKEDIN_IN_RE = re.compile(
    r"https?://(?:www\.|uk\.)?linkedin\.com/in/([A-Za-z0-9\-_%]+)/?",
    re.IGNORECASE,
)

# LinkedIn company-page URL — /company/<slug>. Most UK SMB target sites
# (accountancy practices, agencies) link a company page rather than the
# founder's personal profile, so this is the workhorse for downstream
# LinkedIn enrichment (follower count, recent company posts).
LINKEDIN_COMPANY_RE = re.compile(
    r"https?://(?:www\.|uk\.)?linkedin\.com/company/([A-Za-z0-9\-_%]+)/?",
    re.IGNORECASE,
)

# Paths to probe in addition to the homepage. We've expanded from the
# original 4 to 8 to chase pages where UK SMBs are more likely to
# expose personal emails — case studies typically credit the partner
# who led the engagement, press releases include a PR contact, careers
# pages list a recruiting contact. The trade-off is proxy budget: a
# typical lead now costs 9 fetches in the happy path (homepage + 8
# extras), capped via _MAX_CONSECUTIVE_FAIL early-termination.
EXTRA_PATHS = (
    "about", "about-us",
    "team", "our-team",
    "contact",
    "case-studies", "press", "careers",
)

# Stop trying extra paths after this many consecutive failures —
# usually a sign the site has a strict routing convention we'll never
# match (single-page sites, hash-routed SPAs, broken proxies for the
# host). Saves proxy budget on the long tail.
_MAX_CONSECUTIVE_FAIL = 3

# Per-domain page-fetch cache. UK SMB targets often have multiple
# active officers; without this we'd re-fetch the same 9 pages for
# every officer. Process-lifetime, bounded LRU.
_PAGES_CACHE: dict[str, tuple[list[tuple[str, str]], float]] = {}
_PAGES_CACHE_MAX = 1_000

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
def _evict_pages_cache_oldest() -> None:
    """Drop the oldest half of the per-domain page cache when over cap.
    Cheap because we only run when full, not per-lookup."""
    if len(_PAGES_CACHE) <= _PAGES_CACHE_MAX:
        return
    target = _PAGES_CACHE_MAX // 2
    oldest = sorted(_PAGES_CACHE.items(), key=lambda kv: kv[1][1])[: len(_PAGES_CACHE) - target]
    for k, _ in oldest:
        _PAGES_CACHE.pop(k, None)


def _fetch_pages(website: str) -> list[tuple[str, str]]:
    """Return [(url, body)] for the homepage + a small set of common
    paths. Skips any path that 404s or fails to fetch. Caps each body
    at MAX_BYTES.

    Per-domain caching: identical domain → returns cached page set
    without hitting the proxy stack again. The cache key is the
    parsed domain, not the raw website string — so trailing slashes,
    UTM params, www. prefix etc. all hit the same entry.
    """
    if not scraper_client.is_available():
        return []

    # Cache lookup before any work.
    domain = _domain_of(website)
    if domain and domain in _PAGES_CACHE:
        cached, _ = _PAGES_CACHE[domain]
        return cached

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
        consecutive_fail = 0
        for p in EXTRA_PATHS:
            if consecutive_fail >= _MAX_CONSECUTIVE_FAIL:
                # Bail early — site clearly doesn't use these path
                # conventions. Saves the rest of the proxy budget.
                break
            url = urljoin(base + "/", p)
            try:
                body = scraper_client.fetch(url, max_bytes=MAX_BYTES)
            except Exception:
                consecutive_fail += 1
                continue
            if body:
                out.append((url, body))
                consecutive_fail = 0
            else:
                consecutive_fail += 1

    if domain:
        _PAGES_CACHE[domain] = (out, time.time())
        _evict_pages_cache_oldest()
    return out


# ── Email extraction + classification ────────────────────────────────
def _extract_linkedin_urls(body: str) -> list[str]:
    """Pull every linkedin.com/in/<slug> URL from a page body, deduped
    + normalised (lowercased slug, no trailing slash, no query)."""
    if not body:
        return []
    seen: set[str] = set()
    for m in LINKEDIN_IN_RE.finditer(body):
        slug = m.group(1).lower().strip("-_/")
        if not slug or slug in ("company", "in", "school", "showcase"):
            continue
        url = f"https://www.linkedin.com/in/{slug}"
        if url not in seen:
            seen.add(url)
    return sorted(seen)


# Slugs we reject because they appear in patterns like
# "linkedin.com/company/setup" or "/company/admin" rather than being a
# real company page. Cheap belt-and-braces in case a site templates
# something odd.
_NON_COMPANY_SLUGS = {"setup", "admin", "products", "showcase"}


def _extract_linkedin_company_urls(body: str) -> list[str]:
    """Pull every linkedin.com/company/<slug> URL from a page body."""
    if not body:
        return []
    seen: set[str] = set()
    for m in LINKEDIN_COMPANY_RE.finditer(body):
        slug = m.group(1).lower().strip("-_/")
        if not slug or slug in _NON_COMPANY_SLUGS:
            continue
        url = f"https://www.linkedin.com/company/{slug}"
        seen.add(url)
    return sorted(seen)


def match_linkedin_company_to_business(urls: list[str], business_name: str) -> str | None:
    """Pick the /company/ URL whose slug best matches the business name.

    Heuristic:
      - Single URL → take it (the site links its own page; almost
        never links a partner / supplier company page).
      - Multiple URLs → score by token overlap between business name
        and slug. Return the highest-scoring one if it overlaps at
        all; otherwise the first (sorted) URL as a deterministic
        fallback.

    Corporate-form suffixes (limited / ltd / plc / llp / uk / the /
    and / co) are stripped from the business name tokens because they
    almost never appear in LinkedIn slugs.
    """
    if not urls:
        return None
    if len(urls) == 1:
        return urls[0]
    tokens = re.findall(r"[a-z0-9]+", (business_name or "").lower())
    tokens = [t for t in tokens if t not in {"ltd", "limited", "llp", "plc", "uk", "the", "and", "co"}]
    if not tokens:
        return urls[0]
    scored: list[tuple[int, str]] = []
    for url in urls:
        slug = url.rsplit("/", 1)[-1].lower()
        score = sum(1 for t in tokens if t in slug)
        scored.append((score, url))
    scored.sort(reverse=True)
    best_score, best_url = scored[0]
    return best_url if best_score > 0 else urls[0]


def match_linkedin_to_officer(linkedin_urls: list[str], officer_name: str) -> str | None:
    """Pick the LinkedIn URL whose slug best matches the officer's
    name. Falls back to the only URL when there's exactly one
    /in/ link on the page (small B2B sites typically link only their
    own founder)."""
    if not linkedin_urls:
        return None
    first, last = _split_officer_name(officer_name)
    if not first or not last:
        return linkedin_urls[0] if len(linkedin_urls) == 1 else None

    # Slugs typically look like "jane-smith-12345abc" or "janesmith".
    # Score each by token presence.
    scored: list[tuple[int, str]] = []
    for url in linkedin_urls:
        slug = url.rsplit("/", 1)[-1].lower()
        score = 0
        if first in slug:
            score += 2
        if last in slug:
            score += 3  # surname is more disambiguating
        scored.append((score, url))
    scored.sort(reverse=True)
    best_score, best_url = scored[0]
    if best_score >= 3:  # require at least the surname match
        return best_url
    # No name match — but if there's exactly one /in/ URL on the
    # site, default to it (small-firm 'meet the founder' convention).
    if len(linkedin_urls) == 1:
        return linkedin_urls[0]
    return None


def extract_from_html(body: str, website: str | None) -> dict:
    """Extract emails from a pre-fetched page body.

    Public wrapper around _extract_emails + _extract_emails_from_jsonld +
    _classify_emails so callers that already have HTML in hand
    (instagram_link.find_for_website returns the homepage body) don't
    have to round-trip back through proxy_pool just to scan emails.

    Returns the same {personal_emails, role_emails, domain} subset of
    detect()'s shape — pattern inference is skipped (this is the cheap
    path; callers wanting that should call detect()).
    """
    domain = _domain_of(website)
    if not domain or not body:
        return {"domain": domain, "personal_emails": [], "role_emails": []}
    visible: set[str] = set()
    for e in _extract_emails(body, domain):
        visible.add(e)
    for e in _extract_emails_from_jsonld(body, domain):
        visible.add(e)
    personal, role = _classify_emails(sorted(visible))
    return {"domain": domain, "personal_emails": personal, "role_emails": role}


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


# JSON-LD blocks often contain structured contact data — Person.email,
# Organization.email, ContactPoint.email — that the regex over body
# may miss (especially when the email is rendered behind JS but
# still in the JSON-LD source). Higher-trust source: structured data
# is the company's own declaration, not free-text we might have parsed
# from a footer disclaimer.
_JSONLD_BLOCK_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def _walk_jsonld_emails(node) -> list[str]:
    """Recursively pull every `email` field value from a JSON-LD tree.
    The schema can be deeply nested (Organization → ContactPoint →
    email, or @graph → list of nodes → email). Returns lowercased
    addresses; the caller filters to the own domain."""
    out: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "email" and isinstance(v, str):
                # Strip mailto: prefix if present (schema.org allows
                # both 'mailto:foo@bar' and 'foo@bar').
                addr = v.strip()
                if addr.lower().startswith("mailto:"):
                    addr = addr[7:]
                if "@" in addr:
                    out.append(addr.lower())
            else:
                out.extend(_walk_jsonld_emails(v))
    elif isinstance(node, list):
        for item in node:
            out.extend(_walk_jsonld_emails(item))
    return out


def _extract_emails_from_jsonld(body: str, domain: str) -> list[str]:
    """Pull structured-data emails on the company's own domain. JSON-LD
    parse errors are swallowed silently — a malformed block shouldn't
    abort the rest of the page's signal extraction."""
    if not body or not domain:
        return []
    domain_lower = domain.lower()
    found: set[str] = set()
    for m in _JSONLD_BLOCK_RE.finditer(body):
        raw = m.group(1)
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        for addr in _walk_jsonld_emails(data):
            host = addr.split("@", 1)[1] if "@" in addr else ""
            if host == domain_lower or host.endswith("." + domain_lower):
                found.add(addr)
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


def _detect_format_from_local(local: str) -> str | None:
    """Inspect one local part and infer the pattern structure from
    shape alone — no name cross-check needed.

    Returns the pattern string ('{first}.{last}', '{f}.{last}', etc.)
    or None when the structure is too ambiguous to claim. We're
    deliberately conservative: a long single token like 'janedoe'
    could be `{first}{last}` or `{first}` or even `{last}{first}` —
    we'd need a name match to disambiguate, so we return None instead
    of guessing.
    """
    if not local:
        return None
    # Strip trailing digits ('jane.doe2@') so they don't break the
    # structural match.
    base = re.sub(r"\d+$", "", local)
    if not base or not all(c.isalpha() or c in "._-" for c in base):
        return None

    for sep, full_pattern, initial_pattern in (
        (".", "{first}.{last}", "{f}.{last}"),
        ("_", "{first}_{last}", None),
        ("-", "{first}-{last}", None),
    ):
        if base.count(sep) != 1:
            continue
        left, right = base.split(sep)
        if not (left.isalpha() and right.isalpha()):
            continue
        if len(left) == 1 and len(right) >= 3 and initial_pattern:
            return initial_pattern
        if len(left) >= 2 and len(right) >= 2:
            return full_pattern
    return None


def _infer_pattern_from_format(personal_emails: list[str]) -> str | None:
    """Pattern inference from email FORMAT alone — no officer match
    required. Useful when the company exposes ANY personal email
    (e.g. a partner credited in a case study, the PR contact on a
    press page) but the visible address isn't one of our CH officers.

    Returns the most common confident pattern across the observed
    emails, ties broken alphabetically for stability. Returns None
    if no email had a confidently-detectable structure.
    """
    if not personal_emails:
        return None
    counts: dict[str, int] = {}
    for em in personal_emails:
        pat = _detect_format_from_local(_local_part(em))
        if pat:
            counts[pat] = counts.get(pat, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


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
        "domain":                None,
        "visible_emails":        [],
        "personal_emails":       [],
        "role_emails":           [],
        "linkedin_urls":         [],
        "linkedin_company_urls": [],
        "inferred_pattern":      None,
        "confidence":            "none",
        "errors":                [],
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
    linkedin_urls: set[str] = set()
    linkedin_company_urls: set[str] = set()
    for _url, body in pages:
        for e in _extract_emails(body, domain):
            visible.add(e)
        for e in _extract_emails_from_jsonld(body, domain):
            visible.add(e)
        for u in _extract_linkedin_urls(body):
            linkedin_urls.add(u)
        for u in _extract_linkedin_company_urls(body):
            linkedin_company_urls.add(u)
    visible_sorted = sorted(visible)
    result["visible_emails"]        = visible_sorted
    result["linkedin_urls"]         = sorted(linkedin_urls)
    result["linkedin_company_urls"] = sorted(linkedin_company_urls)

    personal, role = _classify_emails(visible_sorted)
    result["personal_emails"] = personal
    result["role_emails"]     = role

    # Two-tier pattern inference:
    #   Tier 1 — officer-name match. Highest confidence: we have a
    #            visible email that maps exactly to a CH officer's
    #            (first, last) under a known pattern.
    #   Tier 2 — format-only. We see personal emails on the domain
    #            but none of them are our CH officers. The structural
    #            pattern (jane.doe@) still tells us the convention,
    #            which we can apply to our target. Lower confidence
    #            because we haven't verified the convention works for
    #            our specific officer, but far better than guessing.
    inferred = _infer_pattern(personal, officers or [])
    inferred_source = "officer_match" if inferred else None
    if not inferred:
        inferred = _infer_pattern_from_format(personal)
        if inferred:
            inferred_source = "format_only"
    result["inferred_pattern"]        = inferred
    result["inferred_pattern_source"] = inferred_source

    if inferred_source == "officer_match":
        result["confidence"] = "high"
    elif inferred_source == "format_only":
        # Format inferred from at least one visible personal email's
        # shape — the convention is real but we haven't proven it for
        # our target. Medium-confidence: better than guessing, worse
        # than name-match.
        result["confidence"] = "medium"
    elif personal:
        # Personal emails exist but had no inferrable structure
        # (single-word locals like 'janedoe@' that could be many
        # patterns). Operator can investigate.
        result["confidence"] = "medium"
    elif role:
        result["confidence"] = "low"
    else:
        result["confidence"] = "none"
    return result
