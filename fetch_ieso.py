# -*- coding: utf-8 -*-
"""
IESO(캐나다 온타리오) 수집기 v4 — 누적 병합 저장 + 성분 전량 보존 + XSD 덤프

■ v3 실행(2026-08-11)에서 확정된 사실
  1) 존가격 태그 = **ZonalPrice** (하루 24개). 2026-06-01 일평균 32.173 CAD/MWh
     → OT all-in 실측 96.4 CAD/MWh 미만이라 정합. PricingHour(24개·평균 12.5=시간번호)는
     규칙대로 정확히 배제됐다. 72일 전부 같은 태그를 골랐다.
  2) 일별 존가격 실측 보존 = **2026-06-01~ 약 72일(≈2.4개월)**.
     v2의 "183개 = 6개월 롤링"은 `_v1`·`_v2` 버전 사본을 일수로 착각한 오판이었다.
  3) HOEP 88개월(2018-01~2025-04) 확정. 결손 2025-05~2026-05 = **13개월(영구 소실)**.
  4) GA 태그 6개: DocRevision / FirstEstimateRate(115.93) / SecondEstimate(216,393,725)
     / SecondEstimateRate(-1.76) / Actual(176,156,815) / ActualRate(14.38).
     금액과 요율이 섞이고 SecondEstimateRate가 음수 → **최종 Class B 요율 미확정**.
     추정으로 단일값을 고르지 않는다(절대원칙 1: 추정·창작 금지).

■ v4가 고치는 것
  A) [핵심] **누적 병합 저장.** v3는 기존 결과를 읽지 않고 덮어써서, 롤링 윈도(72일) 밖으로
     나간 달이 우리 파일에서도 사라졌다. 매일 돌려도 표본이 영구히 3개월이었다.
     병합 규칙 = **신규 days >= 기존 days 인 달만 교체.** 그래야
       · 진행 중인 달(11일 → 12일)은 갱신되고
       · 롤링에서 잘린 달(30일 → 5일)은 기존 완전값을 지킨다.
     HOEP·GA에도 같은 보호를 넣는다(수집 0이면 기존 유지).
  B) **성분 전량 보존.** 윈도가 72일이라 지금 안 받으면 영구 소실이다. ZonalPrice 외에
     LossPriceCapped·CongestionPriceCapped 등 하루 24개로 오는 수치 태그의 월평균을
     함께 저장한다 → ZonalPrice가 총액인지 에너지분인지 나중에 판정할 수 있다.
  C) **XSD 덤프.** XML 헤더의 xsi:schemaLocation 에서 실제 스키마 URL을 읽어 받아
     요소 정의·documentation 을 probe에 덤프한다. GA 요율 확정용.
  D) **vintage 기록.** 값이 바뀐 날짜를 각 달에 남겨 panel_monthly.csv 의 vintage 로 쓴다.
     (GA는 FirstEstimate → SecondEstimate → Actual 로 개정되므로 vintage가 필수다.)
  E) utcnow() → now(dt.UTC). 로그를 덮던 DeprecationWarning 6건 제거(진단 가독성).

■ 사용
  python fetch_ieso.py --discover   # 탐색만
  python fetch_ieso.py              # 탐색 + 수집 + 병합 저장
"""
import os, sys, json, csv, re, io
import datetime as dt
import xml.etree.ElementTree as ET
import requests

OUT_JSON  = "ot_wholesale_monthly.json"
OUT_CSV   = "ot_wholesale_monthly.csv"
OUT_GA    = "ot_ga_monthly.json"
OUT_PROBE = "ot_probe.json"

HOST = "https://reports-public.ieso.ca/public"
TIMEOUT = 60
START_YEAR = 2018
ZONAL_DIR = "DAHourlyOntarioZonalPrice"
ZONAL_PAT = "PUB_DAHourlyOntarioZonalPrice_{ymd}.xml"
XSD_NS = "{http://www.w3.org/2001/XMLSchema}"
TODAY = dt.datetime.now(dt.UTC).date().isoformat()


