# Transfer Risk Assessment — Innovite CRM

**Controller:** Innovite (UK)
**Last reviewed:** 2026-05-16
**Cadence:** Annual or on processor change

UK GDPR Chapter V requires a written assessment of risk for any
personal data transferred outside the UK. This document covers each
processor we route data to, the transfer mechanism, and the residual
risk after safeguards.

## In-scope transfers

| Processor | Country | Purpose | Personal data shared | Transfer mechanism |
|---|---|---|---|---|
| **Supabase** | EU (Frankfurt, Germany) | Database hosting | All CRM data | UK→EU adequacy decision (mutual adequacy with UK GDPR, no SCCs required) |
| **Hetzner** | EU (Falkenstein, Germany) | VPS hosting (compute) | All CRM data at rest in process memory | UK→EU adequacy decision |
| **Anthropic** | US | LLM inference (email drafting + sentiment classification) | Lead name, business name, decision-maker name, lead's enriched signals (sent in the prompt); inbound reply body (sent for sentiment classification) | UK Extension to the EU-US Data Privacy Framework (DPF); Anthropic is DPF-certified |
| **Apollo.io** | US | Contact-data enrichment | Business name + domain sent in queries; decision-maker contact data returned | DPF certification; Standard Contractual Clauses in their DPA as fallback |
| **Resend** | US (for marketing-site form notifications only — not CRM outbound) | Transactional email | Sender contact details from innovite.io form | DPF certification |
| **Plausible Analytics** | EU (Germany) | Marketing-site analytics | Anonymised page-view data, no IP retained | UK→EU adequacy |
| **Zoho** | EU (where mailbox region selected) | SMTP+IMAP for outbound mailboxes | Outbound email content, inbound reply content | UK→EU adequacy (depends on chosen region — confirm per mailbox) |

## Assessment per non-adequacy transfer

### Anthropic (US)

- **Volume**: ~50-500 prompts per day across drafter + sentiment paths.
- **Categories transferred**: Business contact details (B2B, low sensitivity);
  no special category data; no financial or health data.
- **Recipient context**: Anthropic is DPF-certified; their DPA includes
  SCCs as a fallback. Data is processed for inference only, not used
  to train models on by default (verify the workspace setting).
- **Local-law risk**: US surveillance laws (FISA 702, EO 12333) apply
  to all US processors. For B2B contact data at this volume, the risk
  of disclosure to US authorities under FISA is negligible — Innovite is
  not a target of intelligence interest.
- **Supplementary measures**:
  - Workspace setting: opt out of training on our prompts (verify).
  - No PII beyond business contact data sent — never bank details,
    health info, etc.
  - Prompts and responses are not stored by us beyond the immediate
    Day-1 draft in `crm.emails`.
- **Residual risk**: Low. Transfer is necessary and proportionate.

### Apollo.io (US)

- **Volume**: ~100-1,000 enrichment lookups per day during active runs.
- **Categories transferred**: Business name + domain in the query;
  decision-maker name, title, email, LinkedIn URL returned.
- **Recipient context**: Apollo is a major B2B data provider, DPF-certified.
  They are the *source* for most of the data we then store — the
  transfer is bilateral (we query, they return).
- **Local-law risk**: Same FISA analysis as Anthropic. Negligible for
  B2B contact data.
- **Supplementary measures**:
  - Only business-context data sent (no identifying personal data
    about the operator or our clients).
  - Apollo DPA on file (download from their dashboard; store in this
    folder if not already).
- **Residual risk**: Low.

### Resend (US)

- **Volume**: low — auto-response per form submission, ~10/week.
- **Categories transferred**: name, email address, form answers.
- **Mechanism**: DPF.
- **Residual risk**: Low. Volume is small, data is B2B contact info.

## Conclusion

All transfers are either (a) within the UK/EU adequacy bubble or (b)
covered by the UK Extension to the EU-US Data Privacy Framework with
appropriate processor agreements. No transfer requires Standard
Contractual Clauses + supplementary measures beyond what the DPF
provides.

## Action items

- [ ] Verify Anthropic workspace setting opts out of training on our
      prompts. Document the workspace ID.
- [ ] Download Apollo DPA and store in `crm/docs/compliance/processors/`.
- [ ] Confirm Zoho mailbox region for each connected mailbox is EU
      (mailbox setup page in Zoho admin).
- [ ] Re-assess if any processor changes its DPF status or moves
      processing region.
