"""Interface de acompanhamento da fila automática de vídeos da EVA."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import core
from watcher import FolderWatcher


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('EVA | Estação de Vídeo Automatizada')
        self.geometry('1240x740')
        self.minsize(980, 590)
        self.protocol('WM_DELETE_WINDOW', self.request_close)
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.export_worker = None
        self.busy = False
        self.closing = False
        self.job_progress = {}
        core.init_db()
        self.settings = core.config()
        self.source_dir = tk.StringVar(value=self.settings.get('source_folder', ''))
        self.destination_dir = tk.StringVar(value=self.settings.get(
            'destination_folder', self.settings.get('local_output_folder', '')))
        self.status_text = tk.StringVar(value='Selecione a origem e o destino e inicie o monitoramento.')
        self.counts_text = tk.StringVar()
        self.mode_text = tk.StringVar(value='Monitoramento parado')
        self.error_text = tk.StringVar()
        self.progress_var = tk.DoubleVar(value=0)
        self.make_widgets()
        self.refresh()
        self.after(200, self.consume_events)
        if self.settings.get('watch_on_start', False):
            self.after(400, self.start_monitoring)

    def make_widgets(self):
        self.configure(bg='#eff3f7')
        head = ttk.Frame(self, padding=12)
        head.pack(fill='x')
        ttk.Label(head, text='EVA', font=('Segoe UI', 24, 'bold'),
                  foreground='#187b8f').pack(side='left', padx=(0, 14))
        ttk.Label(head, text='Estação de Vídeo Automatizada',
                  font=('Segoe UI', 12, 'bold')).pack(side='left')
        ttk.Label(head, textvariable=self.mode_text,
                  foreground='#405163').pack(side='right')

        paths = ttk.Frame(self, padding=(12, 4, 12, 8))
        paths.pack(fill='x')
        paths.columnconfigure(1, weight=1)
        self.path_widgets = []
        for row, (label, variable, title) in enumerate((
                ('Origem:', self.source_dir, 'Selecione a pasta de origem dos vídeos'),
                ('Destino:', self.destination_dir, 'Selecione a pasta de destino'))):
            ttk.Label(paths, text=label).grid(row=row, column=0, sticky='w', padx=(0, 10), pady=4)
            entry = ttk.Entry(paths, textvariable=variable)
            entry.grid(row=row, column=1, sticky='ew', pady=4)
            button = ttk.Button(paths, text='Selecionar pasta…',
                                command=lambda v=variable, t=title: self.pick_folder(v, t))
            button.grid(row=row, column=2, padx=(8, 0), pady=4)
            self.path_widgets.extend((entry, button))
        ttk.Label(paths, text='A EVA aguarda a cópia terminar, consulta o histórico e processa os vídeos em sequência.',
                  foreground='#576573').grid(row=2, column=1, columnspan=2, sticky='w', pady=(3, 0))

        toolbar = ttk.Frame(self, padding=(12, 4, 12, 10))
        toolbar.pack(fill='x')
        self.start_button = ttk.Button(toolbar, text='Iniciar monitoramento', command=self.start_monitoring)
        self.start_button.pack(side='left', padx=(0, 8))
        self.stop_button = ttk.Button(toolbar, text='Parar após o vídeo atual',
                                      command=self.request_stop, state='disabled')
        self.stop_button.pack(side='left', padx=(0, 8))
        self.export_button = ttk.Button(toolbar, text='Exportar logs…', command=self.export_logs)
        self.export_button.pack(side='left')

        container = ttk.Frame(self, padding=(12, 0, 12, 8))
        container.pack(fill='both', expand=True)
        columns = ('id', 'arquivo', 'estado', 'progresso', 'duracao', 'corte', 'info')
        self.table = ttk.Treeview(container, columns=columns, show='headings', selectmode='browse')
        labels = {'id': 'ID', 'arquivo': 'Vídeo', 'estado': 'Estado', 'progresso': 'Progresso',
                  'duracao': 'Duração', 'corte': 'Trecho (s)', 'info': 'Informações / falha'}
        widths = {'id': 45, 'arquivo': 235, 'estado': 145, 'progresso': 85,
                  'duracao': 85, 'corte': 120, 'info': 375}
        for col in columns:
            self.table.heading(col, text=labels[col])
            self.table.column(col, width=widths[col], stretch=col in ('arquivo', 'info'),
                              anchor='w' if col in ('arquivo', 'info') else 'center')
        self.table.tag_configure('ok', background='#e2f6eb')
        self.table.tag_configure('error', background='#ffe6e4')
        self.table.tag_configure('pending', background='#fff6da')
        self.table.tag_configure('duplicate', background='#e7e9eb')
        ybar = ttk.Scrollbar(container, orient='vertical', command=self.table.yview)
        xbar = ttk.Scrollbar(container, orient='horizontal', command=self.table.xview)
        self.table.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)
        self.table.grid(row=0, column=0, sticky='nsew')
        ybar.grid(row=0, column=1, sticky='ns')
        xbar.grid(row=1, column=0, sticky='ew')
        self.table.bind('<<TreeviewSelect>>', self.selection_changed)

        bottom = ttk.Frame(self, padding=(12, 0, 12, 12))
        bottom.pack(fill='x')
        ttk.Label(bottom, textvariable=self.counts_text, foreground='#405163').pack(anchor='w', pady=(0, 6))
        ttk.Progressbar(bottom, variable=self.progress_var, maximum=100).pack(fill='x')
        ttk.Label(bottom, textvariable=self.status_text, foreground='#405163',
                  wraplength=1150).pack(anchor='w', pady=(5, 0))
        ttk.Label(bottom, textvariable=self.error_text, foreground='#a12323',
                  wraplength=1150).pack(anchor='w', pady=(4, 0))
        details = ttk.LabelFrame(bottom, text='Detalhes do vídeo selecionado', padding=6)
        details.pack(fill='x', pady=(8, 0))
        self.details = tk.Text(details, height=4, wrap='word', state='disabled',
                               font=('Segoe UI', 9), relief='flat')
        self.details.pack(fill='x')

    @staticmethod
    def pick_folder(variable, title):
        path = filedialog.askdirectory(title=title, mustexist=True)
        if path:
            variable.set(path)

    def set_running(self, running):
        self.busy = running
        self.start_button.configure(state='disabled' if running or self.closing else 'normal')
        self.stop_button.configure(state='normal' if running and not self.stop_event.is_set() else 'disabled')
        for widget in self.path_widgets:
            widget.configure(state='disabled' if running or self.closing else 'normal')
        self.mode_text.set('Monitorando' if running else 'Monitoramento parado')

    def start_monitoring(self):
        if self.closing or self.busy or (self.worker and self.worker.is_alive()):
            return
        settings = dict(self.settings)
        settings['source_folder'] = self.source_dir.get().strip()
        settings['destination_folder'] = self.destination_dir.get().strip()
        self.stop_event.clear()
        self.error_text.set('')
        self.progress_var.set(0)
        self.status_text.set('Validando pastas e ferramentas de vídeo…')
        self.set_running(True)

        def runner():
            try:
                checked = core.validate_watch_settings(settings)
                if self.stop_event.is_set():
                    return
                core.save_config(checked)
                self.events.put(('settings', checked))
                watcher = FolderWatcher(checked, self.notify)
                watcher.run(self.stop_event)
            except Exception as exc:
                try:
                    core.monitor_event('ERRO', 'Falha ao iniciar monitoramento: ' + str(exc))
                except Exception:
                    # A falha original permanece visível mesmo sem acesso ao banco.
                    pass
                self.notify('error', str(exc))
            finally:
                self.notify('done', None)

        self.worker = threading.Thread(target=runner, name='EVA-monitor', daemon=False)
        self.worker.start()

    def notify(self, event_type, payload):
        self.events.put((event_type, payload))

    def consume_events(self):
        need_refresh = False
        try:
            # Limita o trabalho por ciclo para manter a janela responsiva.
            for _ in range(400):
                typ, payload = self.events.get_nowait()
                if typ == 'progress':
                    job_id = payload['job_id']
                    progress = max(0.0, min(1.0, float(payload['progress'])))
                    self.job_progress[job_id] = progress
                    self.progress_var.set(progress * 100)
                    self.status_text.set(f"ID {job_id} — {progress * 100:.1f}% — {payload['message']}")
                    if self.table.exists(str(job_id)):
                        self.table.set(str(job_id), 'progresso', f'{progress * 100:.1f}%')
                elif typ == 'message':
                    self.status_text.set(str(payload))
                elif typ == 'refresh':
                    need_refresh = True
                elif typ == 'error':
                    self.error_text.set('Última falha: ' + str(payload))
                    self.status_text.set('Falha registrada. Consulte a tabela e os logs exportados.')
                    need_refresh = True
                elif typ == 'settings':
                    self.settings = payload
                    self.source_dir.set(payload['source_folder'])
                    self.destination_dir.set(payload['destination_folder'])
                elif typ in ('done', 'stopped'):
                    self.set_running(False)
                    if not self.error_text.get():
                        self.status_text.set('Monitoramento encerrado. O histórico foi preservado.')
                    need_refresh = True
                elif typ == 'exported':
                    self.export_button.configure(state='normal' if not self.closing else 'disabled')
                    self.status_text.set('Logs exportados: ' + str(payload))
                elif typ == 'export_error':
                    self.export_button.configure(state='normal' if not self.closing else 'disabled')
                    self.error_text.set('Falha ao exportar logs: ' + str(payload))
        except queue.Empty:
            pass
        if need_refresh:
            self.refresh()
        if self.closing and not self.threads_running():
            self.destroy()
            return
        self.after(200, self.consume_events)

    def refresh(self):
        rows = core.jobs()
        existing = set(self.table.get_children())
        counts = {}
        for position, job in enumerate(rows):
            job_id = job['id']
            iid = str(job_id)
            status = job['status']
            counts[status] = counts.get(status, 0) + 1
            finished = status in ('CONCLUIDO', 'PREPARADO', 'DUPLICADO')
            persisted_progress = float(job.get('progress', 0.0) or 0.0)
            progress = (1.0 if finished else persisted_progress if status == 'ERRO'
                        else self.job_progress.get(job_id, persisted_progress))
            tag = ('ok' if status in ('CONCLUIDO', 'PREPARADO') else
                   'error' if status == 'ERRO' else
                   'duplicate' if status == 'DUPLICADO' else 'pending')
            cut = (f"{job['mark_in']:.3f} → {job['mark_out']:.3f}"
                   if job['mark_in'] is not None and job['mark_out'] is not None else '—')
            values = (job_id, Path(job['source_path']).name, status, f'{progress * 100:.1f}%',
                      f"{job['duration']:.2f}s" if job['duration'] is not None else '—',
                      cut, job['message'])
            if iid in existing:
                self.table.item(iid, values=values, tags=(tag,))
                existing.remove(iid)
            else:
                self.table.insert('', 'end', iid=iid, values=values, tags=(tag,))
            self.table.move(iid, '', position)
        for iid in existing:
            self.table.delete(iid)
        self.counts_text.set(' | '.join(f'{status}: {count}' for status, count in sorted(counts.items()))
                             or 'Nenhum vídeo cadastrado.')
        self.selection_changed()

    def selection_changed(self, *_):
        picked = self.table.selection()
        text = ''
        if picked:
            try:
                job = core.get_job(int(picked[0]))
                text = f"Origem: {job['source_path']}\n"
                if job.get('output_path'):
                    text += f"Saída: {job['output_path']}\n"
                text += f"{job['status']}: {job['message']}"
            except KeyError:
                pass
        self.details.configure(state='normal')
        self.details.delete('1.0', 'end')
        self.details.insert('1.0', text)
        self.details.configure(state='disabled')

    def request_stop(self):
        if not self.busy:
            return
        self.stop_event.set()
        self.stop_button.configure(state='disabled')
        self.mode_text.set('Encerrando após o vídeo atual')
        self.status_text.set('Aguardando o vídeo atual terminar. Nenhum novo vídeo será iniciado.')

    def threads_running(self):
        return any(thread and thread.is_alive() for thread in (self.worker, self.export_worker))

    def request_close(self):
        if self.closing:
            return
        self.closing = True
        self.request_stop()
        self.start_button.configure(state='disabled')
        self.export_button.configure(state='disabled')
        for widget in self.path_widgets:
            widget.configure(state='disabled')
        if self.threads_running():
            self.mode_text.set('Encerrando com segurança')
            self.status_text.set('A janela fechará quando o vídeo atual e a exportação de logs terminarem.')
        else:
            self.destroy()

    def export_logs(self):
        if self.closing or (self.export_worker and self.export_worker.is_alive()):
            return
        path = filedialog.asksaveasfilename(
            title='Exportar histórico e eventos', defaultextension='.zip',
            filetypes=[('Pacote de logs ZIP', '*.zip')],
            initialfile='EVA_logs_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '.zip')
        if not path:
            return
        self.export_button.configure(state='disabled')

        def runner():
            try:
                core.export_logs(path)
                self.notify('exported', path)
            except Exception as exc:
                self.notify('export_error', str(exc))

        self.export_worker = threading.Thread(target=runner, name='EVA-export', daemon=False)
        self.export_worker.start()


if __name__ == '__main__':
    try:
        App().mainloop()
    except Exception as exc:
        messagebox.showerror('EVA — falha ao iniciar', str(exc))