def get(url):
    try:
        r = requests.get(url, timeout=TIMEOUT)
        return r.status_code, r.text
    except Exception as e:
        return 0, f"EXC {e}"


def load_json(path):
    """기존 산출물을 읽어 병합 대상으로 쓴다. 없거나 깨졌으면 빈 dict."""
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception as e:
            print(f"[경고] {path} 읽기 실패({e}) — 병합 없이 새로 만든다", flush=True)
    return {}


def ym_of(s):
    s = str(s).strip()
    for rx, f in ((r"(\d{4})-(\d{2})", lambda m: f"{m.group(1)}-{m.group(2)}"),
                  (r"(\d{1,2})/(\d{1,2})/(\d{4})", lambda m: f"{m.group(3)}-{int(m.group(1)):02d}"),
                  (r"(\d{4})(\d{2})(\d{2})", lambda m: f"{m.group(1)}-{m.group(2)}")):
        m = re.match(rx, s)
        if m:
            return f(m)
    return None


def shift_ym(ym, k):
    """'2025-04' 을 k개월 이동."""
    t = int(ym[:4]) * 12 + int(ym[5:7]) - 1 + k
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


# ── A) 날짜 사다리 — 일별 존가격 보존 범위 확인 ────────────────────────────────
def zonal_horizon(probe):
    """월초 1일을 과거로 훑어 어디까지 파일이 남아있는지 찾는다."""
    now = dt.datetime.now(dt.UTC)
    ladder, found_oldest = [], None
    y, m = 2025, 5                      # MRP 개편 시점부터
    while (y, m) <= (now.year, now.month):
        ymd = f"{y}{m:02d}01"
        code, body = get(f"{HOST}/{ZONAL_DIR}/{ZONAL_PAT.format(ymd=ymd)}")
        ok = (code == 200 and len(body) > 300)
        ladder.append({"ymd": ymd, "http": code, "len": len(body), "ok": ok})
        print(f"  {'✅' if ok else '❌'} {ymd} HTTP {code} len={len(body)}", flush=True)
        if ok and found_oldest is None:
            found_oldest = f"{y}-{m:02d}"
        m += 1
        if m > 12:
            m = 1; y += 1
    probe["zonal_ladder"] = ladder
    probe["zonal_oldest_month"] = found_oldest
    print(f"[사다리] 일별 존가격 보존 시작월 = {found_oldest}", flush=True)
    return found_oldest


# ── XML 태그 구조 덤프(파서 확정 근거) ────────────────────────────────────────
def dump_xml_struct(text, label, probe, key):
    try:
        root = ET.fromstring(text)
    except Exception as e:
        probe[key] = {"error": f"XML 파싱 실패 {e}", "head": text[:400]}
        print(f"  [{label}] XML 파싱 실패 {e}", flush=True)
        return None
    tags, numeric = {}, {}
    for el in root.iter():
        t = el.tag.split("}")[-1]
        tags[t] = tags.get(t, 0) + 1
        if el.text:
            s = el.text.strip().replace(",", "")
            if re.fullmatch(r"-?\d+(\.\d+)?", s):
                numeric.setdefault(t, []).append(float(s))
    summary = {}
    for t, vs in numeric.items():
        summary[t] = {"n": len(vs), "min": round(min(vs), 3), "max": round(max(vs), 3),
                      "mean": round(sum(vs) / len(vs), 3), "sample": [round(v, 3) for v in vs[:6]]}
    probe[key] = {"tag_counts": dict(sorted(tags.items(), key=lambda x: -x[1])[:20]),
                  "numeric_tags": summary, "head": text[:700]}
    print(f"  [{label}] 숫자 태그: { {t: (d['n'], d['mean']) for t, d in summary.items()} }", flush=True)
    return root


