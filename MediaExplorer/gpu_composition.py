"""Optional shared GPU backplates beneath the existing Qt controls."""
from PySide6.QtCore import Qt,QRect,QRectF,QPoint,QTimer,QEvent
from PySide6.QtGui import QOpenGLContext,QRegion,QPainter
from PySide6.QtWidgets import QStyle,QStyleOption,QToolButton,QLineEdit,QStyleOptionSlider
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtOpenGL import QOpenGLFramebufferObject,QOpenGLPaintDevice,QOpenGLTextureBlitter
from gpu_blur import GpuBlur

class Surface(QOpenGLWidget):
    """Mouse-transparent display of a texture owned by the shared compositor."""
    def __init__(self, parent, on_failure):
        super().__init__(parent)
        self.on_failure = on_failure
        self.failed = False
        self.blitter = None
        self.frame = None
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def fail(self):
        if not self.failed:
            self.failed = True
            QTimer.singleShot(0, self.on_failure)

    def initializeGL(self):
        try:
            self.blitter = QOpenGLTextureBlitter()
            if not self.blitter.create():
                raise RuntimeError('Display blitter unavailable')
        except Exception:
            self.fail()

    def paintGL(self):
        if self.failed:
            return
        try:
            gl = self.context().functions()
            gl.glClearColor(0, 0, 0, 0)
            gl.glClear(0x4000)
            if self.frame is not None and self.blitter is not None:
                texture, _ = self.frame
                viewport = QRect(0, 0, round(self.width() * self.devicePixelRatioF()),
                                 round(self.height() * self.devicePixelRatioF()))
                self.blitter.bind()
                self.blitter.blit(texture, QOpenGLTextureBlitter.targetTransform(viewport, viewport),
                                  QOpenGLTextureBlitter.OriginBottomLeft)
                self.blitter.release()
                if gl.glGetError():
                    self.fail()
        except Exception:
            self.fail()

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Hide:
            self.hide()
        return False

    def assign(self, texture, size):
        self.frame = (texture, size)
        self.update()

