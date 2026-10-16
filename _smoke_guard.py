# -*- coding: utf-8 -*-
"""Офлайн-смоук ЗАЩИТЫ бесплатки: режим /guard, аккаунты «без бесплатки»
(free_off), дневной лимит регистраций (MAX_NEW_USERS_PER_DAY), вехи роста
(USER_MILESTONES), флуд-детект регистраций (автостоп guard), капча
«Я не робот» для новичков во время всплеска и фильтр свежих ID
(одноразовые аккаунты, моложе всех виденных). Никакой реальной отправки
в Telegram.
"""
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")
os.environ["TELEGRAM_BOT_TOKEN"] = "0:test"
os.environ["ADMIN_ID"] = "777999"
_tmpdb = os.path.join(tempfile.gettempdir(), f"vp_guard_{os.getpid()}.db")
for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(_tmpdb + suffix)
    except FileNotFoundError:
        pass
os.environ["DB_PATH"] = _tmpdb
os.environ.setdefault("PORT", "18996")

import bot  # noqa: E402

failures = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (" | " + str(extra) if extra else ""))
    if not cond:
        failures.append(name)


ADMIN = bot.ADMIN_ID  # 777999
sent = []
markups = []

# Пороги флуд-защиты по умолчанию (env в смоуке не задан — берутся дефолты).
check("burst: default thresholds",
      (bot.SIGNUP_BURST_LIMIT, bot.SIGNUP_BURST_WINDOW,
       bot.SIGNUP_CAPTCHA_LIMIT, bot.SIGNUP_CAPTCHA_TTL,
       bot.SIGNUP_CAPTCHA_TIMEOUT) == (20, 10, 5, 900, 300))
# В основных сценариях капча/автостоп глушим — блок 11) проверяет их целепо.
bot.SIGNUP_CAPTCHA_LIMIT = 10**9
bot.SIGNUP_BURST_LIMIT = 10**9


def fake_send(chat_id, text, reply_markup=None, reply_to_message_id=None, **kw):
    sent.append((chat_id, text))
    markups.append((chat_id, reply_markup))

    class _M:
        message_id = 1

        class chat:
            id = chat_id

    return _M()


bot.bot.send_message = fake_send


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def make_reg_msg(uid):
    return _Obj(
        from_user=_Obj(id=uid, language_code="ru", username="tst", first_name="T"),
        chat=_Obj(id=uid), text="/start", message_id=1,
    )


def meta_get(key):
    with bot.db_lock:
        conn = bot.get_db()
        try:
            row = conn.execute(
                "SELECT value FROM bot_meta WHERE key=?", (key,)
            ).fetchone()
            return row["value"] if row else None
        finally:
            conn.close()


def meta_reset(key):
    with bot.db_lock:
        conn = bot.get_db()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO bot_meta (key, value) VALUES (?, '')", (key,)
            )
            conn.commit()
        finally:
            conn.close()


def meta_set(key, value):
    with bot.db_lock:
        conn = bot.get_db()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO bot_meta (key, value) VALUES (?, ?)",
                (key, str(value)),
            )
            conn.commit()
        finally:
            conn.close()


def set_users(uid, **changes):
    with bot.db_lock:
        conn = bot.get_db()
        try:
            sets = ", ".join(f"{k}=?" for k in changes)
            conn.execute(
                f"UPDATE users SET {sets} WHERE user_id=?", (*changes.values(), uid)
            )
            conn.commit()
        finally:
            conn.close()


# 1) Флаги и режим guard.
bot.set_guard(False)
# База «свежего ID»: держим выше всех тестовых uid, чтобы старые сценарии
# не попадали под капчу — блок 12) управляет границей явно.
meta_set("uid_hi", "999999999999")
check("guard off by default", bot.guard_on() is False)

# 2) Обычный новый пользователь — полные разовые подарки.
u1 = 960101
bot.get_balance(u1)  # ensure_daily создаёт строку → политика применяется
_paid, _bonus, tts, chat_left, img, _show = bot.get_balance(u1)
check("signup: chat quota", chat_left == bot.FREE_CHATS_PER_DAY, chat_left)
check("signup: image gift", img == bot.FREE_IMAGES_ONCE, img)
check("signup: tts gift", tts == bot.FREE_TTS_TRIES, tts)
vq, _prem = bot.get_voice_quota(u1)
check("signup: voice gift", vq == bot.FREE_VOICE_TASKS_ONCE, vq)
check("signup: not free_off", bot.free_off(u1) is False)