# ── C) XSD 덤프 — 요소 정의·단위를 스키마에서 직접 확인 ────────────────────────
def fetch_xsd(xml_text, label, probe, key):
    """XML 헤더의 xsi:schemaLocation 에서 URL을 뽑아 스키마를 받아 요소 정의를 덤프."""
    m = re.search(r'schemaLocation\s*=\s*"([^"]+)"', xml_text)
    if not m:
        print(f"  [{label} XSD] schemaLocation 없음 — 건너뜀", flush=True)
        return
    url = m.group(1).split()[-1]          # "<namespace> <url>" 형태 → 마지막 토큰이 URL
    code, body = get(url)
    print(f"  [{label} XSD] {url} HTTP {code} len={len(body)}", flush=True)
    if code != 200:
        probe[key] = {"url": url, "http": code}
        return
    try:
        root = ET.fromstring(body)
    except Exception as e:
        probe[key] = {"url": url, "error": f"XSD 파싱 실패 {e}", "head": body[:1200]}
        print(f"  [{label} XSD] 파싱 실패 {e}", flush=True)
        return
    elems = [{"name": el.get("name"), "type": el.get("type")}
             for el in root.iter(XSD_NS + "element") if el.get("name")]
    docs = [d.text.strip()[:300] for d in root.iter(XSD_NS + "documentation")
            if d.text and d.text.strip()]
    probe[key] = {"url": url, "elements": elems, "documentation": docs, "head": body[:1500]}
    print(f"  [{label} XSD] 요소 {len(elems)}개: {[(e['name'], e['type']) for e in elems][:20]}", flush=True)
    if docs:
        print(f"  [{label} XSD] 주석 {len(docs)}건 앞3: {docs[:3]}", flush=True)


# ── B) 존가격 일별 XML → 월평균 (성분 전량 보존) ───────────────────────────────
def parse_zonal_day(text):
    """하루 24개(±)로 오는 수치 태그를 모두 모으고, 그중 가격 태그를 점수로 고른다."""
    try:
        root = ET.fromstring(text)
    except Exception:
        return None
    numeric = {}
    for el in root.iter():
        t = el.tag.split("}")[-1]
        if el.text:
            s = el.text.strip().replace(",", "")
            if re.fullmatch(r"-?\d+(\.\d+)?", s):
                numeric.setdefault(t, []).append(float(s))
    cand = {t: vs for t, vs in numeric.items() if 20 <= len(vs) <= 60}   # 하루 24시간(±)
    best, bestscore = None, -1
    for t, vs in cand.items():
        score = 0
        if re.search(r"price|lmp|hoep|energy", t, re.I): score += 5
        if all(-200 <= v <= 2000 for v in vs):     # 가격다운 범위
            score += 3
        if re.search(r"hour|interval|delivery|version|sequence", t, re.I):
            score -= 6                             # 시간·버전 번호 배제
        if score > bestscore:
            best, bestscore = t, score
    if best is None or bestscore <= 0:
        return None
    # v4-B) 시간번호성 태그만 빼고 전부 넘긴다 — 윈도 72일이라 지금 못 받으면 영구 소실.
    extras = {t: sum(vs) / len(vs) for t, vs in cand.items()
              if not re.search(r"hour|interval|version|sequence", t, re.I)}
    vs = cand[best]
    return {"tag": best, "n": len(vs), "mean": sum(vs) / len(vs), "extras": extras}


