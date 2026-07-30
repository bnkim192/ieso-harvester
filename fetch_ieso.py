# -*- coding: utf-8 -*-
"""
IESO(캐나다 온타리오) 공개 리포트 → 월평균 도매가(CAD/MWh) + Global Adjustment 수집기.
키·로그인 불필요(공개 파일). 산출: ot_wholesale_monthly.json/.csv (+ ot_ga_monthly.json)

■ 왜 '탐색(discover)' 단계가 있나
  작성자(Claude)는 사내망 차단으로 IESO URL을 직접 확인할 수 없었다.
  그래서 아래 CANDIDATE 목록은 '후보'이며, --discover 가 공개 디렉터리 목록을 실제로 읽어
  **어떤 파일이 존재하는지 Actions 로그에 그대로 출력**한다. 첫 실행 로그를 보고 확정하면 된다.
  ⚠️ 2025-05 시장개편(MRP) 이후 HOEP → 온타리오 存 가격(zonal) 체계로 바뀌었을 수 있어
     구/신 리포트를 모두 후보에 넣었다.

■ 사용
  python fetch_ieso.py --discover    # 공개 디렉터리에 뭐가 있는지 나열(진단)
  python fetch_ieso.py               # 수집·집계·저장
"""
import os, sys, json, csv, re, io
import datetime as dt
import requests

OUT_JSON  = "ot_wholesale_monthly.json"
OUT_CSV   = "ot_wholesale_monthly.csv"
OUT_GA    = "ot_ga_monthly.json"
OUT_PROBE = "ot_probe.json"

START_YEAR = 2018
TIMEOUT    = 90

# IESO 공개 리포트 루트(둘 다 후보 — 로그로 살아있는 쪽 확인)
HOSTS = [
    "https://reports-public.ieso.ca/public",
    "http://reports.ieso.ca/public",
]

# 탐색할 리포트 디렉터리(존재 여부를 --discover 가 알려줌)
DIRS = [
    "PriceHOEPPredispOR",        # (구) 시간별 HOEP + predispatch + OR
    "DispUnconsHOEP",            # (구) HOEP
    "RealtimeOntarioZonalPrice", # (신·MRP) 실시간 온타리오 존 가격
    "HourlyOntarioZonalPrice",   # (신·MRP) 시간별 존 가격
    "GlobalAdjustment",          # GA
    "GlobalAdjustmentSummary",   # GA 요약
]

# 연도별 파일 후보(에너지 가격) — {h}=host, {y}=연도
ENERGY_CANDIDATES = [
    "{h}/PriceHOEPPredispOR/PUB_PriceHOEPPredispOR_{y}.csv",
    "{h}/DispUnconsHOEP/PUB_DispUnconsHOEP_{y}.csv",
    "{h}/HourlyOntarioZonalPrice/PUB_HourlyOntarioZonalPrice_{y}.csv",
    "{h}/RealtimeOntarioZonalPrice/PUB_RealtimeOntarioZonalPrice_{y}.csv",
]
GA_CANDIDATES = [
    "{h}/GlobalAdjustment/PUB_GlobalAdjustment_{y}.csv",
    "{h}/GlobalAdjustmentSummary/PUB_GlobalAdjustmentSummary_{y}.csv",
]


def get(url):
    try:
        r = requests.get(url, timeout=TIMEOUT)
        return r.status_code, r.text
    except Exception as e:
        return 0, f"EXC {e}"


