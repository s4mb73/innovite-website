# Innovite CRM — Data Retention Policy

**Owner:** Sammy Bimpson
**Last reviewed:** 2026-05-16
**Cadence:** Annual

UK GDPR Article 5(1)(e) — the storage limitation principle — requires
that personal data be kept in identifiable form no longer than necessary
for the purposes for which it's processed. This document defines those
periods for each category of data the CRM holds.

## Retention windows

| Data | Retention | Trigger to delete | Where |
|---|---|---|---|
| Cold leads who never replied | **6 months** from last contact attempt | Automated nightly job (TODO — see `pipeline/runner.py`) | `crm.leads` |
| Cold leads who replied (positive / neutral / negative / OOO) | Lifetime of the commercial relationship + 6 years | Manual review; export then delete | `crm.leads`, `crm.replies`, `crm.emails` |
| Booked meetings / won deals | Lifetime of the relationship + 6 years (limitation period) | Manual | `crm.leads` (status='meeting'/'won') |
| Form submissions from innovite.io | 12 months from submission if no engagement | Automated (TODO) | `crm.inbound_leads` |
| Email content (sent/received bodies) | Same as the parent lead | Cascade from lead deletion | `crm.emails`, `crm.replies` |
| Suppression list (`crm.suppressed_addresses`) | **Indefinite** — required to prove we didn't re-contact | Never | `crm.suppressed_addresses` |
| Operator activity log | 12 months | Automated (TODO) | `crm.activity_log` |
| Pipeline run history | 90 days | Automated (TODO) | `crm.pipeline_runs` |
| Outreach actions audit | 12 months | Automated (TODO) | `crm.outreach_actions` |

## Why these specific numbers

- **6 months for non-responders**: industry norm for cold B2B prospecting
  data. Long enough to support re-cadence after job changes or sector
  shifts; short enough that we're not hoarding stale data.
- **6-year tail on engaged leads**: matches the UK contract limitation
  period (Limitation Act 1980 s.5). If a deal goes wrong we may need
  the correspondence to defend or pursue a claim.
- **Indefinite suppression**: legally required. If a recipient opts out
  and we re-contact them six years later, we breach Article 21 (right
  to object) — only way to prove we didn't is to keep the record forever.
- **12 months on form submissions**: the marketing-site form is a public
  intake; submitters who never re-engaged have no expectation we retain
  their data indefinitely.

## How a subject access request is handled

1. Operator receives the request (email or via the privacy notice).
2. Acknowledge receipt within **48 hours**, full response within **30 days**
   (Article 12(3)).
3. Query the CRM for any record matching the requester's email address
   or company name across `leads`, `emails`, `replies`, `inbound_leads`,
   `activity_log`, `outreach_actions`.
4. Bundle as CSV or PDF and email to the address on file.
5. Log the request and response in a request-log file (kept indefinitely).

## How an erasure request is handled

1. Acknowledge within 48 hours.
2. Add the email address to `crm.suppressed_addresses` with
   `reason='unsubscribe', added_by='dsar', detail='right to erasure'`.
3. Run `update crm.leads set ... = null` on all PII fields for matching
   rows (preserve the lead row + suppression record; null out
   identifiers). Do not hard-delete the lead row — the suppression
   table needs to be able to reference back if challenged.
4. Confirm completion to the requester within 30 days.

## Outstanding work

The auto-purge jobs in the table above are not yet implemented. Per
agreement with the engineering team, these will land as Week-N work on
the CRM backend roadmap:

- [ ] Nightly job: delete `crm.leads where status='new' and
      last_contact_at < now() - interval '6 months'`
- [ ] Nightly job: delete `crm.activity_log where created_at < now() - interval '12 months'`
- [ ] Nightly job: delete `crm.pipeline_runs where created_at < now() - interval '90 days'`
- [ ] Nightly job: delete `crm.outreach_actions where created_at < now() - interval '12 months'`

Until these land, retention is enforced manually — review quarterly.
