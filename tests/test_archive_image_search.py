import base64,io,sys,tempfile,unittest,zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'MediaExplorer'))
import main
from PySide6.QtCore import QCoreApplication
app=QCoreApplication.instance() or QCoreApplication([])

class ArchiveImageSearchTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
 def tearDown(self):self.temp.cleanup()
 def image_data(self,width=80,height=120):
  image=main.QImage(width,height,main.QImage.Format_ARGB32);image.fill(main.Qt.transparent)
  data=main.QBuffer();data.open(main.QIODevice.WriteOnly);self.assertTrue(image.save(data,'PNG'));return bytes(data.data())
 def archive(self,suffix='.zip',member='folder/👑 picture.png',data=None):
  path=self.root/('images'+suffix)
  with zipfile.ZipFile(path,'w') as archive:archive.writestr(member,self.image_data() if data is None else data)
  return path,member
 def test_zip_member_encodes_bounded_opaque_jpeg(self):
  path,member=self.archive(data=self.image_data(3000,4000));raw,name=main.prepare_archive_search_upload(path,member)
  result=main.QImage.fromData(raw);self.assertEqual(result.size(),main.QSize(1920,2560));self.assertFalse(result.hasAlphaChannel());self.assertGreater(result.pixelColor(5,5).red(),245);self.assertEqual(name,'👑 picture.jpg');self.assertTrue(raw.startswith(b'\xff\xd8'))
 def test_comic_and_thumbnail_virtual_paths(self):
  path,member=self.archive('.cbz');virtual=main.encode_archive_path(path,member);thumb=main.encode_archive_thumbnail(path,member)
  for value,decode in ((virtual,main.decode_archive_path),(thumb,main.decode_archive_thumbnail)):
   decoded=decode(value);raw,name=main.prepare_archive_search_upload(*decoded[:2]);self.assertFalse(main.QImage.fromData(raw).isNull())
 def test_nested_archive_member(self):
  inner=io.BytesIO()
  with zipfile.ZipFile(inner,'w') as archive:archive.writestr('inside/image.png',self.image_data())
  outer,member=self.archive(member='nested/comic.cbz',data=inner.getvalue())
  nested=main.materialize_nested_archive(outer,member)
  raw,name=main.prepare_archive_search_upload(nested,'inside/image.png');self.assertEqual(name,'image.jpg');self.assertFalse(main.QImage.fromData(raw).isNull())
 def test_corrupt_image_reports_failure(self):
  path,member=self.archive(data=b'broken')
  with self.assertRaisesRegex(RuntimeError,'decode'):main.prepare_archive_search_upload(path,member)
 def test_non_image_is_rejected(self):
  with self.assertRaises(ValueError):main.prepare_archive_search_upload('not-opened.zip','document.txt')
 def test_missing_member_reports_failure(self):
  path,member=self.archive()
  with self.assertRaises(FileNotFoundError):main.prepare_archive_search_upload(path,'absent.png')
 def test_both_providers_use_prepared_archive_bytes_without_disk_access(self):
  path,member=self.archive();prepared=main.prepare_archive_search_upload(path,member)
  for provider in ('google','saucenao'):
   owner=SimpleNamespace(image_search_provider=provider,google_lens_query='',saucenao_api_key='test-only',status=Mock(),grid=Mock(),open_search_bridge=Mock(),image_search_mode=True)
   with patch.object(main.QMessageBox,'warning') as warning,patch.object(main.Path,'stat',side_effect=AssertionError('virtual path must not be statted')):
    main.Explorer.perform_image_search(owner,main.encode_archive_path(path,member),prepared_archive=prepared)
   warning.assert_not_called();owner.open_search_bridge.assert_called_once();html=owner.open_search_bridge.call_args.args[0];self.assertIn(base64.b64encode(prepared[0]).decode(),html);self.assertFalse(owner.image_search_mode)
 def test_virtual_path_dispatches_to_background_preparation(self):
  path,member=self.archive();owner=SimpleNamespace(start_archive_image_search=Mock())
  virtual=main.encode_archive_path(path,member);main.Explorer.perform_image_search(owner,virtual)
  owner.start_archive_image_search.assert_called_once_with(virtual,str(path),member)

if __name__=='__main__':unittest.main()
