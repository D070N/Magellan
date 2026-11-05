import sys,tempfile,unittest,zipfile,shutil,os
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'MediaExplorer'))
from PySide6.QtCore import QCoreApplication,QStandardPaths
app=QCoreApplication.instance() or QCoreApplication([])
app.setApplicationName('MagellanAudit');QStandardPaths.setTestModeEnabled(True)
import main

class SafetyTests(unittest.TestCase):
 def test_partial_cross_drive_move_keeps_complete_copy(self):
  with tempfile.TemporaryDirectory() as raw:
   root=Path(raw);source=root/'source';source.mkdir();(source/'a').write_text('A');(source/'b').write_text('B');dest=root/'dest'
   real_split=os.path.splitdrive;real_remove=shutil.rmtree
   def split(path):
    return ('TARGET' if str(path)==str(dest.resolve()) else 'SOURCE',real_split(path)[1])
   def remove(path,*args,**kwargs):
    if Path(path)==source:(source/'a').unlink();raise PermissionError('simulated partial source deletion')
    return real_remove(path,*args,**kwargs)
   task=main.FileOperationTask('move',[source],dest);results=[];task.signals.done.connect(lambda done,errors:results.append((done,errors)))
   with patch.object(main.os.path,'splitdrive',side_effect=split),patch.object(main.shutil,'rmtree',side_effect=remove):task.run()
   self.assertTrue(results[0][1]);self.assertEqual((dest/'source'/'a').read_text(),'A');self.assertEqual((dest/'source'/'b').read_text(),'B')
 def test_unsafe_archive_names_rejected(self):
  with tempfile.TemporaryDirectory() as raw:
   for name in ('../escape','/absolute','C:/absolute','folder/file:stream','CON.txt','folder/trailing.','folder/aux'):
    with self.subTest(name=name),self.assertRaises(ValueError):main.FileOperationTask.safe_extract_target(raw,name)
 def test_copy_collision_and_extract(self):
  with tempfile.TemporaryDirectory() as raw:
   root=Path(raw);src=root/'a.txt';src.write_text('new');dest=root/'dest';dest.mkdir();(dest/'a.txt').write_text('old')
   main.FileOperationTask('copy',[src],dest).run();self.assertEqual((dest/'a.txt').read_text(),'old');self.assertEqual((dest/'a (1).txt').read_text(),'new')
   archive=root/'book.zip'
   with zipfile.ZipFile(archive,'w') as z:z.writestr('pages/one.txt','page')
   main.FileOperationTask('extract',[archive],dest).run();self.assertEqual((dest/'book'/'pages'/'one.txt').read_text(),'page')
 def test_zip_traversal_rolls_back(self):
  with tempfile.TemporaryDirectory() as raw:
   root=Path(raw);archive=root/'bad.zip'
   with zipfile.ZipFile(archive,'w') as z:z.writestr('../escape.txt','unsafe')
   task=main.FileOperationTask('extract',[archive],root/'out');errors=[];task.signals.done.connect(lambda done,err:errors.extend(err));task.run()
   self.assertTrue(errors);self.assertFalse((root/'out'/'escape.txt').exists());self.assertFalse((root/'out'/'bad').exists())
 def test_jxl_rejects_disguised_other_formats(self):
  import io
  from PIL import Image,UnidentifiedImageError
  import pillow_jxl
  data=io.BytesIO();Image.new('RGB',(24,24),'red').save(data,format='PNG')
  with self.assertRaises(UnidentifiedImageError):main.decode_archive_image(data.getvalue(),'.jxl')
  data=io.BytesIO();Image.new('RGB',(24,24),'red').save(data,format='JXL')
  image,ratio=main.decode_archive_image(data.getvalue(),'.jxl');self.assertFalse(image.isNull());self.assertEqual(image.width(),24)
 def test_copy_race_does_not_overwrite_or_remove_foreign_file(self):
  with tempfile.TemporaryDirectory() as raw:
   root=Path(raw);src=root/'source.txt';src.write_text('source');dest=root/'dest';dest.mkdir();foreign=dest/'source.txt';foreign.write_text('other process')
   task=main.FileOperationTask('copy',[src],dest)
   with patch.object(task,'unique_target',return_value=foreign):task.run()
   self.assertEqual(foreign.read_text(),'other process')
 def test_copy_cancellation_removes_only_partial_output(self):
  with tempfile.TemporaryDirectory() as raw:
   root=Path(raw);src=root/'large.bin';src.write_bytes(b'x'*(3*1024*1024));dest=root/'dest';task=main.FileOperationTask('copy',[src],dest);calls=[0]
   def cancelled():calls[0]+=1;return calls[0]>=4
   with patch.object(task,'isInterruptionRequested',side_effect=cancelled):task.run()
   self.assertTrue(task.cancelled);self.assertEqual(src.stat().st_size,3*1024*1024);self.assertFalse((dest/'large.bin').exists())
 def test_folder_copy_and_move_undo(self):
  with tempfile.TemporaryDirectory() as raw:
   root=Path(raw);src=root/'folder';(src/'nested').mkdir(parents=True);(src/'nested'/'a.txt').write_text('contents');dest=root/'dest'
   main.FileOperationTask('copy',[src],dest).run();self.assertEqual((dest/'folder'/'nested'/'a.txt').read_text(),'contents')
   moved=root/'moved';main.FileOperationTask('move',[src],moved).run();self.assertFalse(src.exists())
   main.FileOperationTask('undo_move',[(str(src),str(moved/'folder'))],root).run();self.assertEqual((src/'nested'/'a.txt').read_text(),'contents')
 def test_nested_zip_member_materialization(self):
  import io
  with tempfile.TemporaryDirectory() as raw:
   root=Path(raw);inner=io.BytesIO()
   with zipfile.ZipFile(inner,'w') as archive:archive.writestr('chapter/page.txt','nested contents')
   outer=root/'outer.zip'
   with zipfile.ZipFile(outer,'w') as archive:archive.writestr('books/inner.zip',inner.getvalue())
   output=io.BytesIO();main.copy_archive_member_to_stream(outer,'books/inner.zip',output)
   with zipfile.ZipFile(io.BytesIO(output.getvalue())) as archive:self.assertEqual(archive.read('chapter/page.txt'),b'nested contents')
 def test_directory_scan_reuses_creation_metadata(self):
  with tempfile.TemporaryDirectory() as raw:
   root=Path(raw);(root/'folder').mkdir();(root/'image.png').write_bytes(b'fixture');results=[]
   task=main.ScanTask(root);task.signals.done.connect(lambda rows,error,g:results.append((rows,error)))
   with patch.object(main,'birth',side_effect=AssertionError('Redundant metadata lookup')):task.run()
   rows,error=results[0];self.assertEqual(error,'');self.assertEqual(len(rows),2)
   for item in rows:
    metadata=item['path'].stat(follow_symlinks=False);self.assertEqual(item['ctime'],getattr(metadata,'st_birthtime',metadata.st_ctime))
 def test_kept_thumbnail_validation_is_throttled_and_recovers(self):
  from types import SimpleNamespace
  with tempfile.TemporaryDirectory() as raw:
   thumbnail=Path(raw)/'thumb.jpg';thumbnail.write_bytes(b'cache');key='folder'
   grid=SimpleNamespace(_closing=False,folder_custom={},folder_cache={key:str(thumbnail)},is_persistently_kept=lambda path:True,_persistent_folder_thumb_path=lambda path:thumbnail,load_persistent_folder_thumb=lambda path:None,folder_futures={},fast_scroll=True,_scroll_speed=2000)
   with patch.object(main.time,'monotonic',return_value=100):self.assertEqual(main.VirtualGrid.ensure_folder_thumb(grid,key),str(thumbnail))
   thumbnail.unlink()
   with patch.object(main.time,'monotonic',return_value=101):self.assertEqual(main.VirtualGrid.ensure_folder_thumb(grid,key),str(thumbnail))
   with patch.object(main.time,'monotonic',return_value=106):self.assertIsNone(main.VirtualGrid.ensure_folder_thumb(grid,key))
   self.assertNotIn(key,grid.folder_cache)
 def test_kept_preview_worker_uses_existing_cache(self):
  from types import SimpleNamespace
  import threading
  with tempfile.TemporaryDirectory() as raw:
   cached=Path(raw)/'kept.jpg';image=main.QImage(64,64,main.QImage.Format_RGB32);image.fill(main.QColor('red'));image.save(str(cached))
   def unexpected(*args,**kwargs):raise AssertionError('Cached preview must not rediscover folder images')
   grid=SimpleNamespace(is_persistently_kept=lambda folder:True,load_persistent_folder_thumb=lambda folder:str(cached),thumbnail_worker=lambda path,w,h:main.QImage(path),folder_image=unexpected,persist_folder_thumb=unexpected)
   source,pixels,size=main.VirtualGrid.resolve_folder_display(grid,'folder',(64,64),threading.Event())
   self.assertEqual(source,str(cached));self.assertEqual(size,(64,64));self.assertEqual(pixels.pixelColor(32,32).red(),254)
 def test_gpu_restart_moves_before_first_repaint(self):
  from types import SimpleNamespace,MethodType
  for gpu in (False,True):
   calls=[];bar=SimpleNamespace(value=lambda:100,minimum=lambda:0,maximum=lambda:10000)
   grid=SimpleNamespace(screen=lambda:SimpleNamespace(refreshRate=lambda:60),viewport=lambda:SimpleNamespace(property=lambda key:gpu))
   driver=SimpleNamespace(running=False,bar=bar,grid=grid,duration=600,timer=SimpleNamespace(start=lambda delay:calls.append(('timer',delay))),schedule=lambda now:calls.append(('schedule',now)),stop=lambda:None)
   driver.advance=MethodType(main.SmoothScrollDriver.advance,driver)
   with patch.object(main.time,'perf_counter',return_value=100):main.SmoothScrollDriver.retarget(driver,1000)
   self.assertEqual(driver.position,100);self.assertEqual(driver.target,1000)
   self.assertEqual(calls,[('timer',1)] if gpu else [('schedule',100)])
   with patch.object(main.time,'perf_counter',return_value=100.001):main.SmoothScrollDriver.retarget(driver,1500)
   self.assertEqual(len(calls),1);self.assertGreater(driver.position,100)
 def test_cancelled_archive_read(self):
  import io
  with tempfile.TemporaryDirectory() as raw:
   archive=Path(raw)/'a.zip'
   with zipfile.ZipFile(archive,'w') as z:z.writestr('a.txt','text')
   with self.assertRaises(InterruptedError):main.copy_archive_member_to_stream(archive,'a.txt',io.BytesIO(),should_cancel=lambda:True)
if __name__=='__main__':unittest.main()