def collect_zonal(oldest_month, probe):
    if not oldest_month:
        print("[존가격] 보존 시작월 미확인 — 수집 생략", flush=True)
        return [], {}
    now = dt.datetime.now(dt.UTC)
    y, m = int(oldest_month[:4]), int(oldest_month[5:7])
    acc, comp, used_tag = {}, {}, {}
    dumped, tried, got = False, 0, 0
    while (y, m) <= (now.year, now.month):
        for d in range(1, 32):
            try:
                dt.date(y, m, d)
            except ValueError:
                continue
            ymd = f"{y}{m:02d}{d:02d}"
            code, body = get(f"{HOST}/{ZONAL_DIR}/{ZONAL_PAT.format(ymd=ymd)}")
            tried += 1
            if code != 200 or len(body) < 300:
                continue
            if not dumped:
                dump_xml_struct(body, f"존가격 {ymd}", probe, "zonal_struct")
                fetch_xsd(body, f"존가격 {ymd}", probe, "zonal_xsd")
                dumped = True
            r = parse_zonal_day(body)
            if not r:
                continue
            key = f"{y}-{m:02d}"
            a = acc.setdefault(key, [0.0, 0])
            a[0] += r["mean"]; a[1] += 1
            cm = comp.setdefault(key, {})
            for t, v in r["extras"].items():
                c = cm.setdefault(t, [0.0, 0])
                c[0] += v; c[1] += 1
            used_tag[r["tag"]] = used_tag.get(r["tag"], 0) + 1
            got += 1
        print(f"[존가격 {y}-{m:02d}] 누적 {acc.get(f'{y}-{m:02d}',[0,0])[1]}일", flush=True)
        m += 1
        if m > 12:
            m = 1; y += 1
    series = []
    for k, (s, c) in sorted(acc.items()):
        if not c:
            continue
        series.append({"ym": k, "cad_mwh": round(s / c, 3), "days": c,
                       "components": {t: round(cs / cn, 4)
                                      for t, (cs, cn) in sorted(comp.get(k, {}).items()) if cn}})
    print(f"[존가격] {tried}일 시도 · {got}일 성공 · {len(series)}개월 · 채택태그 {used_tag}", flush=True)
    probe["zonal_used_tags"] = used_tag
    return series, used_tag


# ── A) 누적 병합 ──────────────────────────────────────────────────────────────
def merge_zonal(old, new):
    """ym 기준 병합. 신규 days >= 기존 days 인 달만 교체(롤링 절단 보호)."""
    by = {r["ym"]: r for r in (old or [])}
    added = replaced = kept = 0
    for r in new:
        o = by.get(r["ym"])
        if o is None:
            r["vintage"] = TODAY
            by[r["ym"]] = r; added += 1
        elif r.get("days", 0) >= o.get("days", 0):
            changed = (r.get("cad_mwh") != o.get("cad_mwh") or r.get("days") != o.get("days"))
            r["vintage"] = TODAY if changed else o.get("vintage", TODAY)
            by[r["ym"]] = r; replaced += 1
        else:
            kept += 1
            print(f"  [병합] {r['ym']} 신규 {r.get('days')}일 < 기존 {o.get('days')}일"
                  f" → 기존 유지(롤링 절단 보호)", flush=True)
    out = [by[k] for k in sorted(by)]
    print(f"[병합] 존가격 신규 {added} · 갱신 {replaced} · 기존유지 {kept} → 총 {len(out)}개월", flush=True)
    return out


def merge_ga(old, new):
    """GA는 FirstEstimate → SecondEstimate → Actual 로 개정되므로 신규가 항상 더 확정적."""
    by = {r["ym"]: r for r in (old or [])}
    added = updated = 0
    for r in new:
        o = by.get(r["ym"])
        if o is None:
            r["vintage"] = TODAY
            by[r["ym"]] = r; added += 1
        else:
            r["vintage"] = TODAY if r.get("by_tag") != o.get("by_tag") else o.get("vintage", TODAY)
            by[r["ym"]] = r; updated += 1
    out = [by[k] for k in sorted(by)]
    print(f"[병합] GA 신규 {added} · 갱신 {updated} → 총 {len(out)}개월", flush=True)
    return out


# ── HOEP(구 체계) ─────────────────────────────────────────────────────────────
def find_header(lines):
    for i, l in enumerate(lines[:60]):
        low = l.lower()
        if "date" in low and ("hour" in low or "he " in low or ",he" in low or "delivery" in low):
            return i
    for i, l in enumerate(lines[:60]):
        if "date" in l.lower() and l.count(",") >= 2:
            return i
    return None


def pick_col(cols, prefer):
    low = [c.strip().lower() for c in cols]
    for want in prefer:
        for i, c in enumerate(low):
            if c == want:
                return i
    for want in prefer:
        for i, c in enumerate(low):
            if want in c:
                return i
    return None


