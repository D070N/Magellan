import sys,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'MediaExplorer'))
import main

class GpuBlurPolicyTests(unittest.TestCase):
 def test_preference_defaults_off_and_rejects_non_boolean(self):
  for data in ({},{'gpu_blur_enabled':'true'},{'gpu_blur_enabled':1}):self.assertIs(main.Explorer._sanitize_settings(data)['gpu_blur_enabled'],False)
  self.assertIs(main.Explorer._sanitize_settings({'gpu_blur_enabled':True})['gpu_blur_enabled'],True)
 def test_software_renderer_does_not_load_gpu_backend(self):
  owner=SimpleNamespace(gpu_blur_enabled=True,grid=SimpleNamespace(viewport=lambda:SimpleNamespace(property=lambda name:False)))
  with patch('builtins.__import__',side_effect=AssertionError('GPU imports must stay lazy')):
   self.assertIsNone(main.Explorer.gpu_blurred_crop(owner,None,None,20))
 def test_failure_disables_experiment_closes_backend_and_preserves_cpu_fallback(self):
  backend=Mock();backend.crop.side_effect=RuntimeError('simulated driver failure')
  owner=SimpleNamespace(gpu_blur_enabled=True,grid=SimpleNamespace(viewport=lambda:SimpleNamespace(property=lambda name:True)),_gpu_blur_backend=backend,_appearance_source_cache=(0,object()),gpu_blur_action=Mock(),save_settings=Mock(),status=Mock())
  owner.close_gpu_blur_backend=lambda:main.Explorer.close_gpu_blur_backend(owner)
  with self.assertLogs('magellan',level='WARNING'):
   self.assertIsNone(main.Explorer.gpu_blurred_crop(owner,object(),object(),20))
  self.assertFalse(owner.gpu_blur_enabled);backend.close.assert_called_once();owner.save_settings.assert_called_once()
  self.assertIsNone(main.Explorer.gpu_blurred_crop(owner,object(),object(),20));self.assertEqual(backend.crop.call_count,1)

if __name__=='__main__':unittest.main()
