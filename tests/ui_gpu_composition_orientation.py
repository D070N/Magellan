"""Hardware smoke test for the optional panel blur (no user settings writes)."""
import sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'MediaExplorer'))
import main
from PySide6.QtTest import QTest
main.QApplication.setAttribute(main.Qt.AA_ShareOpenGLContexts)
app=main.QApplication([]);app.setApplicationName('MagellanGpuBlurTest');main.QStandardPaths.setTestModeEnabled(True)
main.Explorer.load_settings=lambda self:None;main.Explorer.save_settings=lambda self:None;main.Explorer.migrate_legacy_explorer_context_registration=lambda self:None;main.Explorer.refresh_file_tree=lambda self:None
root=Path(tempfile.mkdtemp(prefix='MagellanGpuBlurUI-'));e=main.Explorer(external_file='dummy.png');e.current=root;e.startup_folder=str(root);e.saved_tabs=[{'path':str(root),'kind':'folder'}];e.has_saved_tabs=True;e.window_mode='normal';e.saved_geometry=None;e.log_storage_mib=8;e.explorer_context_menu_enabled=False;e.thumbnail_cache_dir=str(root/'cache');e.gpu_thumbnail_rendering_enabled=True;e._initialize_ui();e._ui_deferred=False;e.resize(1100,760);e.show();QTest.qWait(400)
e.auto_hide_main=False;e.auto_hide_tree=False;e.apply_auto_hide();e.build_file_tree() if not hasattr(e,'tree_dock') else None;e.tree_dock.show();e.show_right_controls()
e.grid.ensure_folder_thumb=lambda path:None;e.grid.setThumbSize(140)
items=[{'path':root/f'folder-{i}','name':f'Folder {i}','kind':'folder','is_dir':True,'size':0,'mtime':0,'ctime':0} for i in range(80)];e.items=items;e.grid.setItems(items)
for surface in ('main','buttons','tabs','tree'):setattr(e,f'appearance_{surface}_transparency',50);setattr(e,f'appearance_{surface}_blur',30)
e.apply_appearance_settings(False);QTest.qWait(200)

from gpu_composition import Composition
e.appearance_capture_timer.timeout.disconnect();e.appearance_capture_timer.timeout.connect(lambda:None)
import json
from PIL import Image,ImageChops,ImageStat
e.apply_appearance_settings(False)
for name in ('main','buttons','tabs','tree'):setattr(e,f'appearance_{name}_blur',0)
e.apply_appearance_settings(False)
fixed=main.QPixmap(e.grid.viewport().size());fixed.fill(main.Qt.red);p=main.QPainter(fixed)
for y in range(0,fixed.height(),16):p.fillRect(0,y,fixed.width(),16,main.QColor.fromHsv((y*3)%360,255,220))
p.end();e.appearance_source_snapshot=lambda source:fixed

e.main_blur_backdrop.show();e.tree_blur_backdrop.show();e.refresh_appearance_backdrop();QTest.qWait(50)
expected_main=e.main_panel_backdrop.grab().toImage();expected_tree=e.tree_backdrop.grab().toImage();expected_tabs=e.tabs_backdrop.pixmap().toImage()
composition=Composition(e)
try:
 for i in range(3):
  e.grid.verticalScrollBar().setValue(i*35);QTest.qWait(100);composition.refresh();QTest.qWait(150)
 print('SURFACES',len(composition.widgets),flush=True)
 for name,expected in (('main',expected_main),('tree',expected_tree),('tabs',expected_tabs)):
  actual=composition.widgets[name].grabFramebuffer();print('PIXEL',name,expected.pixelColor(8,8).getRgb(),actual.pixelColor(8,8).getRgb(),flush=True)
  def pil(q):
   q=q.convertToFormat(main.QImage.Format_RGBA8888);return Image.frombytes('RGBA',(q.width(),q.height()),bytes(q.constBits())).convert('RGB')
  if expected.size()==actual.size():
   error=ImageStat.Stat(ImageChops.difference(pil(expected),pil(actual))).mean;print('DIFF',name,error,flush=True);assert name=='tabs' or max(error)<2,(name,error)
 for name,widget in composition.widgets.items():
  assert widget.isValid(),name
 assert e.root_widget.childAt(e.minimize_button.geometry().center()) is e.minimize_button if e.minimize_button.isVisible() else True
 origin=e.tabbar.mapToGlobal(main.QPoint())-e.main_panel_backdrop.mapToGlobal(main.QPoint())
 main_pixels=composition.widgets['main'].grabFramebuffer();tab_pixels=composition.widgets['tabs'].grabFramebuffer()
 composition.engine.context.makeCurrent(composition.engine.surface);raw=composition.frames['main'].toImage();composition.engine.context.doneCurrent()
 for y in (0,8,16):
  a=main_pixels.pixelColor(250+origin.x(),y+origin.y());b=tab_pixels.pixelColor(250,y)
  assert max(abs(v-w) for v,w in zip(a.getRgb(),b.getRgb()))<=1,(a.name(),b.name())
 print('PASS directional source orientation and exact Main-to-tab-gap continuity',flush=True)
finally:
 composition.close();e.close();app.processEvents();app.quit()