# 3) Guard ON: суточный сброс не выдаёт дневную квоту.
bot.set_guard(True)
set_users(u1, free_day="")
check("guard: chat quota blocked on reset", bot.get_balance(u1)[3] == 0)

# 4) Guard ON: новый аккаунт — без подарков и навсегда free_off.
u2 = 960102
bot.get_balance(u2)
check("guard: new user is free_off", bot.free_off(u2) is True)
_p, _b, tts2, chat2, img2, _s = bot.get_balance(u2)
vq2, _ = bot.get_voice_quota(u2)
check("guard: new user has no gifts", (tts2, chat2, img2, vq2) == (0, 0, 0, 0),
      (tts2, chat2, img2, vq2))
bot.set_guard(False)
set_users(u2, free_day="")
check("free_off persists after guard off", bot.get_balance(u2)[3] == 0)

# 5) Дневной лимит регистраций: сверх лимита — без бесплатки.
old_cap = bot.MAX_NEW_USERS_PER_DAY
bot.MAX_NEW_USERS_PER_DAY = 0
u3 = 960103
bot.get_balance(u3)
check("cap: new user beyond cap is free_off", bot.free_off(u3) is True)
bot.MAX_NEW_USERS_PER_DAY = old_cap

# 6) Вехи роста: уведомление админу, веха запоминается после успешной отправки.
bot.USER_MILESTONES = (1,)
meta_reset("user_milestone")
del sent[:]
bot.register_user(make_reg_msg(960104))
admin_msgs = [t for chat, t in sent if chat == ADMIN]
milestone = [t for t in admin_msgs if "превышена веха" in t]
check("milestone: admin notified", len(milestone) == 1, admin_msgs)
check("milestone: mentions factual count",
      bool(milestone) and "Пользователей:" in milestone[0])
check("milestone: remembered after success", meta_get("user_milestone") == "1",
      meta_get("user_milestone"))

# 7) Нет интернета: отправка падает → веха НЕ запоминается (остаётся повтор).
meta_reset("user_milestone")


def failing_send(chat_id, *a, **kw):
    raise RuntimeError("offline")


bot.bot.send_message = failing_send
bot.register_user(make_reg_msg(960105))  # уведомления падают — перехват внутри
check("milestone: NOT remembered when send fails",
      meta_get("user_milestone") in ("", None), repr(meta_get("user_milestone")))

# «Интернет вернулся» — следующая регистрация повторяет уведомление.
bot.bot.send_message = fake_send
del sent[:]
bot.register_user(make_reg_msg(960106))
admin_msgs = [t for chat, t in sent if chat == ADMIN]
check("milestone: retried on next signup",
      any("превышена веха" in t for t in admin_msgs), admin_msgs)
check("milestone: remembered after retry", meta_get("user_milestone") == "1",
      meta_get("user_milestone"))

# 8) Тексты: статус guard и отказы.
check("i18n: guard_status_on", "ВКЛ" in bot.tr(ADMIN, "guard_status_on",
      total=1, today=1, cap=1, burst=20, window=10, captcha=5))
check("i18n: guard_status_off", "ВЫКЛ" in bot.tr(ADMIN, "guard_status_off",
      total=1, today=1, cap=1, burst=20, window=10, captcha=5))
check("i18n: free_paused mentions protection",
      "защит" in bot.tr(ADMIN, "free_paused").lower())
check("i18n: free_off_notice exists",
      bot.tr(ADMIN, "free_off_notice") != "free_off_notice")

# 9) free_blocked_key выбирает текст по ситуации.
bot.set_guard(True)
check("free_blocked_key: guard wins", bot.free_blocked_key(960102) == "free_paused")
bot.set_guard(False)
check("free_blocked_key: free_off user", bot.free_blocked_key(960102) == "free_off_notice")
check("free_blocked_key: ordinary quota user", bot.free_blocked_key(u1) == "tries_limit")

# 10) Статус /guard рендерит фактические цифры.
guard_txt_on = bot.tr(ADMIN, "guard_status_on", total=5, today=3, cap=300,
                      burst=20, window=10, captcha=5)
check("guard status renders counters", "5" in guard_txt_on and "300" in guard_txt_on)

# 11) Флуд-детект: капча для новичков и автостоп guard по всплеску.
def _age_users():
    """Устарить все регистрации, чтобы они не попадали в скользящее окно."""
    with bot.db_lock:
        conn = bot.get_db()
        try:
            conn.execute("UPDATE users SET created_at = '2020-01-01 00:00:00'")
            conn.commit()
        finally:
            conn.close()


