# 글로벌 모닝 브리핑 개선 패치 가이드

아래 수정은 기존 구조를 유지하면서 다음 기능을 추가합니다.

1. FACT / INTERPRETATION 분리
2. 오늘 시장 핵심축(Key Drivers)
3. 오늘 관전 포인트(Watchpoints)
4. 자동뉴스 노이즈 감소
5. 더 실전적인 텔레그램 출력

---

# 1) build_us_market_tone 반환 구조 변경

## 기존

```python
return f"미국장 분위기: {tone}", evidence
```

## 수정

```python
return {
    "title": "미국장 분위기",
    "tone": tone,
    "evidence": evidence,
}
```

---

# 2) 다른 tone 함수들도 동일하게 변경

아래 함수들도 동일한 방식으로 수정:

- build_world_market_tone
- build_bond_fx_tone
- build_commodity_tone
- build_crypto_tone
- build_most_active_tone

예시:

```python
return {
    "title": "채권/환율 분위기",
    "tone": tone,
    "evidence": evidence,
}
```

---

# 3) 새로운 함수 추가 — 오늘 시장 핵심축

아래 함수를 추가.

```python
def build_key_drivers(macro, indices, news_items):
    drivers = []

    sox = get_pct_from_groups("필라델피아 반도체", indices)
    y10 = get_pct_from_groups("미국 10년물", macro)
    dxy = get_pct_from_groups("달러 인덱스", macro)
    oil = get_pct_from_groups("WTI", macro)
    vix = get_pct_from_groups("VIX", macro)

    # AI 반도체
    if sox is not None:
        if sox >= 1.5:
            drivers.append("AI 반도체 강세 지속 가능성")
        elif sox <= -1.5:
            drivers.append("반도체 차익실현 압력 확대 가능성")

    # 장기금리
    if y10 is not None:
        if y10 >= 1.0:
            drivers.append("장기금리 상승 부담 지속")
        elif y10 <= -1.0:
            drivers.append("금리 완화 기대 확대")

    # 달러
    if dxy is not None:
        if dxy >= 0.5:
            drivers.append("달러 강세로 위험자산 부담 가능성")
        elif dxy <= -0.5:
            drivers.append("달러 약세 기반 위험선호 유지 가능성")

    # 유가
    if oil is not None and abs(oil) >= 2:
        drivers.append("유가 변동성 확대 여부 주시")

    # 변동성
    if vix is not None and vix >= 5:
        drivers.append("변동성 확대 가능성")

    # 중복 제거
    drivers = list(dict.fromkeys(drivers))

    return drivers[:4]
```

---

# 4) 새로운 함수 추가 — 오늘 관전 포인트

```python
def build_watchpoints(macro, indices):
    points = []

    sox = get_pct_from_groups("필라델피아 반도체", indices)
    y10 = get_pct_from_groups("미국 10년물", macro)
    dxy = get_pct_from_groups("달러 인덱스", macro)
    vix = get_pct_from_groups("VIX", macro)

    if sox is not None:
        if sox >= 1:
            points.append(
                "삼성전자·SK하이닉스 시초 강세 이후 외국인 수급 유지 여부"
            )
        elif sox <= -1:
            points.append(
                "반도체 갭하락 이후 낙폭 축소 여부"
            )

    if y10 is not None:
        if y10 >= 1:
            points.append(
                "장기금리 상승으로 성장주 차익실현 여부"
            )
        elif y10 <= -1:
            points.append(
                "금리 하락 기반 기술주 반등 지속 여부"
            )

    if dxy is not None:
        if dxy >= 0.5:
            points.append(
                "원달러 환율 상승 여부 체크"
            )
        elif dxy <= -0.5:
            points.append(
                "외국인 수급 개선 여부 확인"
            )

    if vix is not None and vix >= 5:
        points.append(
            "장초반 변동성 확대 가능성 주의"
        )

    return points[:5]
```

---

# 5) summarize_news_openai 프롬프트 강화

기존 prompt 내부 목표 아래에 추가.

## 추가 문장

```python
- 사실(Fact)과 해석(Interpretation)을 분리
- 시장 영향이 약한 정보는 제거
- ETF 비용률/언급량/SNS 잡음 제외
- 실제 가격/금리/수급/실적 중심
- 같은 사건은 하나로 통합
- "주목", "관심", "기대" 같은 표현 최소화
- 단정적 표현보다 가능성 중심 표현 사용
- 한국 투자자가 오늘 무엇을 봐야 하는지 포함
```

---

# 6) summarize_news_openai 출력 형식 변경

## 기존

```python
출력 형식 예시:
- [매크로] ...
```

## 수정

```python
출력 형식:

[FACT]
- 실제 발생한 핵심 뉴스만

[INTERPRETATION]
- 시장이 어떻게 반응할 가능성이 있는지

[WATCHPOINT]
- 한국장에서 체크할 포인트
```

---

# 7) build_message 구조 개편

기존 build_message 내부를 아래 방식으로 변경.

## 기존

```python
global_tone, global_evidence = build_world_market_tone(indices)
us_tone, us_evidence = build_us_market_tone(macro, indices)
```

## 수정

```python
global_data = build_world_market_tone(indices)
us_data = build_us_market_tone(macro, indices)
bond_data = build_bond_fx_tone(macro)
commodity_data = build_commodity_tone(macro)
```

---

# 8) build_message 출력 형식 개선

## 추천 구조

```python
msg.append(f"<b>해외 모닝 브리핑</b> {html_escape(today)}")
msg.append("")

msg.append("<b>오늘 시장 핵심축</b>")
for item in key_drivers:
    msg.append(f"- {html_escape(item)}")

msg.append("")
msg.append("<b>미국장 FACT</b>")
for line in us_data["evidence"]:
    msg.append(f"- {html_escape(line)}")

msg.append("")
msg.append("<b>해석</b>")
msg.append(f"- {html_escape(us_data['tone'])}")
```

---

# 9) build_message에 관전포인트 추가

```python
msg.append("")
msg.append("<b>오늘 관전 포인트</b>")

for item in watchpoints:
    msg.append(f"- {html_escape(item)}")
```

---

# 10) main() 수정

## 기존

```python
checkpoints = build_korea_facts(macro, indices)

message = build_message(
    macro,
    indices,
    top15,
    crypto,
    news_summary,
    checkpoints,
)
```

## 수정

```python
checkpoints = build_korea_facts(macro, indices)

key_drivers = build_key_drivers(
    macro,
    indices,
    news_items,
)

watchpoints = build_watchpoints(
    macro,
    indices,
)

message = build_message(
    macro,
    indices,
    top15,
    crypto,
    news_summary,
    checkpoints,
    key_drivers,
    watchpoints,
)
```

---

# 11) build_message 시그니처 수정

## 기존

```python
def build_message(macro, indices, top15, crypto, news_summary, checkpoints):
```

## 수정

```python
def build_message(
    macro,
    indices,
    top15,
    crypto,
    news_summary,
    checkpoints,
    key_drivers,
    watchpoints,
):
```

---

# 최종적으로 기대되는 변화

기존:

- 데이터 나열형
- 뉴스 반복
- 실제 매매 연결성 부족
- 해석과 사실 혼재

개선 후:

- FACT / 해석 분리
- 핵심 테마 중심 압축
- 오늘 시장이 거래할 재료 강조
- 실제 트레이딩 관전포인트 제공
- 자동뉴스 특유의 잡음 감소
- 훨씬 읽기 쉬운 텔레그램 브리핑

