"""Day-1 email drafter — Anthropic Haiku with templated fallback.

Reads the lead's enriched profile + the chosen hook_type and writes a
subject + body that respects the Innovite marketing copy rules:
  - UK English (organise, optimise, analyse, ...)
  - No banned words (leverage, utilise, streamline, solutions, ...)
  - No emoji
  - No exclamation marks
  - Short sentences. Concrete > abstract.

Why Haiku, not Sonnet/Opus
--------------------------
Output is a short email. Haiku writes good copy at this length, cheap
and fast (sub-2-second p50). Sonnet is reserved for reply classification
later, where intent inference is the bottleneck not throughput.

Fallback
--------
If ANTHROPIC_API_KEY is missing or the call fails / times out, we return
a templated draft. The drafter NEVER fails the run — a templated email
is better than no email. The fallback is marked in the body subject so
the operator can spot it before approving sends.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"  # Latest Haiku per memory cutoff

SYSTEM_PROMPT = """You are writing the Day-1 outreach email for Innovite, a UK B2B
AI lead-generation agency. Style rules (these are absolute):

- UK English only: organise, optimise, analyse, personalise, behaviour.
- Banned words — do not use any of these: leverage, utilise, streamline,
  solutions, cutting-edge, innovative, transform, revolutionise, synergy,
  ecosystem, empower, game-changer, best-in-class, hyper-personalised.
- No emoji. No exclamation marks. No em-dashes.
- Short sentences. Concrete over abstract.
- Voice: confident founder, direct, anti-fluff.
- Length: 80-130 words for the body. 5-8 words for the subject.
- No fake personal touches ("I was just on your website earlier..."). Be
  honest about why you are writing.
- Sign off as "Sammy" (single line, no title).

