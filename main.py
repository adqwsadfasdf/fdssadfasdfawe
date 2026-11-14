#!/usr/bin/env python3
"""
YouTube Comment Watcher — Telegram-бот.

Вы присылаете боту ссылку на видео -> бот следит за новыми комментариями под ним
и присылает уведомления. Кнопкой можно временно выключить уведомления.

Только чтение через официальный YouTube Data API: бот ничего не пишет, не лайкает
и не смотрит видео, поэтому никакой накрутки нет.

Работает на стандартной библиотеке Python 3.8+.

Переменные окружения (или файл .env рядом со скриптом):
  YT_API_KEY      ключ YouTube Data API v3                      (обязательно)
  TG_BOT_TOKEN    токен бота от @BotFather                       (обязательно)
  TG_CHAT_ID      ваш chat_id; если не задан, хозяином станет тот,
                  кто первым напишет боту /start
  POLL_SECONDS    минимальный интервал проверки, по умолчанию 20
  DAILY_BUDGET    сколько запросов к API тратить в сутки, по умолчанию 8000
                  (бесплатный лимит YouTube — 10 000)
  STATE_FILE      файл состояния, по умолчанию state.json
"""

import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
YT_API = "https://www.googleapis.com/youtube/v3"


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv(BASE_DIR / ".env")


def env(name: str, default: str = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        sys.exit(f"Не задана переменная окружения {name} (см. README.md)")
    return value


YT_API_KEY = env("YT_API_KEY")
# Bothost сам подставляет токен из поля «Токен бота» как BOT_TOKEN
TG_BOT_TOKEN = (os.environ.get("TG_BOT_TOKEN") or os.environ.get("BOT_TOKEN")
                or os.environ.get("TELEGRAM_BOT_TOKEN") or env("TG_BOT_TOKEN"))
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
POLL_SECONDS = int(env("POLL_SECONDS", "20"))
DAILY_BUDGET = int(env("DAILY_BUDGET", "8000"))


def default_state_file() -> Path:
    # На Bothost сохраняется только папка /app/data (DATA_DIR), остальное стирается при деплое
    for d in (os.environ.get("DATA_DIR"), "/app/data"):
        if d and Path(d).is_dir():
            return Path(d) / "state.json"
    return BASE_DIR / "state.json"


STATE_FILE = Path(os.environ.get("STATE_FILE") or default_state_file())
MAX_SEEN_PER_VIDEO = 2000

BTN_PAUSE = "🔕 Выключить уведомления"
BTN_RESUME = "🔔 Включить уведомления"
BTN_LIST = "📋 Мои видео"


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


# ---------------------------------------------------------------- state

lock = threading.RLock()


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"Не смог прочитать {STATE_FILE}: {e!r}")
    return {}


state = load_state()
state.setdefault("paused", False)
state.setdefault("videos", {})  # video_id -> {"title": str, "seen": [comment_id, ...]}
state.setdefault("owner", TG_CHAT_ID or None)
state.setdefault("tg_offset", 0)
if TG_CHAT_ID:
    state["owner"] = TG_CHAT_ID


def save_state() -> None:
    with lock:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_FILE)


# ---------------------------------------------------------------- http

def http_json(url: str, data: dict = None, timeout: int = 30) -> dict:
    body = None
    headers = {}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def yt(endpoint: str, **params) -> dict:
    params["key"] = YT_API_KEY
    return http_json(f"{YT_API}/{endpoint}?{urllib.parse.urlencode(params)}")


def tg(method: str, http_timeout: int = 30, **payload) -> dict:
    return http_json(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}", payload, http_timeout)


def http_error_reason(e: urllib.error.HTTPError) -> str:
    try:
        return json.loads(e.read().decode("utf-8"))["error"]["errors"][0]["reason"]
    except Exception:
        return str(e.code)


# ---------------------------------------------------------------- youtube

VIDEO_ID_RE = re.compile(
    r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/|/v/)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])"
)


def parse_video_id(text: str):
    text = text.strip()
    m = VIDEO_ID_RE.search(text)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", text):
        return text
    return None


def fetch_video_title(video_id: str):
    items = yt("videos", part="snippet", id=video_id).get("items", [])
    return items[0]["snippet"]["title"] if items else None


