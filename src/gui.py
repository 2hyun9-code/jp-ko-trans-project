"""Desktop GUI for the Japanese -> Korean game localizer (RPG Maker MV/MZ, TyranoScript, Ren'Py).

Wraps pipeline.run_all() in a background thread so the window stays
responsive, and streams its log/progress into the window via a queue.
Built with CustomTkinter for a modern look (Fluent-style, light/dark mode)
instead of stock ttk widgets.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

import keystore
import notify
import ollama_ctl
import power
import renpy_engine
import tyrano_engine
import review
from engine import detect_project
from pipeline import MODE_LABELS, REVIEW_FILENAME, run_all
from providers import PROVIDERS, FatalProviderError, ProviderError, create_provider
from textwalk import collect_glossary_names
from translator import OllamaTranslator, protect_codes, restore_codes

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

DEFAULT_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\malgun.ttf",
    r"C:\Windows\Fonts\malgunbd.ttf",
]

FALLBACK_MODELS = [
    "hf.co/hell0ks/ja-ko-vn-12b-v2-gguf:Q5_K_M",
    "kaelri/hy-mt2:7b",
    "qwen2.5:14b-instruct",
    "qwen2.5:7b-instruct",
    "aya-expanse:8b",
    "gemma2:27b",
]

WORKER_CHOICES = [str(n) for n in range(1, 17)]


def list_ollama_models() -> list[str]:
    """Queries the HTTP API (not the `ollama` CLI) so this never has the
    side effect of auto-starting the Ollama app when it's stopped -- the
    CLI does that automatically on Windows, the HTTP API does not."""
    if not ollama_ctl.is_running():
        return FALLBACK_MODELS
    try:
        import requests
        r = requests.get(f"{ollama_ctl.OLLAMA_BASE}/api/tags", timeout=3)
        r.raise_for_status()
        models = [m["name"] for m in r.json().get("models", [])]
        return models or FALLBACK_MODELS
    except Exception:
        return FALLBACK_MODELS


class LocalizerGUI:
    def __init__(self, root: ctk.CTk):
        self.root = root
        root.title("JP-KO Trans · 일본어 게임 한국어화 도구")
        root.geometry("780x720")
        root.minsize(700, 600)

        self.q: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_flag = False
        self.settings = keystore.load_settings()

        pad = {"padx": 10, "pady": 6}

        frm = ctk.CTkFrame(root, fg_color="transparent")
        frm.pack(fill="x", **pad)
        frm.columnconfigure(1, weight=1)

        self.game_var = tk.StringVar()
        self.out_var = tk.StringVar()
        self.font_var = tk.StringVar(value=self._default_font())
        self.model_var = tk.StringVar(value=self.settings["local_model"])
        self.workers_var = tk.StringVar(value=str(self.settings["workers"]))
        self.mode_var = tk.StringVar(value=MODE_LABELS.get(self.settings["mode"], MODE_LABELS["hybrid"]))
        provider_id = self.settings["provider"] if self.settings["provider"] in PROVIDERS else "google_free"
        self.provider_var = tk.StringVar(value=PROVIDERS[provider_id].label)

        self._row(frm, 0, "원본 게임 폴더", self.game_var, self._browse_game)
        self._row(frm, 1, "출력 폴더 (새로 생성됨)", self.out_var, self._browse_out)
        self._row(frm, 2, "한국어 폰트 (.ttf)", self.font_var, self._browse_font)

        ctk.CTkLabel(frm, text="번역 방식").grid(row=3, column=0, sticky="w", padx=10, pady=6)
        ctk.CTkOptionMenu(frm, variable=self.mode_var, values=list(MODE_LABELS.values()),
                          width=220, command=lambda _v: self._update_mode_widgets()).grid(
            row=3, column=1, sticky="w", padx=10, pady=6)

        ctk.CTkLabel(frm, text="API 엔진").grid(row=4, column=0, sticky="w", padx=10, pady=6)
        self.provider_menu = ctk.CTkOptionMenu(
            frm, variable=self.provider_var, values=[c.label for c in PROVIDERS.values()], width=360)
        self.provider_menu.grid(row=4, column=1, sticky="w", padx=10, pady=6)
        self.api_settings_btn = ctk.CTkButton(frm, text="API 설정...", width=110,
                                               command=self._open_api_settings)
        self.api_settings_btn.grid(row=4, column=2, padx=10, pady=6)

        ctk.CTkLabel(frm, text="로컬 모델 (Ollama)").grid(row=5, column=0, sticky="w", padx=10, pady=6)
        self.model_combo = ctk.CTkComboBox(frm, variable=self.model_var, values=FALLBACK_MODELS,
                                            width=360)
        self.model_combo.grid(row=5, column=1, sticky="we", padx=10, pady=6)
        self.download_model_btn = ctk.CTkButton(frm, text="모델 다운로드", width=110,
                                                  command=self._download_model)
        self.download_model_btn.grid(row=5, column=2, padx=10, pady=6)

        ctk.CTkLabel(frm, text="로컬 동시 요청 수").grid(row=6, column=0, sticky="w", padx=10, pady=6)
        self.workers_menu = ctk.CTkOptionMenu(frm, variable=self.workers_var,
                                              values=WORKER_CHOICES, width=90)
        self.workers_menu.grid(row=6, column=1, sticky="w", padx=10, pady=6)

        self.hangul_plugin_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(frm, text="이름 입력창 한글 지원 플러그인 추가 (RPG Maker, 게임에 이름 입력이 있을 때)",
                         variable=self.hangul_plugin_var).grid(
            row=7, column=0, columnspan=3, sticky="w", padx=10, pady=6)

        self.auto_shutdown_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(frm, text="완료 후 자동 종료 (검토 항목 없을 때만 - Ollama 끄고 PC 종료)",
                         variable=self.auto_shutdown_var).grid(
            row=8, column=0, columnspan=3, sticky="w", padx=10, pady=6)

        self.notify_close_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(frm, text="완료 후 알림 (검토 항목 없으면 프로그램+Ollama도 종료, PC는 안 끔)",
                         variable=self.notify_close_var).grid(
            row=9, column=0, columnspan=3, sticky="w", padx=10, pady=6)

        self._update_mode_widgets()

        threading.Thread(target=self._refresh_models, daemon=True).start()

        status_frm = ctk.CTkFrame(root, fg_color="transparent")
        status_frm.pack(fill="x", padx=10, pady=(0, 6))
        self.ollama_dot = tk.Canvas(status_frm, width=14, height=14, highlightthickness=0,
                                     bg=self._bg_hex(self.root))
        self.ollama_dot.pack(side="left", padx=(2, 6))
        self._ollama_dot_id = self.ollama_dot.create_oval(2, 2, 12, 12, fill="#999999",
                                                            outline="")
        self.ollama_status_var = tk.StringVar(value="Ollama 상태: 확인 중...")
        ctk.CTkLabel(status_frm, textvariable=self.ollama_status_var).pack(side="left")
        self.ollama_btn = ctk.CTkButton(status_frm, text="...", width=110,
                                         command=self._toggle_ollama, state="disabled")
        self.ollama_btn.pack(side="right")

        self._ollama_running = False
        threading.Thread(target=self._ollama_status_loop, daemon=True).start()

        btn_frm = ctk.CTkFrame(root, fg_color="transparent")
        btn_frm.pack(fill="x", **pad)
        self.start_btn = ctk.CTkButton(btn_frm, text="번역 시작", command=self._start)
        self.start_btn.pack(side="left", padx=4)
        self.cancel_btn = ctk.CTkButton(btn_frm, text="취소", command=self._cancel,
                                         state="disabled", fg_color="#a83232",
                                         hover_color="#8a2828")
        self.cancel_btn.pack(side="left", padx=4)
        self.open_btn = ctk.CTkButton(btn_frm, text="결과 폴더 열기", command=self._open_output,
                                       state="disabled")
        self.open_btn.pack(side="left", padx=4)
        self.review_btn = ctk.CTkButton(btn_frm, text="검토 · 번역 목록", command=self._open_review)
        self.review_btn.pack(side="left", padx=4)
        self.glossary_btn = ctk.CTkButton(btn_frm, text="용어집 확인/수정", command=self._open_glossary)
        self.glossary_btn.pack(side="left", padx=4)

        self.progress = ctk.CTkProgressBar(root)
        self.progress.set(0)
        self.progress.pack(fill="x", **pad)

        log_frm = ctk.CTkFrame(root, fg_color="transparent")
        log_frm.pack(fill="both", expand=True, **pad)
        self.log_text = ctk.CTkTextbox(log_frm, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True)

        self.result_path: str | None = None
        self.result_cache_path: str | None = None
        self.log_file_path: str | None = None
        root.after(100, self._poll_queue)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    @staticmethod
    def _bg_hex(widget) -> str:
        """Resolves a CTk frame's current background color to a hex string
        the plain tk.Canvas (used for the status dot) can use directly."""
        try:
            mode = 1 if ctk.get_appearance_mode() == "Dark" else 0
            color = widget.cget("fg_color")
            if isinstance(color, (list, tuple)):
                color = color[mode]
            if not color or color == "transparent":
                return "#242424" if mode else "#ebebeb"
            return color
        except Exception:  # noqa: BLE001
            return "#242424"

    def _default_font(self) -> str:
        for c in DEFAULT_FONT_CANDIDATES:
            if os.path.exists(c):
                return c
        return ""

    def _row(self, parent, row, label, var, browse_cmd):
        ctk.CTkLabel(parent, text=label).grid(row=row, column=0, sticky="w", padx=10, pady=6)
        ctk.CTkEntry(parent, textvariable=var).grid(row=row, column=1, sticky="we", padx=10, pady=6)
        ctk.CTkButton(parent, text="찾아보기...", width=90,
                       command=browse_cmd).grid(row=row, column=2, padx=10, pady=6)

    def _browse_game(self):
        path = filedialog.askdirectory(title="원본 게임 폴더 선택 (게임 exe가 있는 폴더)")
        if path:
            self.game_var.set(path)
            if not self.out_var.get():
                self.out_var.set(path + "_KO")

    def _browse_out(self):
        path = filedialog.askdirectory(title="출력 폴더 상위 위치 선택")
        if path:
            self.out_var.set(path)

    def _browse_font(self):
        path = filedialog.askopenfilename(title="한국어 지원 폰트 선택", filetypes=[("TrueType Font", "*.ttf")])
        if path:
            self.font_var.set(path)

    def _selected_mode(self) -> str:
        label = self.mode_var.get()
        return next((k for k, v in MODE_LABELS.items() if v == label), "hybrid")

    def _selected_provider_id(self) -> str:
        label = self.provider_var.get()
        return next((pid for pid, c in PROVIDERS.items() if c.label == label), "google_free")

    def _update_mode_widgets(self):
        mode = self._selected_mode()
        api_state = "disabled" if mode == "local" else "normal"
        local_state = "disabled" if mode == "api" else "normal"
        self.provider_menu.configure(state=api_state)
        self.api_settings_btn.configure(state=api_state)
        self.model_combo.configure(state=local_state)
        self.download_model_btn.configure(state=local_state)
        self.workers_menu.configure(state=local_state)

    def _open_api_settings(self):
        ApiSettingsWindow(self.root, self.settings, self._selected_provider_id(), self._log)

    def _save_run_settings(self, mode: str, provider_id: str, model: str, workers: int):
        self.settings.update(mode=mode, provider=provider_id, local_model=model, workers=workers)
        try:
            keystore.save_settings(self.settings)
        except OSError as e:
            self._log(f"설정 저장 실패 (번역은 계속합니다): {e}")

    def _refresh_models(self):
        models = list_ollama_models()
        self.q.put(("models", models))

    def _ollama_status_loop(self):
        """Polls Ollama's status every 4s. If it's unreachable but the tray
        process is still alive -- the "server died, tray app didn't notice"
        zombie state we've hit before -- auto-restarts it after a couple of
        confirmations, throttled so a genuinely broken setup doesn't loop
        forever."""
        consecutive_down = 0
        last_auto_restart = 0.0
        while True:
            running = ollama_ctl.is_running()
            self.q.put(("ollama_status", running))

            if running:
                consecutive_down = 0
            else:
                consecutive_down += 1
                now = time.time()
                if (consecutive_down >= 2 and ollama_ctl.is_tray_running()
                        and now - last_auto_restart > 60):
                    last_auto_restart = now
                    self.q.put(("log", "Ollama 응답 없음 감지 (트레이는 떠있는데 서버가 "
                                        "죽은 상태) - 자동으로 재시작합니다..."))
                    msg = ollama_ctl.restart()
                    self.q.put(("log", msg))
                    time.sleep(3)
                    self.q.put(("ollama_status", ollama_ctl.is_running()))
                    consecutive_down = 0

            time.sleep(4)

    def _confirm_ollama_stopped_then_close(self, attempts_left: int = 8):
        """Polls is_running() (non-blocking, via root.after so the GUI stays
        responsive) and re-issues a force-kill each time it's still up,
        instead of just guessing a fixed delay is long enough. Only closes
        the program once Ollama is actually confirmed dead -- if it still
        won't die after repeated force-kills, gives up on auto-closing
        (rather than closing anyway and leaving a live Ollama the user
        doesn't know about) and leaves the window open with a warning."""
        if not ollama_ctl.is_running():
            self._append_log("Ollama 종료 확인됨 -> 프로그램을 종료합니다.")
            self.root.destroy()
            return
        if attempts_left <= 0:
            self._append_log("Ollama가 반복된 강제 종료 시도에도 계속 응답합니다. "
                              "자동 종료를 포기하고 프로그램은 열어둡니다 -- 직접 확인해주세요.")
            return
        self._append_log(f"Ollama가 아직 살아있어 다시 강제 종료를 시도합니다... "
                          f"(남은 시도: {attempts_left})")
        ollama_ctl.stop()
        self.root.after(1000, lambda: self._confirm_ollama_stopped_then_close(attempts_left - 1))

    def _on_close(self):
        """Handles the window being closed manually (X button, taskbar ->
        close). If Ollama isn't running there's nothing to ask about; if it
        is, confirm before killing a process the user may still want
        running for something else."""
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(
                "번역 진행 중",
                "지금 번역이 돌고 있어요. 종료하면 번역이 중단됩니다.\n그래도 종료할까요?",
            ):
                return
        if not ollama_ctl.is_running():
            self.root.destroy()
            return
        if not messagebox.askyesno(
            "종료 확인",
            "Ollama가 작동 중입니다. Ollama도 함께 종료하시겠습니까?",
        ):
            self.root.destroy()
            return
        self._show_ollama_shutdown_dialog()

    def _show_ollama_shutdown_dialog(self):
        dlg = ctk.CTkToplevel(self.root)
        dlg.title("종료 중")
        dlg.geometry("320x110")
        dlg.resizable(False, False)
        dlg.protocol("WM_DELETE_WINDOW", lambda: None)  # block closing mid-shutdown
        dlg.grab_set()
        status_var = tk.StringVar(value="Ollama 종료중...")
        ctk.CTkLabel(dlg, textvariable=status_var, font=("", 14), wraplength=280).pack(
            expand=True, padx=20, pady=20)
        ollama_ctl.stop()
        self.root.after(1000, lambda: self._poll_ollama_shutdown_dialog(status_var, 8))

    def _poll_ollama_shutdown_dialog(self, status_var: tk.StringVar, attempts_left: int):
        if not ollama_ctl.is_running():
            status_var.set("Ollama가 종료되었습니다.")
            self.root.after(1200, self.root.destroy)
            return
        if attempts_left <= 0:
            status_var.set("Ollama 종료에 실패했습니다. 직접 확인해주세요.")
            self.root.after(2000, self.root.destroy)
            return
        ollama_ctl.stop()
        self.root.after(1000, lambda: self._poll_ollama_shutdown_dialog(status_var, attempts_left - 1))

    def _toggle_ollama(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(
                "번역 진행 중",
                "지금 번역이 돌고 있어요. Ollama를 중지하면 번역이 실패합니다.\n그래도 진행할까요?",
            ):
                return
        self.ollama_btn.configure(state="disabled")
        action = ollama_ctl.stop if self._ollama_running else ollama_ctl.start

        def run():
            msg = action()
            self.q.put(("log", msg))
            time.sleep(1.5)
            self.q.put(("ollama_status", ollama_ctl.is_running()))

        threading.Thread(target=run, daemon=True).start()

    def _log(self, msg: str):
        self.q.put(("log", msg))

    def _download_model(self):
        model = self.model_var.get().strip()
        if not model:
            messagebox.showerror("오류", "먼저 번역 모델 이름을 선택하거나 입력해주세요.")
            return
        if not self._ollama_running:
            messagebox.showwarning(
                "Ollama가 꺼져 있습니다",
                "모델을 받으려면 먼저 Ollama를 실행해야 해요.",
            )
            return

        self.download_model_btn.configure(state="disabled")
        self._log(f"=== {model} 다운로드 시작 (용량에 따라 몇 분~수십 분 걸릴 수 있어요) ===")

        def run():
            try:
                creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                result = subprocess.run(
                    ["ollama", "pull", model],
                    capture_output=True, text=True, timeout=3600,
                    creationflags=creationflags,
                )
                if result.returncode == 0:
                    self.q.put(("log", f"=== {model} 다운로드 완료 ==="))
                    self.q.put(("models", list_ollama_models()))
                else:
                    err = (result.stderr or result.stdout or "알 수 없는 오류").strip()
                    self.q.put(("log", f"=== 다운로드 실패: {err} ==="))
            except Exception as e:  # noqa: BLE001
                self.q.put(("log", f"=== 다운로드 실패: {e} ==="))
            finally:
                self.q.put(("download_done", None))

        threading.Thread(target=run, daemon=True).start()

    @staticmethod
    def _cache_path_for(out: str) -> str:
        """The translation cache lives next to (not inside) the output
        folder, named after it, so it survives the output folder being
        deleted and recreated on a retried run."""
        return os.path.abspath(os.path.join(out, "..", os.path.basename(out) + "_translations.json"))

    @staticmethod
    def _log_path_for(out: str) -> str:
        """A plain-text mirror of everything that goes into the log box,
        written live as the run progresses -- so a stall can be inspected
        (e.g. by reading the file directly) without needing the app window
        itself or a screenshot."""
        return os.path.abspath(os.path.join(out, "..", os.path.basename(out) + "_log.txt"))

    def _start(self):
        game = self.game_var.get().strip()
        out = self.out_var.get().strip()
        font = self.font_var.get().strip()
        model = self.model_var.get().strip()
        mode = self._selected_mode()
        provider_id = self._selected_provider_id()

        if mode == "local" and not self._ollama_running:
            messagebox.showwarning(
                "Ollama가 꺼져 있습니다",
                "로컬 번역을 하려면 먼저 Ollama를 실행해야 해요.\n"
                "위의 'Ollama 시작' 버튼을 눌러 켠 뒤 다시 시도해주세요.",
            )
            return
        if mode == "hybrid" and not self._ollama_running:
            if not messagebox.askyesno(
                "Ollama가 꺼져 있습니다",
                "Ollama가 꺼져 있어서 API가 놓친 문장을 로컬 모델로 보완할 수 없어요.\n\n"
                "이번에는 API만으로 진행할까요?\n"
                "('아니오'를 누르면 취소되고, Ollama를 켠 뒤 다시 시작할 수 있어요.)",
            ):
                return
            mode = "api"

        provider = None
        if mode != "local":
            cfg = keystore.provider_config(self.settings, provider_id)
            provider = create_provider(provider_id, **cfg)
            try:
                provider.validate()
            except FatalProviderError as e:
                messagebox.showerror("API 설정 필요", str(e))
                self._open_api_settings()
                return

        if not game:
            messagebox.showerror("오류", "원본 게임 폴더를 선택해주세요.")
            return
        if not out:
            messagebox.showerror("오류", "출력 폴더를 지정해주세요.")
            return
        if os.path.exists(out):
            answer = messagebox.askyesno(
                "출력 폴더가 이미 존재합니다",
                f"{out}\n\n이 폴더가 이미 있어요. 보통 이전에 실패했거나 중단된 작업의 "
                "결과물입니다.\n\n삭제하고 원본에서 새로 복사해서 진행할까요?\n"
                "('아니오'를 누르면 취소되고, 출력 폴더 경로를 직접 바꿔서 다시 시작할 수 있어요.)",
            )
            if not answer:
                return
            try:
                shutil.rmtree(out)
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("오류", f"출력 폴더 삭제에 실패했습니다:\n{e}")
                return
        if font and not os.path.exists(font):
            messagebox.showerror("오류", f"폰트 파일을 찾을 수 없습니다:\n{font}")
            return

        self.cancel_flag = False
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.open_btn.configure(state="disabled")
        self.progress.set(0)
        self._clear_log()

        cache_path = self._cache_path_for(out)
        self.result_path = None
        self.result_cache_path = cache_path

        self.log_file_path = self._log_path_for(out)
        engines = f"로컬 모델: {model}" if provider is None else (
            f"API: {provider.label}" + ("" if mode == "api" else f", 로컬 모델: {model}"))
        try:
            with open(self.log_file_path, "w", encoding="utf-8") as f:
                f.write(f"=== 번역 시작 {time.strftime('%Y-%m-%d %H:%M:%S')} "
                        f"({MODE_LABELS[mode]} / {engines}) ===\n")
        except Exception:  # noqa: BLE001
            self.log_file_path = None

        try:
            workers = max(1, int(self.workers_var.get()))
        except ValueError:
            workers = 4
        # Remember the choice (not the run-only "api" downgrade for a stopped Ollama).
        self._save_run_settings(self._selected_mode(), provider_id, model, workers)

        self.worker = threading.Thread(
            target=self._run_worker,
            args=(game, out, font or None, model, cache_path, workers,
                  self.hangul_plugin_var.get(), mode, provider),
            daemon=True,
        )
        self.worker.start()

    def _run_worker(self, game, out, font, model, cache_path, workers, install_hangul_plugin,
                    mode, provider):
        try:
            def progress(done, total):
                self.q.put(("progress", (done, max(total, 1))))

            def should_cancel():
                return self.cancel_flag

            result = run_all(
                game=game, out=out, font=font, model=model, cache_path=cache_path,
                log=self._log, progress=progress, should_cancel=should_cancel,
                workers=workers, install_hangul_plugin=install_hangul_plugin,
                mode=mode, provider=provider,
            )
            self.q.put(("done", result))
        except InterruptedError:
            self.q.put(("cancelled", None))
        except Exception as e:  # noqa: BLE001
            self.q.put(("error", str(e)))

    def _cancel(self):
        self.cancel_flag = True
        self.cancel_btn.configure(state="disabled")
        self._log("취소 요청됨... 현재 번역이 끝나면 중단됩니다.")

    def _open_output(self):
        if self.result_path and os.path.exists(self.result_path):
            os.startfile(self.result_path)  # noqa: S606 (Windows-only helper)

    def _review_path(self) -> Path | None:
        if not self.result_path:
            return None
        p = Path(self.result_path) / REVIEW_FILENAME
        return p if p.exists() else None

    def _review_count(self) -> int:
        p = self._review_path()
        if not p:
            return 0
        try:
            return len(json.loads(p.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            return 0

    def _open_review(self):
        """Opens the review table for this run's output, or -- without a run
        in this session -- for whatever output folder is filled in."""
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("번역 진행 중", "번역이 끝난 뒤에 검토할 수 있어요.")
            return
        out = self.result_path or self.out_var.get().strip()
        if not out:
            messagebox.showerror("오류", "출력 폴더를 먼저 지정해주세요.")
            return
        cache_path = self.result_cache_path or self._cache_path_for(out)
        try:
            session = review.open_session(out, cache_path)
        except FileNotFoundError:
            messagebox.showinfo("검토할 번역 없음",
                                "이 출력 폴더에는 아직 번역 결과가 없어요. 먼저 '번역 시작'을 해주세요.")
            return
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("오류", f"검토 목록을 읽지 못했습니다:\n{e}")
            return
        try:
            workers = max(1, int(self.workers_var.get()))
        except ValueError:
            workers = 4
        ReviewWindow(self.root, session, self.model_var.get().strip() or session.local_model,
                     workers, self._log)

    def _open_glossary(self):
        game = self.game_var.get().strip()
        out = self.out_var.get().strip()
        model = self.model_var.get().strip()
        if not game:
            messagebox.showerror("오류", "먼저 원본 게임 폴더를 선택해주세요.")
            return
        if not out:
            messagebox.showerror("오류", "먼저 출력 폴더를 지정해주세요 (용어집도 번역 캐시 파일에 저장돼요).")
            return
        if not model:
            messagebox.showerror("오류", "번역 모델을 선택해주세요.")
            return
        try:
            layout = detect_project(game)
            if layout.engine == "RENPY":
                names = renpy_engine.extract(layout.root / "game").names  # read-only
            elif layout.engine in ("TYRANO", "ASAR") and (
                    tc := tyrano_engine.find_container(Path(game), include_asar=True)):
                names = tyrano_engine.scan(tc.read_scenarios()).names  # read-only
            else:
                names = collect_glossary_names(layout)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("오류", f"게임 폴더를 분석하지 못했습니다:\n{e}")
            return
        if not names:
            messagebox.showinfo("용어집 없음", "이 게임에서 고유명사(이름) 항목을 찾지 못했어요.")
            return

        cache_path = self._cache_path_for(out)
        translator = OllamaTranslator(model=model, cache_path=cache_path)
        items = [{"source": n, "translated": translator.cache.get(n) or ""} for n in sorted(names)]
        GlossaryWindow(self.root, model, cache_path, items, self._log)

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _append_log(self, msg: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        if self.log_file_path:
            try:
                with open(self.log_file_path, "a", encoding="utf-8") as f:
                    f.write(msg + "\n")
            except Exception:  # noqa: BLE001
                pass  # log file is a debugging aid, never worth failing the run over

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._append_log(str(payload))
                elif kind == "progress":
                    done, total = payload
                    self.progress.set(done / max(total, 1))
                elif kind == "models":
                    self.model_combo.configure(values=payload)
                elif kind == "download_done":
                    self.download_model_btn.configure(state="normal")
                elif kind == "ollama_status":
                    self._ollama_running = bool(payload)
                    if payload:
                        self.ollama_dot.itemconfig(self._ollama_dot_id, fill="#2ecc71")
                        self.ollama_status_var.set("Ollama 상태: 실행 중")
                        self.ollama_btn.configure(text="Ollama 중지", state="normal")
                    else:
                        self.ollama_dot.itemconfig(self._ollama_dot_id, fill="#999999")
                        self.ollama_status_var.set("Ollama 상태: 중지됨")
                        self.ollama_btn.configure(text="Ollama 시작", state="normal")
                elif kind == "done":
                    self.result_path = payload
                    self._append_log("=== 완료 ===")
                    self.start_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                    self.open_btn.configure(state="normal")
                    review_count = self._review_count()

                    if self.notify_close_var.get() or self.auto_shutdown_var.get():
                        body = (f"검토 필요 항목 {review_count}개" if review_count
                                else "검토 항목 없음")
                        self._append_log(notify.notify("JP-KO Trans: 번역 완료", body))

                    if review_count == 0 and self.auto_shutdown_var.get():
                        self._append_log("검토 항목 없음 + 자동 종료 옵션 켜짐 -> Ollama를 끄고 "
                                          "PC 종료를 예약합니다.")
                        self._append_log(ollama_ctl.stop())
                        self._append_log(power.schedule_shutdown(
                            60, "JP-KO Trans: 번역 완료, 검토 항목 없음 - 자동 종료"))
                    elif review_count == 0 and self.notify_close_var.get():
                        self._append_log("검토 항목 없음 + 알림 옵션 켜짐 -> Ollama 종료를 "
                                          "확인한 뒤 프로그램을 닫습니다.")
                        self._append_log(ollama_ctl.stop())
                        self._confirm_ollama_stopped_then_close()
                    elif review_count:
                        messagebox.showinfo(
                            "완료",
                            f"번역이 완료되었습니다:\n{payload}\n\n"
                            f"검토가 필요한 항목이 {review_count}개 있어요 "
                            f"('검토 · 번역 목록' 버튼으로 확인).",
                        )
                    else:
                        messagebox.showinfo("완료", f"번역이 완료되었습니다:\n{payload}")
                elif kind == "cancelled":
                    self._append_log("=== 취소됨 ===")
                    self.start_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                elif kind == "error":
                    self._append_log(f"=== 오류: {payload} ===")
                    self.start_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                    messagebox.showerror("오류", str(payload))
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)


class _TermListWindow(ctk.CTkToplevel):
    """List+detail term editor used for the pre-run glossary: a scrollable
    list of source strings on the left, an editable translation box on the
    right. Glossaries are small, so one button per entry is fine here (the
    post-run review list uses a table instead -- see ReviewWindow)."""

    def __init__(self, parent, title: str, log_label: str, model: str,
                 cache_path: str, items: list[dict], log_fn):
        super().__init__(parent)
        self.title(title)
        self.geometry("800x500")
        self.log_label = log_label
        self.model = model
        self.cache_path = cache_path
        self.items = items
        self.log_fn = log_fn
        self.selected_idx: int | None = None
        self.item_buttons: list[ctk.CTkButton] = []

        pane = ctk.CTkFrame(self, fg_color="transparent")
        pane.pack(fill="both", expand=True, padx=10, pady=10)

        left = ctk.CTkScrollableFrame(pane, width=280, label_text="항목 목록")
        left.pack(side="left", fill="y")
        self.list_frame = left

        right = ctk.CTkFrame(pane, fg_color="transparent")
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))

        ctk.CTkLabel(right, text="원문").pack(anchor="w", pady=(8, 0))
        self.source_text = ctk.CTkTextbox(right, height=80, wrap="word", state="disabled")
        self.source_text.pack(fill="x")

        ctk.CTkLabel(right, text="번역 (직접 고칠 수 있어요)").pack(anchor="w", pady=(8, 0))
        self.translated_text = ctk.CTkTextbox(right, height=140, wrap="word")
        self.translated_text.pack(fill="both", expand=True)

        btn_row = ctk.CTkFrame(right, fg_color="transparent")
        btn_row.pack(fill="x", pady=8)
        self.save_btn = ctk.CTkButton(btn_row, text="저장", command=self._save)
        self.save_btn.pack(side="left", padx=4)
        self.retranslate_btn = ctk.CTkButton(btn_row, text="번역 받기", command=self._retranslate)
        self.retranslate_btn.pack(side="left", padx=4)
        ctk.CTkButton(btn_row, text="닫기", fg_color="#555", hover_color="#444",
                       command=self.destroy).pack(side="right", padx=4)

        self._refresh_listbox()
        if self.items:
            self._select(0)

    def _list_label(self, item: dict) -> str:
        mark = "✓" if item.get("translated") else "…"
        return f"[{mark}] {item['source'][:22]}"

    def _refresh_listbox(self):
        for b in self.item_buttons:
            b.destroy()
        self.item_buttons = []
        for i, it in enumerate(self.items):
            btn = ctk.CTkButton(
                self.list_frame, text=self._list_label(it), anchor="w",
                fg_color="transparent", text_color=("black", "white"),
                hover_color=("#dddddd", "#333333"),
                command=lambda idx=i: self._select(idx),
            )
            btn.pack(fill="x", pady=2)
            self.item_buttons.append(btn)
        self._highlight_selected()

    def _highlight_selected(self):
        for i, b in enumerate(self.item_buttons):
            if i == self.selected_idx:
                b.configure(fg_color=("#c7dcff", "#254a7a"))
            else:
                b.configure(fg_color="transparent")

    def _select(self, idx: int):
        self.selected_idx = idx
        self._highlight_selected()
        item = self.items[idx]
        self.source_text.configure(state="normal")
        self.source_text.delete("1.0", "end")
        self.source_text.insert("1.0", item["source"])
        self.source_text.configure(state="disabled")
        self.translated_text.delete("1.0", "end")
        self.translated_text.insert("1.0", item.get("translated", ""))

    def _save(self):
        if self.selected_idx is None:
            return
        item = self.items[self.selected_idx]
        new_value = self.translated_text.get("1.0", "end").strip()
        if new_value == item.get("translated", ""):
            return
        try:
            translator = OllamaTranslator(model=self.model, cache_path=self.cache_path)
            translator.cache.set(item["source"], new_value, origin="manual")
            translator.cache.save()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("오류", f"저장에 실패했습니다:\n{e}")
            return
        item["translated"] = new_value
        self.log_fn(f"{self.log_label}: 수정 저장됨 -> {item['source'][:30]}")
        self._refresh_listbox()

    def _retranslate(self):
        if self.selected_idx is None:
            return
        item = self.items[self.selected_idx]
        self.save_btn.configure(state="disabled")
        self.retranslate_btn.configure(state="disabled")

        def run():
            try:
                translator = OllamaTranslator(model=self.model, cache_path=self.cache_path)
                new_value = translator.translate(item["source"], force=True)
                translator.cache.save()
            except Exception as e:  # noqa: BLE001
                msg = f"번역에 실패했습니다:\n{e}"  # `e` is unbound once the except block ends
                self.after(0, lambda: messagebox.showerror("오류", msg))
                self.after(0, self._retranslate_done, None)
                return
            self.after(0, self._retranslate_done, new_value)

        threading.Thread(target=run, daemon=True).start()

    def _retranslate_done(self, new_value):
        self.save_btn.configure(state="normal")
        self.retranslate_btn.configure(state="normal")
        if new_value is None or self.selected_idx is None:
            return
        idx = self.selected_idx
        self.items[idx]["translated"] = new_value
        self.log_fn(f"{self.log_label}: 번역 받음 -> {self.items[idx]['source'][:30]}")
        self._refresh_listbox()
        self._select(idx)


class GlossaryWindow(_TermListWindow):
    """Pre-run editor for proper nouns (actor/class/item/... names, map
    display names, MZ name-box speaker names) -- lets you pin a name's
    Korean rendering by hand (or fetch one from the model) before starting
    the main translation, so run_all()'s glossary pass picks it up and the
    same choice gets used everywhere that name appears. Only ever writes to
    the translation cache; there's no output folder yet to patch."""

    def __init__(self, parent, model: str, cache_path: str, items: list[dict], log_fn):
        super().__init__(parent, f"용어집 ({len(items)}개)", "용어집", model,
                          cache_path, items, log_fn)


def _preview(text: str, width: int = 70) -> str:
    one_line = text.replace("\n", " ⏎ ")
    return one_line if len(one_line) <= width else one_line[:width - 1] + "…"


class ReviewWindow(ctk.CTkToplevel):
    """Post-run review: a filterable table of translations (only the flagged
    ones, or every text in the game), hand edits, and re-translating a
    multi-selection with the local model. Changes pile up in `pending` and
    go into the output game in one batch ("게임에 적용")."""

    ROW_LIMIT = 20000
    _VIEWS = {"flagged": "검토 필요 항목", "all": "전체 번역"}

    def __init__(self, parent, session: review.ReviewSession, local_model: str,
                 workers: int, log_fn):
        super().__init__(parent)
        self.session = session
        self.local_model = local_model
        self.workers = workers
        self.log_fn = log_fn
        self.pending: dict[str, str | None] = {}
        self.rows: list[dict] = []
        self.busy = False
        self.cancel_flag = False
        self.q: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.title("검토 · 번역 목록")
        self.geometry("1150x720")
        self.minsize(900, 560)
        self.after(150, self.lift)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=12, pady=(12, 6))
        self.view_var = tk.StringVar(value=self._VIEWS["flagged" if session.flagged else "all"])
        ctk.CTkLabel(bar, text="보기").pack(side="left", padx=(0, 6))
        ctk.CTkOptionMenu(bar, variable=self.view_var, values=list(self._VIEWS.values()),
                          width=150, command=lambda _v: self._reload()).pack(side="left")
        self.origin_var = tk.StringVar(value=review.ORIGIN_FILTERS["all"])
        ctk.CTkLabel(bar, text="출처").pack(side="left", padx=(16, 6))
        ctk.CTkOptionMenu(bar, variable=self.origin_var, values=list(review.ORIGIN_FILTERS.values()),
                          width=130, command=lambda _v: self._reload()).pack(side="left")
        self.search_entry = ctk.CTkEntry(bar, placeholder_text="원문/번역 검색 (Enter)", width=260)
        self.search_entry.pack(side="left", padx=(16, 6))
        self.search_entry.bind("<Return>", lambda _e: self._reload())
        ctk.CTkButton(bar, text="검색", width=60, command=self._reload).pack(side="left")
        self.count_var = tk.StringVar()
        ctk.CTkLabel(bar, textvariable=self.count_var).pack(side="right")

        table = ctk.CTkFrame(self, fg_color="transparent")
        table.pack(fill="both", expand=True, padx=12)
        self._style_tree()
        cols = ("origin", "reason", "source", "translated")
        self.tree = ttk.Treeview(table, columns=cols, show="headings", selectmode="extended",
                                 style="Review.Treeview")
        for col, text, width, stretch in (("origin", "출처", 180, False), ("reason", "사유", 90, False),
                                          ("source", "원문", 420, True),
                                          ("translated", "번역", 420, True)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, stretch=stretch, anchor="w")
        scroll = ctk.CTkScrollbar(table, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._on_select())

        detail = ctk.CTkFrame(self, fg_color="transparent")
        detail.pack(fill="x", padx=12, pady=(8, 0))
        detail.columnconfigure((0, 1), weight=1)
        self.detail_var = tk.StringVar(value="항목을 고르세요. Ctrl/Shift+클릭으로 여러 개 선택할 수 있어요.")
        ctk.CTkLabel(detail, textvariable=self.detail_var, anchor="w").grid(
            row=0, column=0, columnspan=2, sticky="we")
        self.source_box = ctk.CTkTextbox(detail, height=110, wrap="word", state="disabled")
        self.source_box.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=4)
        self.translated_box = ctk.CTkTextbox(detail, height=110, wrap="word")
        self.translated_box.grid(row=1, column=1, sticky="nsew", padx=(6, 0), pady=4)
        self.translated_box.bind("<Control-s>", lambda _e: (self._save_manual(), "break")[1])

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", padx=12, pady=(4, 12))
        self.save_btn = ctk.CTkButton(actions, text="수정 저장 (Ctrl+S)", width=140,
                                      command=self._save_manual, state="disabled")
        self.save_btn.pack(side="left", padx=(0, 6))
        self.retranslate_btn = ctk.CTkButton(actions, text="선택 항목 로컬로 재번역", width=210,
                                             command=self._retranslate, state="disabled")
        self.retranslate_btn.pack(side="left", padx=6)
        self.cancel_btn = ctk.CTkButton(actions, text="중단", width=70, fg_color="#a83232",
                                        hover_color="#8a2828", command=self._cancel)
        self.status_var = tk.StringVar()
        ctk.CTkLabel(actions, textvariable=self.status_var).pack(side="left", padx=10)
        ctk.CTkButton(actions, text="닫기", width=80, fg_color="#555", hover_color="#444",
                      command=self._on_close).pack(side="right")
        self.apply_btn = ctk.CTkButton(actions, text="게임에 적용", width=150,
                                       command=self._apply, state="disabled")
        self.apply_btn.pack(side="right", padx=6)

        self._reload()
        self.after(100, self._poll)

    # ------------------------------------------------------------ table
    def _style_tree(self):
        dark = ctk.get_appearance_mode() == "Dark"
        bg, fg, sel, head = (("#2b2b2b", "#e6e6e6", "#1f538d", "#333333") if dark
                             else ("#ffffff", "#1a1a1a", "#c7dcff", "#e8e8e8"))
        style = ttk.Style(self)
        style.theme_use("clam")
        # Segoe UI + Windows font linking renders both kana and Hangul with
        # normal spacing; Malgun Gothic has no kana and its fallback spreads
        # Japanese out letter by letter.
        style.configure("Review.Treeview", background=bg, fieldbackground=bg, foreground=fg,
                        rowheight=26, font=("Segoe UI", 10), borderwidth=0)
        style.configure("Review.Treeview.Heading", background=head, foreground=fg,
                        font=("Segoe UI", 10, "bold"), relief="flat")
        style.map("Review.Treeview", background=[("selected", sel)],
                  foreground=[("selected", "#ffffff" if dark else "#000000")])

    def _filters(self) -> tuple[str, str, str]:
        view = next(k for k, v in self._VIEWS.items() if v == self.view_var.get())
        origin = next(k for k, v in review.ORIGIN_FILTERS.items() if v == self.origin_var.get())
        return view, origin, self.search_entry.get()

    def _row_values(self, row: dict) -> tuple:
        reason = review.REASON_LABELS.get(row["reason"] or "", "")
        if row["guarded"] and not reason:
            reason = "이름 보호"
        return (review.origin_label(row["origin"], row["translated"]), reason,
                _preview(row["source"]), _preview(row["translated"]))

    def _reload(self):
        view, origin, query = self._filters()
        self.rows = review.rows(self.session, view, origin, query, limit=self.ROW_LIMIT + 1)
        truncated = len(self.rows) > self.ROW_LIMIT
        self.rows = self.rows[:self.ROW_LIMIT]
        self.tree.delete(*self.tree.get_children())
        for i, row in enumerate(self.rows):
            self.tree.insert("", "end", iid=str(i), values=self._row_values(row))
        more = f" (최대 {self.ROW_LIMIT}개까지만 표시 -- 검색/필터로 좁혀주세요)" if truncated else ""
        self.count_var.set(f"{len(self.rows)}개{more}")
        self._on_select()

    def _selected(self) -> list[dict]:
        return [self.rows[int(iid)] for iid in self.tree.selection()]

    def _on_select(self):
        sel = self._selected()
        idle = "disabled" if self.busy else "normal"
        self.retranslate_btn.configure(
            text=f"선택 {len(sel)}개 로컬로 재번역" if sel else "선택 항목 로컬로 재번역",
            state=idle if sel else "disabled")
        self.save_btn.configure(state=idle if len(sel) == 1 else "disabled")
        self.source_box.configure(state="normal")
        self.source_box.delete("1.0", "end")
        self.translated_box.delete("1.0", "end")
        if len(sel) == 1:
            row = sel[0]
            self.source_box.insert("1.0", row["source"])
            self.translated_box.insert("1.0", row["translated"])
            note = " · 플러그인이 이름으로 찾는 값이라 데이터에는 원문, 화면에만 번역 표시" if row["guarded"] else ""
            self.detail_var.set(f"{review.origin_label(row['origin'], row['translated'])}{note}")
        elif sel:
            self.detail_var.set(f"{len(sel)}개 선택됨")
        self.source_box.configure(state="disabled")

    def _refresh_rows(self, sources: set[str]):
        for i, row in enumerate(self.rows):
            if row["source"] in sources:
                tr = self.session.cache.get(row["source"]) or ""
                origin = self.session.cache.get_origin(row["source"])
                row.update(translated=tr, origin=origin, kind=review.origin_kind(origin, tr),
                           reason=review._reason_for(row["source"], tr or None))
                self.tree.item(str(i), values=self._row_values(row))
        self._on_select()
        self._update_apply_btn()

    def _update_apply_btn(self):
        n = len(self.pending)
        self.apply_btn.configure(text=f"게임에 적용 ({n}개 대기)" if n else "게임에 적용",
                                 state="normal" if n and not self.busy else "disabled")

    # ------------------------------------------------------------ edits
    def _save_manual(self):
        sel = self._selected()
        if len(sel) != 1 or self.busy:
            return
        src = sel[0]["source"]
        value = self.translated_box.get("1.0", "end").rstrip("\n")
        if not value.strip() or value == sel[0]["translated"]:
            return
        review.set_manual(self.session, src, value, self.pending)
        self.log_fn(f"검토: 수정 저장됨 -> {src[:30]}")
        self._refresh_rows({src})

    def _retranslate(self):
        sources = [r["source"] for r in self._selected()]
        if not sources or self.busy:
            return
        if not ollama_ctl.is_running():
            messagebox.showwarning("Ollama가 꺼져 있습니다",
                                   "로컬 재번역을 하려면 메인 창에서 Ollama를 먼저 켜주세요.", parent=self)
            return
        if len(sources) > 200 and not messagebox.askyesno(
                "재번역", f"{len(sources)}개를 로컬 모델({self.local_model})로 다시 번역합니다.\n"
                          "시간이 꽤 걸릴 수 있어요. 진행할까요?", parent=self):
            return
        self._set_busy(True, f"로컬 재번역 0/{len(sources)}")
        self.cancel_flag = False

        def run():
            changes, error = review.retranslate_local(
                self.session, sources, self.local_model, self.workers,
                progress=lambda d, t: self.q.put(("status", f"로컬 재번역 {d}/{t}")),
                should_cancel=lambda: self.cancel_flag,
                log=lambda m: self.q.put(("log", m)))
            self.q.put(("retranslated", (changes, error, len(sources))))

        threading.Thread(target=run, daemon=True).start()

    def _cancel(self):
        self.cancel_flag = True
        self.status_var.set("중단하는 중... (진행 중인 문장은 마저 끝납니다)")

    def _apply(self, then_close: bool = False):
        if not self.pending or self.busy:
            if then_close:
                self.destroy()
            return
        changes = dict(self.pending)
        self._set_busy(True, f"게임에 적용 중... ({len(changes)}개)")

        def run():
            try:
                written = review.apply_changes(self.session, changes)
                self.q.put(("applied", (changes, written, None, then_close)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("applied", (changes, 0, str(e), then_close)))

        threading.Thread(target=run, daemon=True).start()

    def _set_busy(self, busy: bool, status: str = ""):
        self.busy = busy
        self.status_var.set(status)
        if busy:
            self.cancel_btn.pack(side="left", padx=6, after=self.retranslate_btn)
        else:
            self.cancel_btn.pack_forget()
        self._on_select()
        self._update_apply_btn()

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "status":
                    self.status_var.set(payload)
                elif kind == "log":
                    self.log_fn(payload)
                elif kind == "retranslated":
                    changes, error, total = payload
                    for src, before in changes.items():
                        self.pending.setdefault(src, before)
                    self._set_busy(False, f"로컬 재번역 {len(changes)}/{total}개 완료")
                    self._refresh_rows(set(changes))
                    self.log_fn(f"검토: 로컬 재번역 {len(changes)}/{total}개")
                    if error:
                        messagebox.showerror("재번역 중단", f"{len(changes)}개까지 하고 멈췄어요:\n{error}",
                                             parent=self)
                elif kind == "applied":
                    changes, written, error, then_close = payload
                    if error:
                        self._set_busy(False, "적용 실패")
                        messagebox.showerror("적용 실패", error, parent=self)
                        continue
                    for src in changes:
                        self.pending.pop(src, None)
                    self._set_busy(False, f"게임에 적용 완료 ({len(changes)}개)")
                    self.log_fn(f"검토: {len(changes)}개 게임에 적용 (데이터 파일 {written}개 반영, "
                                f"나머지는 화면 출력 패치로)")
                    if then_close:
                        self.destroy()
                        return
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._poll)

    def _on_close(self):
        if self.busy:
            if not messagebox.askyesno("작업 중", "재번역/적용이 진행 중이에요. 중단하고 닫을까요?",
                                       parent=self):
                return
            self.cancel_flag = True
        if self.pending and not self.busy:
            answer = messagebox.askyesnocancel(
                "적용 안 된 변경", f"게임에 아직 적용하지 않은 변경이 {len(self.pending)}개 있어요.\n"
                                  "지금 적용할까요?\n\n(아니오: 번역 캐시에는 남아 있어서, 다음에 "
                                  "'번역 시작'을 하면 반영됩니다)", parent=self)
            if answer is None:
                return
            if answer:
                self._apply(then_close=True)
                return
        self.destroy()


