"""Hardware smoke test for the optional panel blur (no user settings writes)."""
import sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'MediaExplorer'))
import main
from PySide6.QtTest import QTest
app=main.QApplication([]);app.setApplicationName('MagellanGpuBlurTest');main.QStandardPaths.setTestModeEnabled(True)
main.Explorer.load_settings=lambda self:None;main.Explorer.save_settings=lambda self:None;main.Explorer.migrate_legacy_explorer_context_registration=lambda self:None;main.Explorer.refresh_file_tree=lambda self:None
root=Path(tempfile.mkdtemp(prefix='MagellanGpuBlurUI-'));e=main.Explorer(external_file='dummy.png');e.current=root;e.startup_folder=str(root);e.saved_tabs=[{'path':str(root),'kind':'folder'}];e.has_saved_tabs=True;e.window_mode='normal';e.saved_geometry=None;e.log_storage_mib=8;e.explorer_context_menu_enabled=False;e.thumbnail_cache_dir=str(root/'cache');e.gpu_thumbnail_rendering_enabled=True;e._initialize_ui();e._ui_deferred=False;e.resize(1100,760);e.show();QTest.qWait(400)
e.auto_hide_main=False;e.auto_hide_tree=False;e.apply_auto_hide();e.build_file_tree() if not hasattr(e,'tree_dock') else None;e.tree_dock.show();e.show_right_controls()
e.grid.ensure_folder_thumb=lambda path:None;e.grid.setThumbSize(140)
items=[{'path':root/f'folder-{i}','name':f'Folder {i}','kind':'folder','is_dir':True,'size':0,'mtime':0,'ctime':0} for i in range(80)];e.items=items;e.grid.setItems(items)
for surface in ('main','buttons','tabs','tree'):setattr(e,f'appearance_{surface}_transparency',50);setattr(e,f'appearance_{surface}_blur',30)
e.apply_appearance_settings(False);QTest.qWait(200)
try:
 assert e.grid.viewport().isValid(),'OpenGL context unavailable for hardware smoke test'
 assert not e.gpu_blur_enabled;e.gpu_blur_action.trigger();assert e.gpu_blur_enabled
 for i in range(6):e.grid.verticalScrollBar().setValue(i*40);e.grid.viewport().update();QTest.qWait(60);e.refresh_appearance_backdrop()
 backend=e._gpu_blur_backend;assert backend is not None and backend.results
 for state in ('fullscreen','restored'):
  e.toggle_fullscreen();QTest.qWait(160)
  e.clear_appearance_preview_cache();e.refresh_appearance_backdrop();QTest.qWait(80)
  assert e.grid.viewport().isValid() and e.tree_dock.isVisible()
  menu=main.AppearanceMenu(e);menu.addAction('GPU blur menu visibility check');menu.configure(e);menu.popup(e.grid.viewport().mapToGlobal(main.QPoint(250,150)));QTest.qWait(100)
  assert menu.isVisible() and not menu.grab().isNull()
  expected=menu.grab().toImage().scaled(menu.size())
  for attempt in range(6):
   desktop=e.screen().grabWindow(0,menu.x(),menu.y(),menu.width(),menu.height()).toImage().scaled(menu.size())
   if max(abs(a-b) for a,b in zip(desktop.pixelColor(5,5).getRgb()[:3],expected.pixelColor(5,5).getRgb()[:3]))<8:break
   QTest.qWait(60)
  if max(abs(a-b) for a,b in zip(desktop.pixelColor(5,5).getRgb()[:3],expected.pixelColor(5,5).getRgb()[:3]))>=8:
   desktop.save(str(Path(__file__).resolve().parents[1]/'MediaExplorer/local_diagnostics'/('gpu-menu-desktop-'+state+'.png')));expected.save(str(Path(__file__).resolve().parents[1]/'MediaExplorer/local_diagnostics'/('gpu-menu-widget-'+state+'.png')))
   print('MENU PIXELS',state,desktop.pixelColor(5,5).getRgb(),expected.pixelColor(5,5).getRgb(),flush=True)
   raise AssertionError('menu not visible in desktop composition')
  menu.close();menu.deleteLater()
 e.showMinimized();QTest.qWait(100);e.showNormal();QTest.qWait(150);e.refresh_appearance_backdrop();assert e.gpu_blur_enabled
 e.gpu_blur_action.trigger();assert not e.gpu_blur_enabled and e._gpu_blur_backend is None and backend.engine.closed
 e.grid.viewport().update();QTest.qWait(80);e.refresh_appearance_backdrop()
 print('PASS GPU on/off, populated shader results, menus/tree, fullscreen, minimize/restore and resource cleanup')
finally:e.close();app.processEvents();app.quit()