def fetch_latest_comments(video_id: str) -> list:
    """100 последних комментариев верхнего уровня под видео (1 единица квоты)."""
    data = yt(
        "commentThreads",
        part="snippet",
        videoId=video_id,
        order="time",
        maxResults=100,
        textFormat="plainText",
    )
    out = []
    for item in data.get("items", []):
        top = item["snippet"]["topLevelComment"]
        s = top["snippet"]
        out.append(
            {
                "id": top["id"],
                "author": s.get("authorDisplayName", "?"),
                "author_url": s.get("authorChannelUrl", ""),
                "text": s.get("textDisplay", ""),
            }
        )
    return out


# ---------------------------------------------------------------- telegram ui

def main_keyboard() -> dict:
    with lock:
        toggle = BTN_RESUME if state["paused"] else BTN_PAUSE
    return {"keyboard": [[{"text": toggle}], [{"text": BTN_LIST}]], "resize_keyboard": True}


def send(text: str, chat_id=None, keyboard=None) -> None:
    payload = {
        "chat_id": chat_id or state["owner"],
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": keyboard or main_keyboard(),
    }
    try:
        tg("sendMessage", **payload)
    except urllib.error.HTTPError as e:
        log(f"Telegram sendMessage {e.code}: {e.read()[:300]!r}")


def format_comment(video_id: str, title: str, c: dict) -> str:
    e = html.escape
    link = f"https://www.youtube.com/watch?v={video_id}&lc={c['id']}"
    author = e(c["author"])
    if c["author_url"]:
        author = f'<a href="{e(c["author_url"])}">{author}</a>'
    text = c["text"] if len(c["text"]) <= 3000 else c["text"][:3000] + "…"
    return (
        f"💬 <b>Новый комментарий</b>\n"
        f"🎬 {e(title)}\n"
        f"👤 {author}\n\n"
        f"{e(text)}\n\n"
        f'<a href="{link}">Открыть комментарий</a>'
    )


def add_video(video_id: str) -> None:
    with lock:
        if video_id in state["videos"]:
            send(f"Уже слежу за «{html.escape(state['videos'][video_id]['title'])}»")
            return
    try:
        title = fetch_video_title(video_id)
        if not title:
            send("Видео не найдено. Проверь ссылку (приватные видео отслеживать нельзя).")
            return
        existing = fetch_latest_comments(video_id)
    except urllib.error.HTTPError as e:
        reason = http_error_reason(e)
        if reason == "commentsDisabled":
            send("Под этим видео отключены комментарии.")
        else:
            send(f"YouTube вернул ошибку: {html.escape(reason)}")
        return
    with lock:
        state["videos"][video_id] = {"title": title, "seen": [c["id"] for c in existing]}
    save_state()
    log(f"Добавлено видео {video_id}: {title}")
    send(
        f"✅ Слежу за «{html.escape(title)}»\n"
        f"Старые комментарии ({len(existing)}) пропускаю, пришлю только новые."
        + interval_note()
    )


def interval_note() -> str:
    sec = current_interval()
    return f"\n⏱ Проверка раз в {sec} c." if sec > POLL_SECONDS else ""


def show_list() -> None:
    with lock:
        videos = [(vid, v["title"]) for vid, v in state["videos"].items()]
        paused = state["paused"]
    if not videos:
        send("Пока ничего не отслеживаю. Пришли ссылку на видео.")
        return
    lines = [f"{'🔕 Уведомления выключены' if paused else '🔔 Уведомления включены'}",
             f"⏱ Проверка раз в {current_interval()} c\n"]
    buttons = []
    for i, (vid, title) in enumerate(videos, 1):
        lines.append(f'{i}. <a href="https://youtu.be/{vid}">{html.escape(title)}</a>')
        short = title if len(title) <= 30 else title[:29] + "…"
        buttons.append([{"text": f"❌ {i}. {short}", "callback_data": f"del:{vid}"}])
    send("\n".join(lines) + "\n\nНажми, чтобы перестать следить:",
         keyboard={"inline_keyboard": buttons})


def set_paused(paused: bool) -> None:
    with lock:
        state["paused"] = paused
    save_state()
    if paused:
        send("🔕 Уведомления выключены. Комментарии, оставленные в это время, не придут "
             "и потом не прилетят пачкой.")
    else:
        send("🔔 Уведомления снова включены.")


