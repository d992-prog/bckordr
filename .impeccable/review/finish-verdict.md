## verdict

Scope: scoring the three material fixes from `finish-review.md` after one bounded fix batch. Product source `c21ecfc11cdad3520a25a10deb9685ed81c63a48`; final test-only change `8dd8a008d7d0bc55ac6551095fbd297b2b2bafaf`. All same 28 required capture paths were reopened and are valid, with the document top and expected sections visible.

1. **resolved — mobile heading density.** `portal-devices-390-light.png` and `portal-devices-390-text-200.png` now place the state badge directly after the device name/subscription. The empty 10em vertical block is gone; desktop heading wrapping is retained. The narrow rule in `frontend/src/vpn-portal/portal.css:813` sets `flex-basis: auto`.
2. **resolved — small blue text contrast.** Home/download, help, legal links and selected navigation text visibly use the darker light-theme text blue in their normal and 200% captures. `frontend/src/vpn-portal/portal.css:815` reuses `--portal-label-blue: #1557c8` (6.50:1 against white), while the dark captures retain their lighter text and the primary actions retain white labels. The supplied contextual contrast/focus assertions pass; no new browser run was performed by this reviewer.
3. **resolved — visible traffic scope.** `cabinet-refresh-final-portrait.png`, 320/390 captures, desktop and enlarged home captures explicitly display “Трафик аккаунта за”. The device captures retain “Трафик устройства за”. The longer account label wraps as whole words, and its modest card-height increase is an intentional product-truth adaptation required by the original finding.

Fix-batch regressions: none observed in the supplied recaptures.

Fresh portrait measurement: `.impeccable/review/diff/final/report.json` records **87.34%, match**, with nine regional `missing` verdicts: brand-mark, brand-name, account-action, access-icon, access-title, access-expiry, overview-rule, slot-label and slot-value. These are registration artifacts, not absent rendered elements. Each of those nine paired crops was opened: their build windows are displaced vertically (the title window cuts through its glyphs into the expiry; the brand-name window contains only the text’s lower edge; the slot-label window captures the value instead). The raw same-frame 853×1844 screenshot and side-by-side show every complete element, with the header still in its original position. The account caption’s extra line moves subsequent card flow; the attribution of the erroneous top cropping to registration sensitivity is an inference from those artifacts. Retain the regional failures in the numerical report and record this visual adjudication; do not claim a no-missing numerical pass or alter the approved comp/captures.

The earlier desktop **54.91%** mechanical result remains a portrait-coordinate/topology mismatch. Fresh native desktop captures retain the approved two-column overview/connection layout; the original review exception still applies without relabeling that measurement as a pass.

## remaining

clear — all three scored material fixes are resolved. Ship covers the scored fixes, not the whole surface. The planned DESIGN.md/documenter handoff and truthful gate-exception recording remain release workflow steps for the parent; they are outside this fix-scoring pass.

Physical iOS/WebKit, actual Happ/Hiddify import and VPN stability remain unchecked here. No detector rerun, new asset work, source/Git/state writes or whole-surface defect hunt was performed.

disposition: ship