def discover():
    """공개 디렉터리 목록을 읽어 실제 파일명을 출력 — 첫 실행에서 엔드포인트 확정용."""
    found = {}
    for h in HOSTS:
        for d in DIRS:
            url = f"{h}/{d}/"
            code, body = get(url)
            hrefs = []
            if code == 200:
                hrefs = re.findall(r'href="([^"]+\.(?:csv|xml))"', body, re.I)
                hrefs = [x.split("/")[-1] for x in hrefs]
            print(f"[DISCOVER] {url} -> HTTP {code} · 파일 {len(hrefs)}개", flush=True)
            for name in sorted(set(hrefs))[-25:]:      # 최신 쪽 위주로 25개
                print(f"           {name}", flush=True)
            if hrefs:
                found[url] = sorted(set(hrefs))[-60:]
    json.dump(found, open(OUT_PROBE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[DISCOVER] 결과 {OUT_PROBE} 저장 (디렉터리 {len(found)}개 응답)", flush=True)
    return found


def ym_of(s):
    s = str(s).strip()
    m = re.match(r"(\d{4})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)          # M/D/YYYY
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}"
    m = re.match(r"(\d{4})(\d{2})(\d{2})", s)                # YYYYMMDD
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return None


def find_header(lines):
    """IESO CSV는 앞에 제목·생성일 등 머리말이 붙는다 → 날짜/시간 헤더 행을 찾는다."""
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
    """시간별(또는 일별) 가격 CSV → [(ym, value)] 목록."""
    lines = text.splitlines()
    hi = find_header(lines)
    if hi is None:
        print(f"    [WARN] {label}: 헤더 인식 실패 (앞 2행: {lines[:2]})", flush=True)
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
        ym = ym_of(r[di])
        if not ym:
            continue
        try:
            v = float(str(r[vi]).replace(",", ""))
        except Exception:
            continue
        out.append((ym, v))
    print(f"    [읽음] {label}: {len(out)}행 · 날짜열='{cols[di]}' 값열='{cols[vi]}'", flush=True)
    return out


def collect(candidates, kind):
    """연도별로 후보 URL을 순회 — 처음 성공한 패턴을 그 연도의 소스로 채택."""
    now = dt.datetime.utcnow()
    pts, used = [], {}
    for y in range(START_YEAR, now.year + 1):
        got = False
        for tmpl in candidates:
            for h in HOSTS:
                url = tmpl.format(h=h, y=y)
                code, body = get(url)
                if code != 200 or len(body) < 200:
                    continue
                p = parse_price_csv(body, f"{kind} {y} {url.split('/')[-1]}")
                if p:
                    pts += p
                    used[str(y)] = url
                    got = True
                    break
            if got:
                break
        print(f"[{kind} {y}] {'OK ' + used.get(str(y), '') if got else '미확보'}", flush=True)
    return pts, used


def agg_monthly(pts):
    acc = {}
    for ym, v in pts:
        a = acc.setdefault(ym, [0.0, 0])
        a[0] += v; a[1] += 1
    return [{"ym": k, "cad_mwh": round(s / c, 3), "n": c} for k, (s, c) in sorted(acc.items()) if c]


def main():
    if "--discover" in sys.argv:
        discover()
        return

    print("=== 1) 에너지(도매) 가격 ===", flush=True)
    epts, eused = collect(ENERGY_CANDIDATES, "energy")
    print("=== 2) Global Adjustment ===", flush=True)
    gpts, gused = collect(GA_CANDIDATES, "GA")

    if not epts:
        print("!! 에너지 가격 0행 — 엔드포인트 미확정. `python fetch_ieso.py --discover` 로그의 "
              "실제 파일명을 보고 ENERGY_CANDIDATES를 고칠 것.", flush=True)
        discover()
        sys.exit(1)

    series = agg_monthly(epts)
    print(f"[월 수] {len(series)} · 최신 {series[-1]}", flush=True)

    out = {
        "source": "IESO public reports (Ontario) — monthly average of hourly",
        "zone": "OT", "unit": "CAD/MWh",
        "note": "에너지(도매) 성분만. 사업장 all-in = 에너지 + Global Adjustment + 송배전 + 규제요금. "
                "GA는 시장가와 역상관(시장가↓→GA↑)이라 all-in은 도매보다 훨씬 안정적 — "
                "파일럿은 사업장 앵커에 스케일해서 사용.",
        "months": len(series), "used_urls": eused, "series": series,
    }
    json.dump(out, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f); w.writerow(["ym", "cad_mwh", "n"])
        for s in series:
            w.writerow([s["ym"], s["cad_mwh"], s["n"]])

    if gpts:
        ga = agg_monthly(gpts)
        json.dump({"source": "IESO Global Adjustment", "unit": "CAD/MWh(추정·원자료 단위 확인 필요)",
                   "months": len(ga), "used_urls": gused, "series": ga},
                  open(OUT_GA, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"[GA] {len(ga)}개월 저장 · 최신 {ga[-1]}", flush=True)
    else:
        print("[GA] 미확보 — all-in 재구성은 보류(사업장 앵커 스케일 방식으로 진행)", flush=True)

    print(f"[OK] {OUT_JSON} / {OUT_CSV} 저장", flush=True)


if __name__ == "__main__":
    main()
