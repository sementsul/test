#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Массовое добавление промо-блока в описания ВСЕХ своих видео на YouTube.

Что делает:
  - авторизуется в твой аккаунт (браузер, один раз),
  - берёт все видео из твоего канала (плейлист «Загрузки»),
  - к КАЖДОМУ описанию ДОПИСЫВАЕТ блок из promo.txt (не затирая текст),
  - идемпотентно: если ссылка уже есть в описании — видео пропускается,
  - --dry-run: только показать, что будет сделано, без изменений.

Подготовка (5 минут, один раз):
  1) console.cloud.google.com → создай проект → «APIs & Services» → включи «YouTube Data API v3».
  2) «OAuth consent screen» → External → добавь себя в Test users.
  3) «Credentials» → Create credentials → OAuth client ID → Desktop app → скачай JSON,
     переименуй в client_secret.json, положи рядом с этим скриптом.
  4) pip install google-api-python-client google-auth-oauthlib
  5) Проверка:   python yt_promo.py --dry-run
     Применить:  python yt_promo.py

Заметки:
  - Квота YouTube API по умолчанию 10 000 ед/день; videos.update = 50 ед → ~190 видео/день.
    Если видео больше — запусти на следующий день, уже обработанные пропустятся.
  - Правит ТОЛЬКО описание; название и категорию сохраняет.
"""
import os, sys, time

from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

HERE = os.path.dirname(os.path.abspath(__file__))
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
CLIENT_SECRET = os.path.join(HERE, "client_secret.json")
TOKEN = os.path.join(HERE, "token.json")
PROMO_FILE = os.path.join(HERE, "promo.txt")
LOG_FILE = os.path.join(HERE, "yt_promo_log.csv")
# Строка-маркер: если она уже есть в описании — считаем, что промо добавлено.
MARKER = "ratescout.ru/?utm_source=youtube"
DRY = "--dry-run" in sys.argv


def auth():
    creds = None
    if os.path.exists(TOKEN):
        creds = Credentials.from_authorized_user_file(TOKEN, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRET):
                sys.exit("Нет client_secret.json рядом со скриптом — см. инструкцию вверху файла.")
            creds = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES).run_local_server(port=0)
        with open(TOKEN, "w") as f:
            f.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def uploads_playlist(yt):
    r = yt.channels().list(part="contentDetails", mine=True).execute()
    items = r.get("items", [])
    if not items:
        sys.exit("Не нашёл канал у этого аккаунта.")
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


def all_video_ids(yt, playlist_id):
    ids, page = [], None
    while True:
        r = yt.playlistItems().list(part="contentDetails", playlistId=playlist_id,
                                    maxResults=50, pageToken=page).execute()
        ids += [i["contentDetails"]["videoId"] for i in r.get("items", [])]
        page = r.get("nextPageToken")
        if not page:
            break
    return ids


YT_DESC_LIMIT = 5000  # жёсткий лимит длины описания у YouTube


def log_row(action, vid, title, extra=""):
    """Дописать строку в CSV-лог (append-only). Без внешних либ — вручную экранируем кавычки."""
    def q(s):
        return '"' + str(s).replace('"', '""') + '"'
    line = ",".join([q(action), q(vid), q(title), q(extra)]) + "\n"
    new_file = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        if new_file:
            f.write("action,video_id,title,extra\n")
        f.write(line)


def main():
    promo = open(PROMO_FILE, encoding="utf-8").read().strip("\n")
    addition = "\n\n" + promo            # что реально дописываем к описанию
    yt = auth()
    vids = all_video_ids(yt, uploads_playlist(yt))
    print(f"Видео найдено: {len(vids)}  |  режим: {'DRY-RUN (без изменений)' if DRY else 'ПРИМЕНЕНИЕ'}")

    changed = skipped = no_room = errors = 0
    quota = False
    for i in range(0, len(vids), 50):
        if quota:
            break
        batch = vids[i:i + 50]
        try:
            resp = yt.videos().list(part="snippet", id=",".join(batch)).execute()
        except HttpError as e:
            if "quotaExceeded" in str(e):
                quota = True
                break
            raise
        for v in resp.get("items", []):
            sn = v["snippet"]
            title = sn.get("title", "")
            desc = sn.get("description", "")
            if MARKER in desc:
                skipped += 1
                continue
            # Промо не влезает в лимит 5000 → НЕ трогаем видео (иначе маркер обрежется
            # и видео будет обновляться вечно, впустую сжигая квоту). Раньше здесь был баг.
            base = desc.rstrip()
            if len(base) + len(addition) > YT_DESC_LIMIT:
                no_room += 1
                print(f"[--] не влезает ({len(base)} симв.): {v['id']}  «{title[:60]}»")
                if not DRY:
                    log_row("no_room", v["id"], title, f"desc_len={len(base)}")
                continue
            new_desc = base + addition
            if DRY:
                print(f"[+] будет обновлено: {v['id']}  «{title[:60]}»")
                changed += 1
                continue
            body = {"id": v["id"], "snippet": {
                "title": title, "categoryId": sn.get("categoryId", "22"), "description": new_desc}}
            if sn.get("defaultLanguage"):
                body["snippet"]["defaultLanguage"] = sn["defaultLanguage"]
            try:
                yt.videos().update(part="snippet", body=body).execute()
                changed += 1
                print(f"[OK] {v['id']}  «{title[:60]}»")
                log_row("updated", v["id"], title, f"desc_len={len(base)}")
                time.sleep(0.3)
            except HttpError as e:
                if "quotaExceeded" in str(e):
                    quota = True
                    break
                errors += 1
                print(f"[ERR] {v['id']}: {e}")
                log_row("error", v["id"], title, str(e).replace("\n", " ")[:200])

    print(f"\nИтог: обновлено {changed}, пропущено (уже есть) {skipped}, "
          f"не влезает {no_room}, ошибок {errors}.")
    if no_room:
        print(f"⚠ {no_room} видео имеют слишком длинное описание — промо в лимит 5000 не помещается. "
              f"Это и были «вечно обновляемые». Сократи их описания или укороти promo.txt.")
    if not DRY:
        print(f"Лог записан в: {LOG_FILE}")
    if quota:
        print("Дневная квота YouTube API исчерпана — запусти скрипт СНОВА завтра: "
              "обработанные видео пропустятся, допишет остаток.")
    elif not DRY and no_room == 0:
        print("Готово: все видео обработаны.")


if __name__ == "__main__":
    main()
