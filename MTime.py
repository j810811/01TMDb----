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
from bs4 import BeautifulSoup
import difflib
import random


# ============================
# 配置参数
# ============================

API_KEY = "bfc7e56904a3869b552abc6f4e9eb3b4"
SAVE_DIR = r"D:\TMDB_剧照库"

MAX_WORKERS = 1  # 降低并发数以避免触发 MTime 限流

# 模式：
#   "popular"   -> TMDB 热门电影
#   "zh_movies" -> TMDB 中文电影（原始语言为中文），并联动 MTime
MODE = "zh_movies"  # ★ 按你选择：只抓中文电影

POPULAR_MAX_PAGES = 500
CHINESE_MAX_PAGES = 500  # 中文电影最多抓多少页

BASE_URL = "https://api.themoviedb.org/3"
IMG_BASE = "https://image.tmdb.org/t/p/original"

# HTTP 头
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}


# ============================
# 目录 & JSON
# ============================

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

RECORD_FILE = os.path.join(BASE_DIR, "downloaded.json")
FAILED_FILE = os.path.join(BASE_DIR, "failed_downloads.json")  # 失败记录文件


# ============================
# 全局状态 & 统计
# ============================

record = None
record_lock = threading.Lock()

session_new_movies = []  # 本次新增电影列表
session_new_images = 0  # 本次新增图片总数
session_movie_new_images = {}  # 每部电影新增图片数（目前未用到，但保留）

pause_requested = False
is_downloading = False
download_thread = None

state_lock = threading.Lock()
list_file_lock = threading.Lock()  # 文件读写锁
is_refreshing = False  # 刷新状态标志

enable_tmdb_download = False
enable_mtime_download = True

def is_tmdb_enabled() -> bool:
    try:
        if logger is not None and getattr(logger, "var_tmdb", None) is not None:
            return bool(logger.var_tmdb.get())
    except Exception:
        pass
    return bool(enable_tmdb_download)


def is_mtime_enabled() -> bool:
    try:
        if logger is not None and getattr(logger, "var_mtime", None) is not None:
            return bool(logger.var_mtime.get())
    except Exception:
        pass
    return bool(enable_mtime_download)

# 统计用
tmdb_ok = 0
tmdb_fail = 0
mtime_ok = 0
mtime_fail = 0

# 连续失败检测与自动暂停
consecutive_fails = 0  # 连续失败计数
CONSECUTIVE_FAIL_THRESHOLD = 5  # 连续失败多少次触发自动暂停
AUTO_PAUSE_DURATION = 3600  # 自动暂停时长（秒），60分钟
last_success_time = None  # 上次成功时间

# ============================
# GUI
# ============================


