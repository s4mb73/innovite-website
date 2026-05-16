# Innovite CRM — Compliance Pack

Working documents that demonstrate Innovite considered its UK GDPR / PECR
obligations when designing the CRM and its outbound activity. Not legal
advice — written by the engineering team. Sammy should review with a
data-protection consultant before scaling sends significantly or onboarding
clients in regulated sectors (finance, healthcare, legal).

## Contents

| Doc | Purpose | Owner | Review cadence |
|---|---|---|---|
| `lia.md` | Legitimate Interests Assessment — justifies cold B2B outbound under UK GDPR Art. 6(1)(f) | Sammy | Annual |
| `retention.md` | Data retention + erasure policy | Sammy | Annual |
| `tra.md` | Transfer Risk Assessment for US processors (Anthropic, Apollo) | Sammy | Annual or on processor change |

## When to update

- New processor (e.g. swap Anthropic for OpenAI, add Cognism) → update TRA
- New client sector with elevated sensitivity (finance, healthcare) → review LIA
- New marketing channel (SMS, LinkedIn) → new LIA section per channel
- After any subject access request or ICO complaint → review whichever doc applies

## What we have not done yet

- Formal DPIA (Data Protection Impact Assessment) — not required at current
  scale and processing types per ICO criteria, but worth doing before
  reaching ~1,000 sends/day or onboarding any sector subject to special
  category data.
- Appointed a Data Protection Officer (DPO) — not required for our size or
  processing volume.
- ICO data-controller registration — required, fee depends on size. Check
  whether Innovite is registered: https://ico.org.uk/ESDWebPages/Search

## Privacy notice

Public-facing notice at `innovite.io/privacy`. Covers form submissions
and the AI-qualifier widget. Outbound-prospect coverage (cold-emailed
recipients) is the section to expand next — see `privacy-notice.md` in
this folder for the proposed addition.
