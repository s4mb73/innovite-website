"""Local email pre-checks — free, no external dependency, no spend.

This is the cheap layer in front of any paid verifier. It kills the
obvious junk (malformed addresses, dead domains, disposable inboxes)
before we burn an external credit on it. Every kill here is one
fewer credit spent at Reoon / NeverBounce / etc.

Pre-check verdicts
------------------
  syntax_invalid — RFC 5322-ish syntax check failed (probably a typo
                   in the pattern generator)
  no_mx          — domain has no MX record (and no fallback A record).
                   The mail server doesn't exist; sending will bounce.
  disposable     — local-part is on a temp-mail / throwaway domain.
                   Never worth verifying — no human at the other end.
  plausible      — passed all cheap checks. Caller should escalate to
                   the paid verifier (which will tell us catch-all vs.
                   real mailbox).

Caching
-------
MX lookups dominate the cost. Cache them per-domain in-process — at
realistic enrichment volumes (hundreds of leads per client) we'll see
the same domain repeatedly (multiple officers per company, the same
website re-enriched on a future run). TTL is the process lifetime —
no need for anything more sophisticated since the worker restarts
nightly.

Disposable list
---------------
Embedded — not fetched at runtime. A small seed (~60 domains) covers
the long tail of consumer throwaway providers that occasionally slip
into pattern-generated candidates (e.g. when a CH officer name
happens to alias a generic dictionary word in our pattern). Refresh
periodically by checking github.com/disposable/disposable-email-domains.
"""
from __future__ import annotations

import logging
import re
import time

try:
    import dns.exception
    import dns.resolver
    _DNS_OK = True
except ImportError:
    _DNS_OK = False

logger = logging.getLogger("crm.scraper.email_verifier_local")

# Permissive RFC 5322-ish regex. We're not validating per the full
# grammar (that path leads to a 500-char monster); just rejecting
# obvious malformations from the pattern generator.
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,63}$"
)

# Same short timeout as dns_signals.py — DNS should be fast or skipped.
_DNS_TIMEOUT = 3.0

# In-process cache: {domain_lower: (verdict, ts)} where verdict is the
# precheck dict. Bounded — at 10k entries we start evicting oldest.
_CACHE: dict[str, tuple[dict, float]] = {}
_CACHE_MAX = 10_000

# Disposable / throwaway email providers. Curated seed list — we kill
# anything matching exactly (suffix match would over-match real
# domains that contain 'mail' etc.).
DISPOSABLE_DOMAINS: frozenset[str] = frozenset({
    "10minutemail.com", "20minutemail.com", "33mail.com", "anonbox.net",
    "byom.de", "dispostable.com", "emailondeck.com", "fakeinbox.com",
    "getairmail.com", "guerrillamail.com", "guerrillamail.net",
    "guerrillamailblock.com", "harakirimail.com", "inboxalias.com",
    "mailcatch.com", "maildrop.cc", "mailforspam.com", "mailinator.com",
    "mailinator.net", "mailmetrash.com", "mailnesia.com", "mailnull.com",
    "mintemail.com", "mohmal.com", "mvrht.com", "mytemp.email",
    "no-spam.ws", "noemail.xyz", "nospamfor.us", "objectmail.com",
    "owlpic.com", "pookmail.com", "rcpt.at", "sharklasers.com",
    "soodonims.com", "spambog.com", "spambox.us", "spamfree24.com",
    "spamgourmet.com", "spaml.de", "tempail.com", "temp-mail.org",
    "tempemail.net", "tempinbox.com", "tempmail.email", "tempmail.it",
    "tempmail.net", "tempmail.us", "tempmailaddress.com",
    "tempmaildemand.com", "throwawaymail.com", "trashmail.com",
    "trashmail.net", "trashmail.ws", "trbvm.com", "vmpan.com",
    "wegwerfemail.de", "wegwerfmail.de", "yopmail.com", "yopmail.fr",
    "yopmail.net", "zetmail.com",
})


def _resolver() -> "dns.resolver.Resolver":
    r = dns.resolver.Resolver()
    r.lifetime = _DNS_TIMEOUT
    r.timeout  = _DNS_TIMEOUT
    return r


