#!/usr/bin/env python3
"""
score_candidates.py - гибридный скоринг кандидатов (КОД + LLM).

Урок 5, второй DEMO-трек (n8n vs Claude Code). Архитектура:
  - КОД (этот файл): парсит профиль, считает годы опыта, применяет must-have
    как hard-фильтр, считает взвешенную сумму, ранжирует. Числа и поток - код.
  - LLM: по каждому критерию даёт суждение (score 0-10 + цитата-доказ). Только
    понимание смысла живого текста. НЕ считает арифметику.

Инженерное правило урока: "числам - код, суждению - модель".

Вход:
  --job        путь к .md/.txt с вакансией (или текст в JOB env)
  --profiles   путь к папке с *.json профилями (формат apify-get-profile)
               ИЛИ к .json-массиву профилей
  --threshold  минимальный fit_score для "пригласить" (default 70)
  --out        куда писать shortlist.md (default ./shortlist.md)

LLM-бэкенд (автоопределение):
  1. если есть `claude` CLI в PATH → зовёт его (claude -p ... --output-format json)
  2. иначе если есть OPENAI_API_KEY → OpenAI chat completions (gpt-4o-mini)
  3. иначе → DRY-режим: считает только код-часть, ставит заглушку по критериям
     (для теста потока без ключа). Помечает вывод как [LLM OFF].

Запуск:
  python3 score_candidates.py --job job.md --profiles _raw/ --threshold 70
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# 1. ПАРС ПРОФИЛЯ (формат apify-get-profile: всё под .basic_info)
# ─────────────────────────────────────────────────────────────────────────────

MONTHS = {  # для парса дат вида "Jan 2022", "2022-01", "2022"
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _year_from(s):
    """Вытащить год (int) из произвольной строки даты. None если не нашёл."""
    if not s:
        return None
    m = re.search(r"(19|20)\d{2}", str(s))
    return int(m.group(0)) if m else None


def _month_from(s):
    if not s:
        return 1
    s = str(s).lower()
    for name, num in MONTHS.items():
        if name in s:
            return num
    m = re.search(r"\b(0?[1-9]|1[0-2])\b", s)
    return int(m.group(0)) if m else 1


def parse_profile(raw):
    """apify-get-profile JSON → нормализованный dict. Безопасно к дрейфу схемы."""
    b = raw.get("basic_info", raw)  # старые payload без вложения
    exp = raw.get("experience", []) or b.get("experience", []) or []
    edu = raw.get("education", []) or b.get("education", []) or []

    # собрать плоский текст профиля (для цитат-доказов и LLM-контекста)
    parts = [b.get("headline", ""), b.get("about", "")]
    for e in exp:
        title = e.get("title") or e.get("position") or ""
        company = e.get("company") or e.get("company_name") or ""
        desc = e.get("description") or ""
        period = f"{e.get('start_date','')} - {e.get('end_date','')}".strip(" -")
        parts.append(f"{title} @ {company} ({period})\n{desc}")
    skills = b.get("top_skills") or raw.get("skills") or []
    if skills:
        parts.append("Навички: " + ", ".join(str(s) for s in skills))
    profile_text = "\n\n".join(p for p in parts if p).strip()

    name = " ".join(x for x in [b.get("first_name", ""), b.get("last_name", "")] if x).strip()
    if not name:
        name = b.get("fullname") or b.get("public_identifier") or "Кандидат"

    return {
        "name": name,
        "slug": b.get("public_identifier", ""),
        "headline": b.get("headline", ""),
        "profile_text": profile_text,
        "experience": exp,
        "education": edu,
        "skills": [str(s) for s in skills],
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2. АРИФМЕТИКА - КОД, НЕ LLM (годы опыта)
# ─────────────────────────────────────────────────────────────────────────────

def total_years_experience(experience):
    """Сумма лет опыта по периодам работы. Считает КОД, не модель.
    Перекрывающиеся периоды объединяет, чтобы не задвоить."""
    intervals = []
    this_year = date.today().year
    for e in experience:
        sy = _year_from(e.get("start_date") or e.get("starts_at") or e.get("start"))
        if not sy:
            continue
        ey_raw = e.get("end_date") or e.get("ends_at") or e.get("end") or ""
        ey = _year_from(ey_raw)
        if ey is None:  # "Present" / "зараз" / пусто → по сейчас
            ey = this_year
        sm = _month_from(e.get("start_date") or e.get("start") or "")
        em = _month_from(ey_raw) if _year_from(ey_raw) else 12
        start = sy + (sm - 1) / 12.0
        end = ey + (em - 1) / 12.0
        if end >= start:
            intervals.append((start, end))
    if not intervals:
        return 0.0
    # merge overlapping
    intervals.sort()
    merged = [list(intervals[0])]
    for s, en in intervals[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], en)
        else:
            merged.append([s, en])
    return round(sum(en - s for s, en in merged), 1)


# ─────────────────────────────────────────────────────────────────────────────
# 3. ВАКАНСИЯ → КРИТЕРИИ С ВЕСАМИ (через LLM один раз)
# ─────────────────────────────────────────────────────────────────────────────

RUBRIC_PROMPT = """Ты технический рекрутер. Из этого описания вакансии выведи 4-6 критериев найма с весами.

