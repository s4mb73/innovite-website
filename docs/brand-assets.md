# Brand assets

Source files for the Innovite mark + spec for everything you'll need across socials.

All SVG files live in `/brand/`. They scale to any size — export to PNG/JPG at the dimensions a platform needs.

---

## What's in /brand/

| File | Contents | Use for |
|---|---|---|
| `mark.svg` | Bare bar + dot mark in `#8FB7FF`, transparent background | Anywhere you need just the mark, on a dark or coloured background |
| `mark-on-light.svg` | Same mark in `#3d7cf5` (saturated variant) | Mark on white / light backgrounds where the lighter blue lacks contrast |
| `profile-dark.svg` | Mark centred on a dark rounded square (`#0b0c11`), 1024×1024 | **Profile picture** for LinkedIn, Instagram, Twitter, TikTok, YouTube |
| `profile-blue.svg` | Mark on solid blue rounded square, 1024×1024 | Alternative profile picture — higher visibility at small sizes |

---

## Profile picture — recommended

**Use `profile-dark.svg` everywhere.**

- Reads as "premium agency / consultancy" rather than "tech startup"
- Holds up at small thumbnail sizes (LinkedIn 30px, IG 32px, Twitter 24px)
- Platforms that crop to a circle (LinkedIn / IG / Twitter / TikTok) just lose the rounded corners — the mark stays visible
- Aligns visually with the website hero (dark background, blue accent)

### Export workflow
1. Open `profile-dark.svg` in [Figma](https://figma.com) (free), [Affinity Designer](https://affinity.serif.com), [Inkscape](https://inkscape.org), or any vector tool
2. Export as PNG at **1000×1000** (works for every platform — they downscale automatically)
3. Upload to each platform's profile picture slot

Or use a one-click online converter (e.g. [svgtopng.com](https://svgtopng.com)) if you don't want to install anything.

---

## Banner / cover image — needs a designer

The wordmark "innovite" sits in [Outfit](https://fonts.google.com/specimen/Outfit) at weight 600. Embedding a font in raw SVG is fragile, so banner images are a real-designer job (Figma / Photoshop / Affinity).

Brief whoever you commission with:

### Dimensions per platform

| Platform | Size | Notes |
|---|---|---|
| LinkedIn company page cover | **1128 × 191 px** | Logo lower-left overlays the cover — keep the lower-left ~250×120 area visually quiet |
| LinkedIn personal cover | **1584 × 396 px** | Same lower-left logo overlay rule |
| Twitter / X header | **1500 × 500 px** | Profile pic overlays bottom-left |
| Instagram (profile) | n/a (just profile picture) | Reels cover thumbnails: 1080×1920, 16:9 safe area |
| Facebook page cover | **1200 × 630 px** | |
| YouTube channel art | **2560 × 1440 px** | Safe-area for mobile is centre 1546×423 |
| Email signature | **600 × auto** | Use `mark-on-light.svg` exported to 600 wide PNG |

### Design system (must follow)

| Token | Value |
|---|---|
| Background | `#0b0c11` near-black |
| Primary accent | `#8FB7FF` light periwinkle |
| Saturated accent (for light bg) | `#3d7cf5` |
| Text colour on dark bg | `#ecedf3` |
| Heading font | Newsreader serif (regular weight or italic) |
| Body / wordmark font | Outfit, weight 600 |
| Mark | `/brand/mark.svg` — do not redraw, do not recolour, do not stretch |
| Wordmark spelling | lowercase "innovite", letter-spacing -0.3px |

### Three layout directions

**A — Type only** *(recommended for first banner)*
Big italic Newsreader serif on the left side: *"Clients find you."* on top line, *"Not the other way around."* on bottom in `#8FB7FF`. Right ⅓: empty (so platform overlays don't clobber the type) or three small metric tiles (Reply rate / Booked meetings / Pipeline growth).

**B — Real content frame**
Behind-the-scenes still from a Vidora content shoot, desaturated and tinted blue. Wordmark "innovite" + tagline "AI lead gen for B2B service firms" overlay in the top-left.

**C — Diagram**
Clean diagram showing the three pillars (AI Outbound → Content → Paid ads) feeding into one pipeline icon. Same Phosphor SVG style used in the website's Services section. Wordmark top-left, tagline bottom-right.

### Anti-patterns (don't ship)

- ❌ Stock business-handshake / cityscape photography
- ❌ Generic AI imagery (head with circuit board, glowing brain)
- ❌ Gradient meshes
- ❌ More than one accent colour
- ❌ Sans-serif default fonts (Inter, Arial, system)
- ❌ Drop shadows on the mark
- ❌ Stretched or recoloured mark

---

## Quick social audit — what to update where

When the new mark is exported and uploaded, refresh:

- [ ] **LinkedIn company page** — profile picture, cover image, page tagline, About section (use `docs/linkedin-launch.md`)
- [ ] **Instagram @innovite.io** — profile picture, bio
- [ ] **Twitter / X** — profile picture, header, handle, bio
- [ ] **TikTok** *(if posting)* — profile picture, bio
- [ ] **YouTube** *(if posting)* — channel icon, channel art
- [ ] **Email signature** — embed mark-on-light.svg → PNG 600w
- [ ] **Slack workspace icon** *(internal)* — `profile-dark.svg` exported to 512×512
- [ ] **Resend / sending domain branding** — profile-dark for email-client previews

---

## Asset versioning

Keep `/brand/` as the canonical source. If the brand pack ever evolves:

1. Update `/brand/*.svg` with new versions
2. Update favicon data URI in `index.html`, `privacy.html`, `terms.html`
3. Re-export PNGs for socials
4. Bump the section in `CLAUDE.md` if the design system tokens change
