"""Public Instagram profile snapshot.

Single HTTP fetch of IG's web_profile_info JSON endpoint — no login,
no Multilogin, no Playwright. Returns the data the Vidora audit
actually needs:

  - follower / following / post counts
  - bio + verified flag + business category
  - last post timestamp + cadence signal
  - 12 most recent posts: thumbnail URL, likes, comments, caption,
    is_video, taken_at

The endpoint is what instagram.com itself calls from the browser when
you open a profile page. It's gated by a fixed X-IG-App-ID header
(936619743392459 — the public web app id) and works without a logged-
in session as long as the requesting IP isn't already burned. The
proxy pool handles the IP rotation; wreq handles the TLS impersonation
so the request looks like a real Chrome browser.

If IG ever blocks anonymous access entirely (they tighten this every
few months), snapshot() returns None and the audit caller treats the
lead as un-scrappable — the pipeline continues, the lead just doesn't
get an IG section in its audit.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from scraper import client

logger = logging.getLogger("crm.scraper.instagram_snapshot")

_API_URL = "https://www.instagram.com/api/v1/users/web_profile_info/?username={}"

# Headers that match what a logged-out Chrome browser sends when the
# profile page JS calls this endpoint. The X-IG-App-ID is public —
# it's hard-coded in IG's own webpack bundles.
_HEADERS = {
    "X-IG-App-ID":     "936619743392459",
    "Accept":          "*/*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer":         "https://www.instagram.com/",
    "Sec-Fetch-Dest":  "empty",
    "Sec-Fetch-Mode":  "cors",
    "Sec-Fetch-Site":  "same-origin",
    "X-ASBD-ID":       "129477",
    "X-IG-WWW-Claim":  "0",
}

# IG handles are 1-30 chars, letters/digits/underscores/periods.
_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


def snapshot(handle: str) -> dict | None:
    """Fetch + parse the public profile of `handle`. Returns None on any failure.

    Failures (all logged, none raised):
      - bad handle format
      - proxy pool empty / wreq missing
      - IG returned non-JSON (likely a challenge / login wall)
      - IG returned {"data": {"user": null}} (handle doesn't exist / private+hidden)
    """
    h = (handle or "").strip().lstrip("@").lower()
    if not h or not _HANDLE_RE.match(h):
        logger.warning("snapshot: invalid handle %r", handle)
        return None

    url = _API_URL.format(h)
    # 2MB ceiling — typical profile JSON is 30-80KB but mega-accounts
    # (verified, hundreds of fields, full grid metadata) can hit ~1MB.
    body = client.fetch(url, headers=_HEADERS, max_bytes=2_000_000)
    if not body:
        return None

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        logger.warning("snapshot: non-JSON body for %s (len=%d, head=%r)",
                       h, len(body), body[:120])
        return None

    user = ((data or {}).get("data") or {}).get("user")
    if not user:
        logger.info("snapshot: no user object for %s — profile gone, private, or blocked", h)
        return None

    return _shape(h, user)


def _shape(handle: str, user: dict) -> dict:
    """IG's verbose response → flat dict the audit pipeline can use."""
    edges = (user.get("edge_owner_to_timeline_media") or {}).get("edges") or []
    posts = [_post(e.get("node") or {}) for e in edges[:12]]

    last_post_at = posts[0]["taken_at"] if posts else None

    return {
        "handle":              handle,
        "user_id":             user.get("id"),
        "full_name":           user.get("full_name"),
        "biography":           user.get("biography"),
        "is_verified":         bool(user.get("is_verified")),
        "is_private":          bool(user.get("is_private")),
        "is_business_account": bool(user.get("is_business_account")),
        "business_category":   user.get("business_category_name") or user.get("category_name"),
        "external_url":        user.get("external_url"),
        "profile_pic_url":     user.get("profile_pic_url_hd") or user.get("profile_pic_url"),
        "follower_count":      (user.get("edge_followed_by") or {}).get("count"),
        "following_count":     (user.get("edge_follow") or {}).get("count"),
        "post_count":          (user.get("edge_owner_to_timeline_media") or {}).get("count"),
        "last_post_at":        last_post_at,
        "posts":               posts,
    }


def _post(node: dict) -> dict:
    return {
        "shortcode":         node.get("shortcode"),
        "thumbnail_url":     node.get("thumbnail_src") or node.get("display_url"),
        "display_url":       node.get("display_url"),
        "is_video":          bool(node.get("is_video")),
        "like_count":        (node.get("edge_liked_by") or node.get("edge_media_preview_like") or {}).get("count"),
        "comment_count":     (node.get("edge_media_to_comment") or {}).get("count"),
        "video_view_count":  node.get("video_view_count"),
        "caption":           _first_caption(node),
        "taken_at":          _iso_from_ts(node.get("taken_at_timestamp")),
    }


def _first_caption(node: dict) -> str | None:
    edges = (node.get("edge_media_to_caption") or {}).get("edges") or []
    if not edges:
        return None
    txt = (edges[0].get("node") or {}).get("text")
    if not txt:
        return None
    return txt[:280]


def _iso_from_ts(ts) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None