class Composition:
 def __init__(self,owner):
  if QOpenGLContext.globalShareContext() is None:raise RuntimeError('Shared OpenGL contexts must be enabled before startup')
  self.owner=owner;self.engine=GpuBlur(QOpenGLContext.globalShareContext());self.frames={};self.widgets={};self.blitter=None;self.closed=False
 def _style(self,widget,size):
  device=QOpenGLPaintDevice(size);painter=QPainter(device);option=QStyleOption();option.initFrom(widget);option.rect=QRect(QPoint(),size);widget.style().drawPrimitive(QStyle.PE_Widget,option,painter,widget);painter.end()
 def _blit(self,texture,source,size,destination,target_size,opacity=1,clip=None,origin=QOpenGLTextureBlitter.OriginBottomLeft):
  gl=self.engine.gl;gl.glEnable(0x0BE2);gl.glBlendFuncSeparate(0x0302,0x0303,1,0x0303);gl.glDisable(0x0B71);gl.glDisable(0x0B90)
  self.blitter.bind();self.blitter.setOpacity(opacity)
  transform=QOpenGLTextureBlitter.targetTransform(QRectF(destination),QRect(QPoint(),target_size));sample=QOpenGLTextureBlitter.sourceTransform(QRectF(source),size,origin)
  region=clip.intersected(QRegion(destination)) if clip is not None else QRegion(destination)
  gl.glEnable(0x0C11)
  for rect in region:
   gl.glScissor(rect.x(),target_size.height()-rect.y()-rect.height(),rect.width(),rect.height());self.blitter.blit(texture,transform,sample)
  gl.glDisable(0x0C11);self.blitter.release()
 def refresh(self):
  owner=self.owner;source_widget=owner.grid.viewport();source_global=QRect(source_widget.mapToGlobal(QPoint()),source_widget.size());source=owner.appearance_source_snapshot(source_widget);image=source.toImage();requests=[];plans=[]
  for name,panel,old in (('main',owner.main_panel_backdrop,owner.main_blur_backdrop),('buttons',owner.controls_bar,owner.controls_backdrop),('tabs',owner.tabbar,owner.tabs_backdrop),('tree',getattr(owner,'tree_dock',None),getattr(owner,'tree_blur_backdrop',None))):
   if panel is None or old is None or not panel.isVisible():continue
   trans=int(getattr(owner,f'appearance_{name}_transparency'));radius=int(getattr(owner,f'appearance_{name}_blur'));glob=QRect(panel.mapToGlobal(QPoint()),panel.size());ops=[];mask=QRegion()
   if name in ('main','tree'):
    if trans>0:
     rect=QRect(0,0,min(panel.width(),image.width()),min(panel.height(),image.height()));sample=image
     if name=='tree' and panel.height()>image.height():sample=image.copy(rect).scaled(panel.size(),Qt.IgnoreAspectRatio,Qt.SmoothTransformation);rect=sample.rect()
     requests.append((rect,radius,sample));ops.append((rect,radius,QRect(rect),trans/100,None))
   else:
    targets=[]
    if name=='tabs' and trans>0:targets=[QRect(panel.mapToGlobal(panel.tabRect(i).topLeft()),panel.tabRect(i).size()) for i in range(panel.count())]
    elif name=='buttons' and trans>0:
     targets=[QRect(w.mapToGlobal(QPoint()),w.size()) for w in [*panel.findChildren(QToolButton),*panel.findChildren(QLineEdit,'search')] if w.isVisibleTo(panel)]
     if hasattr(owner,'slider') and owner.slider.isVisibleTo(panel):
      option=QStyleOptionSlider();owner.slider.initStyleOption(option);handle=owner.slider.style().subControlRect(QStyle.CC_Slider,option,QStyle.SC_SliderHandle,owner.slider);targets.append(QRect(owner.slider.mapToGlobal(handle.topLeft()),handle.size()))
    for target in targets:
     local=target.translated(-glob.topLeft());shape=owner.tab_backdrop_shape_region(local) if name=='tabs' else QRegion(local);mask=mask.united(shape)
     point=target.topLeft()-source_global.topLeft()
     if not source_global.intersects(target):point=target.topLeft()-owner.main_panel_backdrop.mapToGlobal(QPoint())
     requested=QRect(point,target.size());rect=requested.intersected(image.rect())
     if not rect.isEmpty():requests.append((rect,radius));ops.append((rect,radius,QRect(local.topLeft()+rect.topLeft()-requested.topLeft(),rect.size()),1,shape))
   plans.append((name,panel,old,glob,ops,mask))
  # Right-side and fullscreen-control samples use the same frame and atlas.
  for button,old in [*owner.right_control_backdrops.items(),*owner.window_control_backdrops.items()]:
   if not button.isVisible() or owner.appearance_buttons_transparency<=0:continue
   glob=QRect(button.mapToGlobal(QPoint()),button.size());rect=glob.translated(-source_global.topLeft()).intersected(image.rect())
   if rect.isEmpty():old.hide();continue
   radius=owner.appearance_buttons_blur;requests.append((rect,radius));dest=QRect(rect.topLeft()-(glob.topLeft()-source_global.topLeft()),rect.size());plans.append((str(id(button)),button,old,glob,[(rect,radius,dest,1,None)],QRegion()))
  if len(requests)>128:raise RuntimeError('GPU composition sample limit exceeded')
  result=self.engine.process(image,requests,download=False) if requests else None
  if requests and not result:raise RuntimeError('GPU composition atlas limit exceeded')
  texture,atlas_size,tiles=result if result else (None,None,{})
  if any((r.x(),r.y(),r.width(),r.height(),int(rad)) not in tiles for r,rad,*_ in requests):raise RuntimeError('GPU composition atlas incomplete')
  previous=QOpenGLContext.currentContext();previous_surface=previous.surface() if previous else None
  if not self.engine.context.makeCurrent(self.engine.surface):raise RuntimeError('Composition context unavailable')
  gl=self.engine.gl
  try:
   if self.blitter is None:
    self.blitter=QOpenGLTextureBlitter()
    if not self.blitter.create():raise RuntimeError('Composition blitter unavailable')
   for name,panel,old,glob,ops,mask in plans:
    size=panel.size();fbo=self.frames.get(name)
    if fbo is None or fbo.size()!=size:fbo=QOpenGLFramebufferObject(size,QOpenGLFramebufferObject.CombinedDepthStencil);self.frames[name]=fbo
    if not fbo.isValid():raise RuntimeError('Composition framebuffer unavailable')
    fbo.bind();gl.glViewport(0,0,size.width(),size.height());gl.glClearColor(0,0,0,0);gl.glClear(0x4000|0x0400|0x0100)
    if name in ('main','tree'):self._style(owner.main_panel_backdrop if name=='main' else owner.tree_backdrop,size)
    if name in ('tabs','buttons') and 'main' in self.frames:
     main=self.frames['main'];origin=owner.main_panel_backdrop.mapToGlobal(QPoint());crop=glob.translated(-origin).intersected(QRect(QPoint(),main.size()))
     if not crop.isEmpty():
      sample=QRect(crop.x(),main.height()-crop.y()-crop.height(),crop.width(),crop.height())
      self._blit(main.texture(),sample,main.size(),QRect(crop.topLeft()-(glob.topLeft()-origin),crop.size()),size)
    for rect,radius,dest,opacity,clip in ops:
     key=(rect.x(),rect.y(),rect.width(),rect.height(),int(radius));tile=tiles.get(key)
     if tile is None:continue
     # Atlas tiles use GL coordinates; image pixels were uploaded top-first.
     sample=QRect(tile.x(),atlas_size.height()-tile.y()-tile.height(),tile.width(),tile.height())
     self._blit(texture,sample,atlas_size,dest,size,opacity,clip,QOpenGLTextureBlitter.OriginTopLeft)
    if name in ('main','tree'):
     overlays=(owner.main_gradient_overlay,owner.main_path_gradient_overlay) if name=='main' else (owner.tree_gradient_overlay,)
     for overlay in overlays:
      if 'qlineargradient' in overlay.styleSheet():self._style(overlay,size)
    fbo.release()
   gl.glFinish() # Initial correctness gate; replace with fences only after validation.
  finally:
   self.engine.context.doneCurrent()
   if previous and previous_surface:previous.makeCurrent(previous_surface)
  used=set()
  for name,panel,old,glob,ops,mask in plans:
   if name=='buttons' and mask.isEmpty():old.hide();continue
   used.add(name);widget=self.widgets.get(name)
   if widget is None:widget=Surface(old.parentWidget(),lambda:owner.disable_gpu_composition('Display surface unavailable'));self.widgets[name]=widget;panel.installEventFilter(widget)
   old.setGeometry(QRect(old.parentWidget().mapFromGlobal(glob.topLeft()),panel.size()))
   widget.setUpdatesEnabled(True);widget.assign(self.frames[name].texture(),self.frames[name].size());widget.setGeometry(old.geometry());widget.setMask(mask) if name=='buttons' else widget.clearMask();old.hide()
   widget.show();widget.update();widget.parentWidget().update();widget.lower()
   if name=='tabs':widget.stackUnder(owner.tabbar)
   if name.isdigit():widget.stackUnder(panel)
  for name,widget in self.widgets.items():
   if name not in used:widget.hide()
  for overlay in (owner.main_gradient_overlay,owner.main_path_gradient_overlay,getattr(owner,'tree_gradient_overlay',None)):
   if overlay is not None:overlay.hide()
  owner.raise_main_panel_contents()
 def close(self):
  if self.closed:return
  self.closed=True
  for widget in self.widgets.values():widget.frame=None;widget.hide();widget.deleteLater()
  previous=QOpenGLContext.currentContext();surface=previous.surface() if previous else None
  self.engine.context.makeCurrent(self.engine.surface);self.frames.clear();
  if self.blitter is not None:self.blitter.destroy();self.blitter=None
  self.engine.context.doneCurrent()
  if previous and surface:previous.makeCurrent(surface)
  self.engine.close()