bot.SIGNUP_BURST_WINDOW = 60
bot.SIGNUP_CAPTCHA_LIMIT = 3
bot.SIGNUP_BURST_LIMIT = 5
bot.set_guard(False)
meta_reset("burst_at")
meta_reset("burst_alert")
_age_users()

# 11а) До мягкого порога — подарки выдаются сразу, без капчи.
for uid in (960201, 960202, 960203):
    bot.register_user(make_reg_msg(uid))
check("captcha: below limit — no hold", bot.free_off(960203) is False)
_p, _b, tts_l, chats_l, img_l, _s = bot.get_balance(960203)
check("captcha: below limit — gifts granted",
      (tts_l, chats_l, img_l) == (3, 3, 1), (tts_l, chats_l, img_l))

# 11б) Сверх порога: капча замораживает подарки до нажатия.
del sent[:]
del markups[:]
bot.register_user(make_reg_msg(960204))
check("captcha: held on burst", bot.free_off(960204) is True)
_p, _b, tts_l, chats_l, img_l, _s = bot.get_balance(960204)
check("captcha: no gifts before pass",
      (tts_l, chats_l, img_l) == (0, 0, 0), (tts_l, chats_l, img_l))
cap_msgs = [t for chat, t in sent if chat == 960204]
check("captcha: message sent", any("человек" in t for t in cap_msgs), cap_msgs)
btns = [m for chat, m in markups
        if chat == 960204 and m and getattr(m, "keyboard", None)]
cap_data = btns[0].keyboard[0][0].callback_data if btns else ""
check("captcha: button with token", cap_data.startswith("rc:960204:"), cap_data)
check("captcha: pending", bot.captcha_pending(960204) is True)
check("captcha: blocked key", bot.free_blocked_key(960204) == "captcha_needed")

# 11в) Неверный токен отклонён, верный — выдаёт подарки.
bot.signup_captcha_pass(_Obj(
    id="cb1", data="rc:960204:ffffffff",
    from_user=_Obj(id=960204),
    message=_Obj(message_id=5, chat=_Obj(id=960204)),
))
check("captcha: wrong nonce rejected", bot.free_off(960204) is True)
raw = meta_get(f"captcha:960204") or ""
nonce = raw.partition(":")[2]
bot.signup_captcha_pass(_Obj(
    id="cb2", data=f"rc:960204:{nonce}",
    from_user=_Obj(id=960204),
    message=_Obj(message_id=6, chat=_Obj(id=960204)),
))
check("captcha: pass grants gifts", bot.free_off(960204) is False)
_p, _b, tts_l, chats_l, img_l, _s = bot.get_balance(960204)
check("captcha: quotas after pass",
      (tts_l, chats_l, img_l) == (3, 3, 1), (tts_l, chats_l, img_l))
check("captcha: token cleared", bot.captcha_pending(960204) is False)

# 11г) Превышение жёсткого порога → guard включается автоматически.
del sent[:]
bot.register_user(make_reg_msg(960205))  # n=5 — ещё без автостопа
check("burst: no guard at limit", bot.guard_on() is False)
bot.register_user(make_reg_msg(960206))  # n=6 > 5 — автостоп
check("burst: guard auto-enabled", bot.guard_on() is True)
alerts = [t for chat, t in sent if chat == ADMIN and "Всплеск регистраций" in t]
check("burst: admin alerted once", len(alerts) == 1, alerts)
check("burst: alert mentions /guard off",
      bool(alerts) and "/guard off" in alerts[0])
check("burst: cooldown stored", (meta_get("burst_at") or "").isdigit())

# 11д) Оповещение не ушло (нет интернета) → повтор со следующей регистрации.
bot.set_guard(False)
meta_reset("burst_alert")
_age_users()
bot.bot.send_message = failing_send
for uid in range(960210, 960216):  # на 6-й (960215) — автостоп, отправка падает
    bot.register_user(make_reg_msg(uid))
check("burst: guard kept despite send failure", bot.guard_on() is True)
check("burst: alert NOT remembered on failure",
      meta_get("burst_alert") == "1", meta_get("burst_alert"))
bot.bot.send_message = fake_send
del sent[:]
bot.register_user(make_reg_msg(960216))
alerts = [t for chat, t in sent if chat == ADMIN and "Всплеск регистраций" in t]
check("burst: alert retried on next signup", len(alerts) == 1, alerts)
check("burst: alert remembered after success",
      meta_get("burst_alert") == "0", meta_get("burst_alert"))

