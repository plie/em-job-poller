# Fit rubric — Engineering Manager postings

Two independent scores per posting. Each dimension is 0–3. Score only what the
posting says; when it is silent, score 1 and write the interview question that
would settle it.

## Scope gate (pass / fail / unknown) — checked before anything is scored

Out of scope, full stop: more than 5 years of people management required; a
role that manages managers (EMs or team leads report to it); Director, Head of,
VP, CTO titles. A failed gate sets `scope_ok: false` and `resume_score: 0`; the
row stays visible only when the "in scope only" filter is off. "Unknown" (years
not stated) passes with an interview question.

## A. Résumé match: does she meet the posting's stated requirements?

No fixed dimensions and no weights. List every hard requirement the posting
states (the "must have" / "requirements" / "what you bring" section — not the
nice-to-haves and not the responsibilities), mark each one:

- **met** — the résumé shows it directly (quote the résumé line if not obvious).
- **unmet** — a hard requirement she cannot show (native mobile, ML, Go/C
  systems, Spark/data pipelines, growth-program leadership, security research…).
- **unclear** — arguable, or depends on how a screener reads "team lead +
  interim EM" against "N years as an Engineering Manager", or a logistics
  condition (in-person cadence) the posting doesn't pin down.

`resume_met`, `resume_total`; `resume_unmet` and `resume_unclear` as lists of
the requirement phrases. Sort key is met/total. Stack is a yes/no ("does it
require a language she can't show"), never a gradient; domain only counts
where the posting names it as required.

**AI posture** is a note, not a score: record `ai_note` when the posting asks
for AI-assisted development experience or standards (her differentiator), or
reads as an IC role in EM clothing ("90% AI-led", "do the work yourself").

## B. Fit (0–12): would she thrive there?

- **Team** — 3: named direct reports (4–8), mixed seniority, growing or forming,
  mentoring/career development as core duties. 2: reports implied, development
  mentioned. 1: silent. 0: no reports, "influence without authority", all-staff
  specialists, scrum-master in an EM title.
- **Authority** — 3: owns hiring and performance, shapes roadmap with product,
  accountable for delivery, reports to Director/VP Eng. 2: some of these. 1:
  silent (usual). 0: "execute the roadmap defined by…", architecture and
  priorities set elsewhere, matrixed, outcomes without control of staffing/scope.
- **Work** — 3: a product that is an experience for people (events, hospitality,
  travel, arts/entertainment, education, design-led consumer) with launches and
  finish lines. 2: a visible product with milestones, neutral domain. 1:
  internal platform / infra with indirect outcomes. 0: billing, payments,
  abstract financial or insurance back-office; pure keep-the-lights-on.
- **Pace & culture** — 3: sustainable, ownership and autonomy named, async or
  documented decision-making. 2: neutral. 1: one warning phrase. 0: two or more:
  "fast-paced", "aggressive sprints/goals", "hustle", "wear many hats", "thrive
  in ambiguity" used as a euphemism for churn, "high-intensity", "urgency",
  "scrappy", "relentless", on-call expectations for the manager, RTO drift.

## Output per posting

`resume_score` (0–9), `fit_score` (0–12), a one-line verdict, a `flags` list of
warning phrases quoted from the posting, and 2–4 interview questions for the
dimensions scored 1 (unknown). Tag Work=0 postings honestly: they are her
strongest résumé match and her weakest fit, and she decides that trade herself.
