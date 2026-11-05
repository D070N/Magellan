"""Optional panel-blur prototype. Isolated GL state; bounded batched transfers."""
import math,time
from array import array
from PySide6.QtCore import QSize,Qt,QRect
from PySide6.QtGui import QImage,QPixmap,QPainter,QOpenGLContext,QOffscreenSurface,QSurfaceFormat
from PySide6.QtOpenGL import QOpenGLFramebufferObject,QOpenGLShaderProgram,QOpenGLShader,QOpenGLBuffer,QOpenGLTexture

VERTEX='''#version 120
attribute vec2 position;
varying vec2 uv;
uniform vec4 sampleRect;
void main(){ gl_Position=vec4(position,0.0,1.0); uv=sampleRect.xy+(position*0.5+0.5)*sampleRect.zw; }
'''
FRAGMENT='''#version 120
uniform sampler2D image;
uniform vec2 stepSize;
uniform vec4 limits;
uniform float sigma;
varying vec2 uv;
void main(){
 vec4 color=vec4(0.0);float total=0.0;
 for(int i=-80;i<=80;i++){
  float x=float(i);
  if(abs(x)<=ceil(3.0*sigma)){
   float weight=exp(-0.5*x*x/(sigma*sigma));
   color+=texture2D(image,clamp(uv+stepSize*x,limits.xy,limits.zw))*weight;total+=weight;
  }
 }
 gl_FragColor=color/total;
}
'''
class GpuBlur:
 def __init__(self,share_context=None):
  self.closed=False;self.vertices=None;self.program=None;self.texture=None;self.fbos=None;self.size=None;self.timings=[]
  self.context=QOpenGLContext();fmt=QSurfaceFormat();fmt.setVersion(2,0);fmt.setSwapInterval(0);self.context.setFormat(fmt)
  if share_context is not None:self.context.setShareContext(share_context)
  if not self.context.create():raise RuntimeError('Could not create an OpenGL blur context')
  self.surface=QOffscreenSurface();self.surface.setFormat(self.context.format());self.surface.create()
  previous=QOpenGLContext.currentContext();previous_surface=previous.surface() if previous else None
  try:
   if not self.context.makeCurrent(self.surface):raise RuntimeError('Could not activate the blur context')
   self.gl=self.context.functions();self.gl.initializeOpenGLFunctions()
   self.program=QOpenGLShaderProgram()
   for kind,code in ((QOpenGLShader.Vertex,VERTEX),(QOpenGLShader.Fragment,FRAGMENT)):
    if not self.program.addShaderFromSourceCode(kind,code):raise RuntimeError(self.program.log())
   self.program.bindAttributeLocation('position',0)
   if not self.program.link():raise RuntimeError(self.program.log())
   self.vertices=QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
   if not self.vertices.create():raise RuntimeError('Could not create a blur vertex buffer')
   self.vertices.bind();data=array('f',[-1,-1,1,-1,-1,1,1,1]).tobytes();self.vertices.allocate(data,len(data));self.vertices.release()
  finally:
   self.context.doneCurrent()
   if previous and previous_surface:previous.makeCurrent(previous_surface)
 def process(self,source,requests,download=True):
  if source.hasAlphaChannel():raise ValueError('Prototype accepts opaque source images only')
  requests=[(entry[0],entry[1],entry[2] if len(entry)>2 else source) for entry in requests]
  started=time.perf_counter();stages={};jobs=[];allocated_pixels=0;x=y=row=0;width=max(1024,min(4096,max((math.ceil(r.width()/(4 if rad>=24 else (2 if rad>=12 else 1))) for r,rad,_ in requests),default=1)))
  for rect,radius,sample_source in requests:
   if radius<0 or radius>100 or rect.isEmpty() or not sample_source.rect().contains(rect):continue
   factor=4 if radius>=24 else (2 if radius>=12 else 1)
   w=max(1,math.ceil(rect.width()/factor));h=max(1,math.ceil(rect.height()/factor))
   if w>width:continue
   if x+w>width:x=0;y+=row;row=0
   if y+h>4096 or allocated_pixels+w*h>4*1024*1024:continue
   allocated_pixels+=w*h
   tile=(sample_source,rect) if factor==1 else sample_source.copy(rect).scaled(w,h,Qt.IgnoreAspectRatio,Qt.SmoothTransformation)
   jobs.append((rect,radius,QRect(x,y,w,h),max(.1,float(radius)/factor),tile));x+=w;row=max(row,h)
  if not jobs:return {}
  height=1<<max(0,(max(job[2].bottom()+1 for job in jobs)-1).bit_length())
  if width*height>4*1024*1024:return {}
  if height>4096:raise ValueError('Blur atlas exceeds the prototype size limit')
  atlas=getattr(self,'_reusable_atlas',None)
  if atlas is None or atlas.width()!=width or atlas.height()!=height:atlas=QImage(width,height,QImage.Format_RGB32);self._reusable_atlas=atlas
  atlas.fill(Qt.black);painter=QPainter(atlas)
  for _,_,r,_,tile in jobs:
   if isinstance(tile,tuple):painter.drawImage(r,tile[0],tile[1])
   else:painter.drawImage(r.topLeft(),tile)
  painter.end();stages['pack_ms']=(time.perf_counter()-started)*1000;stamp=time.perf_counter();previous=QOpenGLContext.currentContext();previous_surface=previous.surface() if previous else None
  try:
   if not self.context.makeCurrent(self.surface):raise RuntimeError('Blur context unavailable')
   stages['context_ms']=(time.perf_counter()-stamp)*1000;stamp=time.perf_counter();gl=self.gl;size=QSize(width,height)
   if self.size!=size:
    if self.texture is not None:self.texture.destroy();self.texture=None
    self.fbos=None;self.fbos=[QOpenGLFramebufferObject(size),QOpenGLFramebufferObject(size)];self.size=size
    if not all(f.isValid() for f in self.fbos):raise RuntimeError('Blur framebuffer unavailable')
   if self.texture is None:
    self.texture=QOpenGLTexture(atlas,QOpenGLTexture.DontGenerateMipMaps);self.texture.setMinMagFilters(QOpenGLTexture.Linear,QOpenGLTexture.Linear);self.texture.setWrapMode(QOpenGLTexture.ClampToEdge)
   else:
    rgba=atlas.convertToFormat(QImage.Format_RGBA8888);self.texture.setData(QOpenGLTexture.RGBA,QOpenGLTexture.UInt8,rgba.constBits())
   texture=self.texture
   if not texture.isCreated():raise RuntimeError('GPU blur texture unavailable')
   stages['upload_ms']=(time.perf_counter()-stamp)*1000;stamp=time.perf_counter()
   gl.glDisable(0x0BE2);gl.glDisable(0x0B71);self.program.bind();self.vertices.bind();self.program.enableAttributeArray(0);self.program.setAttributeBuffer(0,0x1406,0,2,8);gl.glUniform1i(self.program.uniformLocation(b'image'),0)
   for pass_index in (0,1):
    output=self.fbos[pass_index];output.bind();gl.glViewport(0,0,width,height);gl.glClearColor(0,0,0,1);gl.glClear(0x4000)
    tex=texture.textureId() if pass_index==0 else self.fbos[0].texture();gl.glActiveTexture(0x84C0);gl.glBindTexture(0x0DE1,tex);gl.glTexParameteri(0x0DE1,0x2801,0x2601);gl.glTexParameteri(0x0DE1,0x2800,0x2601)
    gl.glUniform2f(self.program.uniformLocation(b'stepSize'),1/width if pass_index==0 else 0,0 if pass_index==0 else 1/height)
    for _,_,r,sigma,_ in jobs:
     gl.glViewport(r.x(),r.y(),r.width(),r.height());gl.glUniform1f(self.program.uniformLocation(b'sigma'),sigma);gl.glUniform4f(self.program.uniformLocation(b'sampleRect'),r.x()/width,r.y()/height,r.width()/width,r.height()/height);gl.glUniform4f(self.program.uniformLocation(b'limits'),(r.x()+.5)/width,(r.y()+.5)/height,(r.right()+.5)/width,(r.bottom()+.5)/height);gl.glDrawArrays(0x0005,0,4)
   if not download:
    self.program.disableAttributeArray(0);self.vertices.release();self.program.release();self.fbos[1].release()
    error=gl.glGetError()
    if error:raise RuntimeError(f'GPU composition blur failed (OpenGL error {error})')
    return self.fbos[1].texture(),size,{(r.x(),r.y(),r.width(),r.height(),int(rad)):tile for r,rad,tile,_,_ in jobs}
   stages['draw_submit_ms']=(time.perf_counter()-stamp)*1000;stamp=time.perf_counter();image=self.fbos[1].toImage(False);stages['readback_ms']=(time.perf_counter()-stamp)*1000;self.program.disableAttributeArray(0);self.vertices.release();self.program.release();self.fbos[1].release()
   error=gl.glGetError()
   if error:raise RuntimeError(f'GPU blur operation failed (OpenGL error {error})')
   if image.isNull():raise RuntimeError('Could not read the GPU blur atlas')
   return {(r.x(),r.y(),r.width(),r.height(),int(radius)):image.copy(tile_rect).scaled(r.size(),Qt.IgnoreAspectRatio,Qt.SmoothTransformation) for r,radius,tile_rect,_,_ in jobs}
  finally:
   stages['total_ms']=(time.perf_counter()-started)*1000;self.timings.append(stages)
   if len(self.timings)>2000:self.timings.pop(0)
   self.context.doneCurrent()
   if previous and previous_surface:previous.makeCurrent(previous_surface)
 def close(self):
  if self.closed:return
  self.closed=True;self._reusable_atlas=None
  previous=QOpenGLContext.currentContext();previous_surface=previous.surface() if previous else None
  if self.context.makeCurrent(self.surface):
   if self.texture is not None:self.texture.destroy();self.texture=None
   self.fbos=None
   if self.vertices is not None:self.vertices.destroy()
   if self.program is not None:self.program.removeAllShaders();self.program=None
   self.context.doneCurrent()
  self.surface.destroy()
  if previous and previous_surface:previous.makeCurrent(previous_surface)