Output strictly as JSON:
{"subject": "...", "body": "..."}
No markdown, no commentary, no code fences. Just the JSON object."""


HOOK_GUIDANCE = {
    # ── Companies House pain signals (accountancy targeting) ──────────
    "accounts_overdue":
        "Open directly with the overdue filing — their accounts are past due at "
        "Companies House. Ask whether everything is okay and whether they need "
        "support getting compliant.",
    "year_end_imminent":
        "Open with the year-end timing angle — their accounts are due in under "
        "three months. Ask if they are happy with how preparation is going.",
    "director_change_recent":
        "Open with the director change — a new director was appointed recently and "
        "new leadership typically reviews all key suppliers. Ask if they are happy "
        "with their current accountancy arrangements.",
    "new_incorporation":
        "Open with the incorporation date — they registered recently and will need "
        "to file their first accounts within the next few months. Ask if they have "
        "an accountant set up yet.",
    "early_stage":
        "Open noting they are in their first year and likely still making key "
        "supplier decisions including their accountant.",
    "year_end_soon":
        "Open by noting their year-end is approaching in the next few months and "
        "ask if they are getting the advisory input they need beforehand.",
    "confirmation_overdue":
        "Open noting their confirmation statement is overdue — a small thing, but "
        "a signal worth checking on.",

    # ── Reputation + web signals (general SMB targeting) ──────────────
    "low_rating_with_volume":
        "Open with the rating signal — they have many reviews but their average is dragging. "
        "Tie it to lost local search visibility and lead quality.",
    "rating_under_4":
        "Open by acknowledging their reviews exist (so the email is grounded), then offer a "
        "way to improve review velocity and quality.",
    "few_reviews":
        "Open with the review-count signal — they are invisible in local search. "
        "Tie it to competitors who outrank them on review volume.",
    "no_ssl":
        "Open with the trust signal — their site is not on HTTPS so browsers warn visitors. "
        "Tie it to bounce rate before form submissions.",
    "no_website":
        "Open by noting they have a Google Maps presence but no website surfaces in search. "
        "Tie it to outbound becoming the only way to fill the pipeline.",
    "no_decision_maker":
        "Open with the local-business angle — keep it generic but warm; we do not have a "
        "named contact so do not invent one.",
    "dm_name_only_no_email":
        "Open with the contact's name and a question about how they currently handle outbound.",
    "general_growth":
        "Open with the local-business growth angle — no specific weakness yet, so anchor on "
        "their industry and city and ask one specific question.",

    # ── LinkedIn authority signals ───────────────────────────────────
    "linkedin_recent_post":
        "Open by referencing the decision maker's most recent LinkedIn post (title and date "
        "are in the data). Be specific about what you found valuable in it, then bridge to a "
        "single relevant question. Do NOT pretend to have read more than the title — keep it "
        "honest and short.",
    "linkedin_high_influence":
        "Open noting the decision maker has a meaningful following (cite the rough number, "
        "rounded down). Connect it to the credibility their content already gives them, then "
        "ask one specific question about how they currently handle inbound from that audience.",

    # ── Growth signals (jobs) ────────────────────────────────────────
    "growth_scaling":
        "Open with the hiring signal — they have multiple open roles on Reed right now. "
        "Tie it to scaling pains (margin pressure, partner-time bottleneck, onboarding "
        "training the new hires) and ask one specific question.",
    "growth_hiring":
        "Open noting they are actively hiring (cite the number of open roles). Keep it "
        "tighter than scaling — one role doesn't justify a full pitch. Ask whether the "
        "growth is creating any back-office strain.",

    # ── Distress (Gazette) ───────────────────────────────────────────
    "gazette_distressed":
        "Open with care — they have an active insolvency notice in the Gazette. Do NOT "
        "be aggressive or sales-y. Acknowledge the situation indirectly, offer one "
        "specific way you can help (turnaround advisory, creditor communications, "
        "restructure planning) and ask if a short conversation would be useful.",

    # ── Email security / deliverability ──────────────────────────────
    "deliverability_gap":
        "Open with the email-security observation — their domain has neither SPF nor DMARC "
        "configured. Tie it to deliverability (their cold emails will land in spam) and "
        "compliance (anyone can spoof their domain). Concrete and technical — this audience "
        "respects precision.",
    "dmarc_missing":
        "Open noting their domain is missing a DMARC record. Tie it to spoofing risk and "
        "the new Google/Yahoo sender requirements.",
    "spf_missing":
        "Open noting their domain has no SPF record set. Tie it to outbound email "
        "deliverability — without SPF their messages routinely get marked as spam.",
}


def _api_key() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY")


def _templated_fallback(business: dict, hook_type: str) -> dict:
    """Used when Anthropic is unavailable. Spotting this in production
    should trigger a check of the ANTHROPIC_API_KEY env var."""
    name = business.get("decision_maker_name") or "there"
    company = business.get("business_name", "your business")
    return {
        "subject": f"Quick question for {company}",
        "body": (
            f"Hi {name},\n\n"
            f"I run a UK agency that helps {company.lower()}-type businesses get "
            f"more qualified leads without scaling headcount.\n\n"
            f"Worth a 15-minute call to share what we've seen working in your sector?\n\n"
            f"Sammy\n\n"
            f"[Templated fallback — Anthropic call did not complete.]"
        ),
    }


def _call_anthropic(prompt: str, timeout: int = 12) -> str | None:
    payload = json.dumps({
        "model": ANTHROPIC_MODEL,
        "max_tokens": 600,
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
            blocks = data.get("content") or []
            for b in blocks:
                if b.get("type") == "text":
                    return b.get("text", "")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return None
    return None


def _build_prompt(business: dict, hook_type: str,
                  template_hint: dict | None = None) -> str:
    parts = [
        f"Business: {business.get('business_name', '')}",
        f"City: {business.get('city', '')}",
        f"Website: {business.get('website') or '(none)'}",
        f"Google rating: {business.get('google_rating') or '—'} "
        f"({business.get('google_review_count') or 0} reviews)",
    ]
    if business.get("companies_house_revenue_band"):
        parts.append(f"Revenue band (CH heuristic): {business['companies_house_revenue_band']}")
    if business.get("decision_maker_name"):
        parts.append(f"Decision maker: {business['decision_maker_name']} "
                     f"({business.get('decision_maker_title', '')})")

    # Website signals — scraped by crm/scraper/* and Haiku-extracted.
    # When present, they're the strongest grounding signal in the prompt:
    # the email can reference what they ACTUALLY do, not generic
    # hook-type guidance.
    web = business.get("website_signals") or {}
    if isinstance(web, dict) and web:
        if web.get("summary"):
            parts.append(f"What they do (from their website): {web['summary']}")
        if web.get("services"):
            services = web["services"]
            if isinstance(services, list) and services:
                parts.append("Services they list: " + ", ".join(str(s) for s in services[:5]))
        if web.get("team_size_hint"):
            parts.append(f"Team size signal: {web['team_size_hint']}")
        if web.get("recency_hint"):
            parts.append(f"Website activity: {web['recency_hint']}")

    # LinkedIn rich profile — when the lead has linkedin_recent_post or
    # follower counts, Haiku needs the actual title/numbers to reference
    # them authentically. Without these, the hook guidance falls back
    # to generic "open with a post reference" which sounds fake.
    if business.get("linkedin_recent_post_title"):
        post_date = business.get("linkedin_recent_post_at")
        date_str = post_date.strftime("%-d %b") if post_date else "recently"
        parts.append(f"DM's most recent LinkedIn post ({date_str}): \"{business['linkedin_recent_post_title']}\"")
    if business.get("linkedin_current_company"):
        parts.append(f"DM's current company (LinkedIn): {business['linkedin_current_company']}")
    if business.get("linkedin_follower_count"):
        parts.append(f"DM LinkedIn followers: {business['linkedin_follower_count']:,}")

    # Open jobs — the actual number matters when the hook is growth.
    if business.get("jobs_open_count"):
        parts.append(f"Open roles on Reed: {business['jobs_open_count']}")

    # Gazette — when distressed, surface the date so the email can
    # be appropriately recent / non-cold-sounding.
    if business.get("gazette_status") == "distressed":
        last = business.get("gazette_last_notice_date")
        date_str = last.strftime("%-d %b %Y") if last else "(date unknown)"
        parts.append(f"Gazette insolvency notice on file — most recent {date_str}")

    # DNS signals — only surface when the hook actually references them,
    # to keep the prompt focused.
    if hook_type in ("deliverability_gap", "dmarc_missing", "spf_missing"):
        provider = business.get("email_provider") or "unknown"
        parts.append(f"Email provider: {provider}, SPF: {business.get('spf_present')}, DMARC: {business.get('dmarc_present')}")

    guidance = HOOK_GUIDANCE.get(hook_type, HOOK_GUIDANCE["general_growth"])

    intro = "Write the Day-1 outreach email for this lead."
    template_block = ""
    if template_hint and template_hint.get("body_template"):
        # Template-seeded mode: the operator approved a prior draft for
        # this campaign and saved it as a template. Treat it as the
        # structural reference — keep the shape, the rhythm, the close;
        # rewrite the specifics for this lead. Don't quote it verbatim.
        intro = (
            "Write the Day-1 outreach email for this lead, in the style "
            "of the approved template below. Use the same structure, "
            "length, and tone — but the specific signals, names, and "
            "examples MUST come from this lead's data, not the template."
        )
        template_block = (
            f"\n\nApproved template (style reference, do not copy):\n"
            f"Subject: {template_hint.get('subject_template', '')}\n"
            f"Body:\n{template_hint['body_template']}\n"
        )

    return (
        intro + "\n\n"
        + "\n".join(parts)
        + f"\n\nHook guidance: {guidance}"
        + template_block
        + "\n\nOutput the JSON object only."
    )


FOLLOWUP_GUIDANCE = {
    2:  ("Day-3 bump",
         "Short — 40-80 words. No new pitch. One sentence that adds value or a "
         "specific second question. Open with something other than 'just bumping' "
         "or 'circling back'. Reference the original message in subject only — body "
         "should sound like a natural follow-up, not a template."),
    3:  ("Day-7 final",
         "Last touch — 30-60 words. Acknowledge this is the last attempt without "
         "being passive-aggressive. One clear ask: yes / no / 'wrong time, follow "
         "up in N months'. Confident close, no apology."),
}


def _build_followup_prompt(business: dict, parent_subject: str, parent_body: str | None,
                           day_number: int, guidance: str) -> str:
    parts = [
        f"Business: {business.get('business_name', '')}",
        f"City: {business.get('city', '')}",
        f"Decision maker: {business.get('decision_maker_name', '')}",
        f"Day-1 subject: {parent_subject}",
    ]
    if parent_body:
        parts.append("Day-1 body (for context — do not quote it):")
        parts.append(parent_body[:1200])

    return (
        f"Write the Day-{day_number * 2 + 1 if day_number == 3 else day_number * 3 - 3} "
        f"follow-up to this conversation.\n\n"
        + "\n".join(parts)
        + f"\n\nGuidance: {guidance}\n\nOutput the JSON object only."
    )


def _templated_followup(business: dict, parent_subject: str, day_number: int) -> dict:
    name = business.get("decision_maker_name") or "there"
    re_subject = parent_subject if parent_subject.lower().startswith("re:") else f"Re: {parent_subject}"
    if day_number == 2:
        body = (
            f"Hi {name},\n\n"
            f"Following up on the note I sent earlier — happy to send over a one-pager "
            f"on how this works in practice if that's easier than a call.\n\n"
            f"Sammy\n\n[Templated fallback — Anthropic call did not complete.]"
        )
    else:
        body = (
            f"Hi {name},\n\n"
            f"Last message from me on this. If now isn't the time, no problem — happy "
            f"to revisit in a few months. Otherwise a 15-minute call this week?\n\n"
            f"Sammy\n\n[Templated fallback — Anthropic call did not complete.]"
        )
    return {"subject": re_subject, "body": body}


def _parse_haiku_json(raw: str) -> dict | None:
    """Extract the {subject, body} JSON from a Haiku response. Tolerant of
    accidental code-fences. Returns None if unparseable."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:].strip()
    try:
        out = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(out, dict) or "subject" not in out or "body" not in out:
        return None
    return {"subject": str(out["subject"]), "body": str(out["body"])}


