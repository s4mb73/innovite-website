-- Innovite CRM — richer LinkedIn fields parsed from public-profile JSON-LD
--
-- The first cut of the LinkedIn scraper only extracted og:title + og:description
-- (name + current headline). After studying open-source LinkedIn scrapers and
-- inspecting the actual HTML LinkedIn serves to logged-out visitors, we found
-- that every public /in/<slug> page embeds a full schema.org Person entry
-- as JSON-LD — including all the data the original 6-stage plan called for:
-- work history, current employer, location, follower count, and Pulse-post
-- activity dates.
--
-- This migration adds columns for that richer parse. Idempotent.

begin;

alter table crm.leads
  -- Current employer name from JSON-LD worksFor[0].name. Often differs
  -- from companies_house_number's registered name (e.g. CH says
  -- "MICROSOFT LIMITED" but LinkedIn says "Microsoft"). Kept separate.
  add column if not exists linkedin_current_company text,

  -- City/region from JSON-LD address.addressLocality. Useful for
  -- localizing outreach ("Hi Sarah, I noticed you're based in Manchester...")
  add column if not exists linkedin_location text,

  -- alumniOf entries (Organisation only — schools filtered out at the
  -- scraper). text[] so the UI can render each as a separate chip
  -- without parsing.
  add column if not exists linkedin_previous_companies text[],

  -- Top-level interactionStatistic.userInteractionCount when name='Follows'.
  -- Signal of profile reach / influence.
  add column if not exists linkedin_follower_count int,

  -- image.contentUrl from JSON-LD. Could power an avatar in the UI;
  -- also a liveness proxy (deleted profiles have placeholder images).
  add column if not exists linkedin_profile_image_url text,

  -- Most recent Pulse article publication date. The "posts weekly
  -- about business growth" signal the original plan called for —
  -- a recent date here means the person actively publishes.
  add column if not exists linkedin_recent_post_at date,

  -- Title of that most-recent post. Pure outreach gold: "I read your
  -- piece on X" is the single best cold-email opener that exists.
  add column if not exists linkedin_recent_post_title text;

commit;