class GpuBlurBackend:
 """Predict stable crop geometry; every missing/unsupported sample uses CPU blur."""
 def __init__(self):
  self.engine=GpuBlur();self.source_key=None;self.requests={};self.results={}
 def crop(self,source,rect,radius,primary,tree_size=None,tree_radius=0):
  if radius<=0 or primary is None:return None
  is_primary=source.cacheKey()==primary.cacheKey()
  if not is_primary and not (tree_size is not None and source.size()==tree_size and int(radius)==int(tree_radius)):return None
  key=(rect.x(),rect.y(),rect.width(),rect.height(),int(radius))
  if primary.cacheKey()!=self.source_key:
   previous=list(self.requests.values());self.requests={};self.results={};self.source_key=primary.cacheKey()
   image=primary.toImage()
   if (previous or tree_size is not None) and not image.hasAlphaChannel():
    requests=[(r,rad) for r,rad in previous if primary.rect().contains(r)]
    if tree_size is not None and tree_radius>0:
     tree=image.copy(QRect(0,0,min(tree_size.width(),image.width()),image.height())).scaled(tree_size,Qt.IgnoreAspectRatio,Qt.SmoothTransformation)
     requests.append((tree.rect(),tree_radius,tree))
    self.results={k:QPixmap.fromImage(value) for k,value in self.engine.process(image,requests).items()}
  if is_primary and len(self.requests)<128:self.requests[key]=(QRect(rect),int(radius))
  return self.results.get(key)
 def close(self):self.engine.close()