class LoggerWindow:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("TMDB + MTime 剧照下载器 Pro (3列并行版)")
        self.root.geometry("1400x800")

        main_frame = tk.Frame(self.root)
        main_frame.pack(fill="both", expand=True)

        # 使用 PanedWindow 管理日志列
        paned = tk.PanedWindow(main_frame, orient="horizontal", sashrelief="raised")
        paned.pack(side="left", fill="both", expand=True)

        # 1. 合并日志列（MTime + 列表刷新/系统日志）
        frame_log = tk.Frame(paned)
        tk.Label(
            frame_log,
            text="日志",
            font=("微软雅黑", 10, "bold"),
            bg="#e1f5fe",
        ).pack(fill="x")
        self.txt_log = scrolledtext.ScrolledText(
            frame_log, width=80, height=40, font=("Consolas", 9)
        )
        self.txt_log.pack(fill="both", expand=True)
        # 双击下载列表中的行，尝试用系统看图软件打开其中的图片路径
        self.txt_log.bind("<Double-1>", self.on_mtime_double_click)
        paned.add(frame_log)

        # 兼容旧字段：历史代码里仍然使用 txt_mtime/txt_refresh/txt_tmdb
        self.txt_mtime = self.txt_log
        self.txt_refresh = self.txt_log
        self.txt_tmdb = self.txt_log

        # 右侧统计与控制
        right = tk.Frame(main_frame, width=250, relief="groove", borderwidth=2)
        right.pack(side="right", fill="y")

        tk.Label(right, text="实时统计", font=("微软雅黑", 14, "bold")).pack(pady=10)

        # 按钮区域
        btns = tk.Frame(right)
        btns.pack(fill="x", pady=10)

        # 勾选框区域
        checks = tk.Frame(right)
        checks.pack(fill="x", pady=5)

        # 这里的“下载 TMDB”勾选框保留，但现在 TMDB 已不再下载图片，仅保留 UI
        self.var_tmdb = tk.BooleanVar(
            value=False
        )  # 默认不下载 TMDB（即使勾选，也不会真的下载图片）
        self.chk_tmdb = tk.Checkbutton(
            checks, text="下载 TMDB（已禁用）", variable=self.var_tmdb
        )
        # 不再显示 TMDB 勾选框
        # self.chk_tmdb.pack(anchor="w", padx=10)

        self.var_mtime = tk.BooleanVar(value=True)  # 默认下载 MTime
        self.chk_mtime = tk.Checkbutton(
            checks, text="下载 MTime", variable=self.var_mtime
        )
        self.chk_mtime.pack(anchor="w", padx=10)

        self.btn_start = tk.Button(
            btns, text="开始下载", width=12, bg="#4caf50", fg="white"
        )
        self.btn_start.pack(padx=10, pady=5)

        self.btn_pause = tk.Button(
            btns, text="暂停", width=12, bg="#ff9800", fg="white"
        )
        self.btn_pause.pack(padx=10, pady=5)

        self.btn_resume = tk.Button(
            btns, text="继续", width=12, bg="#2196f3", fg="white"
        )
        self.btn_resume.pack(padx=10, pady=5)

        self.btn_refresh = tk.Button(
            btns, text="刷新列表", width=12, bg="#9c27b0", fg="white"
        )
        self.btn_refresh.pack(padx=10, pady=5)

        self.btn_retry = tk.Button(
            btns, text="重试失败", width=12, bg="#e91e63", fg="white"
        )
        self.btn_retry.pack(padx=10, pady=5)

        self.lbl_new_movies = tk.Label(right, text="本次新增电影：0")
        self.lbl_new_movies.pack(anchor="w", padx=10, pady=5)

        self.lbl_tmdb_ok = tk.Label(right, text="TMDB 成功：0")
        # 不再在界面上显示 TMDB 成功统计
        # self.lbl_tmdb_ok.pack(anchor="w", padx=10, pady=5)

        self.lbl_tmdb_fail = tk.Label(right, text="TMDB 失败：0")
        # 不再在界面上显示 TMDB 失败统计
        # self.lbl_tmdb_fail.pack(anchor="w", padx=10, pady=5)

        self.lbl_mtime_ok = tk.Label(right, text="MTime 成功：0")
        self.lbl_mtime_ok.pack(anchor="w", padx=10, pady=5)

        self.lbl_mtime_fail = tk.Label(right, text="MTime 失败：0")
        self.lbl_mtime_fail.pack(anchor="w", padx=10, pady=5)

        self.lbl_pending_retry = tk.Label(right, text="待重试：0")
        self.lbl_pending_retry.pack(anchor="w", padx=10, pady=5)

        self.root.after(500, self.refresh_stats)

    def on_mtime_double_click(self, event):
        """在 MTime 日志中双击一行时，如果该行包含本地文件路径，则尝试用系统默认程序打开。"""

        self.log("[预览调试] 双击事件已触发", category="refresh")
        try:
            index = self.txt_log.index(f"@{event.x},{event.y}")
            line_no = int(index.split(".")[0])
            line_text = self.txt_log.get(f"{line_no}.0", f"{line_no + 1}.0")

            # 调试：把当前双击行输出到系统日志，便于查看实际格式
            self.log(f"[预览调试] 双击第 {line_no} 行: {line_text.strip()}", category="refresh")

            # 1) 先尝试直接从当前行提取完整路径，如：D:\folder\file.jpg
            m = re.search(r"[A-Za-z]:\\[^\n\r]+", line_text)
            if m:
                path = m.group(0).strip()
            else:
                # 2) 如果当前行只有文件名，如 "7663476.jpg"，则尝试从上一行找目录
                name_match = re.search(r"([^\\\s]+\.(?:jpg|jpeg|png|bmp|gif))", line_text, re.IGNORECASE)
                if not name_match or line_no <= 1:
                    self.log("[预览调试] 当前行未识别到文件名或无上一行，放弃预览", category="refresh")
                    return

                filename = name_match.group(1)
                prev_text = self.txt_log.get(f"{line_no-1}.0", f"{line_no}.0")
                self.log(f"[预览调试] 上一行为: {prev_text.strip()}", category="refresh")
                prev_path_match = re.search(r"[A-Za-z]:\\\\[^\n\r]+", prev_text)
                if not prev_path_match:
                    self.log("[预览调试] 上一行未识别到完整路径", category="refresh")
                    return

                prev_full = prev_path_match.group(0).strip()
                directory = os.path.dirname(prev_full)
                path = os.path.join(directory, filename)

            if not os.path.exists(path):
                self.log(f"[预览调试] 路径不存在: {path}", category="refresh")
                return

            try:
                os.startfile(path)
            except Exception as e:
                # 用系统日志列提示错误
                self.log(f"⚠ 打开图片失败：{e}", category="refresh")
        except Exception:
            # 安全兜底，避免双击导致程序崩溃
            pass

    def set_handlers(self, start_cb, pause_cb, resume_cb, refresh_cb, retry_cb):
        self.btn_start.config(command=start_cb)
        self.btn_pause.config(command=pause_cb)
        self.btn_resume.config(command=resume_cb)
        self.btn_refresh.config(command=refresh_cb)
        self.btn_retry.config(command=retry_cb)

    def log(self, msg, category="refresh"):
        """
        category: 'mtime', 'tmdb', 'refresh' (default/system)
        """

        def _add():
            prefix = ""
            if category:
                prefix = f"[{category}] "
            self.txt_log.insert(tk.END, prefix + msg + "\n")
            self.txt_log.see(tk.END)

        self.root.after(0, _add)

    def refresh_stats(self):
        # ✅ 实时刷新统计信息
        try:
            self.lbl_new_movies.config(text=f"本次新增电影：{len(session_new_movies)}")
            self.lbl_tmdb_ok.config(text=f"TMDB 成功：{tmdb_ok}")
            self.lbl_tmdb_fail.config(text=f"TMDB 失败：{tmdb_fail}")
            self.lbl_mtime_ok.config(text=f"MTime 成功：{mtime_ok}")
            self.lbl_mtime_fail.config(text=f"MTime 失败：{mtime_fail}")
            # 显示待重试数量
            pending_count = get_pending_retry_count()
            self.lbl_pending_retry.config(text=f"待重试：{pending_count}")
        except Exception:
            pass

        self.root.after(1000, self.refresh_stats)

    def start(self):
        self.root.mainloop()


