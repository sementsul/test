#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Чистит описания ВСЕХ своих видео: удаляет ЛЮБЫЕ ссылки, КРОМЕ ссылок на твои проекты
(ratescout.ru и magzgold.ru, включая их поддомены). Текст описания не переписывается —
убираются только сторонние URL; пустые строки, оставшиеся от удалённых ссылок, схлопываются.

⚠️ Это правка описаний, поэтому с бэкапом:
  - перед изменением оригинал сохраняется в descriptions_backup.json (первый оригинал не перезатирается);
  - откат:  python yt_cleanlinks.py --restore

Использование:
  python yt_cleanlinks.py --dry-run   # показать, что изменится, без правок
  python yt_cleanlinks.py             # применить (с бэкапом)
  python yt_cleanlinks.py --restore   # вернуть описания из бэкапа

Подготовка/квота — как у yt_promo.py (client_secret.json рядом; ~190 видео/день; запускать в разные дни).
"""
import json, os, re, sys, time
from urllib.parse import urlparse

from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

HERE = os.path.dirname(os.path.abspath(__file__))
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
CLIENT_SECRET = os.path.join(HERE, "client_secret.json")
TOKEN = os.path.join(HERE, "token.json")
BACKUP = os.path.join(HERE, "descriptions_backup.json")

# Ссылки на эти домены (и поддомены) ОСТАВЛЯЕМ, все прочие URL удаляем.
KEEP_HOSTS = ("ratescout.ru", "magzgold.ru")
URL_RE = re.compile(r'https?://[^\s<>()\[\]"\']+', re.IGNORECASE)

DRY = "--dry-run" in sys.argv
RESTORE = "--restore" in sys.argv


def is_kept(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    return any(host == h or host.endswith("." + h) for h in KEEP_HOSTS)


def clean(desc: str) -> str:
    """Удаляет сторонние URL, оставляя проектные; схлопывает лишние пустые строки."""
    def repl(m):
        return m.group(0) if is_kept(m.group(0)) else ""
    out = URL_RE.sub(repl, desc)
    # подчистить строки, ставшие пустыми/из одних разделителей после удаления ссылки
    lines = []
    for ln in out.split("\n"):
        stripped = ln.strip()
        if stripped == "" or re.fullmatch(r'[\s:\-–—•·>|]*', stripped):
            lines.append("")            # оставим как пустую (схлопнём ниже)
        else:
            lines.append(ln.rstrip())
    out = "\n".join(lines)
    out = re.sub(r'\n{3,}', '\n\n', out).strip("\n")
    return out


def auth():
    creds = None
    if os.path.exists(TOKEN):
        creds = Credentials.from_authorized_user_file(TOKEN, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRET):
                sys.exit("Нет client_secret.json рядом со скриптом.")
            creds = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES).run_local_server(port=0)
        open(TOKEN, "w").write(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def all_video_ids(yt):
    r = yt.channels().list(part="contentDetails", mine=True).execute()
    pl = r["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    ids, page = [], None
    while True:
        r = yt.playlistItems().list(part="contentDetails", playlistId=pl, maxResults=50, pageToken=page).execute()
        ids += [i["contentDetails"]["videoId"] for i in r.get("items", [])]
        page = r.get("nextPageToken")
        if not page:
            break
    return ids


def load_backup():
    if os.path.exists(BACKUP):
        try:
            return json.load(open(BACKUP, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_backup(b):
    json.dump(b, open(BACKUP, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def update_desc(yt, v, new_desc):
    sn = v["snippet"]
    body = {"id": v["id"], "snippet": {
        "title": sn.get("title", ""), "categoryId": sn.get("categoryId", "22"), "description": new_desc}}
    if sn.get("defaultLanguage"):
        body["snippet"]["defaultLanguage"] = sn["defaultLanguage"]
    yt.videos().update(part="snippet", body=body).execute()


def run_apply(yt):
    vids = all_video_ids(yt)
    backup = load_backup()
    print(f"Видео: {len(vids)}  |  режим: {'DRY-RUN' if DRY else 'ЧИСТКА ссылок (кроме проектных)'}")
    changed = skipped = errors = 0
    quota = False
    for i in range(0, len(vids), 50):
        if quota:
            break
        batch = vids[i:i + 50]
        try:
            resp = yt.videos().list(part="snippet", id=",".join(batch)).execute()
        except HttpError as e:
            if "quotaExceeded" in str(e):
                quota = True; break
            raise
        for v in resp.get("items", []):
            sn = v["snippet"]
            title = sn.get("title", "")
            cur = sn.get("description", "")
            new = clean(cur)
            if new == cur:
                skipped += 1; continue           # сторонних ссылок нет — не трогаем
            if DRY:
                removed = [u for u in URL_RE.findall(cur) if not is_kept(u)]
                print(f"[+] {v['id']}  «{title[:45]}»  удалит {len(removed)} ссыл.: {', '.join(removed[:3])}")
                changed += 1; continue
            if v["id"] not in backup:
                backup[v["id"]] = {"title": title, "description": cur}
            try:
                update_desc(yt, v, new); changed += 1; print(f"[OK] {v['id']}  «{title[:45]}»"); time.sleep(0.3)
            except HttpError as e:
                if "quotaExceeded" in str(e):
                    quota = True; break
                errors += 1; print(f"[ERR] {v['id']}: {e}")
        if not DRY:
            save_backup(backup)
    if not DRY:
        save_backup(backup)
    print(f"\nИтог: очищено {changed}, пропущено (нет сторонних ссылок) {skipped}, ошибок {errors}. Бэкап: descriptions_backup.json")
    if quota:
        print("Дневная квота исчерпана — запусти скрипт снова завтра, обработанные пропустятся.")


def run_restore(yt):
    backup = load_backup()
    if not backup:
        sys.exit("Нет descriptions_backup.json — откатывать нечего.")
    ids = list(backup.keys())
    done = skipped = errors = 0
    quota = False
    for i in range(0, len(ids), 50):
        if quota:
            break
        batch = ids[i:i + 50]
        try:
            resp = yt.videos().list(part="snippet", id=",".join(batch)).execute()
        except HttpError as e:
            if "quotaExceeded" in str(e):
                quota = True; break
            raise
        for v in resp.get("items", []):
            old = backup.get(v["id"], {}).get("description", None)
            if old is None or v["snippet"].get("description", "") == old:
                skipped += 1; continue
            if DRY:
                print(f"[+] откат: {v['id']}"); done += 1; continue
            try:
                update_desc(yt, v, old); done += 1; print(f"[OK restore] {v['id']}"); time.sleep(0.3)
            except HttpError as e:
                if "quotaExceeded" in str(e):
                    quota = True; break
                errors += 1; print(f"[ERR] {v['id']}: {e}")
    print(f"\nОткат: восстановлено {done}, пропущено {skipped}, ошибок {errors}.")
    if quota:
        print("Квота исчерпана — запусти --restore снова завтра.")


def main():
    yt = auth()
    (run_restore if RESTORE else run_apply)(yt)


if __name__ == "__main__":
    main()
