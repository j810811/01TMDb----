import requests
import os
import json
import time
import re
import threading
import random
import tkinter as tk
from tkinter import scrolledtext
from datetime import datetime
from urllib.parse import urlparse


SAVE_DIR = r"D:\TMDB_剧照库"
MIN_DELAY = 10.0
MAX_DELAY = 25.0

COOKIE_FILE = "maoyan_last_cookie.txt"
RECORD_FILE = "maoyan_downloaded.json"

DETAIL_API = "https://m.maoyan.com/ajax/detailmovie"
SEARCH_API = "https://m.maoyan.com/ajax/search"
HOT_API = "https://m.maoyan.com/ajax/movieOnInfoList"
COMING_API = "https://m.maoyan.com/ajax/comingList"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Referer": "https://m.maoyan.com/",
    "Accept": "application/json, text/plain, */*",
    "Cookie": "",
}


record = {"photos": {}, "daily": {}, "completed": {}}
record_lock = threading.Lock()

is_running = False
pause_event = threading.Event()
pause_event.set()

current_movie_id = None
stats = {"fails": 0}

app_instance = None
_log_hook = None


def log(msg):
    print(msg)
    if _log_hook:
        _log_hook(msg)
        return
    if app_instance:
        app_instance.root.after(0, lambda: app_instance.log(msg))


def set_log_hook(hook):
    global _log_hook
    _log_hook = hook


def random_sleep(a=MIN_DELAY, b=MAX_DELAY):
    time.sleep(random.uniform(a, b))


def today_key():
    return datetime.now().strftime("%Y-%m-%d")


def clean_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "", name or "")


def load_last_cookie():
    if os.path.exists(COOKIE_FILE):
        try:
            with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return ""
    return ""


def save_last_cookie(cookie):
    try:
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            f.write(cookie or "")
    except Exception:
        pass


def load_record():
    global record
    if os.path.exists(RECORD_FILE):
        try:
            with open(RECORD_FILE, "r", encoding="utf-8") as f:
                record = json.load(f)
        except Exception:
            pass

    if "photos" not in record:
        record["photos"] = {}
    if "daily" not in record:
        record["daily"] = {}
    if "completed" not in record:
        record["completed"] = {}


def save_record():
    with record_lock:
        with open(RECORD_FILE, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)


def mark_movie_completed(movie_id, title, score=""):
    with record_lock:
        record.setdefault("completed", {})
        record["completed"][str(movie_id)] = {
            "title": title,
            "score": score,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }


def get_total_recorded_photos():
    total = 0
    with record_lock:
        for mid in record.get("photos", {}):
            total += len(record["photos"][mid])
    return total


def get_total_recorded_movies():
    with record_lock:
        return len(record.get("photos", {}))


def get_current_movie_count():
    if not current_movie_id:
        return 0
    with record_lock:
        return len(record["photos"].get(str(current_movie_id), []))


def get_today_count():
    today = today_key()
    with record_lock:
        return record.get("daily", {}).get(today, 0)


def safe_json_request(url, params=None):
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=20)
        if r.status_code == 200:
            return r.json()
        hint = ""
        if r.status_code in (301, 302, 401, 403):
            hint = "（可能 Cookie 无效/缺失）"
        log(f"❌ 请求错误 {r.status_code}{hint}: {url}")
    except Exception as e:
        log(f"❌ 请求异常: {e}")
    return None


def safe_json_request_allow_400(url, params=None):
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=20)
        if r.status_code == 200:
            return r.json()
        hint = ""
        if r.status_code in (301, 302, 401, 403):
            hint = "（可能 Cookie 无效/缺失）"
        log(f"❌ 请求错误 {r.status_code}{hint}: {url}")
    except Exception as e:
        log(f"❌ 请求异常: {e}")
    return None


def search_movie_candidates(keyword: str, city_id: int = 1):
    keyword = (keyword or "").strip()
    if not keyword:
        return []

    data = safe_json_request_allow_400(SEARCH_API, params={"kw": keyword, "cityId": city_id})
    if not data:
        return []

    movies = data.get("movies") or []
    result = []
    for m in movies:
        mid = m.get("id")
        if mid is None:
            continue
        title = m.get("nm") or ""
        year = m.get("pubDesc") or m.get("rt") or ""
        try:
            mid_int = int(mid)
        except Exception:
            continue
        result.append({"id": mid_int, "title": title, "year": year})

    return result


