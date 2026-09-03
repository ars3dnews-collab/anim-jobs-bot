# -*- coding: utf-8 -*-
"""
Бот вакансий 3D-анимации.

Читает публичную таблицу "Animation/VFX/Game Industry Job Postings",
выбирает из неё вакансии по анимации, переводит на русский и публикует
в телеграм-канал. Каждая вакансия выходит ровно один раз: ключ считается
по студии, должности и ссылке и хранится в posted.json прямо в репозитории.

Устройство ровно такое же, как у бота ригов, и по тем же причинам:
  * бот сам держит паузу между постами по времени прошлой публикации,
    а не полагается на расписание GitHub, которое срабатывает как придётся;
  * спит короткими кусками, поэтому его всегда можно отменить;
  * память пишется в репозиторий сразу после каждого поста, поэтому
    отмена запуска не приводит к дублям.
"""

import os
import re
import io
import csv
import json
import time
import html
import hashlib
import datetime as dt

import requests

import config

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "posted.json")

TG_API = "https://api.telegram.org/bot{token}/{method}"
SHEET_CSV = ("https://docs.google.com/spreadsheets/d/{sid}/gviz/tq"
             "?tqx=out:csv&gid={gid}")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def log(msg):
    print("[{}] {}".format(dt.datetime.now().strftime("%H:%M:%S"), msg),
          flush=True)


# ------------------------------------------------------------------ память

def load_state():
    base = {"posted": [], "glossary": {}, "last_post": 0, "seeded": False}
    if not os.path.exists(STATE_FILE):
        return base
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in base.items():
            data.setdefault(k, v)
        return data
    except Exception as e:
        log("posted.json не читается ({}), начинаю с чистого списка".format(e))
        return base


def save_state(state):
    state["posted"] = state["posted"][-4000:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    push_state()


def push_state():
    """Сохранить память в репозиторий сразу, не дожидаясь конца запуска."""
    if os.environ.get("GIT_AUTOSAVE", "").lower() not in ("1", "true", "yes"):
        return
    import subprocess
    try:
        subprocess.run(["git", "add", "posted.json"], cwd=HERE,
                       check=False, capture_output=True)
        changed = subprocess.run(["git", "diff", "--staged", "--quiet"],
                                 cwd=HERE, capture_output=True).returncode != 0
        if not changed:
            return
        subprocess.run(["git", "commit", "-m", "update posted jobs"],
                       cwd=HERE, check=False, capture_output=True)
        subprocess.run(["git", "pull", "--rebase", "--autostash"],
                       cwd=HERE, check=False, capture_output=True)
        r = subprocess.run(["git", "push"], cwd=HERE, capture_output=True)
        if r.returncode != 0:
            log("  ! память не запушилась: {}".format(
                r.stderr.decode("utf-8", "replace")[:150]))
    except Exception as e:
        log("  ! память не сохранилась: {}".format(e))


# ------------------------------------------------------------------ время

def local_now():
    off = float(getattr(config, "UTC_OFFSET_HOURS", 5))
    return dt.datetime.utcfromtimestamp(time.time() + off * 3600)


def quiet_now():
    start, end = getattr(config, "ACTIVE_HOURS", (8, 23))
    return not (start <= local_now().hour < end)


# ------------------------------------------------------------------ таблица

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}

RU_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
             "августа", "сентября", "октября", "ноября", "декабря")


def parse_date(text):
    """"August 28, 2026" -> date. Пусто или мусор -> None."""
    m = re.match(r"\s*([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", text or "")
    if not m:
        return None
    month = MONTHS.get(m.group(1).lower())
    if not month:
        return None
    try:
        return dt.date(int(m.group(3)), month, int(m.group(2)))
    except ValueError:
        return None


def job_id(row):
    key = "|".join([
        (row.get("studio") or "").strip().lower(),
        (row.get("title") or "").strip().lower(),
        (row.get("link") or "").split("?")[0].rstrip("/").lower(),
    ])
    return hashlib.sha1(key.encode("utf-8", "ignore")).hexdigest()[:16]


def wanted(title):
    low = (title or "").lower()
    if not low:
        return False
    if any(s in low for s in config.SKIP):
        return False
    return any(w in low for w in config.WANT)


