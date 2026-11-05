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


import zipfile,io
from PIL import Image
archives=[]
for i in range(2):
 archive=root/f'book-{i}.cbz'
 with zipfile.ZipFile(archive,'w') as z:
  for j in range(3):
   output=io.BytesIO();Image.new('RGB',(320,480),(i*100,j*80,40)).save(output,format='PNG');z.writestr(f'{j}.png',output.getvalue())
 archives.append(archive)
entries=[{'source':str(a),'member':f'{j}.png','page':None} for a in archives for j in range(3)]
v=main.MediaViewer([str(archives[0])],0,e);v.farview_sequence=entries;v.farview_active=True;v.forced_single_page_mode=True;v.farview_index=2;v.pending_archive_member='2.png';v.show()
try:
 for _ in range(100):
  QTest.qWait(30)
  if v.farview_page_key(entries[3]) in v.farview_page_cache:break
 assert v.farview_page_key(entries[3]) in v.farview_page_cache,'next archive page was not prefetched'
 v.set_farview_index(3)
 assert v.multipage_info is not None and v.multipage_images,'cached swipe did not display synchronously'
 assert not v.loading
 assert sum(value[2].sizeInBytes() for value in v.farview_page_cache.values())<=v.multipage_image_cache_limit
 print('PASS Farview cross-archive preloading, immediate cache hit and memory bound',flush=True)
finally:v.close();e.close();app.processEvents();app.quit()
