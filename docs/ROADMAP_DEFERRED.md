# Deferred Roadmap Items

> **How to read this doc:** This page is intentionally **negative**. Everything listed here is **NOT on the near-term roadmap** for `nl2pbip`. It exists to (a) spare contributors from re-proposing ideas we've already weighed, (b) make the *reasoning* behind each deferral auditable, and (c) record the **unblock condition** that would make us reconsider.
>
> If you're a contributor with bandwidth for one of these items, **open an issue first** before writing code — the unblock condition may have changed, or another item on the [active roadmap](../CHANGELOG.md) may be a higher leverage use of your time. Active phases and shipped work are tracked in [`CHANGELOG.md`](../CHANGELOG.md) and the top-level [`README.md`](../README.md).

---

## 1. Web-based visual editor (drag-drop Power BI layout)

**Rationale.** The differentiator of `nl2pbip` is that an LLM plans and authors the report from a natural-language spec — the visual layout is a *byproduct* of a good plan, not a manual authoring step. Building a drag-drop WYSIWYG editor would put us in direct competition with Power BI Desktop itself, which already ships with a mature, vendor-supported visual editor, free of charge, with pixel-perfect fidelity to Power BI Service rendering. We'd be recreating a decade of Microsoft's layout work, and any visual editor we built would inevitably feel like a worse, second-class version of the real thing. Worse, it would dilute the project's core value proposition: "describe the report in English; get a versioned, Git-diffable `.pbip` out the other side." A visual editor pulls users back toward the imperative, click-heavy workflow this project is trying to replace.

