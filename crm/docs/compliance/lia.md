# Legitimate Interests Assessment — Cold B2B Outbound Email

**Controller:** Innovite (UK)
**Activity assessed:** Sending unsolicited B2B email to UK limited companies, LLPs, and PLCs on behalf of agency clients.
**Lawful basis:** UK GDPR Article 6(1)(f) — legitimate interests.
**Last reviewed:** 2026-05-16. Annual review.

## Purpose test — is there a legitimate interest?

Yes. The processing supports two interrelated interests:

1. **Innovite's commercial interest** — generating revenue by offering
   outbound lead-generation services to UK B2B service firms.
2. **The client's commercial interest** — finding new B2B customers for
   their services.

Both are clearly identified, lawful commercial interests recognised by
ICO guidance on direct marketing (see ICO *Direct Marketing Guidance*,
2024).

## Necessity test — is processing necessary?

Yes. Email is the proportionate channel:
- Less intrusive than telephone calls (no real-time interruption).
- Less intrusive than postal mail (no physical waste, can be deleted in
  one second).
- The volume is targeted (under 100 messages/day across all clients),
  not bulk spray.
- The recipients are decision-makers at their *work* email addresses,
  not personal addresses.
- No alternative achieves the same outcome at the same cost — opt-in
  marketing requires a list, and we don't have one.

## Balancing test — do the recipient's rights override the interest?

No, provided we:

1. **Scope to corporate subscribers only** (Ltd, PLC, LLP). Sole traders
   and ordinary partnerships are "individual subscribers" under PECR
   Reg. 22 and require consent — the CRM filters these out at the
   Companies House enrichment step (a missing CH match prevents
   drafting; see `crm/pipeline/runner.py` compliance gate).
2. **Email decision-makers at their work address** (`name@company.co.uk`),
   not personal accounts.
3. **Provide a simple opt-out** in every email:
   - Plain-text unsubscribe link in the footer
   - RFC 8058 one-click List-Unsubscribe header (Gmail/Outlook native button)
   - Reply-with-stop honoured as a manual suppression
4. **Honour opt-outs without undue delay** (immediate via the
   `/unsubscribe` endpoint; manual replies within 24 hours).
5. **Retain proof of opt-out indefinitely** (the `crm.suppressed_addresses`
   table is never purged — required to prove we didn't re-contact).
6. **Cap retention** of non-responder data at six months from last
   contact attempt (see `retention.md`).
7. **Maintain a public privacy notice** at innovite.io/privacy citing
   legitimate interests as the basis and the right to object.

## Recipients' reasonable expectations

A decision-maker at a UK limited company can reasonably expect to
receive occasional cold B2B emails relevant to their role — this is
standard commercial practice. The hook-based personalisation (year-end
imminent, accounts overdue, recent director change) demonstrates the
emails are targeted, not bulk: a recipient reading the email sees that
it was written for *their* company, not blast-sent to a list.

## Safeguards in the CRM

- Sole trader / non-LLP filter at draft time (`pipeline/runner.py`).
- Operator approval queue (`/approvals`) — every Day-1 cold touch is
  reviewed by a human before sending. Follow-ups (Day 3, Day 7) ship
  automatically because they're not the cold touch.
- Per-mailbox daily caps (max 50/day) prevent volume from looking like
  bulk marketing.
- Auto-suppression on hard bounce, negative reply, and one-click unsubscribe.

## Outcome

Processing is lawful under Article 6(1)(f) provided the safeguards
above remain in place. Re-assess if any of the following change:

- Sole-trader filter removed or bypassed
- Operator approval gate removed
- Volume exceeds 100 messages/day per recipient domain
- Targeting expanded outside corporate subscribers
- Opt-out infrastructure removed or degraded
- Sector expansion into regulated industries with elevated sensitivity

## Sign-off

| Role | Name | Date |
|---|---|---|
| Controller | Sammy Bimpson | __________ |
| Reviewer (annual) | — | __________ |
