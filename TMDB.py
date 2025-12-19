import requests
import os
import json
import time
import re
import threading
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from tkinter import scrolledtext, ttk
import sys

# ============================
# 配置区
# ============================
API_KEY = "bfc7e56904a3869b552abc6f4e9eb3b4"
SAVE_DIR = r"D:\TMDB_剧照库"

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

RECORD_FILE = os.path.join(BASE_DIR, "downloaded.json")

MAX_WORKERS = 8
POPULAR_MAX_PAGES = 500
MODE = "popular"

BASE_URL = "https://api.themoviedb.org/3"

# ============================
# 全局状态
# ============================
record = None
record_lock = threading.Lock()

session_new_movies = []
session_new_images = 0
session_movie_new_images = {}

pause_requested = False
is_downloading = False
download_thread = None
state_lock = threading.Lock()


# ============================
# GUI
# ============================
class LoggerWindow:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("TMDB 剧照下载器（实时显示 + 暂停/继续 + 实时统计）")
        self.root.geometry("1200x700")

        main_frame = tk.Frame(self.root)
        main_frame.pack(fill="both", expand=True)

        # 左侧日志区
        left_frame = tk.Frame(main_frame)
        left_frame.pack(side="left", fill="both", expand=True)

        self.txt = scrolledtext.ScrolledText(
            left_frame, width=110, height=40, font=("Consolas", 11)
        )
        self.txt.pack(fill="both", expand=True, padx=5, pady=5)

        # 右侧统计 + 按钮区
        right_frame = tk.Frame(main_frame, width=260, relief="groove", borderwidth=2)
        right_frame.pack(side="right", fill="y")

        lbl_title = tk.Label(
            right_frame, text="实时统计", font=("微软雅黑", 14, "bold")
        )
        lbl_title.pack(pady=10)

        self.lbl_new_movies = tk.Label(right_frame, text="本次新增电影：0")
        self.lbl_new_movies.pack(anchor="w", padx=10, pady=5)

        self.lbl_new_images = tk.Label(right_frame, text="本次新增剧照：0")
        self.lbl_new_images.pack(anchor="w", padx=10, pady=5)

        ttk.Separator(right_frame, orient="horizontal").pack(fill="x", pady=10)

        self.lbl_total_movies = tk.Label(right_frame, text="累计电影：0")
        self.lbl_total_movies.pack(anchor="w", padx=10, pady=5)

        self.lbl_total_images = tk.Label(right_frame, text="累计剧照：0")
        self.lbl_total_images.pack(anchor="w", padx=10, pady=5)

        ttk.Separator(right_frame, orient="horizontal").pack(fill="x", pady=15)

        # ✅ 按钮现在放到右侧
        self.btn_start = tk.Button(right_frame, text="开始下载", width=14)
        self.btn_start.pack(pady=5)

        self.btn_pause = tk.Button(right_frame, text="暂停", width=14)
        self.btn_pause.pack(pady=5)

        self.btn_resume = tk.Button(right_frame, text="继续", width=14)
        self.btn_resume.pack(pady=5)

        self.root.after(500, self.refresh_stats)

    def set_handlers(self, start_cb, pause_cb, resume_cb):
        self.btn_start.config(command=start_cb)
        self.btn_pause.config(command=pause_cb)
        self.btn_resume.config(command=resume_cb)

    def log(self, msg):
        def _write():
            self.txt.insert(tk.END, msg + "\n")
            self.txt.see(tk.END)

        self.root.after(0, _write)

    def refresh_stats(self):
        global session_new_movies, session_new_images, record

        self.lbl_new_movies.config(text=f"本次新增电影：{len(session_new_movies)}")
        self.lbl_new_images.config(text=f"本次新增剧照：{session_new_images}")

        if record is not None:
            with record_lock:
                total_movies = len(record["movie_ids"])
                total_images = sum(len(v) for v in record["images"].values())
        else:
            total_movies = total_images = 0

        self.lbl_total_movies.config(text=f"累计电影：{total_movies}")
        self.lbl_total_images.config(text=f"累计剧照：{total_images}")

        self.root.after(500, self.refresh_stats)

    def start(self):
        self.root.mainloop()


logger = None
_log_hook = None


# ============================
# 日志
# ============================
def set_log_hook(hook):
    global _log_hook
    _log_hook = hook


def log(msg):
    print(msg)
    if _log_hook:
        _log_hook(msg)
        return
    if logger:
        logger.log(msg)


# ============================
# 工具函数
# ============================
def clean_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "", name)