def collect_hoep():
    now = dt.datetime.now(dt.UTC)
    pts, used = [], {}
    for y in range(START_YEAR, now.year + 1):
        url = f"{HOST}/PriceHOEPPredispOR/PUB_PriceHOEPPredispOR_{y}.csv"
        code, body = get(url)
        if code != 200 or len(body) < 200:
            print(f"[HOEP {y}] HTTP {code} — 없음(개편 후 정상)", flush=True)
            continue
        lines = body.splitlines()
        hi = find_header(lines)
        if hi is None:
            continue
        rows = list(csv.reader(io.StringIO("\n".join(lines[hi:]))))
        cols = rows[0]
        di = pick_col(cols, ["date", "delivery date"])
        vi = pick_col(cols, ["hoep", "price"])
        if di is None or vi is None:
            continue
        c0 = 0
        for r in rows[1:]:
            if len(r) <= max(di, vi):
                continue
            k = ym_of(r[di])
            if not k:
                continue
            try:
                pts.append((k, float(str(r[vi]).replace(",", "")))); c0 += 1
            except Exception:
                pass
        used[str(y)] = url
        print(f"[HOEP {y}] {c0}행", flush=True)
    return pts, used


# ── D) GA ────────────────────────────────────────────────────────────────────
def collect_ga(probe):
    now = dt.datetime.now(dt.UTC)
    series, dumped = [], False
    y, m = 2024, 1
    while (y, m) <= (now.year, now.month):
        ym = f"{y}{m:02d}"
        code, body = get(f"{HOST}/GlobalAdjustment/PUB_GlobalAdjustment_{ym}.xml")
        if code == 200 and len(body) > 200:
            if not dumped:
                dump_xml_struct(body, f"GA {ym}", probe, "ga_struct")
                fetch_xsd(body, f"GA {ym}", probe, "ga_xsd")
                dumped = True
            # 태그별 값을 전부 담아두고 확정은 XSD 확인 후(잘못된 단일값 추정 금지)
            try:
                root = ET.fromstring(body)
                vals = {}
                for el in root.iter():
                    t = el.tag.split("}")[-1]
                    if el.text:
                        s = el.text.strip().replace(",", "")
                        if re.fullmatch(r"-?\d+(\.\d+)?", s):
                            vals.setdefault(t, []).append(float(s))
                series.append({"ym": f"{y}-{m:02d}",
                               "by_tag": {t: [round(sum(v) / len(v), 4), len(v)]
                                          for t, v in sorted(vals.items())}})
            except Exception as e:
                print(f"[GA {ym}] 파싱 실패 {e}", flush=True)
        m += 1
        if m > 12:
            m = 1; y += 1
    print(f"[GA] 이번 수집 {len(series)}개월", flush=True)
    return series


