#!/usr/bin/env python3
"""Genera las tarjetas HUD del perfil (SVG) con datos en vivo de la API GraphQL de GitHub.

Salida (carpeta assets/):
  hud-activity.svg  -> contribuciones 12M, racha actual, racha máxima, total histórico + heatmap
  hud-langs.svg     -> lenguajes más usados en tus repos propios (no forks)

Modos:
  python scripts/generate_cards.py                 # datos reales (requiere GH_TOKEN)
  python scripts/generate_cards.py --placeholder   # tarjetas "esperando primer sync"
  python scripts/generate_cards.py --demo OUT_DIR  # datos inventados, solo para previsualizar el diseño

Variables de entorno:
  GH_TOKEN        token de GitHub (en Actions: secrets.PROFILE_TOKEN o GITHUB_TOKEN)
  PROFILE_LOGIN   usuario (por defecto: Kmurgana)
  EXCLUDE_LANGS   lenguajes a omitir, separados por coma (por defecto: "Jupyter Notebook")
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import sys
import urllib.error
import urllib.request
from html import escape
from pathlib import Path

API = "https://api.github.com/graphql"
ASSETS = Path(__file__).resolve().parent.parent / "assets"

# ── paleta ─────────────────────────────────────────────────────────────────
BG = "#0A0E14"
PANEL = "#0F1621"
LINE = "#1E2A3A"
MUTED = "#6B7F95"
TEXT = "#C9D6E2"
CYAN = "#00E5FF"
MAGENTA = "#FF2E9A"
VIOLET = "#A78BFA"
MINT = "#7CFFB2"
HEAT = ["#111B28", "#0B4A57", "#0E8497", "#00C4E0", "#8DF6FF"]

CSS = """
text{font-family:ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,'Liberation Mono','DejaVu Sans Mono',monospace}
.t{font-size:11px;letter-spacing:2px;fill:%(cyan)s;font-weight:700}
.m{font-size:10px;letter-spacing:1px;fill:%(muted)s}
.l{font-size:10px;letter-spacing:1.5px;fill:%(muted)s}
.v{font-size:26px;font-weight:800}
.u{font-size:11px;fill:%(muted)s}
.n{font-size:12px;fill:%(text)s}
.p{font-size:12px;fill:%(muted)s}
""" % {"cyan": CYAN, "muted": MUTED, "text": TEXT}


# ── GitHub API ─────────────────────────────────────────────────────────────
def gql(query: str, variables: dict, token: str) -> dict:
    req = urllib.request.Request(
        API,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "profile-hud-cards",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:  # pragma: no cover
        raise SystemExit(f"GitHub API HTTP {e.code}: {e.read().decode()[:300]}")
    if payload.get("errors"):
        raise SystemExit(f"GitHub API error: {payload['errors']}")
    return payload["data"]


Q_CREATED = "query($l:String!){ user(login:$l){ createdAt } }"
Q_CAL = """
query($l:String!, $from:DateTime, $to:DateTime){
  user(login:$l){
    contributionsCollection(from:$from, to:$to){
      contributionCalendar{ totalContributions weeks{ contributionDays{ date contributionCount } } }
    }
  }
}"""
Q_LANGS = """
query($l:String!, $after:String){
  user(login:$l){
    repositories(first:100, after:$after, ownerAffiliations:OWNER, isFork:false){
      pageInfo{ hasNextPage endCursor }
      nodes{ languages(first:10, orderBy:{field:SIZE, direction:DESC}){ edges{ size node{ name color } } } }
    }
  }
}"""


def fetch_activity(login: str, token: str) -> dict:
    # 1) últimos 12 meses con el rango por defecto de GitHub (coincide con el contador del perfil)
    cal = gql(Q_CAL, {"l": login}, token)["user"]["contributionsCollection"]["contributionCalendar"]
    weeks = [[d["contributionCount"] for d in w["contributionDays"]] for w in cal["weeks"]]
    recent_days = {d["date"]: d["contributionCount"] for w in cal["weeks"] for d in w["contributionDays"]}
    cutoff = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=364)).isoformat()

    # 2) histórico completo en ventanas de <= 1 año (límite de la API)
    created = dt.datetime.fromisoformat(
        gql(Q_CREATED, {"l": login}, token)["user"]["createdAt"].replace("Z", "+00:00")
    )
    now = dt.datetime.now(dt.timezone.utc)
    start = created.replace(hour=0, minute=0, second=0, microsecond=0)
    all_days: dict[str, int] = {}
    while start <= now:
        end = min(start + dt.timedelta(days=364, hours=23, minutes=59, seconds=59), now)
        c = gql(
            Q_CAL,
            {"l": login, "from": iso(start), "to": iso(end)},
            token,
        )["user"]["contributionsCollection"]["contributionCalendar"]
        lo, hi = start.date().isoformat(), end.date().isoformat()
        for w in c["weeks"]:
            for d in w["contributionDays"]:
                if lo <= d["date"] <= hi:
                    all_days[d["date"]] = d["contributionCount"]
        start += dt.timedelta(days=365)
    all_days.update(recent_days)  # el rango "últimos 12 meses" manda en el solapamiento
    cur, best = streaks(all_days)
    return {
        "year_total": cal["totalContributions"],
        "weeks": weeks,
        "active_days": sum(1 for k, v in recent_days.items() if v > 0 and k >= cutoff),
        "current_streak": cur,
        "longest_streak": best,
        "all_time": sum(all_days.values()),
    }


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def streaks(days: dict[str, int]) -> tuple[int, int]:
    ordered = sorted(days.items())
    best = run = 0
    for _, n in ordered:
        run = run + 1 if n > 0 else 0
        best = max(best, run)
    # racha actual: el día de hoy puede estar todavía en 0 sin cortarla
    cur = 0
    seq = [n for _, n in ordered]
    if seq and seq[-1] == 0:
        seq = seq[:-1]
    for n in reversed(seq):
        if n > 0:
            cur += 1
        else:
            break
    return cur, best


def fetch_languages(login: str, token: str, exclude: set[str]) -> dict:
    totals: dict[str, list] = {}
    repos, after = 0, None
    while True:
        repo_page = gql(Q_LANGS, {"l": login, "after": after}, token)["user"]["repositories"]
        for repo in repo_page["nodes"]:
            repos += 1
            for e in repo["languages"]["edges"]:
                name = e["node"]["name"]
                if name in exclude:
                    continue
                slot = totals.setdefault(name, [0, e["node"]["color"] or CYAN])
                slot[0] += e["size"]
        if not repo_page["pageInfo"]["hasNextPage"]:
            break
        after = repo_page["pageInfo"]["endCursor"]
    if not totals:
        return {"langs": [], "repos": repos}
    grand = sum(v[0] for v in totals.values())
    ranked = sorted(totals.items(), key=lambda kv: kv[1][0], reverse=True)
    # se muestran hasta 6 lenguajes con al menos 0,5 %; el resto se agrupa en "Other"
    top = [(n, s / grand * 100, c) for n, (s, c) in ranked[:6] if s / grand * 100 >= 0.5]
    rest = 100 - sum(p for _, p, _ in top)
    if rest >= 0.05:
        top.append(("Other", rest, MUTED))
    return {"langs": top, "repos": repos}


# ── SVG helpers ────────────────────────────────────────────────────────────
def fmt(n) -> str:
    return "—" if n is None else f"{n:,}"


def shell(title: str, sync: str, body: str, w: int = 420, h: int = 230) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="{escape(title)}">
<title>{escape(title)}</title>
<defs>
  <linearGradient id="bd" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="{CYAN}" stop-opacity=".9"/><stop offset=".55" stop-color="{VIOLET}" stop-opacity=".55"/><stop offset="1" stop-color="{MAGENTA}" stop-opacity=".9"/>
  </linearGradient>
  <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{PANEL}"/><stop offset="1" stop-color="{BG}"/></linearGradient>
  <pattern id="grid" width="20" height="20" patternUnits="userSpaceOnUse"><path d="M20 0H0V20" fill="none" stroke="{LINE}" stroke-width=".6" opacity=".55"/></pattern>
  <filter id="glow" x="-30%" y="-30%" width="160%" height="160%"><feGaussianBlur stdDeviation="2.2" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
</defs>
<style>{CSS}</style>
<rect x=".75" y=".75" width="{w-1.5}" height="{h-1.5}" rx="14" fill="url(#bg)" stroke="url(#bd)" stroke-width="1.5"/>
<rect x="1.5" y="1.5" width="{w-3}" height="{h-3}" rx="13" fill="url(#grid)"/>
<path d="M12 22V12a2 2 0 0 1 2-2h10M{w-12} 22V12a2 2 0 0 0-2-2h-10M12 {h-22}v10a2 2 0 0 0 2 2h10M{w-12} {h-22}v10a2 2 0 0 1-2 2h-10" fill="none" stroke="{CYAN}" stroke-width="1.4" opacity=".75"/>
<text x="24" y="31" class="t">{escape(title)}</text>
<circle cx="{w-24}" cy="27.5" r="3" fill="{MINT}"><animate attributeName="opacity" values="1;.25;1" dur="2.4s" repeatCount="indefinite"/></circle>
<text x="{w-34}" y="31" class="m" text-anchor="end">SYNC {escape(sync)}</text>
{body}
</svg>
"""