def safe_get(url, params=None, stream=False):
    wait = 2
    while True:
        try:
            r = requests.get(url, params=params, stream=stream, timeout=30)
        except Exception as e:
            log(f"📡 网络错误：{e} → {wait}s 后重试")
            time.sleep(wait)
            wait = min(wait * 2, 60)
            continue

        if r.status_code == 200:
            return r

        if r.status_code in (429, 503):
            log(f"⏳ 限速 {r.status_code} → 等待 {wait}s")
            time.sleep(wait)
            wait = min(wait * 2, 60)
            continue

        log(f"❌ API 错误 {r.status_code} → 3s 后重试")
        time.sleep(3)


def load_record():
    if os.path.exists(RECORD_FILE):
        try:
            with open(RECORD_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            log("⚠ JSON 损坏，重新创建")
    return {"movie_ids": [], "images": {}}


def save_record_safe():
    if record is None:
        return
    with record_lock:
        with open(RECORD_FILE, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2, ensure_ascii=False)


# ============================
# 下载单张图片
# ============================
def download_one_image(job):
    img_url = job["img_url"]
    save_path = job["save_path"]
    mid = job["movie_id_str"]
    fp = job["file_path"]

    try:
        img_data = safe_get(img_url, stream=True).content
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        with open(save_path, "wb") as f:
            f.write(img_data)

        with record_lock:
            record["images"][mid].append(fp)

        log("  ✔ 已保存：" + save_path)
    except Exception as e:
        log(f"  ❌ 下载失败：{img_url} 错误：{e}")


# ============================
# 下载一部电影
# ============================
def download_movie_images(movie_id, title):
    global record, session_new_images, pause_requested

    mid_str = str(movie_id)
    safe_title = clean_filename(title)

    movie_dir = os.path.join(SAVE_DIR, safe_title)
    raw_dir = os.path.join(movie_dir, "raw")
    os.makedirs(raw_dir, exist_ok=True)

    with record_lock:
        record["images"].setdefault(mid_str, [])

    log(f"\n🎬 《{title}》")

    url = f"{BASE_URL}/movie/{movie_id}/images"
    resp = safe_get(url, params={"api_key": API_KEY})
    images = resp.json().get("backdrops", [])

    jobs = []
    with record_lock:
        existing = set(record["images"][mid_str])

    for img in images:
        if pause_requested:
            break

        fp = img["file_path"]
        if fp in existing:
            continue

        img_url = "https://image.tmdb.org/t/p/original" + fp
        save_path = os.path.join(raw_dir, fp.replace("/", ""))
        jobs.append(
            {
                "img_url": img_url,
                "save_path": save_path,
                "movie_id_str": mid_str,
                "file_path": fp,
            }
        )

    if not jobs:
        log("  ⏭ 无新剧照")
        return True

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for _ in ex.map(download_one_image, jobs):
            pass

    session_new_images += len(jobs)
    return True


# ============================
# 热门模式
# ============================
def run_popular_mode():
    global record, session_new_movies, pause_requested

    for page in range(1, POPULAR_MAX_PAGES + 1):
        if pause_requested:
            return

        resp = safe_get(
            f"{BASE_URL}/movie/popular", params={"api_key": API_KEY, "page": page}
        )
        movies = resp.json().get("results", [])

        for m in movies:
            if pause_requested:
                return

            movie_id = m["id"]
            title = m.get("title") or "无标题"

            with record_lock:
                if movie_id in record["movie_ids"]:
                    continue

            ok = download_movie_images(movie_id, title)
            if ok:
                with record_lock:
                    record["movie_ids"].append(movie_id)
                session_new_movies.append(title)
                save_record_safe()


# ============================
# 下载线程
# ============================
def download_worker():
    global is_downloading, pause_requested, record

    with state_lock:
        is_downloading = True

    os.makedirs(SAVE_DIR, exist_ok=True)

    if record is None:
        with record_lock:
            record = load_record()

    if MODE == "popular":
        run_popular_mode()

    save_record_safe()

    with state_lock:
        is_downloading = False


# ============================
# 按钮逻辑
# ============================
def start_download():
    global download_thread, pause_requested
    with state_lock:
        if is_downloading:
            return
        pause_requested = False

    download_thread = threading.Thread(target=download_worker, daemon=True)
    download_thread.start()
    log("▶ 开始下载")


def pause_download():
    global pause_requested
    pause_requested = True
    save_record_safe()
    log("⏸ 已暂停")


def resume_download():
    global download_thread, pause_requested
    with state_lock:
        if is_downloading:
            return
        pause_requested = False

    download_thread = threading.Thread(target=download_worker, daemon=True)
    download_thread.start()
    log("▶ 继续下载")


# ============================
# 主入口
# ============================
def main():
    global logger
    logger = LoggerWindow()
    set_log_hook(logger.log)
    logger.set_handlers(start_download, pause_download, resume_download)
    log("👋 TMDB 剧照下载器启动完成")
    logger.start()


if __name__ == "__main__":
    main()