def main():
    probe = {"checked_at": dt.datetime.now(dt.UTC).isoformat()[:16]}
    print("### A) 일별 존가격 보존 범위 사다리 탐색", flush=True)
    oldest = zonal_horizon(probe)

    if "--discover" in sys.argv:
        json.dump(probe, open(OUT_PROBE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"[DISCOVER] {OUT_PROBE} 저장", flush=True)
        return

    prev, prev_ga = load_json(OUT_JSON), load_json(OUT_GA)
    prev_zonal = prev.get("series_zonal", [])
    print(f"[기존] 존가격 {len(prev_zonal)}개월 · GA {len(prev_ga.get('series', []))}개월 — 병합 대상",
          flush=True)

    print("### B) HOEP(구 체계, ≤2025-04)", flush=True)
    hpts, hused = collect_hoep()
    acc = {}
    for k, v in hpts:
        a = acc.setdefault(k, [0.0, 0]); a[0] += v; a[1] += 1
    hoep = [{"ym": k, "cad_mwh": round(s / c, 3), "n": c} for k, (s, c) in sorted(acc.items()) if c]
    if not hoep and prev.get("series"):
        hoep = prev["series"]
        print(f"[HOEP] 수집 0 — 기존 {len(hoep)}개월 유지(덮어쓰기 방지)", flush=True)

    print("### C) 개편 후 존가격(일별→월평균)", flush=True)
    zonal_new, ztags = collect_zonal(oldest, probe)

    print("### C-2) 누적 병합", flush=True)
    zonal = merge_zonal(prev_zonal, zonal_new)

    print("### D) GA", flush=True)
    ga = merge_ga(prev_ga.get("series", []), collect_ga(probe))

    last = (zonal[-1]["ym"] if zonal else (hoep[-1]["ym"] if hoep else None))
    now = dt.datetime.now(dt.UTC)
    lag = None
    if last:
        lag = (now.year - int(last[:4])) * 12 + (now.month - int(last[5:7]))

    gap = None
    if hoep and zonal:
        n = (int(zonal[0]["ym"][:4]) * 12 + int(zonal[0]["ym"][5:7])) \
            - (int(hoep[-1]["ym"][:4]) * 12 + int(hoep[-1]["ym"][5:7])) - 1
        if n > 0:
            gap = {"months": n, "from": shift_ym(hoep[-1]["ym"], 1),
                   "to": shift_ym(zonal[0]["ym"], -1),
                   "note": "IESO 롤링 윈도로 이미 삭제된 구간 — 영구 결손"}

    out = {
        "source": "IESO public reports (Ontario)",
        "version": "v4",
        "zone": "OT", "unit": "CAD/MWh",
        "merge_policy": "존가격은 ym 기준 병합. 신규 days >= 기존 days 인 달만 교체(롤링 절단 보호). "
                        "HOEP·GA도 수집 0이면 기존 유지.",
        "regimes": {
            "hoep": {"desc": "구 체계 HOEP(단일 온타리오 가격). 2025-05 MRP 개편으로 발행 중단",
                     "months": len(hoep), "range": [hoep[0]["ym"], hoep[-1]["ym"]] if hoep else None},
            "zonal": {"desc": "개편 후 Day-Ahead Hourly Ontario Zonal Price(일별 XML 월평균). 태그 ZonalPrice",
                      "months": len(zonal), "range": [zonal[0]["ym"], zonal[-1]["ym"]] if zonal else None,
                      "used_tags": ztags,
                      "components_note": "components = 같은 파일에서 하루 24개로 오는 다른 수치 태그의 "
                                         "월평균(LossPriceCapped·CongestionPriceCapped 등). "
                                         "ZonalPrice가 총액인지 에너지분인지 XSD로 확정할 때 쓴다."},
        },
        "note": "⚠️ HOEP와 존가격은 가격 정의가 달라 하나로 이어붙이지 않았다(레짐 단절이 ETS를 오염시킴). "
                "파일럿이 두 계열을 각각 받아 판단한다. "
                "all-in = 에너지 + GA(Class A·ICI) + 송전(월 최대수요 kW) + 규제요금.",
        "latest": last, "lag_months": lag, "gap": gap,
        "used_urls": hused,
        "series": hoep,          # 하위호환(기존 파일럿이 읽는 필드) = HOEP
        "series_zonal": zonal,   # 개편 후 계열(누적 병합)
    }
    json.dump(out, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f); w.writerow(["regime", "ym", "cad_mwh", "n", "vintage"])
        for s in hoep:
            w.writerow(["hoep", s["ym"], s["cad_mwh"], s["n"], s.get("vintage", "")])
        for s in zonal:
            w.writerow(["zonal", s["ym"], s["cad_mwh"], s["days"], s.get("vintage", "")])
    json.dump({"source": "IESO GlobalAdjustment monthly XML (Class B Rates)",
               "version": "v4",
               "note": "태그별 [평균, 개수] 를 그대로 저장. 어느 태그가 최종 Class B 요율인지는 "
                       "ot_probe.json 의 ga_xsd(요소 정의·주석)로 확정한다. "
                       "OT는 Class A이므로 이 값은 상방 시나리오·기준값 용도로만 쓴다.",
               "months": len(ga), "series": ga},
              open(OUT_GA, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(probe, open(OUT_PROBE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[OK] HOEP {len(hoep)}개월 · 존가격 {len(zonal)}개월 · GA {len(ga)}개월 "
          f"· 최신 {last}(지연 {lag}개월) · 결손 {gap['months'] if gap else 0}개월", flush=True)


if __name__ == "__main__":
    main()