def fetch_sheet():
    """Скачать лист и вернуть отобранные вакансии, свежие сначала."""
    url = SHEET_CSV.format(sid=config.SHEET_ID, gid=config.SHEET_GID)
    r = requests.get(url, headers={"User-Agent": UA}, timeout=90)
    r.raise_for_status()
    r.encoding = "utf-8"

    rows = list(csv.reader(io.StringIO(r.text)))
    log("  в таблице строк: {}".format(len(rows)))

    col = config.COLUMNS
    today = dt.date.today()
    out = []
    for raw in rows[1:]:
        def cell(name):
            i = col[name]
            return (raw[i].strip() if i < len(raw) else "")

        studio, title, link = cell("studio"), cell("title"), cell("link")
        if not studio or not title:
            continue
        if not wanted(title):
            continue

        when = parse_date(cell("date"))
        age = (today - when).days if when else 999
        if age > int(getattr(config, "MAX_AGE_DAYS", 45)):
            continue

        out.append({
            "studio": studio,
            "title": title,
            "city": cell("city"),
            "state": cell("state"),
            "country": cell("country"),
            "level": cell("level"),
            "mode": cell("mode"),
            "software": cell("software"),
            "notes": cell("notes"),
            "link": link,
            "date": when,
            "age": age,
        })

    out.sort(key=lambda j: j["age"])
    log("  подходящих вакансий не старше {} дней: {}".format(
        config.MAX_AGE_DAYS, len(out)))
    return out


# ------------------------------------------------------------------ словарь
# Значения в колонках уровня и формата работы повторяются из строки в
# строку, поэтому переводим их таблицей, а не моделью: так формулировки в
# канале всегда одинаковые и не зависят от настроения ИИ.

LEVELS = {
    "trainee": "стажёр",
    "internship": "стажировка",
    "junior": "junior",
    "mid": "middle",
    "senior": "senior",
    "lead": "lead",
    "director": "директор",
    "manager / supervisor": "супервайзер",
    "head": "head",
}

MODES = {
    "remote": "удалённо",
    "hybrid": "гибрид",
    "on-site": "в офисе",
    "on-sitel": "в офисе",
    "on-site or remote": "офис или удалённо",
    "all options": "офис, гибрид или удалённо",
}

MODE_TAGS = {
    "удалённо": "#удалёнка",
    "гибрид": "#гибрид",
    "в офисе": "#офис",
    "офис или удалённо": "#удалёнка",
    "офис, гибрид или удалённо": "#удалёнка",
}


def ru_level(value):
    parts = [p.strip().lower() for p in (value or "").split(",") if p.strip()]
    got = [LEVELS.get(p, p) for p in parts]
    return ", ".join(got)


def ru_mode(value):
    return MODES.get((value or "").strip().lower(), (value or "").strip())


def ru_date(when):
    if not when:
        return ""
    return "{} {} {}".format(when.day, RU_MONTHS[when.month - 1], when.year)


# ------------------------------------------------------------------ Gemini

GEMINI_URL = ("https://generativelanguage.googleapis.com/{ver}/models/"
              "{model}:generateContent")
_MODELS = []


def _safe(msg):
    text = str(msg)
    if GEMINI_KEY:
        text = text.replace(GEMINI_KEY, "***")
    return re.sub(r"key=[^&\s]+", "key=***", text)


def available_models():
    if _MODELS:
        return _MODELS
    for ver in ("v1beta", "v1"):
        try:
            r = requests.get(
                "https://generativelanguage.googleapis.com/{}/models".format(ver),
                params={"key": GEMINI_KEY, "pageSize": 200}, timeout=40)
            if not r.ok:
                continue
            names = [(ver, m["name"].split("/")[-1])
                     for m in r.json().get("models", [])
                     if "generateContent" in (m.get("supportedGenerationMethods") or [])]
            if names:
                _MODELS.extend(names)
                log("  доступно моделей: {}".format(len(names)))
                return _MODELS
        except Exception as e:
            log("  ! список моделей: {}".format(_safe(e)[:120]))
    return []


