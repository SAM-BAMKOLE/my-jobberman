# Daily remote job digest

Fetches remote dev jobs (Remotive, Remote OK, We Work Remotely, Jobicy), drops roles you
likely can't take from Nigeria (US-only, EU-only, etc.), scores the rest 0-100 against your
stack, and emails only the **new** ones each morning, grouped High / Medium / Stretch.

## Setup (about 10 minutes)

1. Create a **private** GitHub repo and upload everything in this folder
   (keep the `.github/workflows/` path exactly).
2. Gmail: turn on 2-Step Verification, then create an **App Password**
   (Google Account > Security > App passwords). Use that, never your real password.
3. In the repo: Settings > Secrets and variables > Actions > New repository secret:
   - `SMTP_USER` = your Gmail address
   - `SMTP_PASS` = the 16-character app password
   - `EMAIL_TO` = where to receive the digest (can be the same address)
4. Actions tab > "Daily job digest" > **Run workflow** to test. Check your inbox (and spam).
5. After that it runs daily at 07:00 Nigeria time.

Other email provider? Add env vars `SMTP_HOST` / `SMTP_PORT` in the workflow (465 = SSL, 587 = STARTTLS).

## Local checks

    python job_digest.py --selftest   # offline scoring check
    python job_digest.py --dry-run    # live fetch, writes digest.html, sends nothing

## Tuning

Edit the top of `job_digest.py`: `STACK` (keywords and weights), `TITLE_OK`/`TITLE_BAD`,
`TIERS` (score cut-offs), `MAX_PER_TIER`. Set `SEND_EMPTY=1` to get an email even on quiet days.

## Known limits

- Scores are keyword estimates, not a verdict; open every posting before applying.
- Location filtering is heuristic. Jobs that don't state a location are kept with a
  "check before applying" note; some country-restricted jobs can still slip through.
- Feeds sometimes change format. A failed source is skipped and shown in the email footer.
- GitHub can pause scheduled workflows on repos with no activity for 60 days; if emails
  stop, open the Actions tab and re-enable it.
- Respect each site's terms: this makes only a handful of requests per day.