logger = None
_log_hook = None


def set_log_hook(hook):
    global _log_hook
    _log_hook = hook


def log(msg, category="refresh"):
    if _log_hook:
        _log_hook(msg, category)
        return
    if logger:
        logger.log(msg, category)
    else:
        print(f"[{category}] {msg}")


# ============================
# 工具函数
# ============================


def clean_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "", name)


def normalize_title(s: str) -> str:
    if not s:
        return ""
    s = s.strip()
    # 去掉括号里的年份等
    s = re.split(r"[（）()]", s)[0]
    # 全部小写，去掉空格
    s = s.lower().replace(" ", "")
    return s


def safe_get(url, params=None, stream=False):
    """通用请求，自动指数退避重试"""
    wait = 10  # 增加初始等待时间
    max_retries = 5  # 限制重试次数
    retry_count = 0

    while retry_count < max_retries:
        try:
            r = requests.get(
                url, params=params, stream=stream, timeout=30, headers=HEADERS
            )
        except Exception as e:
            retry_count += 1
            log(f"📡 网络错误 {e} → 等待 {wait}s 重试 ({retry_count}/{max_retries})")
            time.sleep(wait)
            wait = min(wait * 2, 60)
            continue

        if r.status_code == 200:
            return r

        if r.status_code in (429, 503, 502):  # 502 也视为限流
            retry_count += 1
            log(
                f"⏳ 限速/服务不可用 {r.status_code} → 等待 {wait}s 重试 ({retry_count}/{max_retries})"
            )
            time.sleep(wait)
            wait = min(wait * 2, 60)
            continue

        retry_count += 1
        log(
            f"❌ HTTP 错误 {r.status_code} → {wait}s 后重试 ({retry_count}/{max_retries})"
        )
        time.sleep(wait)

    # 超过重试次数，返回 None
    log(f"❌ 达到最大重试次数，放弃请求：{url}")
    return None