def model_queue():
    found = available_models()
    if not found:
        return ([("v1beta", config.GEMINI_MODEL)] +
                [("v1beta", m) for m in config.GEMINI_FALLBACK_MODELS])
    bad = ("tts", "image", "embedding", "live", "audio", "native", "vision",
           "thinking", "learnlm", "aqa")
    usable = [(v, n) for v, n in found if not any(b in n for b in bad)] or found
    prefs = [config.GEMINI_MODEL] + list(config.GEMINI_FALLBACK_MODELS)

    def rank(item):
        _, name = item
        alias = 1 if ("latest" in name or name.endswith("-preview")) else 0
        if name in prefs:
            return (0, alias, prefs.index(name))
        if "flash-lite" in name:
            return (1, alias, len(name))
        if "flash" in name:
            return (2, alias, len(name))
        if "gemma" in name:
            return (3, alias, len(name))
        if "pro" in name:
            return (4, alias, len(name))
        return (5, alias, len(name))

    return sorted(usable, key=rank)[:6]


def ask_ai(prompt, timeout=60):
    """Спросить модель, перебирая доступные, пока какая-нибудь не ответит."""
    if not GEMINI_KEY:
        return ""
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1200},
    }
    for ver, model in model_queue():
        try:
            r = requests.post(GEMINI_URL.format(ver=ver, model=model),
                              params={"key": GEMINI_KEY}, json=body,
                              timeout=timeout)
            if not r.ok:
                continue
            cand = (r.json().get("candidates") or [{}])[0]
            parts = (cand.get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts).strip()
            if text:
                return text
        except Exception as e:
            log("    ! {}: {}".format(model, _safe(e)[:100]))
    return ""


TRANSLATE_PROMPT = """Ты редактор русскоязычного телеграм-канала с вакансиями \
для 3D-аниматоров. Переведи данные вакансии на русский язык.

Ответь ТОЛЬКО JSON-объектом без markdown-обёртки, с полями:
  "title"  — должность по-русски, коротко и по-профессиональному
             ("Animator, 3D" -> "3D-аниматор", "Technical Animator" -> \
"технический аниматор", "Animation Supervisor" -> "супервайзер анимации").
             Без названия студии, без слова "вакансия".
  "place"  — место одной строкой: город, регион, страна по-русски
             ("Madison, Wisconsin, United States" -> "Мэдисон, Висконсин, США").
             Общепринятые русские названия городов и стран. Если места нет — "".
  "notes"  — перевод примечания живым русским языком, 1-3 предложения.
             Ничего не выдумывай, только то, что есть в оригинале.
             Если примечания нет или оно бессодержательное — "".

Данные:
Должность: {title}
Студия: {studio}
Место: {place}
Примечание: {notes}
"""


def translate(job, state):
    """Перевести вакансию. Место и должность запоминаем в словарь.

    Одинаковые города и должности встречаются десятки раз, и гонять ради
    них модель заново — только тратить лимиты и рисковать разнобоем в
    формулировках. Поэтому переводы копятся в posted.json.
    """
    place_en = ", ".join([p for p in (job["city"], job["state"],
                                      job["country"]) if p])
    gloss = state.setdefault("glossary", {})

    title_key = "t:" + job["title"].lower()
    place_key = "p:" + place_en.lower()

    known_title = gloss.get(title_key)
    known_place = gloss.get(place_key) if place_en else ""
    need_notes = bool((job["notes"] or "").strip())

    result = {"title": known_title or "", "place": known_place or "",
              "notes": ""}

    if known_title and (known_place or not place_en) and not need_notes:
        return result

    raw = ask_ai(TRANSLATE_PROMPT.format(
        title=job["title"], studio=job["studio"],
        place=place_en or "—", notes=(job["notes"] or "—")[:900]))

    data = {}
    if raw:
        cut = re.sub(r"^```(?:json)?|```$", "", raw.strip(),
                     flags=re.M).strip()
        m = re.search(r"\{.*\}", cut, re.S)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = {}

    title_ru = (data.get("title") or "").strip() or known_title or job["title"]
    place_ru = (data.get("place") or "").strip() or known_place or place_en
    notes_ru = (data.get("notes") or "").strip()

    # мусорные ответы модели в словарь не пускаем
    if title_ru and len(title_ru) < 80:
        gloss[title_key] = title_ru
    if place_ru and len(place_ru) < 120:
        gloss[place_key] = place_ru

    return {"title": title_ru, "place": place_ru, "notes": notes_ru}


