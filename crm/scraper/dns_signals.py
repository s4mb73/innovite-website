"""DNS signals — cheap, universal tech-stack fingerprint per lead.

Why this matters
----------------
Every lead with a domain has DNS records. A single lookup tells us:
  - which email provider they use (Microsoft 365 / Google Workspace /
    other) — proxy for digital maturity and a concrete outreach hook
  - which hosting provider their website sits on — Wix / Squarespace /
    Shopify / Cloudflare etc. tells us how much they invested in their
    web presence
  - whether they have basic email security configured (SPF + DMARC)

The data drives two things:
  1. A sortable digital-maturity tier on the leads page
  2. Outreach copy that references their actual stack ("I see you're
     running Microsoft 365 — three things that hurts deliverability if
     you don't have DMARC on…")

Implementation
--------------
Pure DNS lookups via `dnspython`. No HTTP, no proxy, no defending —
DNS is designed to be queried. We cap every query at a short timeout
and silently return 'unknown' on failure rather than retrying. A bad
lookup is cheap; a slow one is the only risk.

Output shape (None / 'unknown' when we can't tell):
  email_provider:  'microsoft_365' | 'google_workspace' | 'zoho' |
                   'fastmail' | 'protonmail' | 'godaddy' | 'other' | None
  website_host:    'cloudflare' | 'aws' | 'wix' | 'squarespace' |
                   'shopify' | 'vercel' | 'wordpress_com' | 'godaddy' |
                   'other' | None
  dmarc_present:   bool | None  (None = couldn't query)
  spf_present:     bool | None
"""
from __future__ import annotations

import logging
import re
import socket
from urllib.parse import urlparse

try:
    import dns.exception
    import dns.resolver
    import dns.reversename
    _DNS_OK = True
except ImportError:
    _DNS_OK = False

logger = logging.getLogger("crm.scraper.dns_signals")

# Short timeout — DNS should answer in <100ms from a healthy resolver.
# 3s is generous; failures past that are almost always misconfigured
# nameservers, which we treat as 'unknown' rather than blocking the
# pipeline.
_DNS_TIMEOUT = 3.0


def _resolver() -> "dns.resolver.Resolver":
    r = dns.resolver.Resolver()
    r.lifetime = _DNS_TIMEOUT
    r.timeout = _DNS_TIMEOUT
    return r


# ── MX → email provider classifier ──────────────────────────────────
# Match against the MX target hostname (lowercased, trailing dot
# stripped). Order matters: more-specific patterns first.
_MX_PROVIDERS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\.mail\.protection\.outlook\.com$"), "microsoft_365"),
    (re.compile(r"\.outlook\.com$"),                    "microsoft_365"),
    (re.compile(r"\.olc\.protection\.outlook\.com$"),   "microsoft_365"),
    (re.compile(r"(^|\.)aspmx\.l\.google\.com$"),       "google_workspace"),
    (re.compile(r"\.googlemail\.com$"),                 "google_workspace"),
    (re.compile(r"\.google\.com$"),                     "google_workspace"),
    (re.compile(r"\.zoho\.(com|eu)$"),                  "zoho"),
    (re.compile(r"\.messagingengine\.com$"),            "fastmail"),
    (re.compile(r"\.protonmail\.ch$"),                  "protonmail"),
    (re.compile(r"\.protonmail\.com$"),                 "protonmail"),
    (re.compile(r"\.secureserver\.net$"),               "godaddy"),
    (re.compile(r"\.mailgun\.org$"),                    "mailgun"),
    (re.compile(r"\.yahoodns\.net$"),                   "yahoo"),
    (re.compile(r"\.icloud\.com$"),                     "icloud"),
    (re.compile(r"\.123-reg\.co\.uk$"),                 "123_reg"),
    (re.compile(r"\.heart\.co\.uk$"),                   "heartinternet"),
]


def _classify_mx(mx_hosts: list[str]) -> str | None:
    """Pick the strongest provider match across all MX records.
    Multiple MX records on different providers is rare but possible
    (failover setups) — we just return the first hit."""
    if not mx_hosts:
        return None
    for host in mx_hosts:
        h = host.lower().rstrip(".")
        for pattern, provider in _MX_PROVIDERS:
            if pattern.search(h):
                return provider
    return "other"


# ── PTR / A hostname → website host classifier ──────────────────────
# Same suffix-match approach as MX. Source: PTR record on the A IP, or
# the apex domain's CNAME chain. We do PTR rather than maintaining IP
# ranges because cloud providers shuffle IPs constantly.
_HOST_PROVIDERS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\.cloudflare\.(com|net)$"),         "cloudflare"),
    (re.compile(r"\.cloudfront\.net$"),               "aws"),
    (re.compile(r"\.amazonaws\.com$"),                "aws"),
    (re.compile(r"\.compute\.amazonaws\.com$"),       "aws"),
    (re.compile(r"\.shopify\.com$"),                  "shopify"),
    (re.compile(r"\.myshopify\.com$"),                "shopify"),
    (re.compile(r"\.wixsite\.com$"),                  "wix"),
    (re.compile(r"\.wix\.com$"),                      "wix"),
    (re.compile(r"\.squarespace\.com$"),              "squarespace"),
    (re.compile(r"\.sqsp\.net$"),                     "squarespace"),
    (re.compile(r"\.vercel-dns\.com$"),               "vercel"),
    (re.compile(r"\.vercel\.app$"),                   "vercel"),
    (re.compile(r"\.netlify\.com$"),                  "netlify"),
    (re.compile(r"\.wordpress\.com$"),                "wordpress_com"),
    (re.compile(r"\.wp\.com$"),                       "wordpress_com"),
    (re.compile(r"\.secureserver\.net$"),             "godaddy"),
    (re.compile(r"\.fastly\.net$"),                   "fastly"),
    (re.compile(r"\.azureedge\.net$"),                "azure"),
    (re.compile(r"\.azurewebsites\.net$"),            "azure"),
    (re.compile(r"\.github\.io$"),                    "github_pages"),
    (re.compile(r"\.herokuapp\.com$"),                "heroku"),
    (re.compile(r"\.123-reg\.co\.uk$"),               "123_reg"),
    (re.compile(r"\.heart\.co\.uk$"),                 "heartinternet"),
    (re.compile(r"\.ionos\.(co\.uk|com|net)$"),       "ionos"),
    (re.compile(r"\.fasthosts\.co\.uk$"),             "fasthosts"),
]