def get_hot_movie_ids():
    data = safe_json_request_allow_400(HOT_API, params=None)
    if not data:
        return []
    ids = data.get("movieIds") or []
    result = []
    for x in ids:
        try:
            result.append(int(x))
        except Exception:
            continue
    return result


def get_coming_movie_ids(city_id: int = 1, limit: int = 200):
    params = {"ci": int(city_id), "token": "", "limit": int(limit)}
    data = safe_json_request_allow_400(COMING_API, params=params)
    if not data:
        return []
    ids = data.get("movieIds") or []
    result = []
    for x in ids:
        try:
            result.append(int(x))
        except Exception:
            continue
    return result


def parse_filename_from_url(url: str) -> str:
    try:
        p = urlparse(url)
        base = os.path.basename(p.path)
        if not base:
            return ""
        base = base.split("?")[0]
        base = base.split("#")[0]
        return base
    except Exception:
        return ""


def download_file(url, folder, filename):
    path = os.path.join(folder, filename)
    if os.path.exists(path):
        return True

    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code == 200:
            with open(path, "wb") as f:
                f.write(r.content)
            return True
    except Exception:
        pass
    return False


def get_movie_detail(movie_id: int):
    data = safe_json_request(DETAIL_API, params={"movieId": movie_id})
    if not data:
        return None

    detail = data.get("detailMovie") or {}
    title = detail.get("nm") or ""
    score = detail.get("sc")
    score = "" if score is None else str(score)

    photos = detail.get("photos") or []
    cover = detail.get("img")
    if cover:
        photos = [cover] + photos

    cleaned = []
    for u in photos:
        if not u or not isinstance(u, str):
            continue
        cleaned.append(u.strip())

    return {
        "id": str(movie_id),
        "title": title,
        "score": score,
        "photos": cleaned,
    }


def start_download(movie_ids_text: str = "", cookie: str = ""):
    global is_running

    cookie = (cookie or "").strip()
    HEADERS["Cookie"] = cookie
    save_last_cookie(cookie)

    if not is_running:
        is_running = True
        pause_event.set()
        threading.Thread(target=worker_main, args=(movie_ids_text,), daemon=True).start()
        log("🚀 任务启动...")


def pause_download():
    pause_event.clear()
    log("⏸ 已暂停任务")


def resume_download():
    pause_event.set()
    log("▶ 继续任务")


def _parse_movie_ids(text: str):
    if not text:
        return [], []
    raw = re.split(r"[，,\s]+", text.strip())
    ids = []
    keywords = []
    for t in raw:
        if not t:
            continue
        if not re.fullmatch(r"\d+", t):
            keywords.append(t)
            continue
        ids.append(int(t))
    return ids, keywords