Вакансия:
{job}

Верни СТРОГО JSON-массив, без лишнего текста:
[
  {{"criterion": "короткое название", "weight": 0.30, "must_have": true,
    "min_years": 5, "keywords": ["python","go"]}}
]
Правила:
- weight: число 0-1, сумма всех весов = 1.0. Must-have - наибольший вес.
- must_have: true если требование обязательное (отсев), иначе false.
- min_years: если критерий про годы опыта - число лет, иначе 0.
- keywords: 2-5 слов-маркеров этого критерия (для проверки в коде).
Только JSON-массив."""

CRITERION_PROMPT = """Ты технический рекрутер. Оцени кандидата ТОЛЬКО по ОДНОМУ критерию.

Критерий: {criterion}
(must-have: {must_have})

Профиль кандидата:
{profile}

Верни СТРОГО JSON, без лишнего текста:
{{"score": 7, "evidence_quote": "дословная цитата из профиля", "reasoning": "1 строка"}}
Правила:
- score: 0-10 на основе ТОЛЬКО текста профиля.
- evidence_quote: дословный фрагмент профиля, подтверждающий балл.
  Если доказательств нет → score 0 и evidence_quote "ДОКАЗІВ НЕ ЗНАЙДЕНО".
- НИКОГДА не выдумывай опыт, которого нет в тексте.
- Читай смысл: "розробляв на Пайтоні" = Python-опыт.
Только JSON."""


# ─────────────────────────────────────────────────────────────────────────────
# 4. LLM-БЭКЕНД (claude CLI → OpenAI → DRY)
# ─────────────────────────────────────────────────────────────────────────────

def _llm_backend():
    if shutil.which("claude"):
        return "claude"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return "dry"


def _extract_json(text):
    """Достать первый JSON-объект/массив из ответа модели (срезает ```-огорожи)."""
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"(\{.*\}|\[.*\])", text, re.S)
    return m.group(0) if m else text


def llm(prompt, backend):
    if backend == "claude":
        r = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json"],
            capture_output=True, text=True, timeout=120,
        )
        out = r.stdout.strip()
        try:  # claude --output-format json оборачивает в {"result": "..."}
            wrapped = json.loads(out)
            return wrapped.get("result", out)
        except Exception:
            return out
    if backend == "openai":
        import urllib.request
        body = json.dumps({
            "model": os.environ.get("SCORER_MODEL", "gpt-4o-mini"),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }).encode()
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions", data=body,
            headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"]
    return None  # dry


def llm_json(prompt, backend, fallback):
    if backend == "dry":
        return fallback
    try:
        raw = llm(prompt, backend)
        return json.loads(_extract_json(raw))
    except Exception as ex:
        print(f"  [llm parse fail: {ex}] → fallback", file=sys.stderr)
        return fallback


# ─────────────────────────────────────────────────────────────────────────────
# 5. СКОРИНГ ОДНОГО КАНДИДАТА (гибрид)
# ─────────────────────────────────────────────────────────────────────────────

def score_candidate(profile, rubric, backend):
    years = total_years_experience(profile["experience"])  # КОД считает
    breakdown = []
    weighted = 0.0
    knockout = False

    for crit in rubric:
        # LLM-суждение по критерию
        dry_fb = {"score": 5, "evidence_quote": "[LLM OFF - код-заглушка]",
                  "reasoning": "LLM выключен, балл-заглушка"}
        judged = llm_json(
            CRITERION_PROMPT.format(
                criterion=crit["criterion"], must_have=crit.get("must_have", False),
                profile=profile["profile_text"][:6000]),
            backend, dry_fb)
        score = int(judged.get("score", 0))

        # КОД: критерий про годы опыта - проверяем арифметикой, не верим LLM
        min_y = crit.get("min_years", 0) or 0
        if min_y and years:
            score = 10 if years >= min_y else max(0, round(10 * years / min_y))
            judged["evidence_quote"] = f"код посчитал {years} р. опыта (нужно {min_y})"
            judged["reasoning"] = f"{years} р. vs требуемых {min_y}"

        # КОД: must-have как hard-фильтр (knockout)
        if crit.get("must_have") and score < 4:
            knockout = True

        w = float(crit.get("weight", 0))
        weighted += w * score
        breakdown.append({
            "criterion": crit["criterion"], "weight": w, "score": score,
            "evidence_quote": judged.get("evidence_quote", ""),
            "reasoning": judged.get("reasoning", ""),
            "must_have": crit.get("must_have", False),
        })

    fit = round(weighted * 10)  # взвешенная сумма (0-100) - КОД
    if knockout:
        fit = min(fit, 45)  # отсёк по must-have жёстко в коде
    return {
        "name": profile["name"], "slug": profile["slug"],
        "headline": profile["headline"], "years": years,
        "fit_score": fit, "knockout": knockout, "breakdown": breakdown,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 6. ЗАГРУЗКА ПРОФИЛЕЙ
# ─────────────────────────────────────────────────────────────────────────────

def load_profiles(path):
    p = Path(path)
    raws = []
    if p.is_dir():
        for f in sorted(p.glob("*.json")):
            raws.append(json.loads(f.read_text()))
    else:
        data = json.loads(p.read_text())
        raws = data if isinstance(data, list) else [data]
    return [parse_profile(r) for r in raws]


# ─────────────────────────────────────────────────────────────────────────────
# 7. ОТЧЁТ
# ─────────────────────────────────────────────────────────────────────────────

def render(results, threshold, backend):
    results.sort(key=lambda r: r["fit_score"], reverse=True)
    invite = [r for r in results if r["fit_score"] >= threshold and not r["knockout"]]
    maybe = [r for r in results if r not in invite]

    L = []
    tag = " [LLM OFF - только код-часть]" if backend == "dry" else f" (LLM: {backend})"
    L.append(f"# Shortlist кандидатов{tag}\n")
    L.append(f"Порог приглашения: {threshold}/100 · всего: {len(results)} · "
             f"пригласить: {len(invite)}\n")
    L.append("## Ранжированный список\n")
    L.append("| # | Кандидат | fit | Вердикт | Опыт |")
    L.append("|---|---|---|---|---|")
    for i, r in enumerate(results, 1):
        verdict = "ПРИГЛАСИТЬ" if r in invite else ("ОТСЁВ (must-have)" if r["knockout"] else "МОЖЛИВО")
        L.append(f"| {i} | {r['name']} | {r['fit_score']} | {verdict} | {r['years']} р. |")

    for r in invite:
        L.append(f"\n## ✅ {r['name']} - {r['fit_score']}/100")
        L.append(f"*{r['headline']}*\n")
        L.append("| Критерий | Вес | Балл | Доказ |")
        L.append("|---|---|---|---|")
        for c in r["breakdown"]:
            star = " ⭐must" if c["must_have"] else ""
            L.append(f"| {c['criterion']}{star} | {c['weight']:.2f} | {c['score']}/10 | {c['evidence_quote'][:80]} |")

    if maybe:
        L.append("\n## 🔶 МОЖЛИВО / отсев (смотрит человек)")
        for r in maybe:
            why = "must-have не закрыт" if r["knockout"] else "ниже порога"
            L.append(f"- {r['name']} - {r['fit_score']}/100 ({why})")

    L.append("\n---")
    L.append("> **AI оцінив, рішення за вами.** Не для авто-відмови без людини "
             "(EU AI Act, GDPR ст.22). «МОЖЛИВО» обовʼязково дивиться людина.")
    return "\n".join(L)


# ─────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--profiles", required=True)
    ap.add_argument("--threshold", type=int, default=70)
    ap.add_argument("--out", default="shortlist.md")
    args = ap.parse_args()

    backend = _llm_backend()
    job_text = Path(args.job).read_text() if Path(args.job).exists() else args.job

    print(f"LLM backend: {backend}", file=sys.stderr)
    rubric = llm_json(RUBRIC_PROMPT.format(job=job_text), backend, fallback=[
        {"criterion": "Релевантный опыт", "weight": 0.5, "must_have": True, "min_years": 3, "keywords": []},
        {"criterion": "Профильные навыки", "weight": 0.5, "must_have": False, "min_years": 0, "keywords": []},
    ])
    print(f"Критериев из вакансии: {len(rubric)}", file=sys.stderr)

    profiles = load_profiles(args.profiles)
    print(f"Профилей загружено: {len(profiles)}", file=sys.stderr)

    results = []
    for p in profiles:
        print(f"  скоринг: {p['name']}...", file=sys.stderr)
        results.append(score_candidate(p, rubric, backend))

    report = render(results, args.threshold, backend)
    Path(args.out).write_text(report)
    print(f"\n→ {args.out}\n", file=sys.stderr)
    print(report)


if __name__ == "__main__":
    main()
