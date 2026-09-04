# -*- coding: utf-8 -*-
"""Настройки бота вакансий. Меняются здесь, код трогать не нужно."""

# --------------------------------------------------------------- источник
# Публичная таблица "Animation/VFX/Game Industry Job Postings".
# Читаем не HTML-страницу, а CSV-выгрузку того же листа: она отдаётся
# без авторизации, весит меньше и не ломается при смене оформления.
SHEET_ID = "1eR2oAXOuflr8CZeGoz3JTrsgNj3KuefbdXJOmNtjEVM"
SHEET_GID = "0"

# Колонки листа по номерам (нумерация с нуля). Между значимыми колонками
# в таблице стоят пустые технические — отсюда шаг через одну.
COLUMNS = {
    "studio": 0,
    "city": 2,
    "state": 4,
    "country": 6,
    "title": 8,
    "level": 10,
    "mode": 12,
    "date": 14,
    "link": 16,
    "software": 18,
    "notes": 20,
}

# --------------------------------------------------------------- отбор
# Берём вакансию, если в должности есть слово из WANT и нет из SKIP.
WANT = (
    "animator, 3d",
    "gameplay animator",
    "cinematic animator",
    "technical animator",
    "animation director",
    "animation supervisor",
    "animation td",
)
SKIP = (
    "2d",
    "programmer",
    "engineer",
    "editor",
    "professor",
    "recruiter",
    "intern coordinator",
)

# Вакансии старше этого срока не публикуем никогда: они успевают закрыться.
MAX_AGE_DAYS = 45

# Всё, что датировано раньше этого дня, в канал не идёт — ни при догоне,
# ни потом. Канал уже наполнен, дальше только свежее.
MIN_DATE = "2026-09-01"

# --------------------------------------------------------------- ритм
POST_EVERY_MINUTES = 2       # пауза между постами: свежее выходит сразу
MAX_POSTS_PER_RUN = 40       # предохранитель от лавины
LOOP_MINUTES = 345           # сколько живёт один запуск (см. timeout в workflow)
RECHECK_MINUTES = 60         # перечитывать таблицу раз в час
UTC_OFFSET_HOURS = 5         # часовой пояс владельца канала
ACTIVE_HOURS = (0, 24)       # круглосуточно, тихих часов нет

# Догон при первом запуске: сколько дней назад захватить.
# 0 — ничего не догонять, публиковать только новое.
BACKFILL_DAYS = 30

# --------------------------------------------------------------- перевод
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_FALLBACK_MODELS = (
    "gemini-2.5-flash-lite",
    "gemini-flash-latest",
    "gemini-2.5-pro",
    "gemma-3-27b-it",
)

CHANNEL_LINK = ""            # заполнится автоматически из CHANNEL_ID