# ------------------------------------------------------------------ пост

_EMPTY_NOTE = re.compile(
    r"^(нет|отсутству|информац\w+ нет|не указан|—|-)\b", re.I)


def country_tag(country):
    table = {
        "united states": "#сша", "canada": "#канада", "england": "#британия",
        "united kingdom": "#британия", "spain": "#испания",
        "ireland": "#ирландия", "france": "#франция", "germany": "#германия",
        "sweden": "#швеция", "poland": "#польша", "india": "#индия",
        "australia": "#австралия", "japan": "#япония", "china": "#китай",
        "new zealand": "#новаязеландия", "netherlands": "#нидерланды",
        "czech republic": "#чехия", "hungary": "#венгрия", "italy": "#италия",
        "brazil": "#бразилия", "malta": "#мальта", "cyprus": "#кипр",
        "south korea": "#корея", "vietnam": "#вьетнам",
        "philippines": "#филиппины", "malaysia": "#малайзия",
        "thailand": "#таиланд", "indonesia": "#индонезия",
        "singapore": "#сингапур", "israel": "#израиль",
        "türkiye": "#турция", "turkey": "#турция", "serbia": "#сербия",
        "colombia": "#колумбия", "peru": "#перу", "uruguay": "#уругвай",
        "luxembourg": "#люксембург", "mexico": "#мексика",
    }
    return table.get((country or "").strip().lower(), "")


def build_post(job, ru):
    e = html.escape
    lines = ["<b>{}</b> — {}".format(e(ru["title"] or job["title"]),
                                     e(job["studio"])), ""]

    if ru["place"]:
        lines.append("📍 {}".format(e(ru["place"])))

    mode = ru_mode(job["mode"])
    if mode:
        lines.append("💼 {}".format(e(mode)))

    level = ru_level(job["level"])
    if level:
        lines.append("🎚 Уровень: {}".format(e(level)))

    if job["software"]:
        lines.append("🛠 Софт: {}".format(e(job["software"])))

    when = ru_date(job["date"])
    if when:
        lines.append("📅 Опубликовано: {}".format(e(when)))

    note = (ru["notes"] or "").strip()
    if note and not _EMPTY_NOTE.match(note):
        lines += ["", e(note)]

    if job["link"]:
        link = job["link"]
        label = "Заполнить форму" if "docs.google.com/forms" in link \
            else "Откликнуться"
        lines += ["", '🔗 <a href="{}">{} →</a>'.format(e(link, quote=True),
                                                       label)]

    tags = ["#вакансия"]
    low = job["title"].lower()
    if "technical" in low:
        tags.append("#techanim")
    elif "director" in low or "supervisor" in low:
        tags.append("#lead")
    else:
        tags.append("#аниматор")
    tag = MODE_TAGS.get(mode)
    if tag:
        tags.append(tag)
    ct = country_tag(job["country"])
    if ct:
        tags.append(ct)
    lines += ["", " ".join(tags)]

    return "\n".join(lines)


# ------------------------------------------------------------------ Telegram

def tg(method, data=None):
    r = requests.post(TG_API.format(token=BOT_TOKEN, method=method),
                      data=data, timeout=60)
    try:
        payload = r.json()
    except Exception:
        raise RuntimeError("Telegram вернул не-JSON: {}".format(r.text[:200]))
    if not payload.get("ok"):
        raise RuntimeError("Telegram: {}".format(payload.get("description")))
    return payload["result"]


def publish(job, state):
    ru = translate(job, state)
    text = build_post(job, ru)
    tg("sendMessage", {
        "chat_id": CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    })
    log("  ✓ {} — {}".format(ru["title"] or job["title"], job["studio"]))


# ------------------------------------------------------------------ запуск