def draft_day1(business: dict, hook_type: str,
               template_hint: dict | None = None) -> dict:
    """Generate the Day-1 cold email.

    If `template_hint` contains {subject_template, body_template} from
    a previously approved + saved campaign template, Anthropic seeds
    from that — keeps the structure/tone, varies the specifics. Without
    a hint, generates from scratch using only the hook guidance.

    Returns {"subject": "...", "body": "..."}. Never raises.
    """
    if not _api_key():
        return _templated_fallback(business, hook_type)

    raw = _call_anthropic(_build_prompt(business, hook_type, template_hint))
    if not raw:
        return _templated_fallback(business, hook_type)

    parsed = _parse_haiku_json(raw)
    if parsed is None:
        return _templated_fallback(business, hook_type)
    return parsed


def draft_followup(business: dict, parent_subject: str, parent_body: str | None,
                   day_number: int) -> dict:
    """Generate Day-3 or Day-7 follow-up. day_number is the cadence step (2 or 3).

    Returns {"subject", "body"}. Falls back to a templated message if Anthropic
    is unavailable. Subject is always 'Re: <parent_subject>' so Zoho threading
    holds — Anthropic may rewrite the subject but the engine overrides it.
    """
    if day_number not in (2, 3):
        return _templated_followup(business, parent_subject, day_number)

    if not _api_key():
        return _templated_followup(business, parent_subject, day_number)

    label, guidance = FOLLOWUP_GUIDANCE[day_number]
    raw = _call_anthropic(_build_followup_prompt(business, parent_subject,
                                                  parent_body, day_number, guidance))
    if not raw:
        return _templated_followup(business, parent_subject, day_number)
    parsed = _parse_haiku_json(raw)
    if parsed is None:
        return _templated_followup(business, parent_subject, day_number)

    # Threading subject — force Re: prefix even if Haiku rewrote it.
    subj = parsed["subject"]
    if not subj.lower().startswith("re:"):
        subj = f"Re: {parent_subject}"
    return {"subject": subj, "body": parsed["body"]}