def worker_main(movie_ids_text: str):
    global is_running, current_movie_id

    load_record()

    auto_text = (movie_ids_text or "").strip()
    if auto_text == "__AUTO_HOT__":
        log("🔍 正在获取猫眼：正在热映列表...")
        ids = get_hot_movie_ids()
        keywords = []
        log(f"📋 热映电影数量：{len(ids)}")
        random_sleep(3, 6)
    elif auto_text.startswith("__AUTO_COMING__"):
        city_id = 1
        limit = 200
        m_city = re.search(r"city=(\d+)", auto_text)
        if m_city:
            try:
                city_id = int(m_city.group(1))
            except Exception:
                pass
        m_limit = re.search(r"limit=(\d+)", auto_text)
        if m_limit:
            try:
                limit = int(m_limit.group(1))
            except Exception:
                pass
        log(f"🔍 正在获取猫眼：即将上映列表... cityId={city_id} limit={limit}")
        ids = get_coming_movie_ids(city_id=city_id, limit=limit)
        keywords = []
        log(f"📋 即将上映电影数量：{len(ids)}")
        random_sleep(3, 6)
    else:
        ids, keywords = _parse_movie_ids(movie_ids_text)
    if keywords:
        for kw in keywords:
            cands = search_movie_candidates(kw)
            if not cands:
                log(f"❌ 未搜索到：{kw}")
                continue
            chosen = cands[0]
            ids.append(chosen["id"])
            log(f"🔎 搜索《{kw}》 → movieId={chosen['id']} {chosen.get('title','')}")
            random_sleep(2, 4)

    # 去重保持顺序
    seen = set()
    uniq_ids = []
    for x in ids:
        if x in seen:
            continue
        seen.add(x)
        uniq_ids.append(x)
    ids = uniq_ids

    if not ids:
        log("⚠ 未提供 movieId/片名（可输入如：1, 1294273, 1491059 或 霸王别姬）")
        is_running = False
        return

    for mid in ids:
        if not is_running:
            return

        while not pause_event.is_set():
            time.sleep(1)

        current_movie_id = mid

        with record_lock:
            is_completed = str(mid) in record.get("completed", {})
        if is_completed:
            log(f"[maoyan]movieId={mid} ✔")
            continue

        detail = get_movie_detail(mid)
        if not detail:
            log(f"❌ 获取详情失败 movieId={mid}")
            random_sleep(30, 60)
            continue

        title = detail["title"] or f"movie_{mid}"
        score = detail.get("score", "")
        safe_title = clean_filename(title) or f"movie_{mid}"

        save_path = os.path.join(SAVE_DIR, safe_title)
        os.makedirs(save_path, exist_ok=True)

        with record_lock:
            record["photos"].setdefault(str(mid), [])
            known = set(record["photos"].get(str(mid), []))

        log(f"🎬 正在处理：{title} ({score}) movieId={mid}")

        new_cnt = 0
        skip_cnt = 0
        fail_cnt = 0

        photos = detail.get("photos") or []
        if not photos:
            log("  ℹ 未返回 photos，可能无图或被限制")
            mark_movie_completed(mid, title, score)
            save_record()
            continue

        # 如果所有 URL 都已记录，直接完成
        all_known = True
        for u in photos:
            if u not in known:
                all_known = False
                break
        if all_known:
            mark_movie_completed(mid, title, score)
            log(f"[maoyan]{title} ✔")
            save_record()
            continue

        for idx, url in enumerate(photos):
            if not is_running:
                return

            while not pause_event.is_set():
                time.sleep(1)

            with record_lock:
                if url in record["photos"][str(mid)]:
                    skip_cnt += 1
                    continue

            filename = parse_filename_from_url(url)
            if not filename:
                filename = f"{idx + 1}.jpg"

            if download_file(url, save_path, filename):
                rel_path = os.path.relpath(os.path.join(save_path, filename), SAVE_DIR)
                rel_path = rel_path.replace("\\", "/")
                log(f"[maoyan]{rel_path} ✔")

                new_cnt += 1

                with record_lock:
                    record["photos"][str(mid)].append(url)

                    today = today_key()
                    record["daily"].setdefault(today, 0)
                    record["daily"][today] += 1

                save_record()
            else:
                stats["fails"] += 1
                fail_cnt += 1

            random_sleep(10, 25)

        log(f"✅ 《{title}》处理完成：新增 {new_cnt}，跳过 {skip_cnt}，失败 {fail_cnt}")
        if new_cnt == 0 and fail_cnt == 0:
            mark_movie_completed(mid, title, score)
        else:
            mark_movie_completed(mid, title, score)
        save_record()

        random_sleep(60, 120)

    is_running = False
    log("🏁 全部 movieId 处理完成")


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Maoyan 图片下载器（佛系稳定版）")
        self.root.geometry("1100x650")

        main = tk.Frame(root)
        main.pack(fill="both", expand=True)

        left = tk.Frame(main)
        left.pack(side="left", fill="both", expand=True)

        right = tk.Frame(main, width=360)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)

        tk.Label(left, text="日志一览", anchor="w").pack(fill="x", padx=10, pady=(10, 0))
        self.log_area = scrolledtext.ScrolledText(left)
        self.log_area.pack(fill="both", expand=True, padx=10, pady=10)

        tk.Label(right, text="下载来源:").pack(anchor="w", padx=10, pady=(10, 0))
        self.var_source = tk.StringVar(value="手动输入")
        self.opt_source = tk.OptionMenu(
            right,
            self.var_source,
            "手动输入",
            "正在热映",
            "即将上映",
        )
        self.opt_source.pack(fill="x", padx=10)

        row_city = tk.Frame(right)
        row_city.pack(fill="x", padx=10, pady=(6, 0))
        tk.Label(row_city, text="cityId:").pack(side="left")
        self.var_city = tk.IntVar(value=1)
        self.txt_city = tk.Entry(row_city, width=8)
        self.txt_city.pack(side="left", padx=(6, 0))
        self.txt_city.insert(0, "1")
        tk.Label(row_city, text="limit:").pack(side="left", padx=(12, 0))
        self.txt_limit = tk.Entry(row_city, width=8)
        self.txt_limit.pack(side="left", padx=(6, 0))
        self.txt_limit.insert(0, "200")

        tk.Label(right, text="movieId / 片名（逗号/空格分隔）:").pack(anchor="w", padx=10, pady=(10, 0))
        self.txt_movie_ids = tk.Entry(right)
        self.txt_movie_ids.pack(fill="x", padx=10)

        tk.Label(right, text="Cookie（可不填，自动记住上次）:").pack(anchor="w", padx=10, pady=(10, 0))
        self.txt_cookie = tk.Entry(right)
        self.txt_cookie.pack(fill="x", padx=10)

        last_cookie = load_last_cookie()
        if last_cookie:
            self.txt_cookie.insert(0, last_cookie)

        frame_btn = tk.Frame(right)
        frame_btn.pack(fill="x", padx=10, pady=10)

        self.btn_start = tk.Button(frame_btn, text="开始", width=12, command=self.start)
        self.btn_start.pack(side="left", padx=(0, 10))

        self.btn_pause = tk.Button(frame_btn, text="暂停", width=12, command=self.pause)
        self.btn_pause.pack(side="left", padx=(0, 10))

        self.btn_resume = tk.Button(frame_btn, text="继续", width=12, command=self.resume)
        self.btn_resume.pack(side="left")

        self.lbl_total_photos = tk.Label(right, text="已记住图片数：0 张", fg="green", anchor="w")
        self.lbl_total_photos.pack(fill="x", padx=10, pady=(5, 0))

        self.lbl_total_movies = tk.Label(right, text="已记住电影数量：0 部", fg="green", anchor="w")
        self.lbl_total_movies.pack(fill="x", padx=10, pady=(5, 0))

        self.lbl_current_movie = tk.Label(right, text="当前电影已下载：0 张", fg="blue", anchor="w")
        self.lbl_current_movie.pack(fill="x", padx=10, pady=(5, 0))

        self.lbl_today = tk.Label(right, text="今日新增：0 张", fg="purple", anchor="w")
        self.lbl_today.pack(fill="x", padx=10, pady=(5, 0))

        self.update_ui()

    def log(self, msg):
        t = time.strftime("%H:%M:%S")
        self.log_area.insert(tk.END, f"[{t}] {msg}\n")
        self.log_area.see(tk.END)

    def start(self):
        cookie = self.txt_cookie.get().strip()
        source = (self.var_source.get() or "").strip()
        if source == "正在热映":
            start_download(movie_ids_text="__AUTO_HOT__", cookie=cookie)
            return
        if source == "即将上映":
            city_id = (self.txt_city.get() or "1").strip()
            limit = (self.txt_limit.get() or "200").strip()
            start_download(
                movie_ids_text=f"__AUTO_COMING__ city={city_id} limit={limit}",
                cookie=cookie,
            )
            return

        movie_ids = self.txt_movie_ids.get().strip()
        start_download(movie_ids_text=movie_ids, cookie=cookie)

    def pause(self):
        pause_download()

    def resume(self):
        resume_download()

    def update_ui(self):
        self.lbl_total_photos.config(text=f"已记住图片数：{get_total_recorded_photos()} 张")
        self.lbl_total_movies.config(text=f"已记住电影数量：{get_total_recorded_movies()} 部")
        self.lbl_current_movie.config(text=f"当前电影已下载：{get_current_movie_count()} 张")
        self.lbl_today.config(text=f"今日新增：{get_today_count()} 张")

        self.root.after(1000, self.update_ui)


if __name__ == "__main__":
    root = tk.Tk()
    app_instance = App(root)
    root.mainloop()
