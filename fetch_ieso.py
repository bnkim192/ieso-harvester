# -*- coding: utf-8 -*-
"""
IESO(캐나다 온타리오) 공개 리포트 → 월평균 도매가(CAD/MWh) + Global Adjustment 수집기.  v2
키·로그인 불필요(공개 파일).

■ v1 실행에서 확인된 사실 (2026-07-30)
  · 에너지: PriceHOEPPredispOR/PUB_PriceHOEPPredispOR_{연도}.csv → **88개월(2018-01~2025-04) 수집 성공**
    그런데 **2025-05부터 데이터가 없다** = 2025-05-01 온타리오 시장개편(MRP)으로 HOEP가 종료된 것으로 보임.
  · 개편 후 가격: RealtimeOntarioZonalPrice/ 디렉터리는 존재하나 **시간별 XML**
    (PUB_RealtimeOntarioZonalPrice_YYYYMMDDHH_vN.xml) → 월평균을 만들려면 파일이 너무 많음.
    → **일별/월별/연간 집계 파일이 있는지 이 스크립트가 직접 찔러본다(PATTERN_PROBE).**
  · GA: GlobalAdjustment/ 에 **PUB_GlobalAdjustment_YYYYMM.xml (월별)** 존재, 2026-07까지 최신.
    v1은 연간 CSV를 찾아서 실패했음 → v2에서 월별 XML로 수정.

■ 주의: 이 스크립트를 만든 사람(Claude)은 사내 프록시 때문에 IESO에 직접 접속할 수 없다.
  따라서 모든 탐색 결과를 **로그와 ot_probe.json에 남긴다.** 그걸 보고 다음 버전을 확정한다.

■ 사용
  python fetch_ieso.py --discover    # 디렉터리·파일패턴 탐색만(진단)
  python fetch_ieso.py               # 탐색 + 수집·집계·저장
"""
import os, sys, json, csv, re, io
import datetime as dt
import xml.etree.ElementTree as ET
import requests

OUT_JSON  = "ot_wholesale_monthly.json"
OUT_CSV   = "ot_wholesale_monthly.csv"
OUT_GA    = "ot_ga_monthly.json"
OUT_PROBE = "ot_probe.json"

START_YEAR = 2018
TIMEOUT    = 90
HOST       = "https://reports-public.ieso.ca/public"

# ── 1) 디렉터리 후보(존재 여부 확인용) ────────────────────────────────────────
DIRS = [
    # v1에서 존재 확인됨
    "PriceHOEPPredispOR", "RealtimeOntarioZonalPrice", "GlobalAdjustment",
    # 개편(MRP) 후 가격 리포트 후보
    "DAHourlyOntarioZonalPrice", "DayAheadOntarioZonalPrice", "OntarioZonalPrice",
    "RealtimeZonalPrice", "DAZonalPrice", "PredispOntarioZonalPrice",
    "RealtimeEnergyLMP", "DAEnergyLMP", "RealtimeLMP", "DALMP",
    "RealtimeMarketPrice", "DayAheadMarketPrice", "HourlyEnergyPrice",
    # 월간/요약 리포트 후보
    "MonthlyEnergyPrice", "MonthlyMarketSummary", "MonthlySummary",
    "GlobalAdjustmentSummary", "HourlyDemand", "DemandZonal",
]

# ── 2) 파일명 패턴 후보(집계 파일이 있는지 직접 찔러보기) ──────────────────────
#    {y}=2026 {ym}=202607 {ymd}=20260729 {ymdh}=2026072912
PATTERN_PROBE = {
    "RealtimeOntarioZonalPrice": [
        "PUB_RealtimeOntarioZonalPrice.xml",
        "PUB_RealtimeOntarioZonalPrice_{ymd}.xml",
        "PUB_RealtimeOntarioZonalPrice_{ymd}.csv",
        "PUB_RealtimeOntarioZonalPrice_{ym}.xml",
        "PUB_RealtimeOntarioZonalPrice_{ym}.csv",
        "PUB_RealtimeOntarioZonalPrice_{y}.xml",
        "PUB_RealtimeOntarioZonalPrice_{y}.csv",
    ],
    "PriceHOEPPredispOR": [
        "PUB_PriceHOEPPredispOR_{y}.csv",      # 2026 파일이 있나? (있으면 HOEP 계속됨)
        "PUB_PriceHOEPPredispOR.csv",          # 무날짜(최신)
    ],
    "GlobalAdjustment": [
        "PUB_GlobalAdjustment_{ym}.xml",       # v1 목록에서 확인된 패턴
        "PUB_GlobalAdjustment_{y}.xml",
    ],
}


def get(url):
    try:
        r = requests.get(url, timeout=TIMEOUT)
        return r.status_code, r.text
    except Exception as e:
        return 0, f"EXC {e}"