def level(count: int, cuts: list[int]) -> int:
    if count <= 0:
        return 0
    for i, c in enumerate(cuts):
        if count <= c:
            return i + 1
    return 4


def activity_svg(d: dict | None, sync: str, public_only: bool = False) -> str:
    d = d or {}
    tiles = [
        ("LAST 12 MONTHS", fmt(d.get("year_total")), "contribs", CYAN),
        ("CURRENT STREAK", fmt(d.get("current_streak")), "days", MAGENTA),
        ("LONGEST STREAK", fmt(d.get("longest_streak")), "days", VIOLET),
        ("ALL-TIME", fmt(d.get("all_time")), "contribs", MINT),
    ]
    out = []
    for i, (lbl, val, unit, col) in enumerate(tiles):
        x = 24 + (i % 2) * 182
        y = 48 + (i // 2) * 52
        out.append(
            f'<rect x="{x}" y="{y}" width="174" height="45" rx="8" fill="{col}" fill-opacity=".06" stroke="{col}" stroke-opacity=".35"/>'
            f'<text x="{x+10}" y="{y+15}" class="l">{lbl}</text>'
            f'<text x="{x+10}" y="{y+38}" class="v" fill="{col}" filter="url(#glow)">{val}</text>'
            f'<text x="{x+10+max(len(val),1)*15.6+6}" y="{y+38}" class="u">{unit}</text>'
        )
    # heatmap 12M
    out.append(f'<text x="24" y="162" class="l">// 12M HEATMAP</text>')
    weeks = d.get("weeks")
    pitch, x0, y0 = 7.12, 24, 170
    if weeks:
        nz = sorted(c for w in weeks for c in w if c > 0) or [1]
        cuts = [nz[int(len(nz) * q) - 1 if int(len(nz) * q) else 0] for q in (0.25, 0.5, 0.75)]
        n_weeks = len(weeks)
        pitch = min(7.12, (420 - 2 * 24) / max(n_weeks, 1))
        for wi, w in enumerate(weeks):
            for di, c in enumerate(w):
                lv = level(c, cuts)
                x, y = x0 + wi * pitch, y0 + di * pitch
                twinkle = (
                    f'<animate attributeName="opacity" values="1;.45;1" dur="3.6s" begin="{(wi*0.37+di*0.21)%3.6:.2f}s" repeatCount="indefinite"/>'
                    if lv == 4
                    else ""
                )
                out.append(
                    f'<rect x="{x:.2f}" y="{y:.2f}" width="{pitch-1.6:.2f}" height="{pitch-1.6:.2f}" rx="1.2" fill="{HEAT[lv]}">{twinkle}</rect>'
                    if twinkle
                    else f'<rect x="{x:.2f}" y="{y:.2f}" width="{pitch-1.6:.2f}" height="{pitch-1.6:.2f}" rx="1.2" fill="{HEAT[lv]}"/>'
                )
        out.append(
            f'<text x="396" y="162" class="m" text-anchor="end">{d["active_days"]} active days{" · public data only" if public_only else ""}</text>'
        )
    else:
        for wi in range(53):
            for di in range(7):
                out.append(
                    f'<rect x="{x0+wi*pitch:.2f}" y="{y0+di*pitch:.2f}" width="{pitch-1.6:.2f}" height="{pitch-1.6:.2f}" rx="1.2" fill="{HEAT[0]}"/>'
                )
        out.append(f'<text x="396" y="162" class="m" text-anchor="end">AWAITING FIRST SYNC</text>')
    return shell("ACTIVITY.LOG", sync, "\n".join(out))


def langs_svg(d: dict | None, sync: str, excluded: set[str], public_only: bool = False) -> str:
    out = []
    langs = (d or {}).get("langs")
    if not langs:
        out.append(f'<rect x="24" y="54" width="372" height="10" rx="5" fill="{HEAT[0]}"/>')
        if d is None:
            out.append(f'<text x="24" y="100" class="n">AWAITING FIRST SYNC</text>')
        else:
            out.append(f'<text x="24" y="100" class="n">NO LANGUAGE DATA FOUND</text>')
            hint = ["No non-fork repos visible to this token.", "Add PROFILE_TOKEN to include private repos."] if public_only else ["No non-fork repos with code were found."]
            for i, line in enumerate(hint):
                out.append(f'<text x="24" y="{124 + i * 18}" class="m">{escape(line)}</text>')
        return shell("STACK.USAGE", sync, "\n".join(out))
    out.append('<clipPath id="bar"><rect x="24" y="54" width="372" height="10" rx="5"/></clipPath>')
    out.append('<g clip-path="url(#bar)">')
    x = 24.0
    for name, pct, col in langs:
        w = 372 * pct / 100
        out.append(f'<rect x="{x:.2f}" y="54" width="{w+0.6:.2f}" height="10" fill="{col}"/>')
        x += w
    out.append("</g>")
    for i, (name, pct, col) in enumerate(langs):
        cx = 24 + (i % 2) * 190
        cy = 94 + (i // 2) * 30
        out.append(
            f'<circle cx="{cx+5}" cy="{cy-4}" r="4.5" fill="{col}" filter="url(#glow)"/>'
            f'<text x="{cx+18}" y="{cy}" class="n">{escape(name)}</text>'
            f'<text x="{cx+172}" y="{cy}" class="p" text-anchor="end">{pct:.1f}%</text>'
        )
    note = f'{d["repos"]} repos'
    if excluded:
        note += " · " + ", ".join(sorted(excluded)) + " excluded"
    if public_only:
        note += " · public only"
    out.append(f'<text x="24" y="212" class="m">{escape(note)}</text>')
    return shell("STACK.USAGE", sync, "\n".join(out))


# ── demo / main ────────────────────────────────────────────────────────────
def demo_data() -> tuple[dict, dict]:
    rnd = random.Random(7)
    weeks = []
    for w in range(53):
        week = []
        for d in range(7):
            base = 0 if d in (0, 6) and rnd.random() < 0.6 else int(rnd.expovariate(1 / 7))
            if rnd.random() < 0.08:
                base = 0
            if w == 52 and d > 4:
                continue
            week.append(base)
        weeks.append(week)
    act = {
        "year_total": 3285,
        "weeks": weeks,
        "active_days": sum(1 for w in weeks for c in w if c > 0),
        "current_streak": 41,
        "longest_streak": 96,
        "all_time": 11842,
    }
    langs = {
        "langs": [
            ("Python", 46.2, "#3572A5"),
            ("TypeScript", 21.8, "#3178C6"),
            ("SQL", 12.4, "#E38C00"),
            ("C++", 8.1, "#F34B7D"),
            ("MATLAB", 5.7, "#E16737"),
            ("Arduino", 3.4, "#BD79D1"),
            ("Other", 2.4, MUTED),
        ],
        "repos": 38,
    }
    return act, langs


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    print(f"wrote {path} ({len(content)} bytes)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--placeholder", action="store_true")
    ap.add_argument("--demo", metavar="OUT_DIR")
    args = ap.parse_args()
    sync = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    public_only = os.environ.get("PUBLIC_ONLY") == "1"
    exclude = {s.strip() for s in os.environ.get("EXCLUDE_LANGS", "Jupyter Notebook").split(",") if s.strip()}

    if args.demo:
        act, langs = demo_data()
        out = Path(args.demo)
        write(out / "hud-activity.svg", activity_svg(act, sync, public_only))
        write(out / "hud-langs.svg", langs_svg(langs, sync, exclude, public_only))
        return
    if args.placeholder:
        write(ASSETS / "hud-activity.svg", activity_svg(None, "PENDING"))
        write(ASSETS / "hud-langs.svg", langs_svg(None, "PENDING", exclude))
        return

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("Falta GH_TOKEN")
    login = os.environ.get("PROFILE_LOGIN", "Kmurgana")
    act = fetch_activity(login, token)
    langs = fetch_languages(login, token, exclude)
    print(
        f"login={login} public_only={public_only} year_total={act['year_total']} all_time={act['all_time']} "
        f"streak={act['current_streak']}/{act['longest_streak']} repos={langs['repos']} langs={[n for n, _, _ in langs['langs']]}"
    )
    # solo se escribe si todo salió bien: nunca se pisa una tarjeta buena con una rota
    write(ASSETS / "hud-activity.svg", activity_svg(act, sync, public_only))
    write(ASSETS / "hud-langs.svg", langs_svg(langs, sync, exclude, public_only))


if __name__ == "__main__":
    main()