**Unblock condition.** We'd need (a) clear, sustained user demand for in-browser authoring that Power BI Desktop cannot satisfy (e.g. cross-platform Linux/ChromeOS users, or workflows that need to embed authoring in a web app), *and* (b) a credible technical path that doesn't require us to reimplement Power BI Desktop (for example, embedding Power BI's own authoring SDK, if Microsoft ever ships a usable web version). Without both, the effort is unbounded and the outcome is a worse Power BI Desktop.

**Re-evaluation timing.** Permanent defer, not aligned with project goals. Reconsider only if Microsoft itself moves to a web-first authoring model and exposes a stable embedding API.

---

## 2. Streaming visual rendering on a server (headless browser fleet + CDN)

**Rationale.** A natural feature idea is "let users see a pixel preview of the generated `.pbip` without opening Power BI Desktop" — which sounds simple until you enumerate what's required: a pool of headless Chromium / Edge instances (Power BI visuals need the real renderer, not a substitute), a job queue to serialize concurrent preview requests, a CDN with cache invalidation tied to `.pbip` content hashes, and per-tenant isolation for any data-bound previews. That's an entire platform engineering effort — easily six figures of annual infrastructure cost — for a feature that solves a real but narrow pain point (the 10-second wait to open Power BI Desktop locally and refresh). The open-source economics of this project don't support that kind of infra spend, and a half-built, slow, or rate-limited preview server would be worse than no preview at all.

**Unblock condition.** Either (a) a community-contributed, serverless-friendly approach emerges (e.g. Power BI publishes an official server-side render API, or a small static-render subset becomes feasible without a full browser), or (b) the project gains a sponsor willing to fund and operate the headless-browser infra as a managed service, with the cost explicitly socialized to preview-feature users (not the core library).

**Re-evaluation timing.** Defer until v2.x. Revisit only if Microsoft ships a server-side render API; otherwise the cost/benefit never pencils out for a library of this size.

---

## 3. Direct Power BI Service / Fabric integration

**Rationale.** "Push the generated `.pbip` straight to a customer's Power BI workspace" is a natural end-to-end story, but the path there is long and gated. The Power BI REST APIs require Azure AD app registrations with tenant-admin consent, the Fabric APIs are still rolling out and changing shape, and any production-grade integration requires Microsoft Partner Network credentials (and an ISV agreement) plus a security review against Microsoft's data-handling requirements. The sales cycle alone is months. Meanwhile, the project ships a workable alternative today: the `.pbip` format is Git-friendly by design, and `pbi-tools` (a separate, mature open-source project) already handles `push` to Power BI Service from CI. We're not blocked on this — users can wire `nl2pbip generate | pbi-tools push` today and get a fully automated pipeline. Building a first-party integration would duplicate `pbi-tools`, lock us into a specific API version, and create a maintenance burden that scales with Microsoft's release cadence, not ours.

**Unblock condition.** Either (a) we gain partner credentials + an ISV agreement and treat this as a separately-funded product line with its own SLA, or (b) Microsoft standardizes a lightweight, public OAuth flow for `.pbip` upload that doesn't require partner-network onboarding (i.e. the same auth model GitHub Actions uses today, but blessed for Power BI Service). Until then, document and lean on `pbi-tools`.

**Re-evaluation timing.** Permanent defer as a library feature. If a partner-funded track opens up, this becomes its own product, not part of `nl2pbip` core.

---

## 4. Multi-language LLM prompts beyond English

**Rationale.** The planner emits structured JSON, and the structural schema (measure names, column references, table IDs, DAX expressions, visual configurations) is language-neutral by construction — a Japanese prompt produces a plan whose JSON is identical in shape to an English prompt's. Where language *does* leak through is the narrative: report page titles, axis labels, tooltip copy, the user-facing prose in the generated TMDL `description` fields. For those, current-generation LLMs produce noticeably weaker Chinese, Japanese, Korean, and most non-Latin-script languages than English — they translate literally rather than localize idiomatically, and the output reads as "machine-translated" rather than native. Localizing prompts well means more than translating a string; it means region-appropriate measure naming conventions, locale-aware date/number formatting (which Power BI handles but the *plan* has to ask for correctly), and culturally-appropriate chart choices. None of that is impossible, but it's a per-language effort, not a one-time translation, and the maintenance cost compounds — every prompt change requires re-review in every supported language. Without evidence that non-English users are blocked (rather than merely inconvenienced), we'd rather keep the prompt surface small and high-quality than ship a long tail of mediocre localizations.

**Unblock condition.** Concrete user demand — measured as GitHub issues, Discord questions, or survey responses — from at least one non-English-speaking segment large enough to justify a dedicated maintainer for that language. We'd also want a native speaker willing to own the prompt review process long-term, not just a one-time translation.

**Re-evaluation timing.** Defer until v2.x if user demand emerges. Until we have a native-speaker maintainer per language, shipping half-localized prompts would actively make the product worse for those users.

---

## 5. A SaaS hosted version (auth, billing, rate limiting, SLA)

**Rationale.** "Just host it for me" is the most common feature request for any successful open-source tool, and it's also the one that turns a library into a company. A real SaaS version needs: per-tenant auth (probably OAuth + workspace isolation), usage metering and billing (Stripe or equivalent), rate limiting and abuse prevention, a 99.9%+ uptime SLA with an on-call rotation, a status page, customer support channels, GDPR/SOC2 compliance posture, a data-residency story for EU customers, and a refund/cancellation flow. That's not a feature — it's a 3–5 person operations team, an annual six-figure burn, and a year of building before the first paying customer. It also changes the project's incentives: a hosted SaaS has different priorities than a library (lock-in vs. portability, observability vs. simplicity, control plane vs. data plane). Maintaining both well, simultaneously, is a known failure mode — most projects that try end up with a maintained hosted version and an unmaintained library. We'd rather stay focused on the library, keep `nl2pbip` something a single maintainer can carry, and let hosting be a problem the community solves (via `pbi-tools`, Airflow, GitHub Actions, or whatever fits the user's stack).

**Unblock condition.** Either (a) a clearly separate entity — a company, a sponsor, or a dedicated team with its own funding and engineering headcount — takes ownership of the hosted version, with the library remaining independently maintained on its current cadence, or (b) the user base grows to the point where a thin hosted wrapper (e.g. "click to deploy on your own Azure") becomes feasible without us operating it. In either case, the library and the hosted product should have separate roadmaps, separate repos, and separate maintainers.

**Re-evaluation timing.** Permanent defer as part of this repository. A hosted version, if it ever exists, is a separate product owned by a separate team. The library stays library-shaped.

---

Last reviewed: 2026-09-15