def pattern_stats(names):
    """파일명 날짜패턴별 개수 — 집계 파일 존재 여부를 한눈에."""
    pat = {
        "YYYYMMDDHH(시간별)": r"_\d{10}(_v\d+)?\.(xml|csv)$",
        "YYYYMMDD(일별)":     r"_\d{8}(_v\d+)?\.(xml|csv)$",
        "YYYYMM(월별)":       r"_\d{6}(_v\d+)?\.(xml|csv)$",
        "YYYY(연간)":         r"_\d{4}(_v\d+)?\.(xml|csv)$",
    }
    out = {}
    for k, rx in pat.items():
        out[k] = sum(1 for n in names if re.search(rx, n))
    out["무날짜"] = sum(1 for n in names if not re.search(r"\d{4}", n))
    return out


def discover():
    now = dt.datetime.utcnow()
    y   = now.year
    ym  = f"{now.year}{now.month:02d}"
    ymd = (now - dt.timedelta(days=2)).strftime("%Y%m%d")     # 어제/그제(확정본)
    found = {"dirs": {}, "patterns": {}, "checked_at": now.isoformat()[:16]}

    print("### 1) 디렉터리 존재 확인", flush=True)
    for d in DIRS:
        url = f"{HOST}/{d}/"
        code, body = get(url)
        names = []
        if code == 200:
            names = sorted({x.split("/")[-1] for x in
                            re.findall(r'href="([^"]+\.(?:csv|xml))"', body, re.I)})
        if not names:
            print(f"  ❌ {d:32s} HTTP {code}", flush=True)
            continue
        st = pattern_stats(names)
        print(f"  ✅ {d:32s} 파일 {len(names)}개 · {st}", flush=True)
        # 집계 후보(일/월/연)만 예시 출력 — 시간별은 너무 많아 생략
        agg = [n for n in names if re.search(r"_\d{4}(\d{2})?(\d{2})?(_v\d+)?\.(xml|csv)$", n)
               and not re.search(r"_\d{10}", n)]
        for n in agg[-8:]:
            print(f"       {n}", flush=True)
        found["dirs"][d] = {"count": len(names), "stats": st, "agg_examples": agg[-20:],
                            "last": names[-5:]}

    print("### 2) 파일명 패턴 직접 확인(집계 파일 존재?)", flush=True)
    for d, pats in PATTERN_PROBE.items():
        for p in pats:
            fn = p.format(y=y, ym=ym, ymd=ymd)
            url = f"{HOST}/{d}/{fn}"
            code, body = get(url)
            ok = (code == 200 and len(body) > 300)
            head = (body[:160].replace("\n", " ") if ok else "")
            print(f"  {'✅' if ok else '❌'} {fn:52s} HTTP {code} len={len(body)}", flush=True)
            if ok:
                print(f"       head: {head}", flush=True)
            found["patterns"][f"{d}/{fn}"] = {"http": code, "len": len(body), "head": head}

    json.dump(found, open(OUT_PROBE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[DISCOVER] {OUT_PROBE} 저장", flush=True)
    return found


# ── 에너지(HOEP 구 리포트) ────────────────────────────────────────────────────
def ym_of(s):
    s = str(s).strip()
    for rx, f in ((r"(\d{4})-(\d{2})", lambda m: f"{m.group(1)}-{m.group(2)}"),
                  (r"(\d{1,2})/(\d{1,2})/(\d{4})", lambda m: f"{m.group(3)}-{int(m.group(1)):02d}"),
                  (r"(\d{4})(\d{2})(\d{2})", lambda m: f"{m.group(1)}-{m.group(2)}")):
        m = re.match(rx, s)
        if m:
            return f(m)
    return None


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


def parse_price_csv(text, label):
    lines = text.splitlines()
    hi = find_header(lines)
    if hi is None:
        print(f"    [WARN] {label}: 헤더 인식 실패", flush=True)
        return []
    rows = list(csv.reader(io.StringIO("\n".join(lines[hi:]))))
    if not rows:
        return []
    cols = rows[0]
    di = pick_col(cols, ["date", "delivery date"])
    vi = pick_col(cols, ["hoep", "ontario zonal price", "zonal price", "price", "lmp"])
    if di is None or vi is None:
        print(f"    [WARN] {label}: 컬럼 인식 실패 header={cols[:8]}", flush=True)
        return []
    out = []
    for r in rows[1:]:
        if len(r) <= max(di, vi):
            continue
        k = ym_of(r[di])
        if not k:
            continue
        try:
            out.append((k, float(str(r[vi]).replace(",", ""))))
        except Exception:
            pass
    print(f"    [읽음] {label}: {len(out)}행 · 날짜='{cols[di]}' 값='{cols[vi]}'", flush=True)
    return out


def collect_hoep():
    now = dt.datetime.utcnow()
    pts, used = [], {}
    for y in range(START_YEAR, now.year + 1):
        url = f"{HOST}/PriceHOEPPredispOR/PUB_PriceHOEPPredispOR_{y}.csv"
        code, body = get(url)
        if code != 200 or len(body) < 200:
            print(f"[HOEP {y}] HTTP {code} — 없음", flush=True)
            continue
        p = parse_price_csv(body, f"HOEP {y}")
        if p:
            pts += p
            used[str(y)] = url
    return pts, used


# ── GA(월별 XML) ──────────────────────────────────────────────────────────────
def parse_ga_xml(text):
    """GA XML에서 숫자 값을 추출. 구조를 모르므로 태그명을 로그로 남기고 후보를 넓게 잡는다."""
    try:
        root = ET.fromstring(text)
    except Exception as e:
        return None, f"XML 파싱 실패 {e}", []
    tags, vals = {}, []
    for el in root.iter():
        t = el.tag.split("}")[-1]
        tags[t] = tags.get(t, 0) + 1
        if el.text:
            s = el.text.strip().replace(",", "")
            if re.fullmatch(r"-?\d+(\.\d+)?", s):
                vals.append((t, float(s)))
    # GA 총액/단가로 보이는 태그 우선
    prefer = [v for (t, v) in vals if re.search(r"ga|adjust|rate|amount|price", t, re.I)]
    pick = prefer[0] if prefer else (vals[0][1] if vals else None)
    return pick, None, sorted(tags.items(), key=lambda x: -x[1])[:12]


def collect_ga():
    now = dt.datetime.utcnow()
    series, used, shown = [], {}, False
    y, m = START_YEAR, 1
    while (y, m) <= (now.year, now.month):
        ym = f"{y}{m:02d}"
        url = f"{HOST}/GlobalAdjustment/PUB_GlobalAdjustment_{ym}.xml"
        code, body = get(url)
        if code == 200 and len(body) > 200:
            val, err, tags = parse_ga_xml(body)
            if not shown:
                print(f"[GA 구조] {ym} 태그 상위: {tags}", flush=True)
                print(f"[GA 구조] 원문 앞 500자: {body[:500]}", flush=True)
                shown = True
            if val is not None:
                series.append({"ym": f"{y}-{m:02d}", "value": round(val, 4)})
                used[f"{y}-{m:02d}"] = url
            elif err:
                print(f"[GA {ym}] {err}", flush=True)
        m += 1
        if m > 12:
            m = 1; y += 1
    return series, used


def agg_monthly(pts):
    acc = {}
    for k, v in pts:
        a = acc.setdefault(k, [0.0, 0])
        a[0] += v; a[1] += 1
    return [{"ym": k, "cad_mwh": round(s / c, 3), "n": c} for k, (s, c) in sorted(acc.items()) if c]


def main():
    disc = discover()
    if "--discover" in sys.argv:
        return

    print("### 3) 에너지(HOEP) 수집", flush=True)
    epts, eused = collect_hoep()
    if not epts:
        print("!! HOEP 0행 — 위 탐색 로그의 실제 파일명 확인 필요", flush=True)
        sys.exit(1)
    series = agg_monthly(epts)
    last = series[-1]["ym"]
    now = dt.datetime.utcnow()
    lag = (now.year - int(last[:4])) * 12 + (now.month - int(last[5:7]))
    print(f"[에너지] {len(series)}개월 · 최신 {last} (지연 {lag}개월)", flush=True)
    if lag > 2:
        print(f"⚠️ 에너지 시계열이 {lag}개월 뒤처짐 — 2025-05 MRP 개편으로 HOEP 종료 추정. "
              f"위 '2) 파일명 패턴' 결과에서 개편 후 집계 파일을 확인할 것.", flush=True)

    print("### 4) GA 수집", flush=True)
    ga, gused = collect_ga()
    print(f"[GA] {len(ga)}개월" + (f" · 최신 {ga[-1]}" if ga else " — 미확보"), flush=True)

    out = {
        "source": "IESO public reports (Ontario) — monthly average of hourly",
        "zone": "OT", "unit": "CAD/MWh",
        "note": "에너지(도매) 성분만. 사업장 all-in = 에너지 + Global Adjustment + 송배전(월 최대수요 kW 비례) + 규제요금.",
        "months": len(series), "latest": last, "lag_months": lag,
        "gap_warning": (f"HOEP는 {last}까지만 존재(2025-05 MRP 개편으로 종료 추정). "
                        f"개편 후 구간은 미수집 상태." if lag > 2 else ""),
        "used_urls": eused, "series": series,
    }
    json.dump(out, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f); w.writerow(["ym", "cad_mwh", "n"])
        for s in series:
            w.writerow([s["ym"], s["cad_mwh"], s["n"]])
    if ga:
        json.dump({"source": "IESO GlobalAdjustment monthly XML", "months": len(ga),
                   "note": "단위·의미는 태그 구조 로그로 확인 후 확정(1차 자동추출값)",
                   "used_urls": gused, "series": ga},
                  open(OUT_GA, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[OK] {OUT_JSON} 저장 · {OUT_CSV}" + (f" · {OUT_GA}" if ga else ""), flush=True)


if __name__ == "__main__":
    main()
