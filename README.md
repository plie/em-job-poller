# em-job-poller

Daily poll of ~90 company job boards (Greenhouse, Lever, Ashby public JSON endpoints)
for Engineering Manager postings. Results land in `data/` and are committed by a
GitHub Actions cron, so nothing needs to be running on your laptop.

## One-time setup

```sh
cd em-job-poller
git init && git add . && git commit -m "initial"
gh repo create em-job-poller --public --source=. --push   # or --private
gh workflow run poll.yml                                    # first run, ~30s
gh run watch
```

`--private` works too; the tracker reads `data/postings.json` through the raw
GitHub URL, which for a private repo needs a token. Public is simpler, and the
data is only job titles and links.

## Files

- `companies.json` — the target list. Edit freely: `{company, category, ats, slug}`.
  `ats` is `greenhouse` | `lever` | `ashby`; the slug is the last path segment of
  the company's board URL (`job-boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>`,
  `jobs.ashbyhq.com/<slug>`).
- `poll.py` — the poller. Title rules live in `INCLUDE` / `EXCLUDE` near the top;
  remote-US detection in `remote_us()`.
- `data/postings.json` — every currently open match, with `first_seen` / `last_seen`
  and a `remote_us` flag.
- `data/new.json` — matches first seen on the latest run.
- `data/errors.json` — boards that failed on the latest run. Check this
  occasionally: a slug that 404s for a week has probably moved ATS.

## Running locally

```sh
pip install requests
python poll.py
```

## Known gaps

- Companies on Workday, SmartRecruiters, Rippling ATS or their own career sites
  (Shopify, GitHub, Zendesk, Rippling, Snapsheet, most banks and insurers) are not
  covered. LinkedIn and HiringCafe alerts are the backstop for those.
- Title matching is regex; a posting titled "Engineer Manager" (typo) or
  "Lead, Payments Engineering" will be missed. Widen `INCLUDE` if you see misses.
