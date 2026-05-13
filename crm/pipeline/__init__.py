"""Innovite CRM — pipeline package.

The Pipeline Runner: discovers candidate businesses for a client, enriches
across multiple data sources, scores them, and drafts Day-1 emails into the
crm.emails queue.

Architecture
------------
- The Flask web app writes a row to crm.pipeline_runs with status='pending'
  and returns immediately. Gunicorn workers must not block on a 3-5 minute
  pipeline.

- crm-worker.service (a long-running systemd unit) polls pipeline_runs
  for pending rows, claims one at a time (SELECT FOR UPDATE SKIP LOCKED),
  and processes it via runner.run(run_id).

- runner.run dispatches to source modules sequentially: discovery first
  (Google Places), then enrichment (Companies House, Apollo, ...).

- Each source module is a stateless function that takes a lead dict and
  returns an enriched dict. New sources slot in without touching the runner.

- scoring.grade() assigns A/B/C/D/F from the enriched profile.
- drafter.draft_day1() generates the personalised Day-1 email via Anthropic.

Why a package and not loose modules
-----------------------------------
Outreach, Reply, and Reporting engines will share retry/backoff, the
Anthropic client wrapper, and the scoring primitives. Drawing the package
boundary now prevents later engines from re-importing tangled flat files.
"""
