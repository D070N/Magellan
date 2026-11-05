"""Pixel regression for label stability when adaptive card construction defers."""
import sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'MediaExplorer'))
import main
app=main.QApplication([])
owner=main.QMainWindow();owner.thumbnail_cache_enabled=True
owner.file_clipboard_cut=False;grid=main.VirtualGrid(owner)
grid.card_thumbnail_source=lambda item,meta:None
grid._appearance_card_pixels={};grid.image_search_active=False
cases=[('folder',True,False,'A long folder name that wraps across two lines'),('image',False,True,'A long image filename that wraps across two lines.png'),('video',False,False,'▶ A long video filename with punctuation and 👑.mp4'),('favorite_group',False,False,'A long favorite group name')]
checked=0
try:
 for dpr in (1.0,1.5,2.0):
  for width in (142,232,432):
   height=width+48;side=width-2;size=main.QSize(width,height)
   for kind,is_dir,icon,name in cases:
    item={'path':Path('example'),'name':name,'kind':kind,'is_dir':is_dir}
    meta=('', '',icon);grid.items=[item];grid.paint_metadata={id(item):meta};grid.card_cache.clear();grid.cell_w=width
    # Render the cached card and the actual forced-budget fallback at a nonzero origin.
    normal=grid.render_card_pixmap(0,size,side,None,meta,dpr)
    canvas=main.QPixmap(round((width+40)*dpr),round((height+40)*dpr));canvas.setDevicePixelRatio(dpr);canvas.fill(main.QColor('#aa22aa'))
    painter=main.QPainter(canvas);grid._card_budget_enabled=True;grid._cards_built_this_frame=1;grid._card_build_deadline=0
    original_ratio=grid.devicePixelRatioF;grid.devicePixelRatioF=lambda:dpr
    grid.paint_card(painter,0,main.QRect(18,20,width,height));painter.end();grid.devicePixelRatioF=original_ratio
    label=main.QRect(2,side+3,width-4,height-side-5)
    expected=normal.toImage().copy(main.QRect(round(2*dpr),round(label.y()*dpr),round((width-4)*dpr),round(label.height()*dpr)))
    actual=canvas.toImage().copy(main.QRect(round(20*dpr),round((20+label.y())*dpr),round((width-4)*dpr),round(label.height()*dpr)))
    assert actual.convertToFormat(main.QImage.Format_ARGB32)==expected.convertToFormat(main.QImage.Format_ARGB32),(dpr,width,kind)
    checked+=1
 print('PASS',checked,'deferred/cached label cases; label pixels identical at all tested display scales')
finally:
 grid.shutdown_workers();owner.close();app.quit()
