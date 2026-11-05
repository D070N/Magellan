import json, logging, sys, tempfile, threading, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'MediaExplorer'))
from settings_storage import SettingsWriter

class SettingsWriterTests(unittest.TestCase):
 def setUp(self):
  self.directory=tempfile.TemporaryDirectory();self.path=Path(self.directory.name)/'settings.json';self.logger=logging.getLogger('settings-test');self.writer=SettingsWriter(self.path,self.logger)
 def tearDown(self):
  self.writer.finish();self.directory.cleanup()
 def test_complete_json_and_unicode_roundtrip(self):
  expected={'favorites':['👑/画像.png'],'groups':[{'members':['a','b']}],'tabs':[{'positions':{'a':333}}]}
  self.writer.submit(expected);self.assertTrue(self.writer.finish());self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')),expected)
 def test_slow_write_does_not_block_submit_and_only_latest_pending_wins(self):
  entered=threading.Event();release=threading.Event();calls=[];real=self.writer._write
  def slow(payload):
   calls.append((threading.get_ident(),json.loads(payload)['version']))
   if len(calls)==1:entered.set();self.assertTrue(release.wait(2))
   real(payload)
  self.writer._write=slow;self.writer.submit({'version':0});self.assertTrue(entered.wait(2))
  try:
   for value in range(1,101):self.writer.submit({'version':value})
  finally:release.set()
  self.assertTrue(self.writer.finish());self.assertEqual([v for _,v in calls],[0,100]);self.assertTrue(all(tid!=threading.get_ident() for tid,_ in calls));self.assertEqual(json.loads(self.path.read_text())['version'],100)
 def test_identical_snapshot_skips_disk_write(self):
  real=self.writer._write;written=threading.Event()
  def save(payload):real(payload);written.set()
  with patch.object(self.writer,'_write',side_effect=save) as write:
   self.writer.submit({'a':1});self.assertTrue(written.wait(2));self.writer.submit({'a':1});self.assertTrue(self.writer.finish());self.assertEqual(write.call_count,1)
 def test_replace_failure_preserves_original_and_removes_temporary(self):
  self.path.write_text('{"old":true}')
  with patch('settings_storage.os.replace',side_effect=PermissionError('locked')),self.assertLogs('settings-test',level='ERROR'):
   self.writer.submit({'new':True});self.assertFalse(self.writer.finish())
  self.assertEqual(json.loads(self.path.read_text()),{'old':True});self.assertEqual(list(self.path.parent.glob('*.tmp')),[])
 def test_serialization_failure_preserves_original(self):
  self.path.write_text('{"old":true}')
  with self.assertLogs('settings-test',level='ERROR'):
   self.writer.submit({'bad':object()});self.assertFalse(self.writer.finish())
  self.assertEqual(json.loads(self.path.read_text()),{'old':True})
 def test_failed_write_can_recover_on_new_snapshot(self):
  failed=threading.Event();real=self.writer._write
  def save(payload):
   if not failed.is_set():failed.set();raise OSError('temporary failure')
   real(payload)
  with patch.object(self.writer,'_write',side_effect=save),self.assertLogs('settings-test',level='ERROR'):
   self.writer.submit({'v':1});self.assertTrue(failed.wait(2));self.writer.submit({'v':2});self.assertTrue(self.writer.finish())
  self.assertEqual(json.loads(self.path.read_text()),{'v':2})
 def test_finish_timeout_is_bounded_and_can_be_completed(self):
  entered=threading.Event();release=threading.Event();real=self.writer._write
  def save(payload):entered.set();release.wait(2);real(payload)
  self.writer._write=save;self.writer.submit({'v':1});self.assertTrue(entered.wait(2))
  try:self.assertFalse(self.writer.finish(timeout=.001))
  finally:release.set()
  self.assertTrue(self.writer.finish());self.assertEqual(json.loads(self.path.read_text()),{'v':1})
 def test_submit_after_finish_rejected(self):
  self.assertTrue(self.writer.finish())
  with self.assertRaises(RuntimeError):self.writer.submit({'v':1})

if __name__=='__main__':unittest.main()