class ApiSettingsWindow(ctk.CTkToplevel):
    """Key / model / address for one API engine. The key is written only
    through keystore (DPAPI-encrypted) and never goes to the log."""

    _TEST_TEXT = "\\N[1]さん、こんにちは。元気ですか？"

    def __init__(self, parent, settings: dict, provider_id: str, log_fn):
        super().__init__(parent)
        self.settings = settings
        self.provider_id = provider_id
        self.cls = PROVIDERS[provider_id]
        self.log_fn = log_fn
        self.title(f"API 설정 - {self.cls.label}")
        self.geometry("600x420")
        self.after(150, self.lift)

        cfg = keystore.provider_config(settings, provider_id)
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=14, pady=12)
        body.columnconfigure(1, weight=1)

        ctk.CTkLabel(body, text=self.cls.label, font=ctk.CTkFont(size=15, weight="bold")).grid(
            row=0, column=0, columnspan=3, sticky="w")
        ctk.CTkLabel(body, text=self.cls.note, wraplength=560, justify="left").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(4, 10))

        row = 2
        self.key_entry = None
        if self.cls.needs_key or self.cls.needs_base_url:
            label = "API 키" if self.cls.needs_key else "API 키 (선택)"
            ctk.CTkLabel(body, text=label).grid(row=row, column=0, sticky="w", pady=4)
            self.key_entry = ctk.CTkEntry(body, show="•")
            self.key_entry.grid(row=row, column=1, sticky="we", padx=8, pady=4)
            if cfg["key"]:
                self.key_entry.insert(0, cfg["key"])
            self.show_key_var = tk.BooleanVar(value=False)
            ctk.CTkCheckBox(body, text="보기", width=60, variable=self.show_key_var,
                            command=self._toggle_key).grid(row=row, column=2, pady=4)
            row += 1

        self.model_entry = None
        if self.cls.is_llm:
            ctk.CTkLabel(body, text="모델 이름").grid(row=row, column=0, sticky="w", pady=4)
            self.model_entry = ctk.CTkEntry(body, placeholder_text=self.cls.model_hint)
            self.model_entry.grid(row=row, column=1, columnspan=2, sticky="we", padx=8, pady=4)
            model = cfg["model"] or self.cls.default_model
            if model:
                self.model_entry.insert(0, model)
            row += 1

        self.url_entry = None
        if self.cls.needs_base_url:
            ctk.CTkLabel(body, text="주소").grid(row=row, column=0, sticky="w", pady=4)
            self.url_entry = ctk.CTkEntry(body, placeholder_text=self.cls.default_base_url)
            self.url_entry.grid(row=row, column=1, columnspan=2, sticky="we", padx=8, pady=4)
            self.url_entry.insert(0, cfg["base_url"] or self.cls.default_base_url)
            row += 1

        if not (self.cls.needs_key or self.cls.is_llm or self.cls.needs_base_url):
            ctk.CTkLabel(body, text="이 엔진은 따로 설정할 것이 없습니다. 바로 쓸 수 있어요.").grid(
                row=row, column=0, columnspan=3, sticky="w", pady=4)
            row += 1

        ctk.CTkLabel(body, text="키는 이 컴퓨터의 Windows 계정으로 암호화해서 저장합니다 "
                                "(다른 PC나 계정에서는 풀 수 없음).",
                     text_color=("gray40", "gray65"), wraplength=560, justify="left").grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(8, 4))
        row += 1

        self.result_var = tk.StringVar(value="")
        ctk.CTkLabel(body, textvariable=self.result_var, wraplength=560, justify="left").grid(
            row=row, column=0, columnspan=3, sticky="w", pady=6)
        row += 1

        btns = ctk.CTkFrame(body, fg_color="transparent")
        btns.grid(row=row, column=0, columnspan=3, sticky="we", pady=(8, 0))
        self.test_btn = ctk.CTkButton(btns, text="연결 테스트", width=110, command=self._test)
        self.test_btn.pack(side="left", padx=4)
        ctk.CTkButton(btns, text="저장", width=90, command=self._save).pack(side="left", padx=4)
        if self.key_entry is not None:
            ctk.CTkButton(btns, text="키 삭제", width=90, fg_color="#a83232", hover_color="#8a2828",
                          command=self._delete_key).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="닫기", width=90, fg_color="#555", hover_color="#444",
                      command=self.destroy).pack(side="right", padx=4)

    def _toggle_key(self):
        self.key_entry.configure(show="" if self.show_key_var.get() else "•")

    def _values(self) -> tuple[str, str, str]:
        key = self.key_entry.get().strip() if self.key_entry is not None else ""
        model = self.model_entry.get().strip() if self.model_entry is not None else ""
        url = self.url_entry.get().strip() if self.url_entry is not None else ""
        return key, model, url

    def _save(self):
        key, model, url = self._values()
        try:
            keystore.set_provider_config(self.settings, self.provider_id, key, model, url)
            keystore.save_settings(self.settings)
        except OSError as e:
            messagebox.showerror("저장 실패", f"설정을 저장하지 못했습니다:\n{e}", parent=self)
            return
        self.result_var.set("저장했습니다.")
        self.log_fn(f"API 설정 저장됨: {self.cls.label}")

    def _delete_key(self):
        if not messagebox.askyesno("키 삭제", "저장된 API 키를 지울까요?", parent=self):
            return
        self.key_entry.delete(0, "end")
        _key, model, url = self._values()
        keystore.set_provider_config(self.settings, self.provider_id, "", model, url)
        keystore.save_settings(self.settings)
        self.result_var.set("키를 삭제했습니다.")

    def _test(self):
        key, model, url = self._values()
        provider = create_provider(self.provider_id, key, model, url)
        self.test_btn.configure(state="disabled")
        self.result_var.set("테스트 중...")

        def run():
            protected, mapping = protect_codes(self._TEST_TEXT)
            try:
                provider.validate()
                out = restore_codes(provider.translate_batch([protected])[0], mapping)
                msg = f"성공: {self._TEST_TEXT}  →  {out}"
            except (FatalProviderError, ProviderError) as e:
                msg = f"실패: {e}"
            except Exception as e:  # noqa: BLE001
                msg = f"실패: {type(e).__name__}: {e}"
            self.after(0, self._test_done, msg)

        threading.Thread(target=run, daemon=True).start()

    def _test_done(self, msg: str):
        if self.winfo_exists():
            self.test_btn.configure(state="normal")
            self.result_var.set(msg)


def main():
    root = ctk.CTk()
    LocalizerGUI(root)
    root.mainloop()
    # mainloop() returning means the window is gone, but daemon threads
    # (ollama status polling, a translation worker, ...) or the PyInstaller
    # onefile bootloader's parent/child pair have occasionally been seen to
    # keep the process itself alive after that. Hard-exit rather than fall
    # through to Python's normal interpreter shutdown, which waits on
    # whatever is still lingering.
    os._exit(0)


if __name__ == "__main__":
    main()
