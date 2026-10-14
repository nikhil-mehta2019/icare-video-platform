# Website Launch Readiness Standard

This repository adopts the shared [Website Launch Readiness Standard](https://github.com/nikhil-mehta2019/CodeAtlas/blob/feat/website-launch-standard/docs/WEBSITE_LAUNCH_READINESS_STANDARD.md). Once the canonical standard PR is merged, update this URL to point to the default branch.

## Run the audit

```bash
python scripts/website-audit.py
python scripts/website-audit.py --url https://YOUR-STAGING-URL --json website-audit.json
```

The script uses Python's standard library only. Static matches are PARTIAL, not PASS. Live crawling is read-only. Analytics delivery, Search Console access, legal review, redirects, and actual contact-form delivery still require dedicated verification.
