import sys,tempfile,zipfile,threading
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'MediaExplorer'))
import main
from PySide6.QtTest import QTest
app=main.QApplication([]);owner=main.QWidget();owner._closing=False;owner.status=Mock();owner.grid=Mock();owner.image_search_mode=True
results=[];owner.perform_image_search=lambda path,prepared_archive=None:results.append((path,prepared_archive,threading.get_ident()))
with tempfile.TemporaryDirectory() as directory:
 path=Path(directory)/'test.zip';image=main.QImage(100,150,main.QImage.Format_RGB32);image.fill(main.Qt.red)
 buffer=main.QBuffer();buffer.open(main.QIODevice.WriteOnly);image.save(buffer,'PNG')
 with zipfile.ZipFile(path,'w') as archive:archive.writestr('folder/image.png',bytes(buffer.data()))
 virtual=main.encode_archive_path(path,'folder/image.png')
 main.Explorer.start_archive_image_search(owner,virtual,str(path),'folder/image.png')
 for _ in range(150):
  if results:break
  QTest.qWait(20)
 assert len(results)==1 and results[0][0]==virtual and results[0][2]==threading.get_ident()
 assert not main.QImage.fromData(results[0][1][0]).isNull()
 assert owner._archive_image_search_future is None and not owner.image_search_mode
 print('PASS background archive preparation and GUI-thread provider handoff')
owner.close();app.quit()
