# EchoCard — Frontend UI Design System & Philosophy

> A complete reference for designing and building pages in the EchoCard style. Use this as a skill/rule when working on any EchoCard frontend to stay consistent with brand, layout, copy tone, animation, and component patterns.

---

## 1. Brand Identity

### Color Tokens (Tailwind CSS 4 `@theme`)

| Token | Value | Usage |
|---|---|---|
| `brand-cream` | `#FDFBF7` | Page background, light section bg |
| `brand-cream-dark` | `#F3EFE6` | Subtle inset surfaces, tab rails |
| `brand-orange` | `#D97706` | Primary accent — CTAs, highlights, links, eyebrows |
| `brand-ink` | `#064E3B` | Primary text, dark surfaces, navbar, footers |

**Rule:** Never use raw black (`#000`) or generic gray (`#6b7280`) — always use `brand-ink` with opacity (e.g. `text-brand-ink/70`, `text-brand-ink/40`). This keeps everything warm and on-brand.

### Typography

- **Font family:** Space Grotesk (both `--font-sans` and `--font-serif` map to it)
- **Weight system:**
  - `font-black` (900) — headlines, section labels, card names, CTAs
  - `font-bold` / `font-semibold` — subheadings, nav links, badge text
  - `font-medium` — body, captions, supporting info
- **Tracking conventions:**
  - Eyebrow / section labels: `tracking-[0.2em]` or `tracking-widest` + `uppercase` + `text-[10px]` or `text-[11px]`
  - Headlines: `tracking-tight` — tight for large type
  - Body: default tracking (no explicit setting)

### Spacing & Scale

- **Max content width:** `max-w-7xl` (sections), `max-w-4xl` (CTA / narrow sections)
- **Section vertical rhythm:** `py-24 lg:py-36` standard; `py-28 lg:py-40` for hero-adjacent sections
- **Horizontal padding:** `px-6` consistently on all section wrappers
- **Card padding:** `p-7 sm:p-9` (standard cards), `p-8 sm:p-10` (feature/editorial cards), `p-9 sm:p-11` (large editorial blocks)
- **Border radius:** `rounded-2xl` (small cards), `rounded-3xl` (larger cards), `rounded-[2rem]` or `rounded-[2.2rem]` (carousel cards), `rounded-full` (pills, buttons, navbar)

---

## 2. Design Philosophy

### Human-First, Not AI-Generic

EchoCard's UI is deliberately **not** the template AI outputs by default. The principles:

1. **Lead with emotion, not features.** Headlines should address a feeling or pain point ("You hand out your card. Three days later, nobody remembers you.") before explaining what the product does.
2. **Avoid symmetrical 3-column icon grids.** These read as AI-generated. Use asymmetric layouts, editorial columns, and varied card sizes instead.
3. **Generous whitespace.** Less is more. A card with 4 elements and breathing room beats a card with 8 elements.
4. **Authentic copy.** Write like a founder who genuinely cares — conversational, warm, occasionally blunt. No buzzword soup.
5. **Minimal badges and labels.** One label per element max. No badge + icon + uppercase label + subtitle all on the same element.

### Editorial Approach to Sections

Every major section should feel like **one narrative act**, not a list of features:

