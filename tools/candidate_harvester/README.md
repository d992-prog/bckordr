# Candidate Harvester

Standalone tool for finding a small set of low-value domains that are already in a useful pre-pending lifecycle stage.

It is intentionally not connected to the control server. Run it on a powerful host machine, inspect the CSV, then paste selected domains into the control panel `discovery` tab.

## Goal

The tool does not try to process a whole TLD in the control server. It streams large files, filters low-value domains, checks RDAP for a limited number of candidates, and writes only domains with accepted lifecycle statuses.

Default accepted lifecycle:

```text
redemption
```

This is the best starting point because it gives pre-`pendingDelete` evidence. For fast drop-window research, filter redemption domains by their projected `pendingDelete` date instead of waiting weeks.

## Run

Simple safe run:

```bash
python tools/candidate_harvester/quick.py com ./allzonefiles/expired-com.txt
```

This creates:

```text
candidates-com.csv
candidates-com.txt
```

Safe defaults used by `quick.py`:

```text
--limit-output 20
--max-rdap-checks 1000
--concurrency 10
--progress-interval 5
```

For `.net`:

```bash
python tools/candidate_harvester/quick.py net ./allzonefiles/expired-net.txt
```

For `.org`:

```bash
python tools/candidate_harvester/quick.py org ./allzonefiles/expired-org.txt
```

## Full Zonefile Redemption Scan

For a huge full zonefile like:

```text
com.2026-07-02.txt
```

use the dedicated redemption scanner:

```bash
python tools/candidate_harvester/redemption_scan.py com ./zonefiles/com.2026-07-02.txt
```

Fast machine preset:

```bash
python tools/candidate_harvester/redemption_scan_fast.py com ./zonefiles/com.2026-07-02.txt
```

Fast preset defaults:

```text
--sample-mode reservoir
--reservoir-size 200000
--max-rdap-checks 200000
--concurrency 100
--limit-output 20
--min-score 35
--pending-delete-min-days 1
--pending-delete-max-days 2
```

Use the fast preset only on a strong machine and be ready to reduce concurrency if RDAP starts returning errors/timeouts.

This mode:

```text
1. Streams the whole file.
2. Keeps a fixed-size random reservoir of low-value candidates.
3. RDAP-checks that reservoir.
4. Reads RDAP `last changed` / `last update`.
5. Computes `predicted_pending_delete_at = rdap_updated_at + 30 days`.
6. Writes only domains whose predicted pendingDelete is 1-2 days away.
```

Default deep scan limits:

```text
--sample-mode reservoir
--reservoir-size 50000
--max-rdap-checks 50000
--concurrency 30
--limit-output 50
--min-score 45
```

Output:

```text
redemption-candidates-com.csv
redemption-candidates-com.txt
```

If the diagnosis says `lifecycles=registered=...`, then checked domains are active by RDAP. Increase `--max-rdap-checks` / `--reservoir-size`, lower `--min-score`, or use a better pre-expired source.

If `.org` returns mostly `unknown`, the source is usually not useful for this method: many `.org` zonefile entries have no actionable RDAP lifecycle status, and domains in redemption may already be absent from the zonefile. Use an expired/pre-delete source for `.org`, or run a small debug scan with `--accept-lifecycle registered unknown redemption pending_delete` to inspect raw CSV behavior.

Advanced run:

From repository root:

```bash
python tools/candidate_harvester/candidate_harvester.py --input ./allzonefiles/expired-com.txt --tld com --output candidates-com.csv --output-txt candidates-com.txt --limit-output 50 --max-rdap-checks 5000 --concurrency 20
```

For `.net`:

```bash
python tools/candidate_harvester/candidate_harvester.py --input ./allzonefiles/expired-net.txt --tld net --output candidates-net.csv --output-txt candidates-net.txt --limit-output 50 --max-rdap-checks 5000 --concurrency 20
```

For `.org`:

```bash
python tools/candidate_harvester/candidate_harvester.py --input ./allzonefiles/expired-org.txt --tld org --output candidates-org.csv --output-txt candidates-org.txt --limit-output 50 --max-rdap-checks 5000 --concurrency 20
```

## Inputs

Supported input types:

```text
.txt
.csv
.gz
.zip
directory with any of the above
```

The parser streams line by line and extracts domains from common formats, including plain domains, CSV rows, and URLs.

## Safe Defaults

Recommended first run:

```text
--limit-output 20
--max-rdap-checks 1000
--concurrency 10
```

Scale up only after confirming RDAP responses are stable and the source list is correct.

## Output

CSV columns:

```text
domain,tld,lifecycle,status_codes,http_status,checked_at,redemption_anchor_at,predicted_pending_delete_at,days_to_pending_delete,score,reason,error
```

TXT output contains domains only and is convenient for pasting into the control panel.

## Progress Log

During a run the tool prints progress every 5 seconds:

```text
progress lines=120000 parsed=118900 filtered=340 rdap=300/340 written=7/20 speed_lines_s=24000 speed_rdap_s=12.4
```

Meaning:

```text
lines      input lines read
parsed     valid domains extracted
filtered   domains that passed low-value filter
rdap       completed/submitted RDAP checks
written    accepted candidates written to CSV/TXT
```

Change interval:

```bash
python tools/candidate_harvester/candidate_harvester.py ... --progress-interval 2
```

At the end the tool prints a diagnosis:

```text
diagnosis:
lifecycles=registered=1000
RDAP checks worked, but accepted lifecycle was not seen...
```

If `lifecycles` is mostly `registered`, the source file is probably a full zonefile, not an expired/pre-delete candidate list.

## Useful Options

Collect only reliable pre-pending samples:

```bash
--accept-lifecycle redemption
```

Also collect domains already in `pendingDelete`:

```bash
--accept-lifecycle redemption pending_delete
```

Find redemption domains expected to enter `pendingDelete` in 1-2 days:

```bash
--pending-delete-min-days 1 --pending-delete-max-days 2
```

Widen the window if no candidates are found:

```bash
--pending-delete-min-days 0 --pending-delete-max-days 7
```

Raise low-value strictness:

```bash
--min-score 70
```

Lower it if too few candidates are found:

```bash
--min-score 50
```

## Workflow

1. Download/export AllZonefiles lists on the host machine.
2. Run this harvester per TLD.
3. Open the CSV and keep only safe-looking research candidates.
4. Paste domains from the TXT into control panel `discovery`.
5. Prefer domains whose predicted `pendingDelete` is soon, so control can observe the transition within days.
