# Banner image prompts — paste-ready for ChatGPT / Midjourney

Each prompt below is paste-ready. Use ChatGPT's image-gen flow (GPT-4o image generation) or Midjourney with the noted aspect ratio.

**Honest expectation**: ChatGPT-generated banners are a *starting point*, not a final asset. The output won't perfectly render the wordmark or hit brand colours exactly. Take any banner you like into Figma, lay the real `/brand/mark.svg` on top, retype the wordmark in Outfit if needed, and export the final PNG at the spec dimensions.

---

## Master prompt (LinkedIn personal cover — 1584 × 396)

This is the workhorse. Copy and paste into ChatGPT's image gen.

```
Design a LinkedIn personal cover image for Innovite, an AI lead-generation
agency for B2B service firms in the UK. The site lives at innovite.io.
Brand voice: confident founder, direct, anti-fluff. Sits next to Linear,
Vercel, Stripe — premium but not corporate, never generic SaaS.

Output: 1584 × 396 px banner (4:1 landscape). The composition must work
at this exact aspect ratio.

Visual style — strict rules:
- Background: near-black #0b0c11 (not pure black)
- Primary accent: light periwinkle blue #8FB7FF
- Single accent only. NO purple, NO gradients beyond subtle radial glows,
  NO neon, NO film grain, NO 3D, NO bevels
- Typography: editorial serif for display lines (italic feel), clean sans
  for body. Think Newsreader + Outfit
- Restraint over decoration

Composition:
Left third — italic editorial serif headline in light periwinkle:
  "Clients find you." on top line
  "Not the other way around." on bottom line
Text sits with breathing room, centred vertically in the left third.

Middle third — empty negative space, a very subtle circular accent glow
at low opacity is OK but not required.

Right third — three small monoline geometric icons in a horizontal row,
representing the three service pillars: a magnifying glass, a video
camera, and a target / bullseye. Each rendered in light periwinkle
outline only (stroke 1.5, light weight, like Phosphor Icons), on the
dark background, evenly spaced.

Lower-left safe zone:
LinkedIn overlays the profile photo in the lower-left of the cover.
Keep the lower-left ~250×250 px area visually quiet — no critical
content, no text, no icons there.

Strictly avoid:
- Stock business-handshake or cityscape photography
- Generic AI imagery (heads with circuit boards, glowing brains,
  neural network visualisations)
- Mountains, swooshes, rocket ships
- Sans-serif default fonts (Inter, Arial, system)
- Drop shadows, vignettes, or texture overlays

Output: high-resolution PNG, 1584 × 396 px, dark theme, premium.
```

After ChatGPT generates: download the best variant, take it into Figma,
overlay the real wordmark from `/brand/mark.svg` on top if the AI didn't
render it cleanly, export final PNG at 1584 × 396.

---

## Variants per platform

For other platforms, **start from the master prompt above** and change the
relevant lines noted below.

### LinkedIn company page cover — 1128 × 191

Aspect ratio is much wider/shallower than personal. Composition adjustment:

> Replace `1584 × 396 px banner (4:1 landscape)` with `1128 × 191 px banner (~6:1 ultra-wide landscape)`. The wider aspect needs a single horizontal headline, not two stacked lines. Use one line: `Clients find you. Not the other way around.` running across the left two-thirds.

### Twitter / X header — 1500 × 500

> Replace dimensions with `1500 × 500 px banner (3:1 landscape)`. The profile photo overlays the bottom-left of this header — keep the bottom-left ~250×250 quiet. Same composition direction otherwise.

### Facebook page cover — 1200 × 630

> Replace dimensions with `1200 × 630 px banner (~2:1 landscape, slight widescreen)`. No specific safe-zone overlay. Can use the full master composition.

### YouTube channel art — 2560 × 1440

> Replace dimensions with `2560 × 1440 px banner (16:9 landscape)`.
>
> CRITICAL safe-area rule: YouTube renders the centre 1546 × 423 px area on
> mobile devices. Anything outside that gets cropped on mobile. Place the
> headline + icon row entirely within the centred 1546 × 423 area. The
> outer borders should be the dark background only — visually inert.

### Instagram Reel cover — 1080 × 1920 (vertical)

The aspect ratio flips to portrait. Re-compose:

> Replace dimensions with `1080 × 1920 px portrait`.
>
> Stack the composition vertically:
> - Top third: italic Newsreader serif headline `Clients find you. Not the other way around.` (each phrase on its own line, centred horizontally)
> - Middle third: empty negative space with a subtle accent glow
> - Bottom third: the three monoline icons (magnifying glass, video camera, target) stacked vertically OR arranged in a horizontal row centred at the bottom — whichever the model produces more cleanly
>
> Keep the top 250 px and bottom 250 px clear of essential content (Instagram overlays UI elements there).

### Email signature banner — 600 × 200

Different use case — this needs to render at small sizes in email clients,
many of which ignore CSS. Output should be safer and lower-fidelity.

> Replace dimensions with `600 × 200 px banner (3:1 landscape)`.
>
> Simplify the composition:
> - Left half: the wordmark + mark lockup (use the same lockup as on the website)
> - Right half: the tagline `AI lead gen for B2B service firms` in clean sans (Outfit 500), single line, in `#8FB7FF`
> - No decorative icons, no glows, no extra elements

For an email signature it's actually safer to make this in Figma manually
than to let an AI generate it — more typography control.

---

## Workflow

1. Open ChatGPT → image gen flow → paste the relevant prompt above
2. Generate 2–4 variants
3. Download the strongest one
4. Open in Figma (free)
5. Verify colours: any `#8FB7FF` accent that drifted should be hand-fixed
6. Verify wordmark legibility — if AI rendered "innovite" badly, hide that
   layer and replace with proper Outfit text (you have the font installed
   if you've signed in to Google Fonts in Figma)
7. Layer `/brand/mark.svg` on top if the AI didn't include it
8. Export at the platform's spec dimensions, JPG quality 85% or PNG

---

## Stricter alternative: just commission a designer

If you want banners that don't need post-production tweaking, brief a
designer with `/docs/brand-assets.md` and ask for the seven banners
listed there. £150-300 total via 99designs / Fiverr Pro / a freelance
designer who does B2B SaaS work. Output will be cleaner and consistent
across platforms, plus you get editable Figma source files for future
tweaks.

For day-one launch, ChatGPT-generated banners do the job. Iterate later.