def load_record():
    if os.path.exists(RECORD_FILE):
        try:
            with open(RECORD_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            log("⚠ JSON 记录损坏，将重建")

    return {"movie_ids": [], "images": {}}


def load_failed_record():
    """加载失败记录"""
    if os.path.exists(FAILED_FILE):
        try:
            with open(FAILED_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            log("⚠ 失败记录文件损坏，将重建")
    return []  # [{"url": ..., "save_path": ..., "movie_id_str": ..., "remote_key": ..., "movie_title": ...}, ...]


def save_failed_record(failed_list):
    """保存失败记录"""
    try:
        with open(FAILED_FILE, "w", encoding="utf-8") as f:
            json.dump(failed_list, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"⚠ 保存失败记录出错：{e}")


def add_failed_item(job, movie_title=""):
    """添加一个失败项到失败记录"""
    failed_list = load_failed_record()
    # 避免重复添加
    existing_keys = {item.get("remote_key") for item in failed_list}
    if job["remote_key"] not in existing_keys:
        failed_list.append({
            "url": job["url"],
            "save_path": job["save_path"],
            "movie_id_str": job["movie_id_str"],
            "remote_key": job["remote_key"],
            "movie_title": movie_title
        })
        save_failed_record(failed_list)


def remove_failed_item(remote_key):
    """从失败记录中移除成功下载的项"""
    failed_list = load_failed_record()
    failed_list = [item for item in failed_list if item.get("remote_key") != remote_key]
    save_failed_record(failed_list)


def get_pending_retry_count():
    """获取待重试的数量"""
    failed_list = load_failed_record()
    return len(failed_list)


def save_record_safe():
    if record is None:
        return
    with record_lock:
        with open(RECORD_FILE, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
    log("✔ JSON 记录已写入")


# ============================
# TMDB：下载一张图片（已禁用）
# ============================


def download_one_image(job):
    """
    ✅ 原 TMDB 图片下载函数
    ✅ 现在已被禁用，不再真正下载图片，只是保留结构以防其它地方调用时报错
    """
    global tmdb_fail
    tmdb_fail += 1
    # 不做任何下载，直接返回
    return


# ============================
# TMDB：下载一部电影的剧照（已禁用）
# ============================


def download_movie_images(movie_id, title):
    """
    ✅ 旧逻辑：从 TMDB 下载该电影所有剧照
    ❌ 现在：根据你的需求，TMDB 只用于获取电影名，不再下载图片
    """
    log(f"⏭ 已禁用 TMDB 剧照下载：《{title}》", category="tmdb")
    return True


# ============================
# ★ MTime：搜索 & 下载
# ============================


def search_mtime_movie(title_cn: str, title_en: str, year: str):
    """
    使用 front-gateway.mtime.com 的 unionSearch2 接口搜索电影
    """
    best_mid = None
    best_score = 0.0

    def parse_search_page(q: str):
        nonlocal best_mid, best_score

        if not q:
            return

        log(f"  🔍 MTime 搜索：{q}", category="mtime")
        url = "https://front-gateway.mtime.com/mtime-search/search/unionSearch2"
        params = {"keyword": q, "pageIndex": 1, "pageSize": 20, "searchType": 0}

        try:
            resp = requests.get(url, headers=HEADERS, params=params, timeout=20)
        except Exception as e:
            log(f"  ⚠ MTime 搜索失败：{e}", category="mtime")
            return

        if resp.status_code != 200:
            log(f"  ⚠ MTime 搜索 HTTP {resp.status_code}", category="mtime")
            return

        try:
            data = resp.json()
        except Exception as e:
            log(f"  ⚠ MTime 响应非 JSON：{e}", category="mtime")
            return

        movies = data.get("data", {}).get("movies", [])
        if not movies:
            return

        for m in movies:
            mid = m.get("movieId")
            if not mid:
                continue

            name_cn = m.get("name", "")
            name_en = m.get("nameEn", "")
            year_str = str(m.get("year", ""))  # API returns year as string or int?

            # 优先匹配中文名
            target = title_cn or title_en or ""
            if not target:
                continue

            # 尝试匹配中文名
            score_cn = 0.0
            if name_cn:
                n1 = normalize_title(name_cn)
                n2 = normalize_title(target)
                if n1 and n2:
                    score_cn = difflib.SequenceMatcher(None, n1, n2).ratio()

            # 尝试匹配英文名
            score_en = 0.0
            if name_en:
                n1 = normalize_title(name_en)
                n2 = normalize_title(target)
                if n1 and n2:
                    score_en = difflib.SequenceMatcher(None, n1, n2).ratio()

            ratio = max(score_cn, score_en)

            # 年份校验
            year_penalty = 0.0
            if year and year_str:
                try:
                    y1 = int(year)
                    y2 = int(year_str)
                    if abs(y1 - y2) > 2:
                        year_penalty = 0.15
                except ValueError:
                    pass

            score = ratio - year_penalty

            if score > best_score:
                best_score = score
                best_mid = mid

    # 优先用中文名
    if title_cn:
        parse_search_page(title_cn)
        time.sleep(0.5)

    # 不够好/没找到，再用英文名
    if (best_mid is None or best_score < 0.6) and title_en:
        parse_search_page(title_en)
        time.sleep(0.5)

    # 设置一个最低阈值
    if best_mid is not None and best_score >= 0.5:
        log(
            f"  ✅ MTime 匹配成功：movieId={best_mid}（相似度 {best_score:.2f}）",
            category="mtime",
        )
        return best_mid
    else:
        log(
            f"  ⏭ MTime 未找到足够匹配的结果（score={best_score:.2f}）",
            category="mtime",
        )
        return None


def check_and_auto_pause():
    """
    检查是否需要自动暂停（连续失败过多）
    返回 True 表示需要暂停等待
    """
    global consecutive_fails, pause_requested

    if consecutive_fails >= CONSECUTIVE_FAIL_THRESHOLD:
        log(f"⚠ 连续失败 {consecutive_fails} 次，疑似被限流，自动暂停 {AUTO_PAUSE_DURATION} 秒...", category="mtime")
        log(f"⏳ 等待中... 将在 {AUTO_PAUSE_DURATION} 秒后自动恢复", category="refresh")
        
        # 分段等待，以便响应暂停请求
        waited = 0
        while waited < AUTO_PAUSE_DURATION:
            if pause_requested:
                log("⏸ 用户请求暂停，停止自动等待", category="refresh")
                return True
            time.sleep(5)
            waited += 5
            remaining = AUTO_PAUSE_DURATION - waited
            if remaining > 0 and remaining % 30 == 0:
                log(f"⏳ 还需等待 {remaining} 秒...", category="refresh")
        
        # 重置连续失败计数
        consecutive_fails = 0
        log("▶ 自动暂停结束，继续下载...", category="mtime")
    
    return False


def download_one_mtime_image(job, movie_title=""):
    global pause_requested, mtime_ok, mtime_fail, session_new_images
    global consecutive_fails, last_success_time

    # 检查暂停请求
    if pause_requested:
        return

    # 检查是否需要自动暂停
    if check_and_auto_pause():
        return

    url = job["url"]
    save_path = job["save_path"]
    mid_str = job["movie_id_str"]
    remote_key = job["remote_key"]

    # 根据连续失败次数动态调整延迟
    base_delay = 5.0 + consecutive_fails * 1.0  # 失败越多，延迟越长
    max_delay = min(base_delay + 3.0, 30.0)  # 最大延迟30秒
    time.sleep(random.uniform(base_delay, max_delay))

    try:
        resp = safe_get(url, stream=True)
        if not resp:
            raise RuntimeError("MTime 请求失败")

        img_data = resp.content
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        with open(save_path, "wb") as f:
            f.write(img_data)

        with record_lock:
            record["images"][mid_str].append(remote_key)

        mtime_ok += 1
        session_new_images += 1

        # 成功了，重置连续失败计数
        consecutive_fails = 0
        last_success_time = time.time()

        # 如果之前失败过，现在成功了，从失败记录中移除
        remove_failed_item(remote_key)

        log("  ✔ MTime 保存：" + save_path, category="mtime")
    except Exception as e:
        mtime_fail += 1
        consecutive_fails += 1  # 增加连续失败计数
        
        # 记录失败的下载任务，以便下次重试
        add_failed_item(job, movie_title)
        log(f"  ❌ MTime 下载失败（连续{consecutive_fails}次）：{url} 错误：{e}", category="mtime")


def try_download_mtime_images(movie_id, title_cn, title_en, year):
    """
    为某个 TMDB 电影，尝试用标题匹配 MTime 并下载所有类型剧照。
    使用 front-gateway.mtime.com 的 image.api 接口
    """
    global record, session_new_images, session_movie_new_images, pause_requested

    mid_str = str(movie_id)
    base_title = title_cn or title_en or f"movie_{mid_str}"
    safe_title = clean_filename(base_title) or f"movie_{mid_str}"

    movie_dir = os.path.join(SAVE_DIR, safe_title)
    os.makedirs(movie_dir, exist_ok=True)

    with record_lock:
        record["images"].setdefault(mid_str, [])

    log(f"🧩 正在为《{base_title}》匹配 MTime 剧照…", category="mtime")

    mtime_id = search_mtime_movie(title_cn, title_en, year)
    if not mtime_id:
        return

    # 添加延迟，避免连续请求
    time.sleep(random.uniform(3.0, 6.0))

    # 拉取 image.api
    api_url = "https://front-gateway.mtime.com/library/movie/image.api"
    r = safe_get(api_url, params={"movieId": mtime_id})
    if not r:
        log("  ❌ MTime image.api 接口失败", category="mtime")
        return

    try:
        data = r.json()
    except Exception as e:
        log(f"  ❌ MTime JSON 解析失败：{e}", category="mtime")
        return

    image_infos = data.get("data", {}).get("imageInfos", [])
    if not image_infos:
        log("  ⏭ MTime 无新剧照", category="mtime")
        return

    jobs = []
    with record_lock:
        existing = set(record["images"][mid_str])

    # 类型映射（猜测）
    TYPE_MAP = {
        1: "海报",
        6: "剧照",
    }

    for img in image_infos:
        if pause_requested:
            log("  ⏸ 暂停请求 → 停止加入新的 MTime 剧照", category="mtime")
            break

        img_id = img.get("id")
        img_url = img.get("image")
        img_type = img.get("type")

        if not img_url:
            continue

        remote_key = f"mtime:{img_id}" if img_id else f"mtime_url:{img_url}"

        if remote_key in existing:
            continue

        type_name = TYPE_MAP.get(img_type, f"Type_{img_type}")
        type_dir = os.path.join(movie_dir, f"MTime_{type_name}")

        # 使用图片 ID 作为文件名
        original_filename = os.path.basename(img_url)
        ext = os.path.splitext(original_filename)[1] or ".jpg"

        if img_id:
            filename = f"{img_id}{ext}"
        else:
            filename = original_filename

        save_path = os.path.join(type_dir, filename)

        jobs.append(
            {
                "url": img_url,
                "save_path": save_path,
                "movie_id_str": mid_str,
                "remote_key": remote_key,
            }
        )

    if not jobs:
        log("  ⏭ MTime 无新剧照", category="mtime")
        return

    if pause_requested:
        log("  ⏸ 暂停请求 → 取消 MTime 下载任务", category="mtime")
        return

    log(f"  🚀 MTime 开始下载 {len(jobs)} 张（多类型文件夹）…", category="mtime")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for _ in ex.map(lambda j: download_one_mtime_image(j, base_title), jobs):
            pass

    new_count = len(jobs)
    session_new_images += 0  # 单张里已经累加，这里不再重复累加
    session_movie_new_images[base_title] = (
        session_movie_new_images.get(base_title, 0) + new_count
    )

    log(f"  ✔ MTime 完成：《{base_title}》新增 {new_count} 张", category="mtime")


# ============================
# TMDB：热门模式（原逻辑，保留）
# ============================


def run_popular_mode():
    global record, session_new_movies, pause_requested

    for page in range(1, POPULAR_MAX_PAGES + 1):
        if pause_requested:
            log("⏸ 暂停请求 → 停止热门电影拉取")
            return

        log(f"\n📄 TMDB 热门电影 第 {page} 页", category="tmdb")

        r = safe_get(
            f"{BASE_URL}/movie/popular",
            params={
                "api_key": API_KEY,
                "page": page,
                "language": "zh-CN",  # 让 title 尽量是中文
                "region": "CN",
            },
        )
        time.sleep(3)  # 稍微减慢翻页速度
        if not r:
            continue

        movies = r.json().get("results", [])
        if not movies:
            log("无更多热门电影", category="tmdb")
            break

        for m in movies:
            if pause_requested:
                log("⏸ 暂停请求 → 停止处理更多电影", category="tmdb")
                return

            movie_id = m["id"]
            title = m.get("title") or m.get("name") or "无标题"

            with record_lock:
                already = movie_id in record["movie_ids"]

            if already:
                log(f"⏭ 跳过已处理电影：《{title}》", category="tmdb")
                continue

            # ✅ 此处即使调用 TMDB 下载，也会被禁用逻辑拦截
            ok = download_movie_images(movie_id, title)

            if pause_requested:
                log("⏸ 暂停 → 已保存当前进度", category="tmdb")
                save_record_safe()
                return

            if ok:
                with record_lock:
                    record["movie_ids"].append(movie_id)
                session_new_movies.append(title)
                save_record_safe()


# ============================
# TMDB：中文电影模式（★ 会联动 MTime）
# ============================


def collect_new_movies():
    """
    ✅ 扫描 TMDB 接口，收集所有待下载的中文电影
    ✅ 真增量：按 primary_release_date.desc，从断点页继续
    """
    global record, pause_requested

    scan_state_file = os.path.join(BASE_DIR, "scan_state.json")
    start_page = 1
    if os.path.exists(scan_state_file):
        try:
            with open(scan_state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
                start_page = state.get("last_page", 1)
                log(f"📂 发现扫描断点，从第 {start_page} 页继续...", category="tmdb")
        except Exception:
            pass

    movies_list_file = os.path.join(BASE_DIR, "movies_to_download.json")
    existing_ids = set()
    all_movies = []

    if os.path.exists(movies_list_file):
        try:
            with list_file_lock:
                with open(movies_list_file, "r", encoding="utf-8") as f:
                    saved_list = json.load(f)
                    all_movies = saved_list
                    for m in saved_list:
                        existing_ids.add(m["id"])
            log(f"📂 已加载现有列表，共 {len(all_movies)} 部电影", category="tmdb")
        except Exception:
            pass

    current_page = start_page

    for page in range(start_page, CHINESE_MAX_PAGES + 1):
        if pause_requested:
            log("⏸ 暂停请求 → 停止扫描", category="tmdb")
            break

        current_page = page
        log(f"\n📄 TMDB 中文电影 第 {page} 页（按上映时间倒序）", category="tmdb")

        r = safe_get(
            f"{BASE_URL}/discover/movie",
            params={
                "api_key": API_KEY,
                "page": page,
                "with_original_language": "zh",
                "language": "zh-CN",
                "region": "CN",
                "sort_by": "primary_release_date.desc",  # ✅ 核心修改：按上映时间排序
            },
        )
        time.sleep(3)  # 稍微减慢翻页速度
        if not r:
            continue

        data = r.json()
        movies = data.get("results", [])
        if not movies:
            log("无更多中文电影", category="tmdb")
            break

        new_count = 0
        for m in movies:
            if pause_requested:
                break

            movie_id = m["id"]
            if movie_id in existing_ids:
                continue

            all_movies.append(
                {
                    "id": movie_id,
                    "title_cn": m.get("title") or m.get("name") or "",
                    "title_en": m.get("original_title") or "",
                    "year": (m.get("release_date") or "0000")[:4],
                }
            )
            existing_ids.add(movie_id)
            new_count += 1

        if page % 10 == 0 or new_count > 0:
            try:
                with list_file_lock:
                    with open(movies_list_file, "w", encoding="utf-8") as f:
                        json.dump(all_movies, f, ensure_ascii=False, indent=2)

                with open(scan_state_file, "w", encoding="utf-8") as f:
                    json.dump({"last_page": page + 1}, f)

                if page % 10 == 0:
                    log(
                        f"💾 进度已保存：第 {page} 页，累计收集 {len(all_movies)} 部",
                        category="tmdb",
                    )
            except Exception as e:
                log(f"⚠ 保存失败: {e}", category="tmdb")

        # 连续几页都没有新电影，可以早停（可选）
        if new_count == 0 and page >= start_page + 2:
            log("✅ 连续多页无新电影，提前停止扫描", category="tmdb")
            break

    return all_movies


def run_chinese_movies_mode():
    """
    从 movies_to_download.json 读取电影列表
    仅下载未完成的电影（对比 downloaded.json）
    ✅ 现在只使用 MTime 下载图片，TMDB 不再下载图片
    """
    global record, session_new_movies, pause_requested

    movies_list_file = os.path.join(BASE_DIR, "movies_to_download.json")
    if not os.path.exists(movies_list_file):
        log("⚠ 未找到电影列表文件，请先点击【刷新列表】", category="refresh")
        return

    all_movies = []
    try:
        with list_file_lock:
            with open(movies_list_file, "r", encoding="utf-8") as f:
                all_movies = json.load(f)
    except Exception as e:
        log(f"💥 读取列表失败：{e}", category="refresh")
        return

    if not all_movies:
        log("⚠ 电影列表为空，请先点击【刷新列表】", category="refresh")
        return

    pending_movies = []
    with record_lock:
        downloaded_ids = set(record["movie_ids"])

    for m in all_movies:
        if m["id"] not in downloaded_ids:
            pending_movies.append(m)

    if not pending_movies:
        log("✅ 所有列表中的电影都已下载完成", category="refresh")
        return

    log(
        f"\n📊 列表共 {len(all_movies)} 部，待下载 {len(pending_movies)} 部",
        category="refresh",
    )
    log("🚀 启动 MTime 下载线程（TMDB 图片下载已禁用）...\n", category="refresh")

    # TMDB 线程保留结构，但不做实际下载
    def tmdb_worker():
        if is_tmdb_enabled():
            log(
                "ℹ TMDB 图片下载功能已禁用，当前不会从 TMDB 下载剧照。", category="tmdb"
            )
        else:
            log("ℹ 未勾选 TMDB 下载，且功能已禁用。", category="tmdb")
        return

    def mtime_worker():
        for movie in pending_movies:
            if pause_requested:
                log("⏸ MTime 下载线程暂停", category="mtime")
                return

            if not is_mtime_enabled():
                log("ℹ 未勾选 MTime 下载，跳过所有电影", category="mtime")
                return

            movie_id = movie["id"]
            display_title = (
                movie["title_cn"] or movie["title_en"] or f"movie_{movie_id}"
            )

            try:
                try_download_mtime_images(
                    movie["id"], movie["title_cn"], movie["title_en"], movie["year"]
                )

                with record_lock:
                    if movie_id not in record["movie_ids"]:
                        record["movie_ids"].append(movie_id)
                        session_new_movies.append(display_title)
                save_record_safe()
                log(f"  💾 《{display_title}》完成并在记录中归档", category="mtime")

            except Exception as e:
                log(f"  ⚠ MTime 处理异常：{e}", category="mtime")

    # 启动线程：TMDB 只打日志，MTime 真正下载
    tmdb_thread = threading.Thread(target=tmdb_worker, daemon=True, name="TMDB-Worker")
    mtime_thread = threading.Thread(
        target=mtime_worker, daemon=True, name="MTime-Worker"
    )

    tmdb_thread.start()
    mtime_thread.start()

    tmdb_thread.join()
    mtime_thread.join()

    log("\n✅ MTime 下载线程全部完成", category="refresh")


# ============================
# 下载线程
# ============================


def download_worker():
    global is_downloading, record, pause_requested

    with state_lock:
        is_downloading = True

    try:
        log("▶ 下载线程启动", category="refresh")
        os.makedirs(SAVE_DIR, exist_ok=True)

        if record is None:
            log("📂 加载 JSON 记录…", category="refresh")
            loaded = load_record()
            with record_lock:
                globals()["record"] = loaded
            log("📂 JSON 记录加载完成", category="refresh")

        if MODE == "popular":
            run_popular_mode()
        elif MODE == "zh_movies":
            run_chinese_movies_mode()
        else:
            log(f"⚠ 未知 MODE = {MODE}", category="refresh")
    except Exception as e:
        log(f"💥 下载线程异常：{e}", category="refresh")
    finally:
        save_record_safe()
        with state_lock:
            is_downloading = False
        log("✅ 下载线程结束", category="refresh")


# ============================
# GUI 回调
# ============================


def start_download():
    global download_thread, pause_requested

    with state_lock:
        if is_downloading:
            log("ℹ 已经在下载中", category="refresh")
            return
        pause_requested = False

    log("▶ 开始下载", category="refresh")
    download_thread = threading.Thread(target=download_worker, daemon=True)
    download_thread.start()


def pause_download():
    global pause_requested

    with state_lock:
        if not is_downloading and not is_refreshing:
            log("ℹ 当前没有进行中的任务", category="refresh")
            return
        pause_requested = True

    log("⏸ 已请求暂停", category="refresh")
    save_record_safe()


def resume_download():
    global download_thread, pause_requested

    with state_lock:
        if is_downloading:
            log("ℹ 下载正在进行，无需继续", category="refresh")
            pause_requested = False
            return

        if is_refreshing:
            pause_requested = False
            log("▶ 继续刷新 ...", category="refresh")
            return

        pause_requested = False

    log("▶ 继续下载 …", category="refresh")
    download_thread = threading.Thread(target=download_worker, daemon=True)
    download_thread.start()


def refresh_worker():
    global is_refreshing, pause_requested

    with state_lock:
        if is_refreshing:
            log("ℹ 正在刷新列表中，请等待完成", category="refresh")
            return
        is_refreshing = True
        if pause_requested:
            log("ℹ 全局暂停中，刷新任务将响应暂停", category="refresh")

    try:
        log("▶ 开始刷新电影列表...", category="refresh")
        movies = collect_new_movies()
        if movies:
            log(
                f"✅ 刷新完成，共找到 {len(movies)} 部电影（包含历史 + 新增）",
                category="refresh",
            )
        else:
            log("✅ 刷新完成，没有发现电影或无更多新电影", category="refresh")

    except Exception as e:
        log(f"💥 刷新列表异常：{e}", category="refresh")
    finally:
        with state_lock:
            is_refreshing = False


def start_refresh():
    with state_lock:
        if is_refreshing:
            log("ℹ 刷新任务进行中", category="refresh")
            return

    log("▶ 启动列表刷新", category="refresh")
    threading.Thread(target=refresh_worker, daemon=True).start()


# ============================
# 重试失败下载
# ============================

is_retrying = False  # 重试状态标志


def retry_failed_worker():
    """
    重试所有失败的下载任务
    """
    global is_retrying, pause_requested, record, mtime_ok, mtime_fail, session_new_images

    with state_lock:
        if is_retrying:
            log("ℹ 正在重试中，请等待完成", category="refresh")
            return
        is_retrying = True

    try:
        failed_list = load_failed_record()
        if not failed_list:
            log("✅ 没有失败的下载任务需要重试", category="refresh")
            return

        log(f"▶ 开始重试 {len(failed_list)} 个失败的下载任务...", category="mtime")

        # 确保 record 已加载
        if record is None:
            loaded = load_record()
            with record_lock:
                globals()["record"] = loaded

        success_count = 0
        still_failed = []

        retry_consecutive_fails = 0  # 重试时的连续失败计数

        for item in failed_list:
            if pause_requested:
                log("⏸ 暂停请求 → 停止重试", category="mtime")
                # 将未处理的项加入仍失败列表
                still_failed.extend(failed_list[failed_list.index(item):])
                break

            # 检查连续失败是否需要自动暂停
            if retry_consecutive_fails >= CONSECUTIVE_FAIL_THRESHOLD:
                log(f"⚠ 重试连续失败 {retry_consecutive_fails} 次，自动暂停 {AUTO_PAUSE_DURATION} 秒...", category="mtime")
                waited = 0
                while waited < AUTO_PAUSE_DURATION:
                    if pause_requested:
                        log("⏸ 用户请求暂停", category="refresh")
                        still_failed.extend(failed_list[failed_list.index(item):])
                        break
                    time.sleep(5)
                    waited += 5
                if pause_requested:
                    break
                retry_consecutive_fails = 0
                log("▶ 自动暂停结束，继续重试...", category="mtime")

            url = item.get("url")
            save_path = item.get("save_path")
            mid_str = item.get("movie_id_str")
            remote_key = item.get("remote_key")
            movie_title = item.get("movie_title", "")

            log(f"  🔄 重试：《{movie_title}》 - {os.path.basename(save_path)}", category="mtime")

            # 根据连续失败次数动态调整延迟
            base_delay = 5.0 + retry_consecutive_fails * 1.0
            max_delay = min(base_delay + 3.0, 30.0)
            time.sleep(random.uniform(base_delay, max_delay))

            try:
                resp = safe_get(url, stream=True)
                if not resp:
                    raise RuntimeError("MTime 请求失败")

                img_data = resp.content
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                with open(save_path, "wb") as f:
                    f.write(img_data)

                # 确保 images 字典中有该电影的记录
                with record_lock:
                    if mid_str not in record["images"]:
                        record["images"][mid_str] = []
                    record["images"][mid_str].append(remote_key)

                mtime_ok += 1
                session_new_images += 1
                success_count += 1
                retry_consecutive_fails = 0  # 成功，重置计数

                log(f"  ✔ 重试成功：{save_path}", category="mtime")

            except Exception as e:
                mtime_fail += 1
                retry_consecutive_fails += 1  # 增加连续失败计数
                still_failed.append(item)
                log(f"  ❌ 重试失败（连续{retry_consecutive_fails}次）：{url} 错误：{e}", category="mtime")

        # 更新失败记录文件
        save_failed_record(still_failed)
        save_record_safe()

        log(f"✅ 重试完成：成功 {success_count} 个，仍失败 {len(still_failed)} 个", category="refresh")

    except Exception as e:
        log(f"💥 重试异常：{e}", category="refresh")
    finally:
        with state_lock:
            is_retrying = False


def start_retry():
    """启动重试失败下载"""
    global pause_requested

    with state_lock:
        if is_retrying:
            log("ℹ 重试任务进行中", category="refresh")
            return
        if is_downloading:
            log("ℹ 下载任务进行中，请等待完成后再重试", category="refresh")
            return
        pause_requested = False

    log("▶ 启动重试失败下载", category="refresh")
    threading.Thread(target=retry_failed_worker, daemon=True).start()


# ============================
# 主入口
# ============================


def main():
    global logger
    logger = LoggerWindow()
    set_log_hook(logger.log)
    logger.set_handlers(start_download, pause_download, resume_download, start_refresh, start_retry)

    log("👋 TMDB + MTime 中文电影剧照下载器启动", category="refresh")
    log(f"当前模式：{MODE}", category="refresh")
    log("说明：", category="refresh")
    log(
        "  - MODE = 'zh_movies'：只抓 TMDB 中文电影，并尝试匹配 MTime 高清剧照",
        category="refresh",
    )
    log("  - TMDB 剧照下载已禁用，现在只用于获取电影名列表", category="refresh")
    log("  - MTime 剧照按类型保存到 MTime_前缀文件夹中", category="refresh")
    log("  - 支持暂停/继续，JSON 记录断点续传", category="refresh")
    log("  - 点击【重试失败】可重新下载之前失败的图片", category="refresh")
    log(f"  - 连续失败 {CONSECUTIVE_FAIL_THRESHOLD} 次将自动暂停 {AUTO_PAUSE_DURATION} 秒", category="refresh")

    logger.start()


if __name__ == "__main__":
    main()