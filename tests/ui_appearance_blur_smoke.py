"""Compare optimized blur against the previous algorithm, including alpha edges."""
import sys,random,gc
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'MediaExplorer'))
import main
from PIL import Image,ImageFilter
app=main.QApplication([])
def reference(pixmap,radius):
 if radius<=0 or pixmap.isNull():return pixmap
 image=pixmap.toImage().convertToFormat(main.QImage.Format_RGBA8888);w=image.width();h=image.height()
 pil=Image.frombytes('RGBA',(w,h),bytes(image.constBits()),'raw','RGBA',image.bytesPerLine())
 factor=4 if radius>=24 else (2 if radius>=12 else 1)
 if factor>1:pil=pil.resize((max(1,main.math.ceil(w/factor)),max(1,main.math.ceil(h/factor))),Image.Resampling.BILINEAR).filter(ImageFilter.GaussianBlur(radius/factor)).resize((w,h),Image.Resampling.BILINEAR)
 else:pil=pil.filter(ImageFilter.GaussianBlur(radius))
 return main.QPixmap.fromImage(main.QImage(pil.tobytes(),w,h,w*4,main.QImage.Format_RGBA8888).copy())
rng=random.Random(20261104);checked=0;retained=[]
for opaque in (False,True):
 for width,height in ((1,1),(58,42),(300,900),(1900,100)):
  raw=rng.randbytes(width*height*4);image=main.QImage(raw,width,height,width*4,main.QImage.Format_RGBA8888).copy()
  if opaque:image=image.convertToFormat(main.QImage.Format_RGB32)
  pixmap=main.QPixmap.fromImage(image)
  for radius in (0,1,5,11,12,20,23,24,30,46,100):
   expected=reference(pixmap,radius).toImage().convertToFormat(main.QImage.Format_ARGB32)
   actual=main.Explorer.blur_appearance_sample(pixmap,radius)
   assert actual.toImage().convertToFormat(main.QImage.Format_ARGB32)==expected,(opaque,width,height,radius)
   retained.append((actual,expected));checked+=1
# Verify returned pixmaps keep their pixels after temporary Python buffers die.
gc.collect()
for actual,expected in retained:assert actual.toImage().convertToFormat(main.QImage.Format_ARGB32)==expected
assert main.Explorer.blur_appearance_sample(main.QPixmap(),30).isNull()
print('PASS',checked,'exact blur comparisons, alpha preservation and buffer lifetime')
app.quit()
