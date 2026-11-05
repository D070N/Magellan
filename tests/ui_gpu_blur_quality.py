"""Optional hardware checks: portable shader path, bounds, colors and GL ownership."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'MediaExplorer'))
import main
from gpu_blur import GpuBlur
app=main.QApplication([]);engine=GpuBlur()
try:
 image=main.QImage(500,400,main.QImage.Format_RGB32);image.fill(main.Qt.red)
 for color in (main.Qt.red,main.Qt.blue):
  image.fill(color);result=next(iter(engine.process(image,[(main.QRect(0,0,500,400),30)]).values()))
  assert result.pixelColor(20,20)==main.QColor(color),'upload storage reused stale pixels'
 source=main.QImage(400,400,main.QImage.Format_RGB32);p=main.QPainter(source)
 for x,y,color in ((0,0,main.Qt.green),(200,0,main.Qt.blue),(0,200,main.Qt.red),(200,200,main.Qt.white)):p.fillRect(x,y,200,200,color)
 p.end();r=next(iter(engine.process(source,[(main.QRect(0,0,400,400),20)]).values()))
 for x,y,color in ((30,30,main.Qt.green),(370,30,main.Qt.blue),(30,370,main.Qt.red),(370,370,main.Qt.white)):
  expected=main.QColor(color);actual=r.pixelColor(x,y);assert max(abs(a-b) for a,b in zip(actual.getRgb(),expected.getRgb()))<=1,'orientation/color error'
 # Adjacent atlas jobs must not sample each other's contrasting colors.
 jobs=engine.process(source,[(main.QRect(0,0,200,200),30),(main.QRect(200,0,200,200),30)])
 for key,image in jobs.items():
  expected=main.QColor(main.Qt.green if key[0]==0 else main.Qt.blue)
  assert image.pixelColor(image.width()-1,10)==expected,'blur leaked across crop boundary'
 large=main.QImage(1900,100,main.QImage.Format_RGB32);large.fill(main.Qt.red)
 assert next(iter(engine.process(large,[(large.rect(),5)]).values())).pixelColor(10,10)==main.QColor(main.Qt.red)
 assert engine.process(source,[(main.QRect(-5,0,10,10),20)])=={}
 alpha=main.QImage(20,20,main.QImage.Format_ARGB32);alpha.fill(main.Qt.transparent)
 try:engine.process(alpha,[(alpha.rect(),20)]);raise AssertionError('alpha source must use CPU fallback')
 except ValueError:pass
 engine.context.makeCurrent(engine.surface);other=GpuBlur()
 try:
  other.process(source,[(source.rect(),20)])
  from PySide6.QtGui import QOpenGLContext
  assert QOpenGLContext.currentContext()==engine.context,'caller GL context not restored'
 finally:other.close();engine.context.doneCurrent()
 print('PASS GPU colors, orientation, atlas clipping, stale-upload prevention, bounds, alpha fallback and context restoration')
finally:engine.close();engine.close();app.quit()