def _classify_host(ptr_hosts: list[str]) -> str | None:
    if not ptr_hosts:
        return None
    for host in ptr_hosts:
        h = host.lower().rstrip(".")
        for pattern, provider in _HOST_PROVIDERS:
            if pattern.search(h):
                return provider
    return "other"


# ── Domain extraction ────────────────────────────────────────────────
def _extract_domain(website: str | None) -> str | None:
    """Pull the registrable host from a website URL. Returns None for
    empty/invalid inputs. Strips leading 'www.' so DNS queries hit the
    apex domain where MX and SPF/DMARC live."""
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
    if host.startswith("www."):
        host = host[4:]
    return host or None


# ── Lookups (wrapped so caller can swallow exceptions uniformly) ────
def _mx_records(domain: str) -> list[str]:
    try:
        ans = _resolver().resolve(domain, "MX")
        return [str(r.exchange).rstrip(".") for r in ans]
    except Exception:
        return []


def _txt_records(domain: str) -> list[str]:
    try:
        ans = _resolver().resolve(domain, "TXT")
    except Exception:
        return []
    out: list[str] = []
    for r in ans:
        # rdata.strings is a tuple of bytes chunks (TXT records may be
        # split into 255-byte pieces). Join and decode best-effort.
        try:
            parts = [b.decode("utf-8", errors="replace") for b in r.strings]
            out.append("".join(parts))
        except Exception:
            continue
    return out


def _a_record(domain: str) -> str | None:
    try:
        ans = _resolver().resolve(domain, "A")
        return str(ans[0])
    except Exception:
        return None


def _ptr_host(ip: str) -> str | None:
    """Reverse-DNS lookup. Returns the PTR target or None.
    Many cloud IPs lack PTRs; that's expected and not an error."""
    try:
        rev = dns.reversename.from_address(ip)
        ans = _resolver().resolve(rev, "PTR")
        return str(ans[0]).rstrip(".")
    except Exception:
        return None


# ── Public entry ─────────────────────────────────────────────────────
def check(website: str | None) -> dict:
    """Resolve DNS signals for a website URL.

    Returns:
      {email_provider, website_host, dmarc_present, spf_present, errors}
    All values may be None when the domain itself can't be parsed or
    when DNS resolution failed. errors is a list of short strings for
    operator-visible debugging.
    """
    result: dict = {
        "email_provider": None,
        "website_host":   None,
        "dmarc_present":  None,
        "spf_present":    None,
        "errors":         [],
    }

    if not _DNS_OK:
        result["errors"].append("dnspython not installed")
        return result

    domain = _extract_domain(website)
    if not domain:
        result["errors"].append("no website / unparseable domain")
        return result

    # Email provider via MX records.
    mx = _mx_records(domain)
    result["email_provider"] = _classify_mx(mx) if mx else None

    # SPF lives in a TXT on the apex; DMARC lives in a TXT on _dmarc.<apex>.
    apex_txt = _txt_records(domain)
    result["spf_present"] = any(
        t.lower().startswith("v=spf1") for t in apex_txt
    ) if apex_txt or mx else None

    dmarc_txt = _txt_records(f"_dmarc.{domain}")
    # If we got *any* TXT answer for the apex we know DNS is reachable,
    # so a missing _dmarc record means "no DMARC", not "unknown".
    if dmarc_txt:
        result["dmarc_present"] = any(
            t.lower().startswith("v=dmarc1") for t in dmarc_txt
        )
    elif apex_txt or mx:
        result["dmarc_present"] = False

    # Hosting provider — A record → PTR → classify.
    ip = _a_record(domain)
    if ip:
        ptr = _ptr_host(ip)
        if ptr:
            result["website_host"] = _classify_host([ptr])

    return result


# ── Pipeline adapter ────────────────────────────────────────────────
name = "dns_signals"


def enrich(business: dict) -> dict:
    """Annotate the business dict with email_provider, website_host,
    dmarc_present, spf_present. Never raises — failures attach to
    source_errors."""
    site = business.get("website") or ""
    try:
        r = check(site)
    except Exception as e:
        logger.exception("dns_signals check failed for %s", site[:80])
        business.setdefault("source_errors", {})["dns_signals"] = f"unexpected: {str(e)[:120]}"
        return business

    business["email_provider"] = r["email_provider"]
    business["website_host"]   = r["website_host"]
    business["dmarc_present"]  = r["dmarc_present"]
    business["spf_present"]    = r["spf_present"]
    if r["errors"]:
        business.setdefault("source_errors", {})["dns_signals"] = " | ".join(r["errors"])[:240]
    return business
