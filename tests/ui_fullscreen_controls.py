"""Hardware smoke test for the optional panel blur (no user settings writes)."""
import sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'MediaExplorer'))
import main
from PySide6.QtTest import QTest
app=main.QApplication([]);app.setApplicationName('MagellanFullscreenControlsTest');main.QStandardPaths.setTestModeEnabled(True)
main.Explorer.hide_main_panel=lambda self:None;main.Explorer.check_titlebar_hover=lambda self:None
main.Explorer.load_settings=lambda self:None;main.Explorer.save_settings=lambda self:None;main.Explorer.migrate_legacy_explorer_context_registration=lambda self:None;main.Explorer.refresh_file_tree=lambda self:None
root=Path(tempfile.mkdtemp(prefix='MagellanGpuBlurUI-'));e=main.Explorer(external_file='dummy.png');e.current=root;e.startup_folder=str(root);e.saved_tabs=[{'path':str(root),'kind':'folder'}];e.has_saved_tabs=True;e.window_mode='normal';e.saved_geometry=None;e.log_storage_mib=8;e.explorer_context_menu_enabled=False;e.thumbnail_cache_dir=str(root/'cache');e.gpu_thumbnail_rendering_enabled=not (len(sys.argv)>1 and sys.argv[1]=="cpu");e._initialize_ui();e._ui_deferred=False;e.resize(1100,760);e.show();QTest.qWait(400)
e.auto_hide_main=False;e.auto_hide_tree=False;e.apply_auto_hide();e.build_file_tree() if not hasattr(e,'tree_dock') else None;e.tree_dock.show();e.show_right_controls()
e.grid.ensure_folder_thumb=lambda path:None;e.grid.setThumbSize(140)
items=[{'path':root/f'folder-{i}','name':f'Folder {i}','kind':'folder','is_dir':True,'size':0,'mtime':0,'ctime':0} for i in range(80)];e.items=items;e.grid.setItems(items)
for surface in ('main','buttons','tabs','tree'):setattr(e,f'appearance_{surface}_transparency',50);setattr(e,f'appearance_{surface}_blur',30)
e.apply_appearance_settings(False);QTest.qWait(200)
try:
 e.tree_dock.hide();QTest.qWait(80);e.toggle_fullscreen();QTest.qWait(200)
 if len(sys.argv)>1 and sys.argv[1]=='gpu-blur':e.set_gpu_blur_enabled(True)
 checks=0
 for hide_main,hide_tree in ((False,False),(True,False),(False,True),(True,True)):
  e.auto_hide_main=hide_main;e.auto_hide_tree=hide_tree;e.apply_auto_hide();e.set_main_panel_visible(True)
  for shown in (True,False,True,False):
   if shown and hide_tree:e.show_tree_overlay()
   else:e.tree_dock.setVisible(shown)
   QTest.qWait(120)
   for button in (e.minimize_button,e.fullscreen_button,e.fullscreen_close_button):
    assert button.isVisible()
    assert e.root_widget.rect().contains(button.geometry()),'control clipped outside central widget'
    assert e.root_widget.childAt(button.geometry().center()) is button,'control covered by another layer'
    assert e.window_control_backdrops[button].geometry()==button.geometry(),'appearance layer displaced'
    checks+=1
 print('PASS',sys.argv[1] if len(sys.argv)>1 else 'gpu',checks,'fullscreen-control geometry, stacking and backdrop checks')
finally:e.close();app.processEvents();app.quit()
