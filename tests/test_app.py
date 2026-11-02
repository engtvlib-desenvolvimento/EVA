"""Interface Tk oculta: modos de corte e janela limitada do histórico."""
import sys
import threading
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import core


def material(job_id, status='NOVO'):
    return dict(id=job_id,source_path=f'C:/origem/P{job_id:07d}_teste.mxf',status=status,
                duration=40.04,mark_in=5.005,mark_out=35.035,message='Material na fila',
                progress=0.0,output_path=None,cut_video=1)


class TestApp(unittest.TestCase):
    def setUp(self):
        self.settings=dict(source_folder='C:/origem',destination_folder='C:/destino',
                           cut_video=True,ui_history_limit=50,watch_on_start=False)
        self.rows=[material(i) for i in range(50,0,-1)]
        self.counts={'NOVO':30000}
        self.now=0.0
        self.database_calls=[]
        self.running=threading.Event()
        self.finished=threading.Event()
        original_init=tk.Tk.__init__
        def hidden_init(window,*args,**kwargs):
            original_init(window,*args,**kwargs)
            window.withdraw()
        self.patches=[patch.object(tk.Tk,'__init__',hidden_init),
                      patch.object(core,'init_db'),patch.object(core,'config',side_effect=lambda:dict(self.settings)),
                      patch.object(core,'jobs',side_effect=self.jobs),
                      patch.object(core,'job_counts',side_effect=lambda:dict(self.counts),create=True),
                      patch.object(core,'get_job',side_effect=self.get_job),
                      patch.object(core,'monitor_event'),
                      patch.object(app.time,'monotonic',side_effect=lambda:self.now)]
        for current in self.patches:
            current.start()
            self.addCleanup(current.stop)
        try:
            self.window=app.App()
        except tk.TclError as exc:
            self.skipTest(f'Tk indisponível: {exc}')
        self.addCleanup(self.close_window)

    def close_window(self):
        self.finished.set()
        if self.window.worker:
            self.window.stop_event.set()
            self.window.worker.join(2)
        try:
            for after_id in self.window.tk.call('after','info'):
                self.window.after_cancel(after_id)
            self.window.destroy()
        except tk.TclError:
            pass

    def jobs(self,limit=None,offset=0):
        self.database_calls.append((limit,offset))
        return [dict(row) for row in self.rows[offset:offset+limit if limit else None]]

    def get_job(self,job_id):
        row=next((row for row in self.rows if row['id']==job_id),None)
        if row:
            return dict(row)
        if job_id==1:
            return material(1,'PROCESSANDO')
        raise KeyError(job_id)

    def test_cut_checkbox_default_name_and_disabled_state(self):
        self.assertEqual(self.window.cut_video_checkbox.cget('text'),'corte de vídeo')
        self.assertTrue(self.window.cut_video_var.get())
        self.window.set_running(True)
        self.assertTrue(self.window.cut_video_checkbox.instate(['disabled']))
        self.window.set_running(False)
        self.assertFalse(self.window.cut_video_checkbox.instate(['disabled']))
        self.window.closing=True
        self.window.set_running(False)
        self.assertTrue(self.window.cut_video_checkbox.instate(['disabled']))

    def test_xml_status_distinguishes_conference_from_importer_template(self):
        self.assertEqual(self.window.xml_status_text.get(),
                         'XML de conferência: cadastro no Smartware ainda não configurado.')
        checked=dict(self.settings,xml_profile='smartware_template',cut_video=False)
        self.window.notify('settings',checked)
        self.window.consume_events()
        self.assertEqual(self.window.xml_status_text.get(),'XML configurado por template do importador.')
        self.assertFalse(self.window.cut_video_var.get())
        self.window.notify('settings',dict(checked,xml_profile='eva'))
        self.window.consume_events()
        self.assertEqual(self.window.xml_status_text.get(),
                         'XML de conferência: cadastro no Smartware ainda não configurado.')

    def test_xml_status_identifies_builtin_floripa_profile(self):
        checked=dict(self.settings,xml_profile='smartware')
        self.window.notify('settings',checked)
        self.window.consume_events()
        self.assertEqual(self.window.xml_status_text.get(),
                         'XML Smartware: padrão da Floripa · operador EVA.')
        self.assertNotIn('confirmado',self.window.xml_status_text.get().lower())
        self.window.notify('settings',dict(checked,xml_profile='smartware_template'))
        self.window.consume_events()
        self.assertEqual(self.window.xml_status_text.get(),'XML configurado por template do importador.')

    def test_unchecked_mode_is_saved_and_passed_as_worker_snapshot(self):
        captured=[]
        outer=self
        class Worker:
            def __init__(self,settings,notify):
                captured.append(dict(settings))
            def run(self,stop_event):
                outer.running.set()
                outer.finished.wait(2)
        self.window.cut_video_var.set(False)
        with patch.object(core,'validate_watch_settings',side_effect=lambda values:dict(values)), \
                patch.object(core,'save_config') as save, patch.object(app,'FolderWatcher',Worker):
            self.window.start_monitoring()
            self.assertTrue(self.running.wait(2))
            self.assertFalse(self.window.worker.daemon)
            self.assertFalse(save.call_args.args[0]['cut_video'])
            self.assertFalse(captured[0]['cut_video'])
            # Alterar a variável depois da captura não muda o lote em andamento.
            self.window.cut_video_var.set(True)
            self.assertFalse(captured[0]['cut_video'])
            self.finished.set()
            self.window.worker.join(2)
        self.window.consume_events()
        self.assertFalse(self.window.cut_video_var.get())

    def test_history_is_bounded_and_global_counts_are_preserved(self):
        self.assertEqual(self.database_calls[-1],(50,0))
        self.assertEqual(len(self.window.table.get_children()),50)
        self.assertIn('30000',self.window.counts_text.get())
        self.window.table.selection_set('30')
        with patch.object(self.window.table,'item',wraps=self.window.table.item) as item, \
                patch.object(self.window.table,'move',wraps=self.window.table.move) as move:
            self.window.refresh()
        self.assertFalse([call for call in item.call_args_list if 'values' in call.kwargs],
                         'A mesma página não deve reescrever todas as linhas')
        move.assert_not_called()
        self.assertEqual(self.window.table.selection(),('30',))

    def test_current_old_job_stays_visible_without_growing_history_or_progress_cache(self):
        self.rows=[material(i) for i in range(30000,29950,-1)]
        self.window.current_job_id=1
        self.window.job_progress={i:.5 for i in range(1,30001)}
        self.window.refresh()
        self.assertEqual(len(self.window.table.get_children()),50)
        self.assertEqual(self.window.table.get_children()[0],'1')
        self.assertEqual(len(self.window.job_progress),50)
        self.assertTrue(self.window.table.exists('1'))

    def test_progress_refresh_requests_are_coalesced_to_one_query_per_second(self):
        initial=len(self.database_calls)
        for _ in range(1000):
            self.window.notify('refresh',None)
        self.now=.2
        self.window.consume_events()
        self.assertEqual(len(self.database_calls),initial)
        self.now=1.2
        self.window.consume_events()
        self.assertEqual(len(self.database_calls),initial+1)
        self.now=1.3
        self.window.notify('refresh',None)
        self.window.consume_events()
        self.assertEqual(len(self.database_calls),initial+1)
        self.assertTrue(self.window.refresh_pending)
        self.now=2.3
        self.window.consume_events()
        self.assertEqual(len(self.database_calls),initial+2)
        self.assertFalse(self.window.refresh_pending)

    def test_ten_thousand_refresh_notifications_use_one_queue_entry(self):
        for _ in range(10000):
            self.window.notify('refresh',None)
        self.assertEqual(self.window.events.qsize(),1)
        self.window.notify('progress',dict(job_id=1,progress=.25,message='Codificando'))
        self.window.notify('message','Continuando o lote')
        self.assertEqual(self.window.events.qsize(),3)
        self.now=.2
        self.window.consume_events()
        self.assertEqual(self.window.events.qsize(),0)
        self.assertEqual(self.window.job_progress[1],.25)
        self.assertEqual(self.window.status_text.get(),'Continuando o lote')
        self.assertFalse(self.window.refresh_queued)
        self.window.notify('refresh',None)
        self.assertEqual(self.window.events.qsize(),1)

    def test_parallel_producers_share_one_pending_refresh_notification(self):
        workers=[threading.Thread(target=lambda:[self.window.notify('refresh',None) for _ in range(2500)])
                 for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(self.window.events.qsize(),1)


if __name__=='__main__':
    unittest.main()
