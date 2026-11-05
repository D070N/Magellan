"""Hardware smoke test for the optional panel blur (no user settings writes)."""
import sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'MediaExplorer'))
import main
from PySide6.QtTest import QTest
main.QApplication.setAttribute(main.Qt.AA_ShareOpenGLContexts)
app=main.QApplication([]);app.setApplicationName('MagellanGpuBlurTest');main.QStandardPaths.setTestModeEnabled(True)
main.Explorer.load_settings=lambda self:None;main.Explorer.save_settings=lambda self:None;main.Explorer.migrate_legacy_explorer_context_registration=lambda self:None;main.Explorer.refresh_file_tree=lambda self:None
root=Path(tempfile.mkdtemp(prefix='MagellanGpuBlurUI-'));e=main.Explorer(external_file='dummy.png');e.current=root;e.startup_folder=str(root);e.saved_tabs=[{'path':str(root),'kind':'folder'}];e.has_saved_tabs=True;e.window_mode='normal';e.saved_geometry=None;e.log_storage_mib=8;e.explorer_context_menu_enabled=False;e.thumbnail_cache_dir=str(root/'cache');e.gpu_thumbnail_rendering_enabled=True;e.gpu_composition_enabled=True;e._initialize_ui();e._ui_deferred=False;e.resize(1100,760);e.show();QTest.qWait(400)
e.auto_hide_main=False;e.auto_hide_tree=False;e.apply_auto_hide();e.build_file_tree() if not hasattr(e,'tree_dock') else None;e.tree_dock.show();e.show_right_controls()
e.grid.ensure_folder_thumb=lambda path:None;e.grid.setThumbSize(140)
items=[{'path':root/f'folder-{i}','name':f'Folder {i}','kind':'folder','is_dir':True,'size':0,'mtime':0,'ctime':0} for i in range(80)];e.items=items;e.grid.setItems(items)
for surface in ('main','buttons','tabs','tree'):setattr(e,f'appearance_{surface}_transparency',50);setattr(e,f'appearance_{surface}_blur',30)
e.apply_appearance_settings(False);QTest.qWait(200)


e.gpu_composition_enabled=True
try:
 for hide_main in (False,True):
  for hide_tree in (False,True):
   e.auto_hide_main=hide_main;e.auto_hide_tree=hide_tree;e.apply_auto_hide();e.set_main_panel_visible(True)
   if hide_tree:e.show_tree_overlay()
   else:e.tree_dock.show()
   for radius in (0,30):
    for name in ('main','buttons','tabs','tree'):setattr(e,f'appearance_{name}_blur',radius)
    e.apply_appearance_settings(False);e.refresh_appearance_backdrop();QTest.qWait(150)
    comp=e._gpu_composition;assert comp is not None and e.gpu_composition_enabled
    for name,panel in (('main',e.main_panel_backdrop),('buttons',e.controls_bar),('tabs',e.tabbar),('tree',e.tree_dock)):
     assert comp.widgets[name].isValid(),name
     assert comp.widgets[name].size()==panel.size(),(name,comp.widgets[name].size(),panel.size())
    print('PASS visibility',hide_main,hide_tree,radius,flush=True)
 e.toggle_fullscreen();QTest.qWait(200);e.set_main_panel_visible(True);e.refresh_appearance_backdrop();QTest.qWait(200)
 for button in (e.minimize_button,e.fullscreen_button,e.fullscreen_close_button):
  assert button.isVisible();assert e.root_widget.childAt(button.geometry().center()) is button
 menu=main.AppearanceMenu(e);menu.addAction('GPU composition menu check');menu.configure(e);menu.popup(e.grid.viewport().mapToGlobal(main.QPoint(250,150)));QTest.qWait(400)
 assert menu.isVisible() and not menu.grab().isNull()
 expected=menu.grab().toImage().scaled(menu.size());visible=False
 for attempt in range(6):
  desktop=e.screen().grabWindow(0,menu.x(),menu.y(),menu.width(),menu.height()).toImage().scaled(menu.size())
  if max(abs(a-b) for a,b in zip(desktop.pixelColor(5,5).getRgb()[:3],expected.pixelColor(5,5).getRgb()[:3]))<8:visible=True;break
  QTest.qWait(60)
 assert visible,'context menu missing from desktop composition'
 menu.close();menu.deleteLater();print('PASS desktop context menu visibility',flush=True)
 e.showMinimized();QTest.qWait(150);e.showNormal();QTest.qWait(200);e.refresh_appearance_backdrop();QTest.qWait(150)
 e.close_gpu_composition();e.gpu_composition_enabled=False;e.apply_appearance_settings(False);e.refresh_appearance_backdrop();QTest.qWait(100)
 assert e.main_blur_backdrop.isVisible()
 print('PASS fullscreen controls, minimize/restore and renderer fallback',flush=True)
 e.cache_dir=root
 e.set_gpu_composition_enabled(True);e.refresh_appearance_backdrop();QTest.qWait(100)
 assert (root/'gpu_composition.enabled').exists()
 def fail():raise RuntimeError('Injected graphics failure')
 e._gpu_composition.refresh=fail
 e.refresh_appearance_backdrop();QTest.qWait(100)
 assert not e.gpu_composition_enabled and e._gpu_composition is None
 assert not (root/'gpu_composition.enabled').exists()
 assert not e.gpu_composition_action.isChecked()
 print('PASS injected failure restores regular panels and clears startup request',flush=True)
finally:
 e.close();app.processEvents();app.quit()
