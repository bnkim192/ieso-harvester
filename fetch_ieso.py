# -*- coding: utf-8 -*-
"""
IESO(캐나다 온타리오) 수집기 v3 — 개편 후 존가격 + GA 정밀화

■ v2 실행(2026-08-07)에서 확정된 사실
  1) HOEP는 **2025-04에서 종료**. `PUB_PriceHOEPPredispOR.csv`(무날짜) 헤더가
     "Created at 2025-05-01 / For 2025" → 2025-05-01 MRP 개편으로 발행 중단 확인.
     `..._2026.csv`는 404. 따라서 HOEP로는 개편 후 구간을 절대 못 채운다.
  2) **개편 후 정답 소스 = `DAHourlyOntarioZonalPrice/PUB_DAHourlyOntarioZonalPrice_YYYYMMDD.xml`**
     (일별 XML, 목록에 183개 = 약 6개월 롤링, 2026-08-07까지 최신)
  3) GA = `GlobalAdjustment/PUB_GlobalAdjustment_YYYYMM.xml` (월별, 2026-08까지)
     그런데 v2의 범용 숫자 추출이 파일마다 다른 태그를 집어 값이 뒤죽박죽
     (0.06 / 72.7 / 99.1 / 25.95) → **태그 구조를 probe에 덤프**해 파서를 확정한다.
  4) RealtimeEnergyLMP(12,842개)·RealtimeOntarioZonalPrice(12,823개)는 시간별 단위라
     월평균 만들기에 부적합 → 일별 DA 파일을 쓴다.

■ v3가 하는 일
  A) 날짜 사다리 탐색 — 일별 존가격이 **어디까지 과거로 남아있는지** 실제로 찔러본다.
  B) 존가격 일별 XML을 월평균으로 집계(스키마 미상 → 태그 빈도로 가격 필드 자동 판별 + 근거를 probe에 덤프).
  C) GA XML 태그 구조를 probe에 덤프하고, 후보 태그별 값을 함께 저장(다음 버전에서 확정).
  D) HOEP(≤2025-04)와 존가격(개편 후)을 **합치지 않고 별도 계열로** 저장.
     둘은 가격 정의가 달라 이어붙이면 레짐 단절이 생겨 ETS를 오염시킨다.

■ 사용
  python fetch_ieso.py --discover   # 탐색만
  python fetch_ieso.py              # 탐색 + 수집·저장
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


def get(url):
    try:
        r = requests.get(url, timeout=TIMEOUT)
        return r.status_code, r.text
    except Exception as e:
        return 0, f"EXC {e}"


def ym_of(s):
    s = str(s).strip()
    for rx, f in ((r"(\d{4})-(\d{2})", lambda m: f"{m.group(1)}-{m.group(2)}"),
                  (r"(\d{1,2})/(\d{1,2})/(\d{4})", lambda m: f"{m.group(3)}-{int(m.group(1)):02d}"),
                  (r"(\d{4})(\d{2})(\d{2})", lambda m: f"{m.group(1)}-{m.group(2)}")):
        m = re.match(rx, s)
        if m:
            return f(m)
    return None


# ── A) 날짜 사다리 — 일별 존가격 보존 범위 확인 ────────────────────────────────
def zonal_horizon(probe):
    """월초 1일을 과거로 훑어 어디까지 파일이 남아있는지 찾는다."""
    now = dt.datetime.utcnow()
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


# ── B) 존가격 일별 XML → 월평균 ───────────────────────────────────────────────
def parse_zonal_day(text):
    """스키마 미상 → 시간당 1개(=하루 24개 전후)로 나타나고 값 범위가 가격다운 태그를 고른다."""
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
    best, bestscore = None, -1
    for t, vs in numeric.items():
        n = len(vs)
        if n < 20 or n > 60:                       # 하루 24시간(±) 만 후보
            continue
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
    vs = numeric[best]
    return {"tag": best, "n": len(vs), "mean": sum(vs) / len(vs)}


def collect_zonal(oldest_month, probe):
    if not oldest_month:
        print("[존가격] 보존 시작월 미확인 — 수집 생략", flush=True)
        return [], {}
    now = dt.datetime.utcnow()
    y, m = int(oldest_month[:4]), int(oldest_month[5:7])
    acc, used_tag, dumped, tried, got = {}, {}, False, 0, 0
    while (y, m) <= (now.year, now.month):
        days = 31
        for d in range(1, days + 1):
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
                dumped = True
            r = parse_zonal_day(body)
            if not r:
                continue
            key = f"{y}-{m:02d}"
            a = acc.setdefault(key, [0.0, 0])
            a[0] += r["mean"]; a[1] += 1
            used_tag[r["tag"]] = used_tag.get(r["tag"], 0) + 1
            got += 1
        print(f"[존가격 {y}-{m:02d}] 누적 {acc.get(f'{y}-{m:02d}',[0,0])[1]}일", flush=True)
        m += 1
        if m > 12:
            m = 1; y += 1
    series = [{"ym": k, "cad_mwh": round(s / c, 3), "days": c} for k, (s, c) in sorted(acc.items()) if c]
    print(f"[존가격] {tried}일 시도 · {got}일 성공 · {len(series)}개월 · 채택태그 {used_tag}", flush=True)
    probe["zonal_used_tags"] = used_tag
    return series, used_tag


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
    now = dt.datetime.utcnow()
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


# ── C) GA ────────────────────────────────────────────────────────────────────
def collect_ga(probe):
    now = dt.datetime.utcnow()
    series, dumped = [], False
    y, m = 2024, 1
    while (y, m) <= (now.year, now.month):
        ym = f"{y}{m:02d}"
        code, body = get(f"{HOST}/GlobalAdjustment/PUB_GlobalAdjustment_{ym}.xml")
        if code == 200 and len(body) > 200:
            if not dumped:
                dump_xml_struct(body, f"GA {ym}", probe, "ga_struct")
                dumped = True
            # 태그별 값을 전부 담아두고 확정은 다음 버전에서(잘못된 단일값 추정 금지)
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
                               "by_tag": {t: (round(sum(v) / len(v), 4), len(v)) for t, v in vals.items()}})
            except Exception as e:
                print(f"[GA {ym}] 파싱 실패 {e}", flush=True)
        m += 1
        if m > 12:
            m = 1; y += 1
    print(f"[GA] {len(series)}개월 · 태그별 값 저장(확정은 구조 확인 후)", flush=True)
    return series


def main():
    probe = {"checked_at": dt.datetime.utcnow().isoformat()[:16]}
    print("### A) 일별 존가격 보존 범위 사다리 탐색", flush=True)
    oldest = zonal_horizon(probe)

    if "--discover" in sys.argv:
        json.dump(probe, open(OUT_PROBE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"[DISCOVER] {OUT_PROBE} 저장", flush=True)
        return

    print("### B) HOEP(구 체계, ≤2025-04)", flush=True)
    hpts, hused = collect_hoep()
    acc = {}
    for k, v in hpts:
        a = acc.setdefault(k, [0.0, 0]); a[0] += v; a[1] += 1
    hoep = [{"ym": k, "cad_mwh": round(s / c, 3), "n": c} for k, (s, c) in sorted(acc.items()) if c]

    print("### C) 개편 후 존가격(일별→월평균)", flush=True)
    zonal, ztags = collect_zonal(oldest, probe)

    print("### D) GA", flush=True)
    ga = collect_ga(probe)

    last = (zonal[-1]["ym"] if zonal else (hoep[-1]["ym"] if hoep else None))
    now = dt.datetime.utcnow()
    lag = None
    if last:
        lag = (now.year - int(last[:4])) * 12 + (now.month - int(last[5:7]))

    out = {
        "source": "IESO public reports (Ontario)",
        "zone": "OT", "unit": "CAD/MWh",
        "regimes": {
            "hoep": {"desc": "구 체계 HOEP(단일 온타리오 가격). 2025-05 MRP 개편으로 발행 중단",
                     "months": len(hoep), "range": [hoep[0]["ym"], hoep[-1]["ym"]] if hoep else None},
            "zonal": {"desc": "개편 후 Day-Ahead Hourly Ontario Zonal Price(일별 XML 월평균)",
                      "months": len(zonal), "range": [zonal[0]["ym"], zonal[-1]["ym"]] if zonal else None,
                      "used_tags": ztags},
        },
        "note": "⚠️ HOEP와 존가격은 가격 정의가 달라 하나로 이어붙이지 않았다(레짐 단절이 ETS를 오염시킴). "
                "파일럿이 두 계열을 각각 받아 판단한다. all-in = 에너지 + GA + 송전(월 최대수요 kW) + 규제요금.",
        "latest": last, "lag_months": lag,
        "gap_warning": ("HOEP 종료(2025-04)와 존가격 보존 시작(" + str(oldest) + ") 사이가 비어 있을 수 있음"
                        if oldest and oldest > "2025-05" else ""),
        "used_urls": hused,
        "series": hoep,          # 하위호환(기존 파일럿이 읽는 필드) = HOEP
        "series_zonal": zonal,   # 개편 후 계열
    }
    json.dump(out, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f); w.writerow(["regime", "ym", "cad_mwh", "n"])
        for s in hoep:
            w.writerow(["hoep", s["ym"], s["cad_mwh"], s["n"]])
        for s in zonal:
            w.writerow(["zonal", s["ym"], s["cad_mwh"], s["days"]])
    json.dump({"source": "IESO GlobalAdjustment monthly XML",
               "note": "태그별 평균값을 그대로 저장. 어느 태그가 GA 단가인지 ot_probe.json의 ga_struct 로 확정 후 다음 버전에서 확정 파싱.",
               "months": len(ga), "series": ga},
              open(OUT_GA, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(probe, open(OUT_PROBE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[OK] HOEP {len(hoep)}개월 · 존가격 {len(zonal)}개월 · GA {len(ga)}개월 · 최신 {last}(지연 {lag}개월)", flush=True)


if __name__ == "__main__":
    main()