# 11е) Кулдаун: guard выключен и rate низкий — новичок всё равно под капчей.
bot.set_guard(False)
_age_users()
bot.register_user(make_reg_msg(960217))
check("captcha: cooldown holds new user",
      bot.free_off(960217) is True and bot.captcha_pending(960217) is True)
check("captcha: blocked key during cooldown",
      bot.free_blocked_key(960217) == "captcha_needed")

# 11ж) i18n-ключи капчи и пороги в статусе /guard.
check("i18n: captcha_required ru", "человек" in bot.tr(ADMIN, "captcha_required", sec=300))
check("i18n: captcha_btn", "не робот" in bot.tr(ADMIN, "captcha_btn"))
check("i18n: captcha_granted renders",
      "3" in bot.tr(ADMIN, "captcha_granted", chats=3, img=1, voice=5, tts=3))
check("i18n: captcha_needed exists",
      bot.tr(ADMIN, "captcha_needed") != "captcha_needed")
check("guard status shows flood thresholds",
      "20" in guard_txt_on and "Флуд" in guard_txt_on)

# 12) Свежий ID (C-lite): одноразовый свежесозданный аккаунт — капча даже
# одной регистрацией, без порогов всплеска; граница uid_hi скользит за новичком.
bot.SIGNUP_CAPTCHA_LIMIT = 10**9
bot.SIGNUP_BURST_LIMIT = 10**9
bot.set_guard(False)
meta_reset("burst_at")
_age_users()

# 12а) Аккаунт не новее границы — подарки сразу, без капчи.
meta_set("uid_hi", "960400")
del sent[:]
del markups[:]
bot.register_user(make_reg_msg(960350))
check("fresh: old uid not held",
      bot.free_off(960350) is False and bot.captcha_pending(960350) is False)
_p, _b, tts_l, chats_l, img_l, _s = bot.get_balance(960350)
check("fresh: old uid keeps gifts",
      (tts_l, chats_l, img_l) == (3, 3, 1), (tts_l, chats_l, img_l))

# 12б) Свежий uid при тихом окне — капча одной регистрацией.
del sent[:]
del markups[:]
bot.register_user(make_reg_msg(960401))
check("fresh: new uid held",
      bot.free_off(960401) is True and bot.captcha_pending(960401) is True)
check("fresh: boundary moved", meta_get("uid_hi") == "960401",
      meta_get("uid_hi"))
_p, _b, tts_l, chats_l, img_l, _s = bot.get_balance(960401)
check("fresh: no gifts before pass",
      (tts_l, chats_l, img_l) == (0, 0, 0), (tts_l, chats_l, img_l))
cap_msgs = [t for chat, t in sent if chat == 960401]
check("fresh: captcha message sent",
      any("человек" in t for t in cap_msgs), cap_msgs)
btns = [m for chat, m in markups
        if chat == 960401 and m and getattr(m, "keyboard", None)]
cap_data = btns[0].keyboard[0][0].callback_data if btns else ""
check("fresh: button with token", cap_data.startswith("rc:960401:"), cap_data)
check("fresh: blocked key", bot.free_blocked_key(960401) == "captcha_needed")

# 12в) Следующий расходник снова «свежий» (граница скользит),
# аккаунт старше границы проходит без капчи.
bot.register_user(make_reg_msg(960402))
check("fresh: chain keeps flagging", bot.captcha_pending(960402) is True)
bot.register_user(make_reg_msg(960390))
check("fresh: older uid passes",
      bot.free_off(960390) is False and bot.captcha_pending(960390) is False)
check("fresh: boundary at max uid", meta_get("uid_hi") == "960402",
      meta_get("uid_hi"))

# 12г) Kill-switch: SIGNUP_FRESH_CAPTCHA=0 — граница копится, капча не ставится.
bot.SIGNUP_FRESH_CAPTCHA = False
bot.register_user(make_reg_msg(960501))
check("fresh: switch off skips hold",
      bot.captcha_pending(960501) is False and bot.free_off(960501) is False)
check("fresh: boundary still tracked", meta_get("uid_hi") == "960501",
      meta_get("uid_hi"))
bot.SIGNUP_FRESH_CAPTCHA = True

# 12д) Отказ по капче рендерится для держателя свежего ID.
check("fresh: captcha_needed renders",
      bot.tr(960401, "captcha_needed") != "captcha_needed")

os.remove(_tmpdb)
print()
if failures:
    print(f"SMOKE GUARD FAILED: {len(failures)} checks")
    sys.exit(1)
print("SMOKE GUARD OK")