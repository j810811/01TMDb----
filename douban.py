import requests
import os
import json
import time
import re
import threading
import random
import tkinter as tk
from tkinter import scrolledtext
from bs4 import BeautifulSoup
from datetime import datetime

# ============================
# ✅ 基本配置（你只需要改这里）
# copy(document.cookie)

# ============================

SAVE_DIR = r"D:\TMDB_剧照库"
MIN_DELAY = 10.0
MAX_DELAY = 25.0

COOKIE_FILE = "last_cookie.txt"
RECORD_FILE = "douban_downloaded.json"

SEARCH_API = "https://movie.douban.com/j/search_subjects"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Referer": "https://movie.douban.com/tv/#!type=variety",
    "Accept": "application/json, text/plain, */*",
    "Cookie": "",
}

# ============================
# 全局状态
# ============================

record = {"photos": {}, "daily": {}}
record_lock = threading.Lock()

is_running = False
pause_event = threading.Event()
pause_event.set()

current_subject_id = None  # 当前正在处理的综艺
stats = {"fails": 0}

app_instance = None
_log_hook = None

# ============================
# ✅ 通用工具
# ============================


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


def start_download(cookie: str = ""):
    global is_running

    cookie = (cookie or "").strip()
    HEADERS["Cookie"] = cookie
    save_last_cookie(cookie)

    if not is_running:
        is_running = True
        pause_event.set()
        threading.Thread(target=worker_main, daemon=True).start()
        log("🚀 任务启动...")


def pause_download():
    pause_event.clear()
    log("⏸ 已暂停任务")


def resume_download():
    pause_event.set()
    log("▶ 继续任务")


def random_sleep(a=MIN_DELAY, b=MAX_DELAY):
    time.sleep(random.uniform(a, b))


def today_key():
    return datetime.now().strftime("%Y-%m-%d")


# ============================
# ✅ 记住 Cookie
# ============================


def load_last_cookie():
    if os.path.exists(COOKIE_FILE):
        try:
            with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                return f.read().strip()
        except:
            return ""
    return ""


def save_last_cookie(cookie):
    try:
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            f.write(cookie)
    except:
        pass


# ============================
# ✅ 记录已下载图片（断点续爬 + 今日统计）
# ============================


def load_record():
    global record
    if os.path.exists(RECORD_FILE):
        try:
            with open(RECORD_FILE, "r", encoding="utf-8") as f:
                record = json.load(f)
        except:
            pass

    if "photos" not in record:
        record["photos"] = {}
    if "daily" not in record:
        record["daily"] = {}


def save_record():
    with record_lock:
        with open(RECORD_FILE, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)


# ============================
# ✅ 统计函数（你要求的 4 大核心统计）
# ============================


def get_total_recorded_photos():
    total = 0
    with record_lock:
        for sid in record.get("photos", {}):
            total += len(record["photos"][sid])
    return total


def get_total_recorded_subjects():
    with record_lock:
        return len(record.get("photos", {}))


def get_current_subject_count():
    if not current_subject_id:
        return 0
    with record_lock:
        return len(record["photos"].get(current_subject_id, []))


def get_today_count():
    today = today_key()
    with record_lock:
        return record.get("daily", {}).get(today, 0)


# ============================
# ✅ 安全请求
# ============================


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


def safe_html_request(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code == 200:
            return r.text
        hint = ""
        if r.status_code in (301, 302, 401, 403):
            hint = "（可能 Cookie 无效/缺失）"
        log(f"❌ 请求错误 {r.status_code}{hint}: {url}")
    except Exception as e:
        log(f"❌ 请求异常: {e}")
    return ""


# ============================
# 综艺列表（网页端稳定接口）
# ============================


def get_variety_subjects(page=0):
    params = {"type": "tv", "tag": "综艺", "page_limit": 20, "page_start": page * 20}

    data = safe_json_request(SEARCH_API, params=params)
    if not data:
        return []

    subjects = data.get("subjects", [])
    result = []

    for item in subjects:
        result.append(
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "rate": item.get("rate", ""),
            }
        )

    return result


# ============================
# ✅ 剧照解析
# ============================


def get_photos_page(subject_id, start=0):
    url = f"https://movie.douban.com/subject/{subject_id}/photos?type=S&start={start}"
    html = safe_html_request(url)

    if not html:
        return None, False

    soup = BeautifulSoup(html, "html.parser")
    imgs = soup.select("ul.poster-col3 li img")

    result = []
    for img in imgs:
        src = img.get("src")
        if src:
            large = src.replace("/m/public/", "/l/public/")
            pid = large.split("/")[-1]
            result.append((pid, large))

    has_next = bool(soup.select_one("span.next a"))
    return result, has_next


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
    except:
        pass
    return False


# ============================
# ✅ 主工作线程（佛系稳定）
# ============================