def handle_update(upd: dict) -> None:
    if "callback_query" in upd:
        cq = upd["callback_query"]
        chat_id = str(cq["message"]["chat"]["id"])
        if chat_id != str(state["owner"]):
            return
        data = cq.get("data", "")
        answer = ""
        if data.startswith("del:"):
            vid = data[4:]
            with lock:
                v = state["videos"].pop(vid, None)
            save_state()
            answer = f"Больше не слежу: {v['title']}" if v else "Уже удалено"
            log(f"Удалено видео {vid}")
        try:
            tg("answerCallbackQuery", callback_query_id=cq["id"], text=answer[:200])
        except Exception:
            pass
        if answer:
            send("🗑 " + html.escape(answer))
        return

    msg = upd.get("message")
    if not msg or "text" not in msg:
        return
    chat_id = str(msg["chat"]["id"])
    text = msg["text"].strip()

    if not state["owner"]:
        if text.startswith("/start"):
            with lock:
                state["owner"] = chat_id
            save_state()
            log(f"Хозяин бота: {chat_id}")
        else:
            return
    if chat_id != str(state["owner"]):
        send("Это личный бот, извини.", chat_id=chat_id, keyboard={"remove_keyboard": True})
        return

    if text.startswith("/start") or text.startswith("/help"):
        send("Привет! Пришли ссылку на YouTube-видео — буду присылать новые комментарии под ним.\n"
             "Можно прислать несколько ссылок.\n\n"
             "Кнопки внизу: выключить/включить уведомления и список видео.")
    elif text == BTN_PAUSE or text == "/pause":
        set_paused(True)
    elif text == BTN_RESUME or text == "/resume":
        set_paused(False)
    elif text == BTN_LIST or text == "/list":
        show_list()
    else:
        ids = []
        for word in text.split():
            vid = parse_video_id(word)
            if vid and vid not in ids:
                ids.append(vid)
        if not ids:
            send("Не нашёл ссылку на видео. Пришли ссылку вида youtube.com/watch?v=… или youtu.be/…")
        for vid in ids:
            add_video(vid)


def bot_loop() -> None:
    while True:
        try:
            res = tg("getUpdates", http_timeout=60, offset=state["tg_offset"],
                     timeout=50, allowed_updates=["message", "callback_query"])
            for upd in res.get("result", []):
                with lock:
                    state["tg_offset"] = upd["update_id"] + 1
                try:
                    handle_update(upd)
                except Exception as e:
                    log(f"Ошибка обработки сообщения: {e!r}")
            if res.get("result"):
                save_state()
        except Exception as e:
            log(f"Telegram getUpdates: {e!r}")
            time.sleep(5)


# ---------------------------------------------------------------- watcher

def current_interval() -> int:
    """Чтобы не превысить суточную квоту, с ростом числа видео проверяем реже."""
    with lock:
        n = max(1, len(state["videos"]))
    return max(POLL_SECONDS, int(n * 86400 / DAILY_BUDGET) + 1)


def check_video(video_id: str) -> None:
    try:
        comments = fetch_latest_comments(video_id)
    except urllib.error.HTTPError as e:
        reason = http_error_reason(e)
        log(f"{video_id}: ошибка YouTube {reason}")
        if reason in ("videoNotFound", "commentsDisabled"):
            with lock:
                v = state["videos"].pop(video_id, None)
            save_state()
            if v:
                send(f"⚠️ Перестал следить за «{html.escape(v['title'])}»: "
                     f"{'видео удалено или скрыто' if reason == 'videoNotFound' else 'комментарии отключены'}.")
        elif reason in ("quotaExceeded", "dailyLimitExceeded"):
            raise
        return

    with lock:
        v = state["videos"].get(video_id)
        if v is None:
            return
        seen = set(v["seen"])
        new = [c for c in reversed(comments) if c["id"] not in seen]  # от старых к новым
        if not new:
            return
        v["seen"].extend(c["id"] for c in new)
        v["seen"] = v["seen"][-MAX_SEEN_PER_VIDEO:]
        paused = state["paused"]
        title = v["title"]
    save_state()
    for c in new:
        log(f"{video_id}: новый комментарий от {c['author']}{' (пауза)' if paused else ''}")
        if not paused and state["owner"]:
            send(format_comment(video_id, title, c))


def watch_loop() -> None:
    while True:
        with lock:
            videos = list(state["videos"].keys())
        try:
            for vid in videos:
                check_video(vid)
        except urllib.error.HTTPError:
            log("Закончилась суточная квота YouTube API, жду час")
            time.sleep(3600)
            continue
        except Exception as e:
            log(f"Ошибка проверки: {e!r}")
        time.sleep(current_interval())


def main() -> None:
    log(f"Запущен. Видео: {len(state['videos'])}, пауза: {state['paused']}")
    if not state["owner"]:
        log("TG_CHAT_ID не задан: напиши боту /start, и он запомнит тебя как хозяина")
    threading.Thread(target=watch_loop, daemon=True).start()
    bot_loop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
