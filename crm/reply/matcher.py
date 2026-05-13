"""Match an inbound message back to the outbound email + lead.

Strategy, in order (per backend-plan §SERVICE 4):

1. In-Reply-To header → exact match against crm.emails.message_id.
   Confident match.

2. References header chain → any one of our message_ids appears.
   Confident match against that lead.

3. from_address matches a leads.decision_maker_email AND we have a
   recent (last 30 days) email sent to that lead.
   Heuristic match — logged with reply_match='heuristic' so the operator
   can see we are not 100% sure.

4. No match → returns (None, None). The engine writes a replies row
   with lead_id=null so the Inbox > Needs you surfaces it for manual
   triage.

Returned dicts are slim — just the ids and a match-kind label for audit.
"""
from __future__ import annotations

import db


def _parse_msgids(header_value: str | None) -> list[str]:
    """Pull <id@host> tokens out of a header value.

    RFC 5322 allows whitespace, commas, etc. between angle-bracketed IDs.
    Cheap parser: split on '>', keep anything containing '<'.
    """
    if not header_value:
        return []
    out: list[str] = []
    for chunk in header_value.split(">"):
        if "<" not in chunk:
            continue
        out.append("<" + chunk.split("<", 1)[1].strip() + ">")
    return out


def find_email_for_inbound(headers: dict, from_address: str | None) -> dict | None:
    """Return matched {email_id, lead_id, client_id, match_kind} or None.

    Caller passes a dict of headers (case-insensitive keys are fine; we
    normalise) and the inbound from address.
    """
    h = {k.lower(): v for k, v in (headers or {}).items()}

    # 1. In-Reply-To
    in_reply_to = h.get("in-reply-to")
    msgids = _parse_msgids(in_reply_to)
    if msgids:
        rows = db.fetch_all(
            "select id, lead_id, client_id from crm.emails "
            "where message_id = any(%s) order by id desc limit 1",
            (msgids,),
        )
        if rows:
            r = rows[0]
            r["match_kind"] = "in_reply_to"
            return r

    # 2. References chain — Zoho splits these with whitespace
    refs = _parse_msgids(h.get("references"))
    if refs:
        rows = db.fetch_all(
            "select id, lead_id, client_id from crm.emails "
            "where message_id = any(%s) order by id desc limit 1",
            (refs,),
        )
        if rows:
            r = rows[0]
            r["match_kind"] = "references"
            return r

    # 3. Heuristic: from_address ↔ decision_maker_email with a recent send.
    if from_address:
        rows = db.fetch_all(
            """
            select e.id, e.lead_id, e.client_id
            from crm.emails e
            join crm.leads l on l.id = e.lead_id
            where lower(l.decision_maker_email) = lower(%s)
              and e.sent_at is not null
              and e.sent_at >= now() - interval '30 days'
            order by e.sent_at desc
            limit 1
            """,
            (from_address,),
        )
        if rows:
            r = rows[0]
            r["match_kind"] = "heuristic"
            return r

    return None
