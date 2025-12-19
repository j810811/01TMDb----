import json
import os
import sys
import threading
import tkinter as tk
from tkinter import scrolledtext, ttk

import TMDB
import MTime
import douban


CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard_ui_config.json")


class LogView:
    def __init__(self, parent):
        self.txt = scrolledtext.ScrolledText(parent, font=("Consolas", 9), wrap="word")
        self.txt.pack(fill="both", expand=True)

    def write(self, msg: str):
        self.txt.insert(tk.END, msg + "\n")
        self.txt.see(tk.END)

    def write_with_prefix(self, prefix: str, msg: str):
        self.write(f"[{prefix}] {msg}")


class Section:
    def __init__(self, parent, title: str, with_log: bool = True):
        self.frame = tk.Frame(parent, relief="groove", borderwidth=2)

        header = tk.Label(self.frame, text=title, font=("微软雅黑", 12, "bold"))
        header.pack(fill="x")

        body = tk.PanedWindow(self.frame, orient="horizontal", sashrelief="raised")
        body.pack(fill="both", expand=True)
        self.body = body

        left = tk.Frame(body)
        right = tk.Frame(body)

        self.log_view = LogView(left) if with_log else None
        self.right = right

        if with_log:
            body.add(left, stretch="always")
            body.add(right, stretch="always")
        else:
            body.add(right, stretch="always")

    def set_split_ratio(self, ratio: float = 0.5):
        try:
            if self.log_view is None:
                return
            w = self.body.winfo_width()
            if w <= 1:
                return
            x = int(w * ratio)
            self.body.sash_place(0, x, 0)
        except Exception:
            pass

    def get_split_ratio(self) -> float | None:
        try:
            if self.log_view is None:
                return None
            w = self.body.winfo_width()
            if w <= 1:
                return None
            x, _y = self.body.sash_coord(0)
            if x < 0:
                return None
            r = x / w
            if r < 0.05:
                r = 0.05
            if r > 0.95:
                r = 0.95
            return r
        except Exception:
            return None


class DashboardApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("TMDB / Douban / MTime 控制台")
        self.root.geometry("1500x820")

        self._tray_icon = None
        self._tray_thread = None
        self._tray_available = False

        container = tk.Frame(self.root)
        container.pack(fill="both", expand=True)

        cols = tk.PanedWindow(container, orient="horizontal", sashrelief="raised")
        cols.pack(fill="both", expand=True)
        self.cols = cols

        self.sec_tmdb = Section(cols, "TMDB")
        self.sec_douban = Section(cols, "Douban")

        self.cols.add(self.sec_tmdb.frame, stretch="always")
        self.cols.add(self.sec_douban.frame, stretch="always")

        self._split_job = None
        self._save_job = None
        self._split_ratios = self._load_split_ratios()
        self._col_ratios = self._load_col_ratios()

        self.root.bind("<Configure>", self._on_resize)
        self.root.bind("<Unmap>", self._on_window_unmap)
        self.cols.bind("<ButtonRelease-1>", lambda e: self._on_user_col_change())
        self.sec_tmdb.body.bind("<ButtonRelease-1>", lambda e: self._on_user_split_change("tmdb"))
        self.sec_douban.body.bind("<ButtonRelease-1>", lambda e: self._on_user_split_change("douban"))

        self.root.after(0, self._apply_layout)

        self._build_tmdb_controls(self.sec_tmdb.right)
        self._build_douban_controls(self.sec_douban.right)

        TMDB.set_log_hook(lambda m: self._ui_call(self.sec_tmdb.log_view.write, m))
        douban.set_log_hook(lambda m: self._ui_call(self.sec_douban.log_view.write, m))
        MTime.set_log_hook(
            lambda m, c="refresh": self._ui_call(
                self.sec_douban.log_view.write_with_prefix,
                "mtime",
                self._format_mtime_log(c, m),
            )
        )

        self.root.after(500, self._refresh_stats)

        self.root.after(600, self._sync_toggle_buttons)

        self._init_tray()

    def _load_split_ratios(self):
        defaults = {"tmdb": 0.75, "douban": 0.75}
        try:
            if not os.path.exists(CONFIG_PATH):
                return defaults
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            ratios = data.get("split_ratios", {})
            for k in list(defaults.keys()):
                v = ratios.get(k)
                if isinstance(v, (int, float)) and 0.05 <= float(v) <= 0.95:
                    defaults[k] = float(v)
            return defaults
        except Exception:
            return defaults

    def _load_col_ratios(self):
        defaults = {"sash0": 1 / 2}
        try:
            if not os.path.exists(CONFIG_PATH):
                return defaults
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            ratios = data.get("col_ratios", {})
            for k in list(defaults.keys()):
                v = ratios.get(k)
                if isinstance(v, (int, float)) and 0.05 <= float(v) <= 0.95:
                    defaults[k] = float(v)
            return defaults
        except Exception:
            return defaults

    def _save_split_ratios(self):
        try:
            payload = {"split_ratios": self._split_ratios, "col_ratios": self._col_ratios}
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _apply_splits(self):
        self.sec_tmdb.set_split_ratio(self._split_ratios.get("tmdb", 0.75))
        self.sec_douban.set_split_ratio(self._split_ratios.get("douban", 0.75))

    def _apply_cols(self):
        try:
            w = self.cols.winfo_width()
            if w <= 1:
                return
            if len(self.cols.panes()) <= 1:
                return
            x0 = int(w * self._col_ratios.get("sash0", 1 / 2))
            self.cols.sash_place(0, x0, 0)
        except Exception:
            pass

    def _apply_layout(self):
        self._apply_cols()
        self._apply_splits()

    def _on_user_col_change(self):
        try:
            w = self.cols.winfo_width()
            if w <= 1:
                return
            if len(self.cols.panes()) <= 1:
                return
            x0, _y0 = self.cols.sash_coord(0)
            r0 = x0 / w
            if r0 < 0.05 or r0 > 0.95:
                return
            self._col_ratios["sash0"] = r0
        except Exception:
            return

        if self._save_job is not None:
            try:
                self.root.after_cancel(self._save_job)
            except Exception:
                pass
        self._save_job = self.root.after(300, self._save_split_ratios)

    def _on_user_split_change(self, key: str):
        section = None
        if key == "tmdb":
            section = self.sec_tmdb
        elif key == "douban":
            section = self.sec_douban

        if not section:
            return

        r = section.get_split_ratio()
        if r is None:
            return

        self._split_ratios[key] = r

        if self._save_job is not None:
            try:
                self.root.after_cancel(self._save_job)
            except Exception:
                pass
        self._save_job = self.root.after(300, self._save_split_ratios)

    def _on_resize(self, _event=None):
        if self._split_job is not None:
            try:
                self.root.after_cancel(self._split_job)
            except Exception:
                pass
        self._split_job = self.root.after(150, self._apply_layout)

    def _on_window_unmap(self, _event=None):
        try:
            if self.root.state() == "iconic":
                self._hide_to_tray()
        except Exception:
            pass

    def _init_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw

            def _create_image():
                img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
                draw = ImageDraw.Draw(img)
                draw.rounded_rectangle((8, 8, 56, 56), radius=12, fill=(35, 130, 255, 255))
                draw.text((22, 18), "T", fill=(255, 255, 255, 255))
                return img

            image = _create_image()

            menu = pystray.Menu(
                pystray.MenuItem("显示", lambda: self._show_from_tray()),
                pystray.MenuItem("退出", lambda: self._quit_from_tray()),
            )

            self._tray_icon = pystray.Icon("tmdb_douban_mtime", image, "控制台", menu)
            self._tray_available = True

            def _run():
                try:
                    self._tray_icon.run()
                except Exception:
                    pass

            self._tray_thread = threading.Thread(target=_run, daemon=True)
            self._tray_thread.start()
        except Exception:
            self._tray_available = False

    def _hide_to_tray(self):
        if not self._tray_available:
            return
        try:
            self.root.withdraw()
        except Exception:
            pass

    def _show_from_tray(self):
        try:
            self.root.after(0, self.root.deiconify)
            self.root.after(0, self.root.lift)
            self.root.after(0, lambda: self.root.focus_force())
        except Exception:
            pass

    def _quit_from_tray(self):
        try:
            if self._tray_icon is not None:
                try:
                    self._tray_icon.stop()
                except Exception:
                    pass
        finally:
            try:
                self.root.after(0, self.root.destroy)
            except Exception:
                try:
                    self.root.destroy()
                except Exception:
                    pass

    def _ui_call(self, fn, *args):
        self.root.after(0, lambda: fn(*args))

    def _build_tmdb_controls(self, parent):
        tk.Label(parent, text="控制", font=("微软雅黑", 10, "bold")).pack(anchor="w", padx=10, pady=(10, 0))

        self.tmdb_btn_toggle = tk.Button(parent, text="开始", command=self._tmdb_toggle, width=12)
        self.tmdb_btn_toggle.pack(padx=10, pady=6)

        ttk.Separator(parent, orient="horizontal").pack(fill="x", padx=10, pady=10)
        tk.Label(parent, text="统计", font=("微软雅黑", 10, "bold")).pack(anchor="w", padx=10)

        self.tmdb_lbl_new_movies = tk.Label(parent, text="本次新增电影：0", anchor="w")
        self.tmdb_lbl_new_movies.pack(fill="x", padx=10, pady=2)
        self.tmdb_lbl_new_images = tk.Label(parent, text="本次新增剧照：0", anchor="w")
        self.tmdb_lbl_new_images.pack(fill="x", padx=10, pady=2)
        self.tmdb_lbl_total_movies = tk.Label(parent, text="累计电影：0", anchor="w")
        self.tmdb_lbl_total_movies.pack(fill="x", padx=10, pady=2)
        self.tmdb_lbl_total_images = tk.Label(parent, text="累计剧照：0", anchor="w")
        self.tmdb_lbl_total_images.pack(fill="x", padx=10, pady=2)

    def _build_douban_controls(self, parent):
        tk.Label(parent, text="Cookie", font=("微软雅黑", 10, "bold")).pack(anchor="w", padx=10, pady=(10, 0))

        self.douban_cookie = tk.Text(parent, height=10, wrap="word")
        self.douban_cookie.pack(fill="x", padx=10)
        last_cookie = douban.load_last_cookie()
        if last_cookie:
            self.douban_cookie.insert("1.0", last_cookie)

        tk.Label(parent, text="douban控制", font=("微软雅黑", 10, "bold")).pack(anchor="w", padx=10, pady=(10, 0))

        self.douban_btn_toggle = tk.Button(parent, text="开始", command=self._douban_toggle, width=12)
        self.douban_btn_toggle.pack(padx=10, pady=6)

        ttk.Separator(parent, orient="horizontal").pack(fill="x", padx=10, pady=10)
        tk.Label(parent, text="MTime控制", font=("微软雅黑", 10, "bold")).pack(anchor="w", padx=10)
        tk.Button(parent, text="刷新列表", command=MTime.start_refresh, width=12).pack(padx=10, pady=4)
        tk.Button(parent, text="重试失败", command=MTime.start_retry, width=12).pack(padx=10, pady=4)

        ttk.Separator(parent, orient="horizontal").pack(fill="x", padx=10, pady=10)
        tk.Label(parent, text="douban统计", font=("微软雅黑", 10, "bold")).pack(anchor="w", padx=10)

        self.douban_lbl_total_photos = tk.Label(parent, text="已记住图片数：0 张", anchor="w")
        self.douban_lbl_total_photos.pack(fill="x", padx=10, pady=2)
        self.douban_lbl_total_subjects = tk.Label(parent, text="已记住综艺数量：0 部", anchor="w")
        self.douban_lbl_total_subjects.pack(fill="x", padx=10, pady=2)
        self.douban_lbl_current_subject = tk.Label(parent, text="当前节目已下载：0 张", anchor="w")
        self.douban_lbl_current_subject.pack(fill="x", padx=10, pady=2)
        self.douban_lbl_today = tk.Label(parent, text="今日新增：0 张", anchor="w")
        self.douban_lbl_today.pack(fill="x", padx=10, pady=2)

        ttk.Separator(parent, orient="horizontal").pack(fill="x", padx=10, pady=10)
        tk.Label(parent, text="MTime统计", font=("微软雅黑", 10, "bold")).pack(anchor="w", padx=10)

        self.mtime_lbl_new_movies = tk.Label(parent, text="本次新增电影：0", anchor="w")
        self.mtime_lbl_new_movies.pack(fill="x", padx=10, pady=2)
        self.mtime_lbl_mtime_ok = tk.Label(parent, text="MTime 成功：0", anchor="w")
        self.mtime_lbl_mtime_ok.pack(fill="x", padx=10, pady=2)
        self.mtime_lbl_mtime_fail = tk.Label(parent, text="MTime 失败：0", anchor="w")
        self.mtime_lbl_mtime_fail.pack(fill="x", padx=10, pady=2)
        self.mtime_lbl_pending_retry = tk.Label(parent, text="待重试：0", anchor="w")
        self.mtime_lbl_pending_retry.pack(fill="x", padx=10, pady=2)

    def _douban_get_cookie(self) -> str:
        try:
            return (self.douban_cookie.get("1.0", "end-1c") or "").strip()
        except Exception:
            return ""

    def _douban_toggle(self):
        try:
            running = bool(getattr(douban, "is_running", False))
            paused = not bool(douban.pause_event.is_set())
        except Exception:
            running = False
            paused = False

        try:
            m_active = bool(getattr(MTime, "is_downloading", False))
            m_paused = bool(getattr(MTime, "pause_requested", False))
        except Exception:
            m_active = False
            m_paused = False

        if (running and not paused) or (m_active and not m_paused):
            try:
                douban.pause_download()
            except Exception:
                pass
            try:
                MTime.pause_download()
            except Exception:
                pass
            return

        if running and paused:
            douban.resume_download()
        else:
            douban.start_download(self._douban_get_cookie())

        try:
            MTime.start_download()
        except Exception:
            pass

    def _format_mtime_log(self, category: str, msg: str) -> str:
        try:
            if category != "mtime":
                return msg

            token = "✔ MTime 保存："
            if token in msg:
                save_path = msg.split(token, 1)[1].strip()
                base = getattr(MTime, "SAVE_DIR", "")
                rel = save_path
                if base and rel.startswith(base):
                    rel = rel[len(base):]
                rel = rel.lstrip("\\/")
                rel = rel.replace("/", "\\")
                return f"{rel}✔"
        except Exception:
            pass
        return msg

    def _tmdb_toggle(self):
        try:
            active = bool(getattr(TMDB, "is_downloading", False))
            paused = bool(getattr(TMDB, "pause_requested", False))
        except Exception:
            active = False
            paused = False

        if active and not paused:
            TMDB.pause_download()
            return

        TMDB.start_download()

    def _sync_toggle_buttons(self):
        try:
            active = bool(getattr(TMDB, "is_downloading", False))
            paused = bool(getattr(TMDB, "pause_requested", False))
            if hasattr(self, "tmdb_btn_toggle"):
                self.tmdb_btn_toggle.config(text=("暂停" if active and not paused else "开始"))
        except Exception:
            pass

        try:
            running = bool(getattr(douban, "is_running", False))
            paused = not bool(douban.pause_event.is_set())
            m_active = bool(getattr(MTime, "is_downloading", False))
            m_paused = bool(getattr(MTime, "pause_requested", False))
            if hasattr(self, "douban_btn_toggle"):
                self.douban_btn_toggle.config(
                    text=("暂停" if (running and not paused) or (m_active and not m_paused) else "开始")
                )
        except Exception:
            pass

        self.root.after(300, self._sync_toggle_buttons)

    def _refresh_stats(self):
        try:
            self.tmdb_lbl_new_movies.config(text=f"本次新增电影：{len(TMDB.session_new_movies)}")
            self.tmdb_lbl_new_images.config(text=f"本次新增剧照：{TMDB.session_new_images}")

            if TMDB.record is not None:
                with TMDB.record_lock:
                    total_movies = len(TMDB.record.get("movie_ids", []))
                    total_images = sum(len(v) for v in TMDB.record.get("images", {}).values())
            else:
                total_movies = 0
                total_images = 0

            self.tmdb_lbl_total_movies.config(text=f"累计电影：{total_movies}")
            self.tmdb_lbl_total_images.config(text=f"累计剧照：{total_images}")
        except Exception:
            pass

        try:
            self.douban_lbl_total_photos.config(text=f"已记住图片数：{douban.get_total_recorded_photos()} 张")
            self.douban_lbl_total_subjects.config(text=f"已记住综艺数量：{douban.get_total_recorded_subjects()} 部")
            self.douban_lbl_current_subject.config(text=f"当前节目已下载：{douban.get_current_subject_count()} 张")
            self.douban_lbl_today.config(text=f"今日新增：{douban.get_today_count()} 张")
        except Exception:
            pass

        try:
            self.mtime_lbl_new_movies.config(text=f"本次新增电影：{len(MTime.session_new_movies)}")
            self.mtime_lbl_mtime_ok.config(text=f"MTime 成功：{MTime.mtime_ok}")
            self.mtime_lbl_mtime_fail.config(text=f"MTime 失败：{MTime.mtime_fail}")
            self.mtime_lbl_pending_retry.config(text=f"待重试：{MTime.get_pending_retry_count()}")
        except Exception:
            pass

        self.root.after(1000, self._refresh_stats)

    def start(self):
        self.root.mainloop()


def main():
    app = DashboardApp()
    app.start()


if __name__ == "__main__":
    main()