- **ACT I — The Problem** (visceral, relatable pain point)
- **ACT II — Real People Using It** (social proof via persona vignettes)
- **ACT III — How It Works** (show, don't tell — mockups over bullet lists)
- **ACT IV — Beliefs / Principles** (brand voice, not feature list)

---

## 3. Layout Patterns

### Asymmetric Editorial Grid

```jsx
// The hero "problem" layout — 60/40 split
<div className="grid lg:grid-cols-[1fr_0.6fr] gap-12 lg:gap-20 items-center">
  <div>  {/* Big left: headline + prose */}  </div>
  <div>  {/* Right: stat punch cards, mockup, or visual */}  </div>
</div>
```

Use `lg:grid-cols-[1fr_0.6fr]` or `lg:grid-cols-[0.6fr_1fr]` for intentional imbalance — more interesting than `lg:grid-cols-2`.

### Tiled Grid (borderless, seamless)

```jsx
// "What we believe" — 3 beliefs without individual card boxes
<div className="grid lg:grid-cols-3 gap-px bg-brand-ink/6 rounded-3xl overflow-hidden border border-brand-ink/6">
  {items.map((item, i) => (
    <div key={i} className="bg-[#FDFBF7] p-8 sm:p-10 group hover:bg-white transition-colors duration-300">
      {/* content */}
    </div>
  ))}
</div>
```

`gap-px` with a shared background on the grid creates thin dividers without explicit border elements — feels seamless and editorial.

### Floating Pill Navbar

```jsx
<header className="fixed top-6 left-1/2 -translate-x-1/2 z-50 w-[90%] max-w-4xl">
  <div className="bg-white/90 backdrop-blur-xl border border-brand-ink/10 shadow-lg rounded-full px-4 py-2 flex items-center justify-between">
    {/* Logo | Nav Links | CTA */}
  </div>
</header>
```

- **Always floating**, never static
- `backdrop-blur-xl` + `bg-white/90` — glassmorphism feel
- `rounded-full` — pill shape, no corners
- CTA button inside navbar: `rounded-full bg-brand-ink text-brand-cream` — small, tight

---

## 4. Component Patterns

### Section Eyebrow / Label

The standard way to label a section — a subtle horizontal rule + small-caps text. **Never use a pill badge for section labels** (too decorative):

```jsx
<div className="flex items-center gap-4 mb-10">
  <span className="w-8 h-px bg-brand-orange" />
  <p className="text-[11px] font-black uppercase tracking-[0.2em] text-brand-ink/50">
    People already using it
  </p>
</div>
```

Alternatively for hero eyebrows, use a pill — but only once per section:
```jsx
<span className="inline-flex items-center gap-2 bg-brand-orange/10 text-brand-orange px-4 py-1.5 rounded-full text-xs font-black uppercase tracking-widest border border-brand-orange/20">
  <Zap className="w-3 h-3" />
  The End of Lost Connections
</span>
```

### Stat Punch Card

Two-box vertical stack for bold numerical storytelling:

```jsx
// Light box
<div className="bg-white border border-brand-ink/8 rounded-3xl p-8 shadow-sm relative overflow-hidden">
  <div className="absolute top-0 right-0 w-32 h-32 bg-brand-orange/5 rounded-full blur-2xl" />
  <p className="text-[68px] sm:text-[80px] font-black text-brand-ink leading-none tracking-tighter">
    88<span className="text-brand-orange">%</span>
  </p>
  <p className="text-sm font-semibold text-brand-ink/60 mt-2 max-w-[180px]">
    of paper business cards are thrown away within a week
  </p>
</div>

// Dark box
<div className="bg-brand-ink rounded-3xl p-8 shadow-lg relative overflow-hidden">
  <div className="absolute bottom-0 left-0 w-40 h-40 bg-brand-orange/10 rounded-full blur-3xl" />
  <p className="text-[68px] sm:text-[80px] font-black text-brand-cream leading-none tracking-tighter">
    5<span className="text-brand-orange">×</span>
  </p>
  <p className="text-sm font-semibold text-brand-cream/50 mt-2 max-w-[200px]">
    more likely to close a deal with a structured WhatsApp follow-up
  </p>
</div>
```

**Key:** Text is huge (`text-[68px]`), supporting copy is tiny and muted. The contrast does the work.

### Editorial Feature Block (with ghost watermark number)

```jsx
<div className="group relative bg-white border border-brand-ink/8 rounded-[2rem] p-9 sm:p-11 overflow-hidden hover:shadow-xl transition-shadow duration-500">
  {/* Corner orb */}
  <div className="absolute -top-20 -right-20 w-64 h-64 rounded-full bg-brand-orange/8 blur-3xl group-hover:bg-brand-orange/14 transition-all duration-700 pointer-events-none" />

  <div className="relative z-10">
    {/* Ghost watermark number */}
    <span className="text-[80px] font-black text-brand-ink/5 leading-none absolute -top-4 -left-1 select-none">01</span>

    <div className="relative">
      <p className="text-[11px] font-black uppercase tracking-[0.18em] text-brand-orange mb-4">Share Instantly</p>
      <h3 className="text-3xl sm:text-4xl font-black text-brand-ink leading-tight mb-5">
        Your card.<br />Their phone.<br /><span className="text-brand-orange">Done in 2 seconds.</span>
      </h3>
      <p className="text-brand-ink/65 text-sm leading-relaxed mb-7 max-w-md">
        Body copy here. Short. Honest.
      </p>
      {/* Inline mini mockup or feature tags below */}
    </div>
  </div>
</div>
```

Dark variant — swap `bg-white` for `bg-gradient-to-br from-[#0A2E22] to-[#061A13]`, text to `text-white`, orb to emerald.

### Carousel Card (Persona / Digital Business Card)

The minimal, realistic business card design:

```jsx
<div className="rounded-[2rem] bg-white border border-brand-ink/8 shadow-[0_20px_60px_-12px_rgba(28,25,23,0.10)] hover:shadow-[0_28px_70px_-12px_rgba(28,25,23,0.15)] transition-shadow duration-500 overflow-hidden">

  {/* Thin accent color bar — the only branding flourish */}
  <div className="h-1 w-full" style={{ background: `linear-gradient(90deg, ${accentColor}, ${accentColor}55)` }} />

  <div className="p-8 sm:p-10">
    {/* Circle monogram avatar */}
    <div className="flex items-start gap-5 mb-8">
      <div
        className="w-14 h-14 rounded-full flex items-center justify-center text-base font-black shrink-0 border-2"
        style={{ borderColor: accentColor, color: accentColor, background: `${accentColor}12` }}
      >
        {initials}
      </div>
      <div className="pt-0.5">
        <h4 className="text-xl font-black text-brand-ink leading-tight tracking-tight">{name}</h4>
        <p className="text-sm text-brand-ink/55 mt-0.5 font-medium">{role}</p>
        <p className="text-xs text-brand-ink/35 mt-0.5">{company}</p>
      </div>
    </div>

    {/* Single profile link, labelled minimally */}
    <div className="mb-8 pb-8 border-b border-brand-ink/6">
      <p className="text-[10px] font-black uppercase tracking-[0.15em] text-brand-ink/30 mb-1">Digital Profile</p>
      <p className="text-sm text-brand-ink/70 font-medium">{link}</p>
    </div>

    {/* Single CTA in accent color */}
    <div
      className="w-full py-3 rounded-2xl text-sm font-bold text-center text-white flex items-center justify-center gap-2 mb-7"
      style={{ background: accentColor }}
    >
      <MessageSquare className="w-4 h-4 opacity-90" />
      {actionText}
    </div>

    {/* Quote — human touch, italic, muted */}
    <p className="text-[12px] text-brand-ink/45 italic leading-relaxed">{quote}</p>
  </div>
</div>
```

**Card design rules:**
- One accent color per card persona (amber / emerald / orange)
- No emoji in contact rows
- No dual buttons — one clear primary action
- Quote always sits at the bottom, italic, small, barely there

### 3D Carousel Container

```jsx
// Card placement logic
animate={{
  x: isCenter ? 0 : isLeft ? '-70%' : '70%',
  scale: isCenter ? 1 : 0.86,
  opacity: isCenter ? 1 : 0.4,
  rotateY: isCenter ? 0 : isLeft ? 12 : -12,
  zIndex: isCenter ? 30 : 10,
}}
transition={{ type: 'spring', stiffness: 280, damping: 26 }}
```

Side cards are visible but dimmed (`opacity: 0.4`, `scale: 0.86`) and clickable to navigate. An `AnimatePresence` ambient orb behind the active card shifts color per persona.

### Pricing Cards

```jsx
// Toggle: Annual (default) / Monthly
<div className="flex bg-brand-cream-dark p-1 rounded-full border border-brand-ink/8">
  <button className={cycle === 'annual' ? 'bg-brand-ink text-brand-cream rounded-full px-4 py-1.5 text-xs font-black' : 'text-brand-ink/60 px-4 py-1.5 text-xs font-semibold'}>
    Annual  <span className="text-brand-orange">Save 27%</span>
  </button>
  <button ...>Monthly</button>
</div>
```

Pricing tiers:
| Tier | Monthly | Annual | Daily framing |
|---|---|---|---|
| Starter | ₹0 | ₹0 | Free forever |
| Pro | ₹129/mo | ₹1,299/yr | Less than ₹3.60/day |
| Elite | ₹399/mo | ₹3,499/yr | Less than ₹9.60/day |

- Default to annual view
- Daily cost framing ("Less than your morning chai") anchors value
- Free tier: 25 contacts/month (resets monthly)
- Annual savings callout: "Save up to 27%"

---

## 5. Atmospheric / Decorative Patterns

### Ambient Orbs

Soft blurred radial gradients used as atmospheric backgrounds — never as primary design elements:

```jsx
{/* Section-level orb — corner placement */}
<div className="absolute top-0 right-0 w-[700px] h-[700px] rounded-full bg-brand-orange/4 blur-[180px] pointer-events-none" />
<div className="absolute bottom-0 left-0 w-[600px] h-[600px] rounded-full bg-amber-200/20 blur-[160px] pointer-events-none" />

{/* Card-level hover orb */}
<div className="absolute -top-20 -right-20 w-64 h-64 rounded-full bg-brand-orange/8 blur-3xl group-hover:bg-brand-orange/14 transition-all duration-700 pointer-events-none" />
```

**Rules:**
- Always `pointer-events-none`
- Always low opacity (`/4` to `/20` for section orbs, `/8` to `/14` for card orbs)
- Increase slightly on `group-hover` for subtle interactivity
- Use `blur-3xl` (card) or `blur-[140px]+` (section)

### Highlight Underline Decoration

For an important phrase in a headline:

```jsx
<span className="relative inline-block">
  <span className="relative z-10 text-brand-orange">nobody remembers you.</span>
  <span className="absolute bottom-1 left-0 w-full h-3 bg-brand-orange/10 rounded-full -z-0" />
</span>
```

### Dark Section Surfaces

```jsx
// Used in the WhatsApp / follow-up feature block
className="bg-gradient-to-br from-[#0A2E22] to-[#061A13] border border-white/5 rounded-[2rem]"

// Used in the Final CTA section
className="bg-brand-ink"

// Footer
className="bg-brand-ink text-white border-t border-white/5"
```

### Shadow Scale

| Use case | Shadow class |
|---|---|
| Minimal card | `shadow-sm` |
| Standard card | `shadow-md` |
| Feature card | `shadow-[0_20px_60px_-12px_rgba(28,25,23,0.10)]` |
| Center carousel card | `shadow-[0_20px_60px_-12px_rgba(28,25,23,0.10)]` |
| Dark feature card | `shadow-xl` or `shadow-2xl` |
| CTA button | `shadow-[0_8px_30px_rgba(251,146,60,0.3)]` |

---

## 6. Animation Patterns (Framer Motion)

### Page Entry Stagger

```jsx
<motion.div
  initial={{ opacity: 0, y: 10 }}
  animate={{ opacity: 1, y: 0 }}
  transition={{ duration: 0.5, delay: 0.1 }}
>
```

Hero elements stagger with `delay: 0`, `0.1`, `0.2`, `0.3`. Keep it subtle — `y: 10`, not `y: 40`.

### Mobile Menu Dropdown

```jsx
<motion.div
  initial={{ opacity: 0, y: -12, scale: 0.97 }}
  animate={{ opacity: 1, y: 0, scale: 1 }}
  exit={{ opacity: 0, y: -10, scale: 0.97 }}
  transition={{ type: 'spring', stiffness: 320, damping: 28 }}
  className="origin-top"
>
```

Menu items stagger individually with `delay: i * 0.05`.

### Network Loader Overlay

Triggered on "Slide to Deploy" completion — full-screen `bg-brand-ink` overlay with:
- SVG lines tracing network connections (`pathLength: 0 → 1`)
- Orange pulsing nodes (`scale: 0 → 1.4 → 1`) with ring pulse animations
- Progress bar (`width: 0% → 100%`) over 2.8 seconds
- Branded copy: "Deploying your card..." / "Building your digital identity"

### Carousel Card Spring

```jsx
transition={{ type: 'spring', stiffness: 280, damping: 26 }}
```

Spring physics for card transitions — feels physical without being bouncy.

### Animated Ambient Orb Swap (carousel)

```jsx
<AnimatePresence mode="wait">
  <motion.div
    key={currentCard.id}
    initial={{ opacity: 0, scale: 0.8 }}
    animate={{ opacity: 1, scale: 1 }}
    exit={{ opacity: 0, scale: 0.8 }}
    transition={{ duration: 0.6 }}
    className={`absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[500px] h-[500px] rounded-full bg-gradient-to-tr ${card.orbGradient} blur-[140px] pointer-events-none`}
  />
</AnimatePresence>
```

---

## 7. Copy & Tone Guidelines

### Headlines

- Lead with the **human problem**, not the product feature
- Short punchy lines, often 3 lines with a line break after each:
  ```
  You hand out your card.
  Three days later,
  nobody remembers you.
  ```
- Accent the **resolution** or **brand promise** in `text-brand-orange`
- Use contraction-friendly, conversational language ("You've", "We didn't", "There's no reason")

### Body Copy

- Max 2–3 short paragraphs per block
- Each paragraph = one idea
- End with the **emotional/decisive** point as a `font-semibold` line

### Section Eyebrows

Concise, 2–4 words, all-caps, muted:
- "Why We Built This"
- "People already using it"
- "How it works for you"
- "What we believe"

### CTA Copy

- Primary: "Claim Your Free Card", "Get Started Free"
- Secondary/contextual: "Explore EchoRooms", "See Pricing"
- Avoid: "Learn More", "Click Here", "Sign Up Now"

### EchoRooms Rule

EchoRooms is a **secondary feature** — it should never be headlined or given its own feature card. The appropriate treatment:

```jsx
<p className="text-center text-xs text-brand-ink/35 font-medium">
  Also building for events? <Link href="#echorooms" className="text-brand-ink/55 underline underline-offset-2 hover:text-brand-orange transition-colors">EchoRooms</Link> is our live networking wall for summits & mixers — a small but powerful part of the EchoCard family.
</p>
```

One quiet sentence at the bottom of the About section. That's it.

---

## 8. Tech Stack

| Layer | Choice |
|---|---|
| Framework | Next.js 16.2.7 (App Router) |
| Language | TypeScript 5 |
| Styling | Tailwind CSS 4 (`@theme` tokens in `globals.css`) |
| Animation | Framer Motion 12 |
| Icons | Lucide React |
| Font | Space Grotesk (via `next/font`) |
| Auth / DB | Supabase SSR |
| Messaging | Twilio (WhatsApp webhooks) |

### Icons Used in Landing Page

```tsx
import {
  Sparkles, Users, Database, Zap, Smartphone,
  ChevronRight, ChevronLeft, Share2, QrCode, Paintbrush,
  ScanLine, Send, Bot, CheckCircle2, Lock, AlertTriangle,
  Menu, X, Building2, Briefcase, MessageSquare, ArrowUpRight,
  Star, FileText, Check
} from 'lucide-react';
```

### Key Custom Animations (globals.css)

```css
@keyframes float {
  0%, 100% { transform: translateY(0px) rotate(0deg); }
  50% { transform: translateY(-12px) rotate(1.5deg); }
}

@keyframes soft-pulse {
  0%, 100% { transform: scale(1); opacity: 0.8; }
  50% { transform: scale(1.05); opacity: 1; }
}

@utility animate-float { animation: float 6s infinite ease-in-out; }
@utility animate-soft-pulse { animation: soft-pulse 4s infinite ease-in-out; }
```

---

## 9. Page Section Map

| Section | ID | Background | Key Pattern |
|---|---|---|---|
| Hero | (main) | `ShootingStarsGrid` component | 2-col grid, slide-to-deploy CTA, live event simulator |
| Features / Experience | `#experience` | `bg-brand-cream` | Interactive dot matrix, tab switcher |
| EchoRooms | `#echorooms` | Dark green gradient | Feature showcase with demo |
| Pricing | `#pricing` | `bg-[#FDFBF7]` | Toggle annual/monthly, 3-tier cards |
| About / Our Story | `#about` | `bg-[#FDFBF7]` | 4-Act editorial: Problem → Personas → Features → Beliefs |
| Final CTA | — | `bg-brand-ink` | Centered dark section, one button |
| Footer | — | `bg-brand-ink` | 4-column grid, social links |

---

## 10. What NOT to Do

These patterns have been explicitly rejected in the EchoCard design process:

| ❌ Anti-pattern | ✅ Do instead |
|---|---|
| Symmetrical 3-col icon grid cards | Asymmetric editorial blocks or tiled grid |
| `Icon + UPPERCASE LABEL + Title + Body + Footer stat` card structure | Just title + short prose + one visual or CTA |
| Pill badges on every element | One eyebrow per section max |
| Emoji in contact rows (📞, 🌐) | Clean plain text with a small muted label |
| Dual action buttons side-by-side | One clear primary CTA per card |
| Generic "Learn More" / "Sign Up" CTAs | Specific, action-oriented copy |
| Centered pill label as sole section header | Horizontal rule + small-caps inline label |
| Orange gradient square avatar | Circle monogram with accent border |
| Over-prominent EchoRooms callout | Single quiet footnote sentence |
| Full white background with drop shadow everywhere | Varied surfaces: cream, white, brand-ink, dark green |