def _domain_of(email: str) -> str:
    return email.rsplit("@", 1)[1].lower().strip()


def _has_mx_or_a(domain: str) -> tuple[bool, list[str]]:
    """Return (deliverable, mx_hosts).

    Modern mail follows RFC 5321 §5.1: if MX records exist, use them.
    If MX is absent, the receiver should fall back to A/AAAA on the
    apex (the 'implicit MX'). So an apex A record alone is sufficient
    for deliverability in principle, though in practice it's rare for
    a real business inbox.
    """
    if not _DNS_OK:
        return True, []  # can't verify → don't block (worker will catch later)
    r = _resolver()
    mx_hosts: list[str] = []
    try:
        ans = r.resolve(domain, "MX")
        for rdata in ans:
            host = str(rdata.exchange).rstrip(".").lower()
            if host:
                mx_hosts.append(host)
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN,
            dns.resolver.NoNameservers, dns.exception.Timeout):
        pass
    if mx_hosts:
        return True, mx_hosts

    # Implicit MX — apex A/AAAA. Cheap fall-back.
    try:
        r.resolve(domain, "A")
        return True, []
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN,
            dns.resolver.NoNameservers, dns.exception.Timeout):
        return False, []


def has_mx_records(domain: str) -> bool:
    """Strict MX-only check — True iff the domain has at least one
    explicit MX record. Used by the decision-maker gate.

    Different from `_has_mx_or_a` (used by precheck) which also
    accepts the RFC 5321 implicit-MX A-record fallback. In practice
    no real business operates on implicit MX — every UK SMB with a
    real inbox has MX records configured by their email provider.
    Absence of MX is a strong "this domain doesn't receive email"
    signal even when an A record exists for the website.
    """
    if not _DNS_OK or not domain:
        return True  # can't check → don't block downstream work
    try:
        ans = _resolver().resolve(domain, "MX")
        return any(str(r.exchange).rstrip(".") for r in ans)
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN,
            dns.resolver.NoNameservers, dns.exception.Timeout):
        return False


def _evict_oldest():
    """Cache eviction — LRU-ish (oldest insertion wins). Cheap because
    we only run when over capacity, not per-lookup."""
    if len(_CACHE) <= _CACHE_MAX:
        return
    target = _CACHE_MAX // 2
    oldest = sorted(_CACHE.items(), key=lambda kv: kv[1][1])[:len(_CACHE) - target]
    for k, _ in oldest:
        _CACHE.pop(k, None)


def precheck(email: str | None) -> dict:
    """Run all cheap pre-checks against an email. Returns:
      {
        'status':     'syntax_invalid' | 'no_mx' | 'disposable' | 'plausible',
        'mx_hosts':   [...],   # populated when status == 'plausible'
        'reason':     'short human-readable explanation',
        'cache_hit':  bool,
      }
    Never raises. Always returns a dict with at least 'status'.
    """
    if not email or "@" not in email:
        return {"status": "syntax_invalid", "mx_hosts": [],
                "reason": "no @ in address", "cache_hit": False}

    addr = email.strip().lower()
    if not _EMAIL_RE.match(addr):
        return {"status": "syntax_invalid", "mx_hosts": [],
                "reason": "fails RFC 5322 shape", "cache_hit": False}

    domain = _domain_of(addr)
    if domain in DISPOSABLE_DOMAINS:
        return {"status": "disposable", "mx_hosts": [],
                "reason": f"{domain} is a known throwaway provider",
                "cache_hit": False}

    # Domain-level checks (MX) are cacheable; the local-part doesn't
    # affect them. Hit the cache before the resolver.
    cached = _CACHE.get(domain)
    if cached is not None:
        verdict, _ = cached
        return {**verdict, "cache_hit": True}

    has_mail, mx_hosts = _has_mx_or_a(domain)
    if not has_mail:
        verdict = {"status": "no_mx", "mx_hosts": [],
                   "reason": f"{domain} has no MX or A record",
                   "cache_hit": False}
    else:
        verdict = {"status": "plausible", "mx_hosts": mx_hosts,
                   "reason": "passed local pre-checks",
                   "cache_hit": False}
    _CACHE[domain] = (verdict, time.time())
    _evict_oldest()
    return verdict