def seed(state, jobs):
    """Первый запуск: пометить старое виденным, оставить окно догона."""
    days = int(getattr(config, "BACKFILL_DAYS", 0))
    known = set(state["posted"])
    marked = 0
    for job in jobs:
        if job["age"] > days:
            jid = job_id(job)
            if jid not in known:
                known.add(jid)
                state["posted"].append(jid)
                marked += 1
    state["seeded"] = True
    log("Первый запуск: помечено виденными {} старых вакансий, "
        "к публикации окно в {} дней".format(marked, days))
    save_state(state)


def main():
    force = os.environ.get("FORCE", "").lower() in ("1", "true", "yes")
    dry = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")

    # Холостой прогон полезен и без ключей: он показывает, что таблица
    # читается и отбор работает, ещё до того как заведён канал.
    if not dry and not (BOT_TOKEN and CHANNEL_ID):
        raise SystemExit("нет BOT_TOKEN или CHANNEL_ID")

    state = load_state()
    log("Бот вакансий на связи. В памяти {} вакансий".format(
        len(state["posted"])))

    log("Читаю таблицу...")
    jobs = fetch_sheet()

    # Холостой прогон не должен ничего помечать виденным: иначе вакансии
    # окажутся «уже опубликованными» ещё до того, как появился канал.
    if not state.get("seeded") and not dry:
        seed(state, jobs)

    known = set(state["posted"])
    fresh = [j for j in jobs if job_id(j) not in known]
    # самые новые публикуем первыми
    fresh.sort(key=lambda j: j["age"])
    log("Новых к публикации: {}".format(len(fresh)))

    if dry:
        for j in fresh[:15]:
            log("  · [{} дн] {} — {} · {} · {}".format(
                j["age"], j["title"], j["studio"], j["country"], j["mode"]))
        if GEMINI_KEY and fresh:
            log("Пробный перевод первой вакансии:")
            for line in build_post(fresh[0], translate(fresh[0], state)).split("\n"):
                log("    " + line)
        return

    # Запуск живёт долго и сам держит ритм. Расписание GitHub срабатывает
    # раз в несколько часов, и если выходить сразу после публикации, канал
    # молчит до следующего стука. Поэтому: опубликовали очередь — не
    # выходим, а через RECHECK_MINUTES перечитываем таблицу, и так до
    # конца отведённого времени или до ночи.
    gap = int(getattr(config, "POST_EVERY_MINUTES", 20)) * 60
    limit = int(getattr(config, "MAX_POSTS_PER_RUN", 40))
    recheck = int(getattr(config, "RECHECK_MINUTES", 30)) * 60
    deadline = time.time() + int(getattr(config, "LOOP_MINUTES", 300)) * 60
    posted = 0

    def nap(seconds):
        """Спать короткими кусками, чтобы запуск можно было отменить."""
        end = time.time() + seconds
        while time.time() < end:
            time.sleep(min(30, max(1, end - time.time())))

    while True:
        if not fresh:
            log("Новых вакансий нет.")
        for job in fresh:
            if posted >= limit:
                break
            if not force:
                if quiet_now():
                    break
                waited = time.time() - float(state.get("last_post", 0))
                if waited < gap:
                    left = gap - waited
                    log("Следующий пост через {} мин".format(int(left // 60) + 1))
                    nap(left)
                    if quiet_now():
                        break
            try:
                publish(job, state)
            except Exception as e:
                log("  ! не опубликовалось: {}".format(str(e)[:200]))
                continue
            state["posted"].append(job_id(job))
            state["last_post"] = time.time()
            save_state(state)
            posted += 1

        if force or posted >= limit or time.time() >= deadline:
            break
        if quiet_now():
            log("Ночь — остальное утром.")
            break
        if time.time() + recheck >= deadline:
            break

        log("Проверю таблицу снова через {} мин".format(recheck // 60))
        nap(recheck)
        if quiet_now():
            log("Наступила ночь — остальное утром.")
            break
        try:
            jobs = fetch_sheet()
        except Exception as e:
            log("  ! таблица не прочиталась: {}".format(str(e)[:150]))
            continue
        known = set(state["posted"])
        fresh = sorted([j for j in jobs if job_id(j) not in known],
                       key=lambda j: j["age"])
        if fresh:
            log("Новых к публикации: {}".format(len(fresh)))

    log("Готово. Опубликовано за запуск: {}".format(posted))


if __name__ == "__main__":
    main()