def worker_main():
    global is_running, current_subject_id

    load_record()
    page = 0

    while is_running:
        log(f"🔍 正在扫描综艺列表 第 {page + 1} 页...")
        subjects = get_variety_subjects(page)

        if not subjects:
            log("⚠ 本页无数据，进入佛系休眠...")
            random_sleep(60, 120)
            continue

        for subj in subjects:
            if not is_running:
                return

            while not pause_event.is_set():
                time.sleep(1)

            sid = subj["id"]
            current_subject_id = sid
            title = subj["title"]
            rate = subj["rate"]

            safe_title = re.sub(r'[\\/:*?"<>|]', "", title)
            save_path = os.path.join(SAVE_DIR, safe_title)
            os.makedirs(save_path, exist_ok=True)

            with record_lock:
                if sid not in record["photos"]:
                    record["photos"][sid] = []

            log(f"🎬 正在处理：{title} ({rate})")

            new_cnt = 0
            skip_cnt = 0
            fail_cnt = 0
            pages_cnt = 0

            start = 0
            has_next = True

            while has_next and is_running:
                photos, has_next = get_photos_page(sid, start)

                if not photos:
                    if start == 0:
                        log("  ℹ 未获取到剧照列表（可能无剧照/被限制/需要有效 Cookie）")
                    break

                pages_cnt += 1

                for pid, url in photos:
                    with record_lock:
                        if url in record["photos"][sid]:
                            skip_cnt += 1
                            continue

                    if download_file(url, save_path, pid):
                        log(f"  ✔ 下载成功: {pid}")

                        new_cnt += 1

                        with record_lock:
                            record["photos"][sid].append(url)

                            today = today_key()
                            record["daily"].setdefault(today, 0)
                            record["daily"][today] += 1

                        save_record()
                    else:
                        stats["fails"] += 1
                        fail_cnt += 1

                    random_sleep(10, 25)

                start += 30
                random_sleep(20, 40)

            log(
                f"✅ 《{title}》处理完成：新增 {new_cnt}，跳过 {skip_cnt}，失败 {fail_cnt}，扫描页 {pages_cnt}"
            )
            random_sleep(60, 120)

        page += 1


# ============================
# ✅ GUI 主界面
# ============================


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Douban 综艺剧照下载器（终极佛系稳定版）")
        self.root.geometry("1100x650")

        main = tk.Frame(root)
        main.pack(fill="both", expand=True)

        left = tk.Frame(main)
        left.pack(side="left", fill="both", expand=True)

        right = tk.Frame(main, width=320)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)

        tk.Label(left, text="日志一览", anchor="w").pack(fill="x", padx=10, pady=(10, 0))
        self.log_area = scrolledtext.ScrolledText(left)
        self.log_area.pack(fill="both", expand=True, padx=10, pady=10)

        tk.Label(right, text="Cookie（自动记住上次，可不填）:").pack(anchor="w", padx=10, pady=(10, 0))
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
        self.btn_pause.pack(side="left")

        self.lbl_total_photos = tk.Label(
            right, text="已记住图片数：0 张", fg="green", anchor="w"
        )
        self.lbl_total_photos.pack(fill="x", padx=10, pady=(5, 0))

        self.lbl_total_subjects = tk.Label(
            right, text="已记住综艺数量：0 部", fg="green", anchor="w"
        )
        self.lbl_total_subjects.pack(fill="x", padx=10, pady=(5, 0))

        self.lbl_current_subject = tk.Label(
            right, text="当前节目已下载：0 张", fg="blue", anchor="w"
        )
        self.lbl_current_subject.pack(fill="x", padx=10, pady=(5, 0))

        self.lbl_today = tk.Label(right, text="今日新增：0 张", fg="purple", anchor="w")
        self.lbl_today.pack(fill="x", padx=10, pady=(5, 0))

        self.update_ui()

    def log(self, msg):
        t = time.strftime("%H:%M:%S")
        self.log_area.insert(tk.END, f"[{t}] {msg}\n")
        self.log_area.see(tk.END)

    def start(self):
        global is_running

        cookie = self.txt_cookie.get().strip()
        HEADERS["Cookie"] = cookie
        save_last_cookie(cookie)

        if not is_running:
            is_running = True
            pause_event.set()
            threading.Thread(target=worker_main, daemon=True).start()
            self.log("🚀 任务启动...")

    def pause(self):
        pause_event.clear()
        self.log("⏸ 已暂停任务")

    def update_ui(self):
        self.lbl_total_photos.config(
            text=f"已记住图片数：{get_total_recorded_photos()} 张"
        )
        self.lbl_total_subjects.config(
            text=f"已记住综艺数量：{get_total_recorded_subjects()} 部"
        )
        self.lbl_current_subject.config(
            text=f"当前节目已下载：{get_current_subject_count()} 张"
        )
        self.lbl_today.config(text=f"今日新增：{get_today_count()} 张")

        self.root.after(1000, self.update_ui)


# ============================
# ✅ 程序入口
# ============================

if __name__ == "__main__":
    root = tk.Tk()
    app_instance = App(root)
    root.mainloop()
