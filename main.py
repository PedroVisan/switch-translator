import json
import os
import threading
from tkinter import filedialog
import customtkinter as ctk

from mtp_bridge import list_installed_games
from extractor import extract_texts, build_layeredfs_patch
from translator import translate_with_checkpoint

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

BATCH_SIZE = 250  # Quantidade de falas traduzidas por chamada na API


class SwitchTranslatorApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Switch Auto-Translator (CNX)")
        self.geometry("700x640")
        self.resizable(False, False)

        self.detected_games = []
        self.romfs_dir = ""

        # --- Bloco 1: Conexão com o Switch / Title ID ---
        self.frame_switch = ctk.CTkFrame(self)
        self.frame_switch.pack(fill="x", padx=20, pady=(18, 8))

        ctk.CTkLabel(
            self.frame_switch,
            text="1. Identificação do Jogo (Switch DBI ou Manual)",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w", padx=15, pady=(10, 6))

        row_switch = ctk.CTkFrame(self.frame_switch, fg_color="transparent")
        row_switch.pack(fill="x", padx=15, pady=(0, 8))

        self.btn_scan = ctk.CTkButton(
            row_switch,
            text="Detectar via DBI MTP",
            width=180,
            command=self.scan_switch,
        )
        self.btn_scan.pack(side="left", padx=(0, 10))

        self.game_menu = ctk.CTkOptionMenu(
            row_switch,
            values=["Nenhum Switch detectado"],
            width=440,
            command=self.on_game_selected,
        )
        self.game_menu.pack(side="left")

        row_tid = ctk.CTkFrame(self.frame_switch, fg_color="transparent")
        row_tid.pack(fill="x", padx=15, pady=(0, 12))

        ctk.CTkLabel(row_tid, text="Title ID (16 dígitos):").pack(side="left", padx=(0, 8))
        self.tid_entry = ctk.CTkEntry(
            row_tid,
            placeholder_text="Ex: 010083E00F40E000 (preenchido auto ou manual)",
            width=360,
        )
        self.tid_entry.pack(side="left")

        # --- Bloco 2: Pasta RomFS e Chave da API ---
        self.frame_ai = ctk.CTkFrame(self)
        self.frame_ai.pack(fill="x", padx=20, pady=8)

        ctk.CTkLabel(
            self.frame_ai,
            text="2. Arquivos do Jogo e Gemini API",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w", padx=15, pady=(10, 6))

        self.api_entry = ctk.CTkEntry(
            self.frame_ai,
            placeholder_text="Cole sua Gemini API Key aqui (ou múltiplas separadas por vírgula)",
            width=630,
            show="*",
        )
        self.api_entry.pack(padx=15, pady=(0, 10))

        row_folder = ctk.CTkFrame(self.frame_ai, fg_color="transparent")
        row_folder.pack(fill="x", padx=15, pady=(0, 12))

        self.btn_folder = ctk.CTkButton(
            row_folder,
            text="Selecionar Pasta RomFS",
            width=180,
            command=self.select_romfs_folder,
        )
        self.btn_folder.pack(side="left", padx=(0, 10))

        self.lbl_folder = ctk.CTkLabel(
            row_folder, text="Nenhuma pasta selecionada", text_color="gray"
        )
        self.lbl_folder.pack(side="left")

        # --- Bloco 3: Progresso e Execução ---
        self.btn_translate = ctk.CTkButton(
            self,
            text="Extrair, Traduzir e Gerar Patch LayeredFS",
            height=42,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="#10a37f",
            hover_color="#0d8a6a",
            command=self.start_pipeline,
        )
        self.btn_translate.pack(fill="x", padx=20, pady=(10, 8))

        self.lbl_progress = ctk.CTkLabel(
            self, text="Aguardando início (0%)", text_color="gray"
        )
        self.lbl_progress.pack(anchor="w", padx=22)

        self.progress_bar = ctk.CTkProgressBar(self, width=660)
        self.progress_bar.set(0)
        self.progress_bar.pack(padx=20, pady=(2, 10))

        # --- Console de Logs ---
        self.log_box = ctk.CTkTextbox(self, height=185)
        self.log_box.pack(fill="both", expand=True, padx=20, pady=(4, 18))
        self.log("Sistema pronto. Selecione a pasta 'romfs' do jogo e informe o Title ID.")

    # --- Helpers de Interface (Thread-Safe) ---
    def log(self, msg: str):
        self.after(0, lambda: self._append_log(msg))

    def _append_log(self, msg: str):
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")

    def update_progress(self, fraction: float, text: str):
        self.after(0, lambda: self._set_progress(fraction, text))

    def _set_progress(self, fraction: float, text: str):
        self.progress_bar.set(fraction)
        self.lbl_progress.configure(text=text, text_color="white")

    # --- Ações do Switch ---
    def scan_switch(self):
        self.btn_scan.configure(state="disabled")
        self.log("Procurando Switch conectado via DBI MTP...")
        threading.Thread(target=self._run_scan, daemon=True).start()

    def _run_scan(self):
        try:
            games = list_installed_games()
            if games is None:
                self.log("[Aviso] Switch não encontrado. Ative 'Run MTP Responder' no DBI.")
            elif not games:
                self.log("[Aviso] Nenhum jogo encontrado em 'Installed games'.")
            else:
                self.detected_games = games
                names = [g["name"] for g in games]
                first_tid = games[0]["title_id"]
                self.after(0, lambda: self.game_menu.configure(values=names))
                self.after(0, lambda: self.game_menu.set(names[0]))
                self.after(0, lambda: self._fill_title_id(first_tid))
                self.log(f"[OK] {len(games)} jogo(s) detectado(s).")
        except Exception as e:
            self.log(f"[Erro MTP] {e}")
        finally:
            self.after(0, lambda: self.btn_scan.configure(state="normal"))

    def on_game_selected(self, choice: str):
        for g in self.detected_games:
            if g["name"] == choice:
                self._fill_title_id(g["title_id"])
                self.log(f"Jogo selecionado -> Title ID: {g['title_id']}")
                break

    def _fill_title_id(self, tid: str):
        self.tid_entry.delete(0, "end")
        if tid != "SEM_ID":
            self.tid_entry.insert(0, tid)

    # --- Seleção de Pasta RomFS ---
    def select_romfs_folder(self):
        folder = filedialog.askdirectory(title="Selecione a pasta romfs extraída do jogo")
        if folder:
            self.romfs_dir = folder
            short_path = folder if len(folder) < 55 else "..." + folder[-52:]
            self.lbl_folder.configure(text=short_path, text_color="white")
            self.log(f"Pasta RomFS definida: {folder}")

    # --- Pipeline Completo: Extração -> Lotes Gemini -> Patch LayeredFS ---
    def start_pipeline(self):
        api_key = self.api_entry.get().strip()
        title_id = self.tid_entry.get().strip().upper()

        if not api_key:
            self.log("[Aviso] Cole sua Gemini API Key antes de iniciar.")
            return
        if not self.romfs_dir or not os.path.isdir(self.romfs_dir):
            self.log("[Aviso] Selecione uma pasta RomFS válida.")
            return
        if len(title_id) != 16:
            self.log("[Aviso] O Title ID precisa ter exatamente 16 caracteres hexadecimais.")
            return

        self.btn_translate.configure(state="disabled")
        self.update_progress(0.02, "Extraindo textos do jogo... (2%)")
        threading.Thread(
            target=self._run_pipeline, args=(api_key, title_id), daemon=True
        ).start()

    def _run_pipeline(self, api_key: str, title_id: str):
        try:
            work_dir = os.path.join("trabalho_temp", title_id)

            # Etapa 1: Extração automática conforme a engine
            self.log("Analisando engine e extraindo legendas do RomFS...")
            ext_info = extract_texts(self.romfs_dir, work_dir)
            total_strings = ext_info["total_strings"]
            engine = ext_info["engine"]

            self.log(f"[Extrator] Engine: {engine.upper()} | Falas encontradas: {total_strings}")

            if total_strings == 0:
                self.log("[Aviso] Nenhuma legenda legível foi extraída desta pasta.")
                self.update_progress(0.0, "Nenhuma legenda encontrada (0%)")
                return

            with open(ext_info["strings_path"], "r", encoding="utf-8") as f:
                all_strings: dict = json.load(f)

            # Etapa 2: Descoberta automática de modelos + tradução com checkpoint
            translated_out_path = os.path.join(work_dir, "strings_traduzidas.json")
            translate_with_checkpoint(
                all_strings=all_strings,
                api_key=api_key,
                translated_out_path=translated_out_path,
                batch_size=BATCH_SIZE,
                log_fn=self.log,
                progress_fn=self.update_progress,
            )

            # Etapa 3: Montagem da pasta atmosphere/contents/<TITLE_ID>/romfs/ + fontes
            self.update_progress(0.93, "Reempacotando arquivos e fontes LayeredFS... (93%)")
            self.log("Gerando estrutura final e injetando acentos nas fontes...")

            atmosphere_path = build_layeredfs_patch(
                title_id=title_id,
                romfs_dir=self.romfs_dir,
                translated_json_path=translated_out_path,
                manifest_path=ext_info["manifest_path"],
                log_fn=self.log,
            )

            self.update_progress(1.0, "Patch concluído com sucesso! (100%)")
            self.log(f"[SUCESSO] Patch pronto em: {os.path.abspath(atmosphere_path)}")
            self.log("Copie a pasta 'atmosphere' gerada para a raiz do cartão SD do Switch.")

        except Exception as e:
            self.log(f"[Erro] {e}")
            self.update_progress(0.0, "Falha no processamento")
        finally:
            self.after(0, lambda: self.btn_translate.configure(state="normal"))


if __name__ == "__main__":
    app = SwitchTranslatorApp()
    app.mainloop()