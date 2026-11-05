"""Disposable UI smoke: run separately from QCoreApplication safety tests."""
import sys,tempfile,time,os
from pathlib import Path
sys.path=[entry for entry in sys.path if entry!=str(Path(__file__).parent)]
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'MediaExplorer'))
import main
from PySide6.QtTest import QTest
app=main.QApplication([]);app.setApplicationName('MagellanRegression');main.QStandardPaths.setTestModeEnabled(True)
main.Explorer.load_settings=lambda self:None
main.Explorer.save_settings=lambda self:None
main.Explorer.migrate_legacy_explorer_context_registration=lambda self:None
root=Path(tempfile.mkdtemp(prefix='MagellanRegression-'));(root/'original.txt').write_text('test')
e=main.Explorer(external_file='dummy.png');e.current=root;e.startup_folder=str(root);e.saved_geometry=None;e.window_mode='normal';e.saved_tabs=[{'path':str(root),'kind':'folder'}];e.has_saved_tabs=True;e.thumbnail_cache_dir=str(root/'cache');e.log_storage_mib=8;e.explorer_context_menu_enabled=False;e.gpu_thumbnail_rendering_enabled=True;e._initialize_ui();e.show();QTest.qWait(700)


e._ui_deferred=False;errors=[]
sys.excepthook=lambda kind,value,tb:(errors.append(str(value)),sys.__excepthook__(kind,value,tb))
import zipfile
from PySide6.QtGui import QPdfWriter
image=root/'one.png';second=root/'two.png';q=main.QImage(600,800,main.QImage.Format_RGB32);q.fill(main.QColor('blue'));q.save(str(image));q.fill(main.QColor('green'));q.save(str(second))
archive=root/'pages.cbz'
with zipfile.ZipFile(archive,'w') as z:z.write(image,'01.png');z.write(second,'02.png')
pdf=root/'pages.pdf';writer=QPdfWriter(str(pdf));painter=main.QPainter(writer);painter.drawText(80,100,'one first');writer.newPage();painter.drawText(80,100,'one second');painter.end();del writer
paths=[str(image),main.encode_archive_path(str(archive),'01.png'),main.encode_archive_path(str(archive),'02.png'),main.encode_archive_path(str(pdf),main.encode_pdf_page_member(1)),str(second)]
items=[{'path':path,'name':str(i),'kind':'document' if i==3 else 'image','is_dir':False,'size':1,'mtime':0,'ctime':0} for i,path in enumerate(paths)]
g=e.grid;g.setItems(items)
assert e.open_farview_entry(paths[0]);v=e.viewer
for expected in (1,2,3,4,0):
 v.step(1)
 for _ in range(100):
  app.processEvents();time.sleep(.01)
  if not v.loading:break
 app.processEvents();time.sleep(.01);app.processEvents()
 assert v.farview_index==expected,(v.farview_index,expected)
 assert v.counter_position()==(expected+1,5)
 if expected==2:assert v.current_multipage_page()==1
 if expected==3:assert v.current_multipage_page()==1
v.step(-1);assert v.farview_index==4
v.go_to_position(2);assert v.farview_index==2
for _ in range(30):v.step(1);app.processEvents();time.sleep(.005)
v.close();app.processEvents();assert not errors,errors
print('PASS Farview forward/back/jump/wrap, archive members, explicit PDF page and rapid navigation',flush=True)
g.shutdown_workers();os._exit(0)
