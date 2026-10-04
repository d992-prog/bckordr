---
version: 1
slug: "frontend-src-vpn-portal-portal-tsx"
primary_target: "frontend/src/vpn-portal/Portal.tsx"
related_targets: ["frontend/src/vpn-portal/portal.css","frontend/src/vpn-site/main.tsx","frontend/src/vpn-site/site.css","frontend/src/App.tsx","frontend/src/styles.css"]
---

# Veltrix VPN customer surfaces

Scope: redesign `/cabinet/` first, then extend the same world to `/vpn/` and the VPN-only admin areas. Mode: Operate for cabinet/admin, Persuade for public site.

Audience: nontechnical Russian-speaking users arriving from Telegram; secondary audience is the service operator. User job: understand whether access is ready and open, copy, or learn how to use the profile. Primary action: `Открыть профиль`. Proof is existing subscription/profile/trial state from the API. Never claim an active tunnel, speed, stability, scale, or purchase capability without evidence.

Chosen direction: Liquid Glass with one expressive glass object per client viewport and matte data sheets; admin is the denser operational translation. Approved comp: `.impeccable/mocks/portal-liquid-glass-c.png`. The memorable moment is the large asymmetric status lens with the primary action crossing its lower edge. The generated logo is a direction only; ship a recognizable three-lens flat SVG source plus optical large-format treatment.

Constraints: preserve API/session/CSRF/generation behavior; no payment; protect connection URIs; responsive Telegram safe areas; WCAG AA; reduced motion; graceful no-blur fallback; no new UI framework. Comp literals such as the sample date are replaced by real data. Unresolved: typeface and raster plate boundaries are selected by measured comp tooling.

## Direction contract

**THESIS**
Veltrix turns technical VPN access into one calm, obvious next step and rejects the category-default shield, world map, speed gauge, and power-button dashboard.

**OWN-WORLD**
Pearl-blue light field, deep navy ink, blue/mint/lilac spectrum, one optical status lens, glass controls, matte information sheets, and a recognizable three-lens mark.

**STORY**
The user understands whether a profile is ready, sees the access term, then opens the profile, copies its link, or reads the relevant instruction. The operator sees the same states in a denser working form.

**FIRST VIEWPORT**
At 390 px: compact brand row; a large asymmetric status lens; a blue `Открыть профиль` control crossing its lower edge; matte quick actions; factual access card; `Главная / Профили / Аккаунт` glass navigation inside the safe area.

**FORM**
Impeccable pick Liquid Glass; selected composition three, `Статус и действия`; seed `1a448fec`; approved comp `.impeccable/mocks/portal-liquid-glass-c.png`.

**FINISH**
unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
