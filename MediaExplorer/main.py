import os, sys, json, subprocess, threading, hashlib, re, random, tempfile, base64, time, ctypes, copy, shutil, struct, logging, atexit, math
from logging.handlers import RotatingFileHandler
from ctypes import wintypes
from pathlib import Path

# Keep Qt's and shiboken's bundled runtimes ahead of the interpreter's DLLs.
# PyInstaller puts the interpreter runtimes at the bundle root, where they can
# shadow the versions paired with Qt and make QtCore fail to load.
_BUNDLED_DLL_DIR_HANDLES=[]
if sys.platform.startswith('win') and getattr(sys,'frozen',False):
    _qt_dll_dirs=(Path(sys._MEIPASS)/'PySide6',Path(sys._MEIPASS)/'shiboken6')
    _qt_dll_dirs=[_dll_dir for _dll_dir in _qt_dll_dirs if _dll_dir.is_dir()]
    if _qt_dll_dirs:
        os.environ['PATH']=os.pathsep.join([*(str(_dll_dir) for _dll_dir in _qt_dll_dirs),os.environ.get('PATH','')])
    for _dll_dir in _qt_dll_dirs:
        if _dll_dir.is_dir():
            _BUNDLED_DLL_DIR_HANDLES.append(os.add_dll_directory(str(_dll_dir)))

from datetime import datetime
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
try:
    import imageio_ffmpeg
except Exception:
    imageio_ffmpeg=None

from PySide6.QtCore import Qt, QSize, QSizeF, QRect, QRectF, QPoint, QPointF, Signal, QObject, QThread, QStandardPaths, QTimer, QDir, QEvent, QMimeData, QEventLoop, QLockFile, QAbstractAnimation, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QPixmap, QPainter, QPen, QColor, QFont, QFontMetricsF, QCursor, QImage, QImageReader, QIcon, QMovie, QDrag
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QAbstractScrollArea,
    QToolButton, QLabel, QFrame, QSlider, QMenu, QMenuBar, QFileDialog,
    QMessageBox, QInputDialog, QDialog, QDialogButtonBox, QListWidget,
    QTabBar, QTreeWidget, QTreeWidgetItem, QListWidgetItem, QDockWidget, QCheckBox, QSpinBox, QDoubleSpinBox, QComboBox, QScrollArea, QAbstractItemView,
    QWidgetAction, QGridLayout, QPushButton, QScroller, QScrollerProperties
)

# Large JPEGs can need a little headroom for decoder scratch buffers even when
# QImageReader is asked for a bounded scaled output. Keep this capped; the
# viewer scales very large sources before decoding them.
QImageReader.setAllocationLimit(512)

IMAGE_EXTS={'.jpg','.jpeg','.jfif','.png','.webp','.gif','.bmp','.tif','.tiff','.avif','.heic','.heif','.jxl'}
PDF_EXTS={'.pdf'}
PREVIEW_EXTS=IMAGE_EXTS|PDF_EXTS
VIDEO_EXTS={'.mp4','.mkv','.mov','.avi','.webm','.wmv','.m4v','.ts'}
MEDIA_EXTS=IMAGE_EXTS|VIDEO_EXTS
AUDIO_EXTS={'.mp3','.wav','.flac','.ogg','.m4a','.aac','.opus'}
DOC_EXTS={'.pdf','.txt','.md','.doc','.docx','.xls','.xlsx','.ppt','.pptx'}
TREE_PATH_ROLE=Qt.UserRole+1
TREE_POPULATED_ROLE=Qt.UserRole+2
TREE_POPULATING_ROLE=Qt.UserRole+3
TREE_CHILDREN_ROLE=Qt.UserRole+4
TREE_PAGE_ROLE=Qt.UserRole+5
TREE_PAGE_SIZE=400
TREE_SCAN_POOL=ThreadPoolExecutor(max_workers=2,thread_name_prefix='file-tree')
VIEWER_MAX_DECODE_PIXELS=48_000_000
FOLDER_SIZE_REFRESH_INTERVAL=300
THUMB_CACHE_CLEANUP_POOL=ThreadPoolExecutor(max_workers=1,thread_name_prefix='thumb-cache-cleanup')

logger=logging.getLogger('magellan')
logger.addHandler(logging.NullHandler())
_PENDING_SEARCH_BRIDGES=set()

def cleanup_search_bridge(path):
    bridge=Path(path)
    try:bridge.unlink(missing_ok=True)
    except OSError:logger.debug('Could not remove temporary image-search bridge %s',bridge,exc_info=True)
    _PENDING_SEARCH_BRIDGES.discard(str(bridge))

def cleanup_search_bridges_at_exit():
    for bridge in tuple(_PENDING_SEARCH_BRIDGES):cleanup_search_bridge(bridge)

atexit.register(cleanup_search_bridges_at_exit)

def log_unhandled_exception(exc_type,exc_value,traceback):
    logger.critical('Unhandled application exception',exc_info=(exc_type,exc_value,traceback))

class DuplicateMessageFilter(logging.Filter):
    """Coalesce identical repeated records while keeping log growth bounded."""
    def __init__(self,interval_seconds=5):
        super().__init__();self.interval_seconds=interval_seconds;self.messages={};self.lock=threading.Lock()
    def filter(self,record):
        with self.lock:
            now=time.monotonic();message=record.getMessage();key=(record.levelno,message)
            previous=self.messages.get(key)
            if previous and now-previous[0]<self.interval_seconds:
                self.messages[key]=(previous[0],previous[1]+1);return False
            if previous and previous[1]:
                record.msg=f'{message} [repeated {previous[1]} times]';record.args=()
            if len(self.messages)>2048:self.messages.clear()
            self.messages[key]=(now,0);return True

def configure_logging(directory, max_storage_mib=8):
    """Keep the active log and one rotated log within the configured storage cap."""
    try:
        directory.mkdir(parents=True,exist_ok=True)
        cap_bytes=max(1,int(max_storage_mib))*1024*1024
        handler_limit=max(256*1024,cap_bytes//2)
        log_path=directory/'magellan.log'
        for old_log in (log_path,Path(str(log_path)+'.1')):
            if old_log.exists() and old_log.stat().st_size>handler_limit:
                with old_log.open('rb') as stream:
                    stream.seek(-handler_limit,os.SEEK_END);tail=stream.read(handler_limit)
                newline=tail.find(b'\n');trimmed=tail[newline+1:] if newline>=0 else tail
                temporary=old_log.with_name(old_log.name+'.trim')
                temporary.write_bytes(trimmed);os.replace(temporary,old_log)
        for handler in list(logger.handlers):
            if isinstance(handler,RotatingFileHandler):
                logger.removeHandler(handler);handler.close()
        handler=RotatingFileHandler(log_path,maxBytes=handler_limit,backupCount=1,encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        handler.addFilter(DuplicateMessageFilter())
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        sys.excepthook=log_unhandled_exception
    except OSError:
        logger.exception('Could not initialize application logging')

def natural_key(value):
    """Natural ordering with punctuation-prefixed names before alphanumeric names."""
    text=str(value).casefold()
    lead=0 if text and not text[0].isalnum() else 1
    parts=re.split(r'(\d+)', text)
    converted=[]
    for part in parts:
        if part.isdigit(): converted.append((0,int(part)))
        else: converted.append((1,part))
    return (lead, tuple(converted))

def javascript_literal(value):
    """Serialize a value for an inline script without permitting a script break-out."""
    return (json.dumps(value,ensure_ascii=True)
            .replace('<',r'\u003c').replace('>',r'\u003e').replace('&',r'\u0026')
            .replace('\u2028',r'\u2028').replace('\u2029',r'\u2029'))


VIEWER_IMAGE_POOL=ThreadPoolExecutor(max_workers=3,thread_name_prefix='viewer-image')

def load_pdf_document(path,timeout_ms=10000):
    document=QPdfDocument()
    event_loop=QEventLoop()
    timeout=QTimer();timeout.setSingleShot(True);timeout.timeout.connect(event_loop.quit)
    document.statusChanged.connect(lambda status: event_loop.quit() if status!=QPdfDocument.Status.Loading else None)
    result=document.load(str(path))
    if result not in (QPdfDocument.Error.None_,QPdfDocument.Error.DataNotYetAvailable):return None
    if document.status()==QPdfDocument.Status.Loading:
        timeout.start(timeout_ms);event_loop.exec()
    if document.status()!=QPdfDocument.Status.Ready:return None
    return document

class ViewerImageLoadBridge(QObject):
    loaded=Signal(str,object)

class FolderThumbnailBridge(QObject):
    completed=Signal(str,object)

def decode_viewer_image(path,preview_size,max_dimension=8000,max_file_bytes=100*1024*1024,preload=False):
    suffix=Path(path).suffix.casefold()
    try:file_size=Path(path).stat().st_size
    except OSError:return QImage(),QImage(),False,QSize(),None
    cacheable=file_size<=max_file_bytes
    source_size=QSize()
    if suffix not in PDF_EXTS and suffix!='.jxl':
        try:
            probe=QImageReader(str(path));probe.setAutoTransform(True);probe.setDecideFormatFromContent(True);size=probe.size()
            if size.isValid():source_size=QSize(size)
            cacheable=cacheable and (not size.isValid() or max(size.width(),size.height())<=max_dimension)
        except Exception:logger.debug('Could not inspect viewer image dimensions',exc_info=True)
    if preload and not cacheable:return QImage(),QImage(),False,source_size,file_size
    if suffix in PDF_EXTS:
        document=load_pdf_document(path)
        if document is None or document.pageCount()<1:return QImage(),QImage(),False,source_size,file_size
        page=document.pagePointSize(0);cacheable=cacheable and max(page.width(),page.height())<=max_dimension
        if preload and not cacheable:return QImage(),QImage(),False,source_size,file_size
        limit=max(1,min(8000,max(preview_size.width(),preview_size.height())*2));ratio=max(.01,page.width()/max(.01,page.height()))
        if ratio>=1:size=QSize(limit,max(1,int(limit/ratio)))
        else:size=QSize(max(1,int(limit*ratio)),limit)
        image=document.render(0,size);source_size=image.size()
    elif suffix=='.jxl':
        try:
            from PIL import Image
            import pillow_jxl
            with Image.open(path) as source:
                cacheable=cacheable and max(source.size)<=max_dimension
                source_size=QSize(*source.size)
                if preload and not cacheable:return QImage(),QImage(),False,source_size,file_size
                source=source.convert('RGBA')
                image=QImage(source.tobytes(),source.width,source.height,source.width*4,QImage.Format_RGBA8888).copy()
        except Exception:
            logger.debug('JPEG XL viewer decode failed',exc_info=True)
            return QImage(),QImage(),False,source_size,file_size
    else:
        reader=QImageReader(str(path));reader.setAutoTransform(True);reader.setDecideFormatFromContent(True)
        if not source_size.isValid() and reader.size().isValid():source_size=QSize(reader.size())
        if source_size.isValid() and source_size.width()*source_size.height()>VIEWER_MAX_DECODE_PIXELS:
            factor=min(1.0,(VIEWER_MAX_DECODE_PIXELS/(source_size.width()*source_size.height()))**0.5,max_dimension/max(source_size.width(),source_size.height()))
            scaled_size=QSize(max(1,round(source_size.width()*factor)),max(1,round(source_size.height()*factor)))
            reader.setScaledSize(scaled_size)
        image=reader.read()
        if reader.error()==QImageReader.ImageReaderError.InvalidDataError:
            logger.warning('Could not decode viewer image %s: %s',path,reader.errorString())
            return QImage(),QImage(),False,source_size,file_size
    if image.isNull():return image,image,False,source_size,file_size
    cacheable=cacheable and max(image.width(),image.height())<=max_dimension
    preview=image if image.width()<=preview_size.width() and image.height()<=preview_size.height() else image.scaled(preview_size,Qt.KeepAspectRatio,Qt.SmoothTransformation)
    if not source_size.isValid():source_size=image.size()
    return image,preview,cacheable,source_size,file_size


QSS='''
*{font-family:"Segoe UI",Arial,sans-serif} QMainWindow,QWidget#root{background:#202124;color:#eee}
QMenuBar{background:#202124;color:#eee;border-bottom:1px solid #101114} QMenuBar::item{padding:6px 11px} QMenuBar::item:selected{background:#34363b}
QMenu{background:#25272b;color:#eee;border:1px solid #45484d} QMenu::item{padding:7px 24px 7px 10px} QMenu::item:selected{background:#393c41}
QFrame#top{background:#202124} QFrame#navigation{background:#202124} QFrame#line{background:#101114;max-height:1px}
QToolButton{color:#eee;background:#303236;border:1px solid #4b4e53;min-width:40px;min-height:38px;padding:0 8px;font-size:18px}
QToolButton:hover{background:#3a3d42;border-color:#60646b}
QToolButton:pressed{background:#292b2f}
QToolButton:checked{background:#51441d;border-color:#d0a32d}
QToolButton#sort,QToolButton#layout{min-width:40px;min-height:38px}
QToolButton#searchclear{min-width:30px;max-width:30px;min-height:38px;padding:0;font-size:21px}
QToolButton#gold{background:#303236;border-color:#4b4e53}
QLineEdit#search{background:#2a2c30;color:#ddd;border:1px solid #4c4f55;padding:6px 11px;min-height:27px}
QLabel#brand{font-size:27px;font-weight:700} QLabel#blue{color:#1597ff;font-size:27px;font-weight:700} QLabel#section{color:#aaa;font-size:12px;font-weight:600}
QLabel#crumb{color:#bbb;padding:5px} QLabel#status{color:#858990;font-size:12px} QSlider::groove:horizontal{height:4px;background:#45484c}
QSlider::handle:horizontal{width:15px;margin:-6px 0;background:#c9cbd0;border:1px solid #858990}
QScrollArea{border:0;background:#080a10} QScrollBar:vertical{background:#15171c;width:12px;margin:2px 2px 2px 0;border-radius:6px} QScrollBar::handle:vertical{background:#737982;min-height:42px;border-radius:6px} QScrollBar::handle:vertical:hover{background:#747a83} QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0}
QDockWidget#fileTreeDock{background:#15171c;color:#eee;border-right:1px solid #3b3e44} QDockWidget#fileTreeDock::title{background:#202124;color:#eee;padding:7px;font-weight:600} QTabBar::tab{background:#2b2d31;color:#aaa;border:1px solid #44474c;border-bottom:none;padding:5px 34px 5px 14px;min-width:170px;max-width:170px;margin-right:2px;border-top-left-radius:5px;border-top-right-radius:5px} QTabBar::tab:selected{background:#3a3c41;color:#fff;border-color:#65686d} QTabBar::tab:hover{background:#34373c}
'''

def kind(p):
    if p.is_dir(): return 'folder'
    e=p.suffix.lower()
    if e in IMAGE_EXTS:return 'image'
    if e in VIDEO_EXTS:return 'video'
    if e in AUDIO_EXTS:return 'audio'
    if e in DOC_EXTS:return 'document'
    return 'file'

def birth(p):
    try:
        s=p.stat()
        if hasattr(s,'st_birthtime'):return s.st_birthtime
        if sys.platform.startswith('win'):return s.st_ctime
        return s.st_ctime
    except (OSError,ValueError,OverflowError):return 0

def datefmt(t):
    try:return datetime.fromtimestamp(t).strftime('%d/%m/%Y %H:%M')
    except (OSError,ValueError,OverflowError):return '—'

def sizefmt(n):
    if n<1024:return f'{n} B'
    for u in ('KB','MB','GB','TB'):
        n/=1024
        if n<1024:return f'{n:.1f} {u}'
    return f'{n:.1f} PB'

class ScanSignals(QObject): done=Signal(list,str,int)
class ScanTask(QThread):
    def __init__(self,folder,recursive=False,generation=0,selection=None,manual_orders=None,manual_sort_settings=None):super().__init__();self.folder=Path(folder);self.recursive=recursive;self.generation=generation;self.selection=list(selection or []);self.manual_orders=manual_orders or {};self.manual_sort_settings=manual_sort_settings or {};self.signals=ScanSignals()
    def order_names(self,parent,names):
        try:
            parent_key=os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(parent))))
            if self.manual_sort_settings.get(parent_key,{}).get('sort')!='manual':return sorted(names,key=natural_key)
            order=self.manual_orders.get('folder:'+parent_key,[]);ranks={path:rank for rank,path in enumerate(order)}
            return sorted(names,key=lambda name:(ranks.get(os.path.normcase(os.path.normpath(os.path.abspath(os.path.join(os.fspath(parent),name)))),len(ranks)),natural_key(name)))
        except (OSError,TypeError,ValueError):return sorted(names,key=natural_key)
    def run(self):
        out=[]
        try:
            if self.recursive:
                roots=[Path(p) for p in self.selection] if self.selection else [self.folder]
                seen=set();seen_dirs=set()
                def add_media(path):
                    if path.suffix.casefold() not in PREVIEW_EXTS:return
                    path_key=os.path.normcase(os.path.abspath(str(path)))
                    if path_key in seen:return
                    seen.add(path_key)
                    try:
                        st=path.stat(follow_symlinks=False)
                        out.append({'path':path,'name':path.name,'is_dir':False,'kind':kind(path),'mtime':st.st_mtime,'ctime':birth(path),'size':st.st_size,'parent':str(path.parent)})
                    except OSError:pass
                for selected_root in roots:
                    if self.isInterruptionRequested():return
                    if selected_root.is_file():
                        add_media(selected_root);continue
                    if not selected_root.is_dir():continue
                    pending=[selected_root]
                    while pending:
                        if self.isInterruptionRequested():return
                        folder=pending.pop()
                        folder_key=os.path.normcase(os.path.normpath(os.path.abspath(str(folder))))
                        if folder_key in seen_dirs:continue
                        seen_dirs.add(folder_key)
                        try:
                            with os.scandir(folder) as entries:
                                directories=[];files=[]
                                for entry in entries:
                                    if self.isInterruptionRequested():return
                                    if entry.is_dir(follow_symlinks=False):
                                        if not entry.name.startswith('.') or entry.name.startswith('. '):directories.append(entry.name)
                                    elif entry.is_file(follow_symlinks=False):files.append(entry.name)
                        except OSError as error:
                            logger.warning('Farview could not read %s: %s',folder,error);continue
                        if self.manual_sort_settings.get(folder_key,{}).get('sort')=='manual':
                            entries=self.order_names(folder,[*directories,*files])
                            for name in reversed(entries):
                                path=folder/name
                                if name in directories:pending.append(path)
                                else:
                                    if path.suffix.casefold() in PREVIEW_EXTS:
                                        # Stack file markers preserve mixed folder/file order.
                                        pending.append(('file',path))
                        else:
                            for name in self.order_names(folder,files):add_media(folder/name)
                            for name in reversed(self.order_names(folder,directories)):pending.append(folder/name)
                        while pending and isinstance(pending[-1],tuple):
                            _,file_path=pending.pop();add_media(file_path)
                self.signals.done.emit(out,'',self.generation);return
            with os.scandir(self.folder) as it:
                for e in it:
                    try:
                        # Torrent clients can leave a companion .parts file beside the
                        # real media. It is an implementation detail, so keep it out
                        # of the explorer entirely.
                        if not e.is_dir(follow_symlinks=False) and e.name.casefold().endswith('.parts'):
                            continue
                        if self.isInterruptionRequested(): return
                        p=Path(e.path); st=e.stat(follow_symlinks=False); d=e.is_dir(follow_symlinks=False)
                        out.append({'path':p,'name':e.name,'is_dir':d,'kind':'folder' if d else kind(p),'mtime':st.st_mtime,'ctime':birth(p),'size':None if d else st.st_size})
                    except:pass
            self.signals.done.emit(out,'',self.generation)
        except Exception as ex:
            logger.exception('Directory scan failed for %s',self.folder)
            self.signals.done.emit([],str(ex),self.generation)

class RecursiveSearchSignals(QObject): done=Signal(list,str,int,bool,str)
class RecursiveSearchTask(QThread):
    def __init__(self,folder,query,generation):
        super().__init__();self.folder=Path(folder);self.query=query.casefold();self.generation=generation;self.signals=RecursiveSearchSignals();self.result_limit=10000
    def run(self):
        out=[];limited=False
        try:
            def walk_error(error):logger.warning('Recursive search could not read %s: %s',getattr(error,'filename',self.folder),error)
            for root,dirs,files in os.walk(self.folder,followlinks=False,onerror=walk_error):
                if self.isInterruptionRequested():return
                dirs[:]=[d for d in dirs if (not d.startswith('.') or d.startswith('. ')) and not (Path(root)/d).is_symlink()]
                for names,is_dir in ((dirs,True),(files,False)):
                    for name in names:
                        if self.isInterruptionRequested():return
                        if name.casefold().endswith('.parts') or self.query not in name.casefold():continue
                        path=Path(root)/name
                        try:
                            st=path.stat(follow_symlinks=False)
                            out.append({'path':path,'name':name,'is_dir':is_dir,'kind':'folder' if is_dir else kind(path),'mtime':st.st_mtime,'ctime':st.st_ctime,'size':None if is_dir else st.st_size,'parent':str(path.parent)})
                        except OSError:continue
                        if len(out)>=self.result_limit:limited=True;break
                    if limited:break
                if limited:break
            self.signals.done.emit(out,'',self.generation,limited,str(self.folder))
        except Exception as error:
            logger.exception('Recursive search failed for %s',self.folder)
            self.signals.done.emit([],str(error),self.generation,False,str(self.folder))

class TreeScanBridge(QObject): completed=Signal(object,str,list,int,str)

class MarqueeLabel(QLabel):
    def __init__(self,parent=None):
        super().__init__(parent);self._offset=0;self._pause_until=0.0;self._marquee_timer=QTimer(self);self._marquee_timer.setInterval(35);self._marquee_timer.timeout.connect(self.advance_marquee);self.setMinimumWidth(90)
    def setText(self,text):
        super().setText(text);self._offset=0;self._pause_until=0.0;self._marquee_timer.stop()
        if QFontMetricsF(self.font()).horizontalAdvance(str(text))>self.width():self._marquee_timer.start()
        self.update()
    def resizeEvent(self,event):
        super().resizeEvent(event)
        if QFontMetricsF(self.font()).horizontalAdvance(self.text())>self.width():self._marquee_timer.start()
        else:self._marquee_timer.stop();self._offset=0
    def advance_marquee(self):
        width=QFontMetricsF(self.font()).horizontalAdvance(self.text());limit=max(0,width-self.width()+18)
        if not limit:self._marquee_timer.stop();self._offset=0;self.update();return
        if time.monotonic()<self._pause_until:return
        self._offset+=1.1
        if self._offset>=limit:self._offset=0;self._pause_until=time.monotonic()+.18
        self.update()
    def paintEvent(self,event):
        p=QPainter(self);p.setClipRect(self.rect());p.setPen(self.palette().color(self.foregroundRole()));text_y=(self.height()+QFontMetricsF(self.font()).height())/2-QFontMetricsF(self.font()).descent();p.drawText(QPointF(-self._offset,text_y),self.text())

class ActivityOverlay(QWidget):
    """Compact selection and long-operation feedback, painted without child churn."""
    def __init__(self,parent):
        super().__init__(parent);self.selection_text='';self.operation_text='';self.spinner_phase=0
        self.setAttribute(Qt.WA_TransparentForMouseEvents,True);self.setAttribute(Qt.WA_TranslucentBackground,True)
        self._timer=QTimer(self);self._timer.setInterval(80);self._timer.timeout.connect(self._animate)
        self.hide()
    def _animate(self):
        self.spinner_phase=(self.spinner_phase+1)%12;self.update()
    def set_selection(self,text):
        self.selection_text=text or '';self._sync()
    def set_operation(self,text):
        self.operation_text=text or ''
        if self.operation_text:self._timer.start()
        else:self._timer.stop()
        self._sync()
    def _sync(self):
        visible=bool(self.selection_text or self.operation_text)
        self.setVisible(visible)
        if visible:self.adjustSize();self.update()
    def sizeHint(self):
        fm=QFontMetricsF(self.font());lines=[t for t in (self.operation_text,self.selection_text) if t]
        width=max((fm.horizontalAdvance(t) for t in lines),default=0)+52
        return QSize(max(210,min(460,int(width))),max(42,16+len(lines)*22))
    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing);p.setPen(QPen(QColor(255,255,255,45),1));p.setBrush(QColor(12,14,19,205));p.drawRoundedRect(self.rect().adjusted(0,0,-1,-1),8,8)
        y=18
        if self.operation_text:
            center=QPointF(17,y-5);p.setPen(QPen(QColor('#d0a32d'),2))
            for i in range(8):
                alpha=45+int(210*((i-self.spinner_phase)%8)/7);p.setPen(QPen(QColor(235,190,65,alpha),2));angle=i*math.pi/4
                p.drawLine(QPointF(center.x()+math.cos(angle)*4,center.y()+math.sin(angle)*4),QPointF(center.x()+math.cos(angle)*8,center.y()+math.sin(angle)*8))
            p.setPen(QColor('#eeeeee'));p.drawText(32,y,self.operation_text);y+=22
        if self.selection_text:
            p.setPen(QColor('#eeeeee'));p.drawText(12,y,self.selection_text)
        p.end()

class VirtualGrid(QAbstractScrollArea):
    openItem=Signal(str); middleOpenItem=Signal(str); goBack=Signal(); setThumb=Signal(str); context=Signal(str); imageSearchRequested=Signal(str); toggleItemFavorite=Signal(str)
    def __init__(self,parent=None):
        super().__init__(parent);self._explorer_window=parent.window() if parent is not None else None;self._closing=False;self.items=[];self.items_by_path={};self._thumb_cache_roots_cache=None;self._thumb_cache_roots_generation=None;self.thumb=230;self.columns=5;self.mode='vertical';self.cache=OrderedDict();self.cache_bytes=0;self.cache_limit_bytes=256*1024*1024;self._visible_cache_keys=None;self.thumb_futures={};self.folder_cache={};self.folder_custom={};self.folder_futures={};self.folder_last_visible={};self.pool=ThreadPoolExecutor(max_workers=4);self.folder_pool=ThreadPoolExecutor(max_workers=2);self.size_pool=ThreadPoolExecutor(max_workers=1);self.hover_size_pool=ThreadPoolExecutor(max_workers=1);self.folder_size_cache={};self.folder_size_cache_checked_at={};self.folder_size_futures={};self.hover_folder_size_futures={};self.folder_size_events={};self.hover_folder_size_events={};self.folder_size_queue=deque();self.folder_size_queued=set();self.hover_favorite_rect=QRect();self.hover_overlay_rect=QRect();self.hover_tooltip_rect=QRect();self.persistent_folder_cache_dir=None;self.last_scroll_value=0;self.last_scroll_time=0;self.fast_scroll=False;self.search_hover=-1;self.hover_index=-1;self.hover_animation_started=0;self.drag_source_index=-1;self.drag_started=False;self.drag_start=QPoint();self.drop_indicator=None;self.last_favorite_drag=0.0;self.pending_favorite_item=-1;self.suppress_favorite_release=False;self.pending_group_rename_id=None;self.favorite_picker_delay=QTimer(self);self.favorite_picker_delay.setSingleShot(True);self.favorite_picker_delay.setInterval(120);self.favorite_picker_delay.timeout.connect(self.open_hover_favorite_picker);self.favorite_picker_key=None;self.favorite_picker_hover_key=None;self.favorite_click_timer=QTimer(self);self.favorite_click_timer.setSingleShot(True);self.favorite_click_timer.setInterval(220);self.favorite_click_timer.timeout.connect(self.dispatch_favorite_open);self.group_rename_timer=QTimer(self);self.group_rename_timer.setSingleShot(True);self.group_rename_timer.timeout.connect(self.dispatch_group_rename);self.setAcceptDrops(True);self.viewport().setAcceptDrops(True);self.setFrameShape(QFrame.NoFrame);self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);self.viewport().setMouseTracking(True);self.viewport().installEventFilter(self);self.image_search_active=False
        icon_dir=getattr(parent,'icon_dir',None);image_icon=Path(icon_dir)/'image.png' if icon_dir else None;self.image_name_icon=QPixmap(str(image_icon)) if image_icon and image_icon.is_file() else QPixmap()
        # Reuse immutable drawing resources; rebuilding QFont/QPen objects for
        # every card on every scroll repaint adds avoidable GUI-thread work.
        self.paint_fonts={
            'label':QFont('Segoe UI',11),'horizontal_label':QFont('Segoe UI',12),
            'horizontal_detail':QFont('Segoe UI',10),'center':QFont('Segoe UI',48),
            'horizontal_center':QFont('Segoe UI',32),'group':QFont('Segoe UI Symbol',11,QFont.Bold),
        }
        self.paint_metadata={}
        self.card_cache=OrderedDict();self.card_cache_bytes=0;self.card_cache_limit_bytes=64*1024*1024
        self.selected_paths=set();self.selected_metadata={};self.selection_undo=[];self.selection_dragging=False;self.selection_drag_moved=False;self.selection_anchor=None;self.selection_current=None;self.selection_base=set();self.last_mouse_pos=QPoint();self.navigation_index=-1;self.navigation_key=None;self.navigation_started=0.0
        self.selection_scroll_accumulator=0.0;self.selection_scroll_timer=QTimer(self);self.selection_scroll_timer.setInterval(16);self.selection_scroll_timer.timeout.connect(self.auto_scroll_selection)
        self.wheel_scroller=QScroller.scroller(self);wheel_properties=self.wheel_scroller.scrollerProperties();wheel_properties.setScrollMetric(QScrollerProperties.FrameRate,QScrollerProperties.Standard);wheel_properties.setScrollMetric(QScrollerProperties.VerticalOvershootPolicy,QScrollerProperties.OvershootAlwaysOff);self.wheel_scroller.setScrollerProperties(wheel_properties)
        self.smooth_scroll_animation=QPropertyAnimation(self.verticalScrollBar(),b'value',self);self.smooth_scroll_target=0
        scroll_curve=QEasingCurve(QEasingCurve.BezierSpline);scroll_curve.addCubicBezierSegment(QPointF(.15,.35),QPointF(.65,.98),QPointF(1,1));self.smooth_scroll_animation.setEasingCurve(scroll_curve)
        self._scroll_diagnostic=None
        self.thumb_timer=QTimer(self);self.thumb_timer.timeout.connect(self.poll_async);self.thumb_timer.start(60)
        self.scroll_settle_timer=QTimer(self);self.scroll_settle_timer.setSingleShot(True);self.scroll_settle_timer.timeout.connect(self.scroll_settled)
        self.hover_delay_timer=QTimer(self);self.hover_delay_timer.setSingleShot(True);self.hover_delay_timer.timeout.connect(self.begin_hover_animation)
        self.hover_animation_timer=QTimer(self);self.hover_animation_timer.setInterval(16);self.hover_animation_timer.timeout.connect(self.advance_hover_animation)
        self.setFocusPolicy(Qt.StrongFocus);self.viewport().setAttribute(Qt.WA_OpaquePaintEvent,True);self.verticalScrollBar().valueChanged.connect(self.on_scroll)
    def explorer(self):
        """Return the top-level Explorer window, not the grid host widget."""
        return self._explorer_window or self.window()

    def setItems(self,items,reset_scroll=True):
        if hasattr(self,'wheel_scroller'):self.wheel_scroller.stop()
        if hasattr(self,'smooth_scroll_animation'):
            self.smooth_scroll_animation.stop();self.smooth_scroll_target=self.verticalScrollBar().value()
        self.set_hover_index(-1)
        self.navigation_index=-1
        # Navigating to a new view should never leave old folder-discovery jobs ahead of the new viewport.
        for f in list(self.folder_futures.values()):
            try: f.cancel()
            except: pass
        self.folder_futures.clear()
        self.clear_card_cache();self.items=items;items_by_path={};paint_metadata={}
        for item in items:
            path=item.get('path')
            if path:items_by_path.setdefault(str(path),[]).append(item)
            item_kind=item.get('kind','file');suffix=Path(str(path or '')).suffix.casefold()
            if item.get('is_dir') or item_kind=='favorite_group':prefix=''
            elif item_kind=='video' or suffix=='.gif':prefix='▶  '
            elif item_kind=='image':prefix='' if not self.image_name_icon.isNull() else '▧  '
            elif item_kind=='audio':prefix='♫  '
            else:prefix='▤  '
            paint_metadata[id(item)]=(suffix,prefix,item_kind=='image' and suffix!='.gif' and not self.image_name_icon.isNull())
        self.items_by_path=items_by_path;self.paint_metadata=paint_metadata;self.recalc()
        # A new folder/view always begins at the top; the Explorer restores a
        # saved position explicitly after the scan has finished.
        if reset_scroll:self.verticalScrollBar().setValue(0)
    def clear_card_cache(self):
        self.card_cache.clear();self.card_cache_bytes=0
    def trim_card_cache(self):
        while self.card_cache and self.card_cache_bytes>self.card_cache_limit_bytes:
            _,oldest=self.card_cache.popitem(last=False);self.card_cache_bytes=max(0,self.card_cache_bytes-self.pixmap_bytes(oldest))
    def setFolderThumbs(self,custom):self.folder_custom=custom or {};self.clear_card_cache();self.viewport().update()
    def set_hover_settings(self,enabled,delay):
        self.hover_delay_timer.stop();self.hover_animation_timer.stop();self.hover_index=-1;self.hover_animation_started=0
        self.explorer().hover_details_enabled=bool(enabled);self.explorer().hover_details_delay=max(0,int(delay));self.viewport().update()
    def refresh_hover_from_cursor(self):
        pos=self.viewport().mapFromGlobal(QCursor.pos())
        self.set_hover_index(self.thumbnail_hit(pos) if self.viewport().rect().contains(pos) else -1)
    def folder_size_needs_refresh(self,key):
        if key not in self.folder_size_cache:return True
        parent=self.explorer();persistent=parent is not None and key in getattr(parent,'pinned_folder_sizes',{})
        return persistent and time.time()-self.folder_size_cache_checked_at.get(key,0)>=FOLDER_SIZE_REFRESH_INTERVAL
    def apply_cached_folder_sizes(self,items):
        for item in items:
            if not item.get('is_dir'):continue
            key=str(item['path'])
            if key in self.folder_size_cache:item['size']=self.folder_size_cache[key]
    def schedule_folder_sizes(self,items,priority_keys=()):
        if self._closing:return
        priority_keys=set(priority_keys);priority=[];remaining=[];queued=set()
        for item in items:
            if not item.get('is_dir'):continue
            key=str(item['path'])
            if key in self.folder_size_cache:item['size']=self.folder_size_cache[key]
            if not self.folder_size_needs_refresh(key) or key in self.folder_size_futures or key in self.hover_folder_size_futures:continue
            if key in queued:continue
            queued.add(key)
            (priority if key in priority_keys else remaining).append(key)
        self.folder_size_queue=deque(priority+remaining);self.folder_size_queued=queued
        self.start_next_folder_size()
    def start_next_folder_size(self):
        if self._closing or self.folder_size_futures:return
        while self.folder_size_queue:
            key=self.folder_size_queue.popleft()
            if key not in self.folder_size_queued:continue
            self.folder_size_queued.discard(key)
            if key in self.folder_size_futures or key in self.hover_folder_size_futures or not self.folder_size_needs_refresh(key):continue
            self._submit_folder_size(self.folder_size_futures,self.folder_size_events,self.size_pool,key)
            return
    def _submit_folder_size(self,futures,events,executor,key):
        cancel_event=threading.Event();future=executor.submit(self.folder_size_worker,key,cancel_event)
        futures[key]=future;events[key]=cancel_event
    def clear_folder_size_queue(self):
        self.folder_size_queue.clear();self.folder_size_queued.clear()
        for key,future in list(self.folder_size_futures.items()):
            event=self.folder_size_events.get(key)
            if event:event.set()
            if not future.running() and future.cancel():self.folder_size_futures.pop(key,None);self.folder_size_events.pop(key,None)
    def request_folder_size(self,item,priority=False):
        if self._closing:return
        key=str(item['path'])
        if key in self.folder_size_cache:
            item['size']=self.folder_size_cache[key]
        if not self.folder_size_needs_refresh(key):return
        if key in self.hover_folder_size_futures:return
        if priority:
            self.folder_size_queued.discard(key)
            # A hovered item should not wait behind the size-sort backlog.
            queued=self.folder_size_futures.get(key)
            if queued is not None:
                event=self.folder_size_events.get(key)
                if event:event.set()
                if queued.cancel():self.folder_size_futures.pop(key,None);self.folder_size_events.pop(key,None)
            self._submit_folder_size(self.hover_folder_size_futures,self.hover_folder_size_events,self.hover_size_pool,key)
        elif key not in self.folder_size_futures and key not in self.folder_size_queued:
            self.folder_size_queue.append(key);self.folder_size_queued.add(key);self.start_next_folder_size()
    def folder_size_worker(self,path,cancel_event=None):
        total=0
        pending=[Path(path)]
        while pending and not self._closing and not (cancel_event and cancel_event.is_set()):
            current=pending.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        if self._closing or (cancel_event and cancel_event.is_set()):return total
                        try:
                            if entry.is_dir(follow_symlinks=False):pending.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):total+=entry.stat(follow_symlinks=False).st_size
                        except OSError:continue
            except OSError:continue
        return total
    def shutdown_workers(self):
        if self._closing:return
        self._closing=True
        for timer in (self.thumb_timer,self.scroll_settle_timer,self.hover_delay_timer,self.hover_animation_timer,self.favorite_picker_delay,self.favorite_click_timer,self.group_rename_timer,self.selection_scroll_timer):timer.stop()
        self.wheel_scroller.stop();self.smooth_scroll_animation.stop()
        for events in (self.folder_size_events,self.hover_folder_size_events):
            for event in events.values():event.set()
            events.clear()
        for futures in (self.thumb_futures,self.folder_futures,self.folder_size_futures,self.hover_folder_size_futures):
            for future in futures.values():future.cancel()
            futures.clear()
        self.folder_size_queue.clear();self.folder_size_queued.clear()
        for executor in (self.pool,self.folder_pool,self.size_pool,self.hover_size_pool):executor.shutdown(wait=False,cancel_futures=True)
    def setThumbSize(self,n):self.thumb=int(n);self.recalc()
    def setColumns(self,n):self.columns=max(1,int(n));self.mode='vertical';self.recalc()
    def setMode(self,m):self.mode=m;self.recalc()
    def recalc(self):
        if self.mode=='horizontal':
            self.row_h=max(92,int(self.thumb*0.52)); rows=len(self.items); h=rows*self.row_h
        else:
            gap=9; width=max(1,self.viewport().width()-18)
            # The slider is a target size. Columns adapt automatically so the grid
            # always fits the window and never needs horizontal scrolling.
            self.columns=max(1,int((width+gap)//(self.thumb+gap)))
            self.cell_w=max(1,int((width-gap*(self.columns-1))/self.columns))
            self.row_h=self.cell_w+58; rows=(len(self.items)+self.columns-1)//self.columns; h=rows*self.row_h
        self.verticalScrollBar().setRange(0,max(0,h-self.viewport().height()));self.verticalScrollBar().setPageStep(self.viewport().height());self.viewport().update()
    def resizeEvent(self,e):self.recalc();super().resizeEvent(e)
    def scrollContentsBy(self,dx,dy):
        # Move the existing viewport and let Qt repaint only the newly exposed
        # strip. Repainting all cards for each animation frame increased paint
        # cost without removing the reported tearing.
        started=time.perf_counter() if self._scroll_diagnostic is not None else None
        self.viewport().scroll(dx,dy)
        if started is not None:self._scroll_diagnostic['shift_ms'].append((time.perf_counter()-started)*1000)
    def wheelEvent(self,e):
        delta=e.angleDelta().y()
        if delta:
            distance=-(delta/120.0)*self.row_h*max(.1,min(12.0,float(getattr(self.explorer(),'wheel_scroll_rows',1))))
        else:
            distance=-e.pixelDelta().y()*max(.1/3.0,min(4.0,float(getattr(self.explorer(),'wheel_scroll_rows',1))/3.0))
        if distance:
            self.begin_scroll_diagnostic()
            if self._scroll_diagnostic is not None:self._scroll_diagnostic['wheel_events']+=1
            if getattr(self.explorer(),'smooth_wheel_scroll',False):
                bar=self.verticalScrollBar();animation=self.smooth_scroll_animation
                animation_active=animation.state()==QAbstractAnimation.Running
                base=self.smooth_scroll_target if animation_active else bar.value()
                target=max(0,min(bar.maximum(),base+distance))
                if target!=base:
                    animation.stop();animation.setDuration(max(380,min(1050,int(380+abs(target-bar.value())*.28))))
                    animation.setStartValue(bar.value());animation.setEndValue(round(target));self.smooth_scroll_target=target;animation.start()
            else:
                self.stop_smooth_scroll()
                bar=self.verticalScrollBar();bar.setValue(bar.value()+round(distance));self.smooth_scroll_target=bar.value()
        self.setFocus();e.accept()
    def event(self,e):
        if e.type()==QEvent.ScrollPrepare:
            e.setViewportSize(QSizeF(self.viewport().size()))
            e.setContentPosRange(QRectF(0,0,0,self.verticalScrollBar().maximum()))
            e.setContentPos(QPointF(0,self.verticalScrollBar().value()));e.accept();return True
        if e.type()==QEvent.Scroll:
            diag=self._scroll_diagnostic
            if diag is not None:
                stamp=time.perf_counter()
                diag['qscroll_events']+=1
                if diag['last_qscroll'] is not None:diag['qscroll_gaps_ms'].append((stamp-diag['last_qscroll'])*1000)
                diag['last_qscroll']=stamp
            position=e.contentPos().y();self.verticalScrollBar().setValue(max(0,min(self.verticalScrollBar().maximum(),round(position))))
            e.accept();return True
        return super().event(e)
    def stop_smooth_scroll(self):
        self.wheel_scroller.stop();self.smooth_scroll_animation.stop();self.smooth_scroll_target=self.verticalScrollBar().value()
    def visible_range(self):
        top=self.verticalScrollBar().value(); bottom=top+self.viewport().height()
        if self.mode=='horizontal':return max(0,top//self.row_h-1),min(len(self.items),(bottom//self.row_h)+2)
        first=max(0,top//self.row_h*self.columns-self.columns); last=min(len(self.items),((bottom//self.row_h)+2)*self.columns);return first,last
    def paintEvent(self,e):
        diag=self._scroll_diagnostic
        paint_started=time.perf_counter() if diag is not None else None
        if diag is not None:
            if diag['last_paint'] is not None:diag['paint_gaps_ms'].append((paint_started-diag['last_paint'])*1000)
            diag['last_paint']=paint_started
        card_started=None;painted_cards=0
        self._visible_cache_keys=set() if not getattr(self.explorer(),'thumbnail_cache_enabled',True) else None
        p=QPainter(self.viewport());p.fillRect(self.viewport().rect(),QColor('#080a10'))
        if not self.items:
            area=self.viewport().rect();p.setPen(QColor('#d0a32d'));p.setFont(QFont('Segoe UI Symbol',54,QFont.Bold));p.drawText(QRect(0,area.height()//2-100,area.width(),90),Qt.AlignCenter,'◈')
            p.setPen(QColor('#e5e5e5'));p.setFont(QFont('Segoe UI',20,QFont.DemiBold));p.drawText(QRect(0,area.height()//2-8,area.width(),36),Qt.AlignCenter,'MAGELLAN')
            p.setPen(QColor('#92969d'));p.setFont(QFont('Segoe UI',11));p.drawText(QRect(0,area.height()//2+32,area.width(),32),Qt.AlignCenter,'Choose a folder to get started')
            p.end();self._trim_cache_after_paint()
            if diag is not None:self.record_scroll_paint(diag,e,paint_started,0,0.0)
            return
        first,last=self.paint_range(e)
        card_started=time.perf_counter() if diag is not None else None
        region=e.region();cache_enabled=getattr(self.explorer(),'thumbnail_cache_enabled',True)
        paint_indices=[]
        for i in range(first,last):
            card_rect=self.card_rect(i)
            # paint_range returns rows intersecting Qt's bounding rectangle.
            # QRegion may contain separated damaged strips; skip cards that do
            # not intersect the actual update region instead of redrawing the
            # empty space between those strips.
            if not cache_enabled or region.intersects(card_rect):paint_indices.append(i)
        painted_cards=len(paint_indices)
        if self.mode=='horizontal':
            for i in paint_indices:self.paint_horizontal(p,i)
        else:
            for i in paint_indices:self.paint_card(p,i,self.card_rect(i))
        if self.drop_indicator is not None:
            index,before=self.drop_indicator
            if first<=index<last:
                if self.mode=='horizontal':
                    r=QRect(10,index*self.row_h-self.verticalScrollBar().value(),self.viewport().width()-20,self.row_h-7);y=r.top()-4 if before else r.bottom()+4
                    p.fillRect(QRect(r.left()+2,y-1,max(1,r.width()-4),3),QColor('#ffffff'));r=QRect()
                else:
                    row,col=divmod(index,self.columns);r=QRect(9+col*(self.cell_w+9),row*self.row_h-self.verticalScrollBar().value(),self.cell_w,self.cell_w+48);x=r.left()-5 if before else r.right()+5
                    p.fillRect(QRect(x,r.top()+2,3,max(1,r.height()-4)),QColor('#ffffff'))
        if self.selection_dragging and self.selection_anchor is not None and self.selection_current is not None:
            rect=QRect(self.selection_anchor,self.selection_current).normalized();rect.translate(0,-self.verticalScrollBar().value());p.setPen(QPen(QColor(190,190,190,145),1));p.setBrush(QColor(170,170,170,55));p.drawRect(rect)
        for i in paint_indices:
            item=self.items[i];selected=self.selection_key(item) in self.selected_paths
            if not selected and i!=self.navigation_index:continue
            if self.mode=='horizontal':r=QRect(10,i*self.row_h-self.verticalScrollBar().value(),self.viewport().width()-20,self.row_h-7)
            else:
                row,col=divmod(i,self.columns);r=QRect(9+col*(self.cell_w+9),row*self.row_h-self.verticalScrollBar().value(),self.cell_w,self.cell_w+48)
            p.setPen(QPen(QColor('#ffffff'),3));p.setBrush(Qt.NoBrush);p.drawRect(r.adjusted(1,1,-2,-2))
        if self.hover_index in paint_indices and getattr(self.explorer(),'hover_details_enabled',True):
            i=self.hover_index
            if self.mode=='horizontal':
                y=i*self.row_h-self.verticalScrollBar().value();preview=QRect(14,y+4,max(1,self.row_h-15),max(1,self.row_h-15))
            else:
                row,col=divmod(i,self.columns);x=9+col*(self.cell_w+9);y=row*self.row_h-self.verticalScrollBar().value();preview=QRect(x+1,y+1,max(1,self.cell_w-2),max(1,self.cell_w-2))
            self.paint_hover_details(p,preview,self.items[i])
        p.end();self._trim_cache_after_paint()
        if diag is not None:self.record_scroll_paint(diag,e,paint_started,painted_cards,(time.perf_counter()-card_started)*1000 if card_started is not None else 0.0)

    def record_scroll_paint(self,diag,event,started,cards,card_ms):
        elapsed=(time.perf_counter()-started)*1000
        rect=event.region().boundingRect()
        area=max(0,rect.width())*max(0,rect.height())
        diag['paint_ms'].append(elapsed);diag['card_ms'].append(card_ms);diag['painted_cards']+=cards
        diag['damage_areas'].append(area)
        if elapsed>=40:
            diag['slow_paints'].append((elapsed,rect.width(),rect.height(),cards,len(self.thumb_futures),len(self.folder_futures)))
            diag['slow_paints']=sorted(diag['slow_paints'],key=lambda row:row[0],reverse=True)[:5]
    def card_rect(self,index):
        if self.mode=='horizontal':
            y=index*self.row_h-self.verticalScrollBar().value()
            return QRect(10,y,self.viewport().width()-20,self.row_h-7)
        row,col=divmod(index,self.columns);x=9+col*(self.cell_w+9);y=row*self.row_h-self.verticalScrollBar().value()
        return QRect(x,y,self.cell_w,self.cell_w+48)
    def paint_range(self,event):
        # The cache-disabled mode needs a complete visible pass so its RAM cache
        # can retain all displayed items. With caching enabled, paint only the
        # rows touched by the invalidated region; rendering extra rows here made
        # high-column folders exceed a frame while fast scrolling.
        if not getattr(self.explorer(),'thumbnail_cache_enabled',True):return self.visible_range()
        rect=event.rect();scroll=self.verticalScrollBar().value();top=max(0,rect.top()+scroll);bottom=max(top,rect.bottom()+1+scroll)
        if self.mode=='horizontal':
            first_row=max(0,top//self.row_h)
            last_row=min(len(self.items),(bottom+self.row_h-1)//self.row_h)
            return first_row,last_row
        first_row=max(0,top//self.row_h)
        last_row=min((len(self.items)+self.columns-1)//self.columns,(bottom+self.row_h-1)//self.row_h)
        return first_row*self.columns,min(len(self.items),last_row*self.columns)
    def _trim_cache_after_paint(self):
        keep=getattr(self,'_visible_cache_keys',None)
        if keep is None:return
        for key in list(self.cache):
            if key not in keep:
                pixmap=self.cache.pop(key);self.cache_bytes=max(0,self.cache_bytes-self.pixmap_bytes(pixmap))
        self._visible_cache_keys=None
    def folder_image(self, folder):
        # Breadth-first-ish: check direct children first, then descend. Stop at the first image.
        try:
            if self._closing:return None
            root=Path(folder)
            direct=[]
            for e in os.scandir(root):
                if self._closing:return None
                if e.is_file() and Path(e.name).suffix.lower() in (MEDIA_EXTS|PDF_EXTS): direct.append(Path(e.path))
            for candidate in sorted(direct,key=lambda p:natural_key(p.name)):
                if self._closing:return None
                if candidate.suffix.casefold() in VIDEO_EXTS or self.preview_is_readable(candidate):return str(candidate)
            q=[root]
            depth=0
            while q and depth<8:
                nq=[]
                for d in q:
                    if self._closing:return None
                    try:
                        for e in os.scandir(d):
                            if self._closing:return None
                            p=Path(e.path)
                            if e.is_file() and p.suffix.lower() in (MEDIA_EXTS|PDF_EXTS):
                                if p.suffix.casefold() in VIDEO_EXTS or self.preview_is_readable(p):return str(p)
                            if e.is_dir() and (not e.name.startswith('.') or e.name.startswith('. ')): nq.append(p)
                    except OSError:
                        logger.debug('Could not inspect folder preview directory %s',d,exc_info=True)
                q=nq; depth+=1
        except Exception:
            logger.debug('Could not resolve a folder preview in %s',folder,exc_info=True)
        return None
    def resolve_folder_thumbnail(self,folder):
        """Resolve and persist a pinned preview on a worker, never during paint/poll."""
        source=self.folder_image(folder)
        if source and self.is_persistently_kept(folder):
            return self.persist_folder_thumb(folder,source)
        return source
    def preview_is_readable(self,path):
        """Reject incomplete/corrupt previews and keep searching for a usable sibling."""
        try:
            return self.thumbnail_worker(str(path),64,64) is not None
        except Exception:
            return False
    def _thumb_cache_roots(self):
        parent=self.explorer()
        if parent is None:return set()
        generation=getattr(parent,'_pinned_folders_generation',0)
        if self._thumb_cache_roots_cache is None or generation!=self._thumb_cache_roots_generation:
            self._thumb_cache_roots_cache={os.path.normcase(os.path.abspath(os.path.normpath(os.fspath(x)))) for x in getattr(parent,'pinned_folders',set())}
            self._thumb_cache_roots_generation=generation
        return self._thumb_cache_roots_cache
    def is_persistently_kept(self, folder):
        try:
            p=os.path.normcase(os.path.abspath(os.path.normpath(os.fspath(folder))))
            roots=self._thumb_cache_roots()
            return p in roots or os.path.dirname(p) in roots
        except (OSError,TypeError,ValueError):
            logger.debug('Could not check pinned-cache membership for %s',folder,exc_info=True)
        return False

    def _persistent_folder_thumb_path(self, folder):
        if not self.persistent_folder_cache_dir: return None
        try:
            key=hashlib.sha1(os.path.normcase(str(Path(folder).resolve())).encode('utf-8')).hexdigest()
            return Path(self.persistent_folder_cache_dir)/(key+'.jpg')
        except Exception: return None
    def load_persistent_folder_thumb(self, folder):
        p=self._persistent_folder_thumb_path(folder)
        return str(p) if p and p.exists() else None
    def persist_folder_thumb(self, folder, source):
        p=self._persistent_folder_thumb_path(folder)
        if not p or not source: return source
        temporary=None
        try:
            p.parent.mkdir(parents=True,exist_ok=True)
            img=self.thumbnail_worker(str(source),512,512)
            if img is not None and not img.isNull():
                temporary=p.with_name(f'{p.stem}.{threading.get_ident()}.tmp')
                ok=img.save(str(temporary),'JPEG',92)
                if ok:os.replace(temporary,p)
                if ok and p.exists() and p.stat().st_size>0:
                    return str(p)
        except Exception:
            logger.warning('Could not persist folder thumbnail %s',p,exc_info=True)
        finally:
            if temporary is not None:
                try:temporary.unlink(missing_ok=True)
                except OSError:logger.debug('Could not clean temporary thumbnail %s',temporary,exc_info=True)
        return None
    def ensure_folder_thumb(self,path):
        if self._closing:return None
        key=str(path)
        custom=getattr(self,'folder_custom',{}).get(key)
        if custom and Path(custom).exists(): return custom
        # Folder preview sources are already memoized after first resolution.
        # Check that mapping before resolving pinned roots and stat'ing the SSD
        # cache file; doing those filesystem checks for every painted card made
        # warm scrolling perform repeated disk metadata work.
        cached=self.folder_cache.get(key)
        if cached:
            if self.is_persistently_kept(key) and cached==str(self._persistent_folder_thumb_path(key)) and not Path(cached).is_file():
                self.folder_cache.pop(key,None)
            else:return cached
        parent=self.explorer()
        if self.is_persistently_kept(key):
            persistent=self.load_persistent_folder_thumb(path)
            if persistent:
                self.folder_cache[key]=persistent; return persistent
            # A pinned folder without a local cache must be regenerated. Do not
            # fall through to a stale source path from older settings.
            self.folder_cache.pop(key,None)
        if key in self.folder_cache:return self.folder_cache[key]
        if key in self.folder_futures:return None
        fut=self.folder_pool.submit(self.resolve_folder_thumbnail,path);self.folder_futures[key]=fut;return None
    def poll_async(self):
        changed_indices=set();sizes_changed=False
        first,last=self.visible_range() if self.items and getattr(self,'row_h',0)>0 else (0,0);visible_paths={};visible_sources={}
        for i in range(first,min(last,len(self.items))):
            item=self.items[i];path=str(item.get('path',''))
            if path:visible_paths.setdefault(path,[]).append(i)
            source=None
            if item.get('kind') in ('image','video') or (item.get('kind')=='document' and Path(path).suffix.casefold() in PDF_EXTS):source=path
            elif item.get('kind')=='favorite_group':
                group_id=path.split('://',1)[-1];preview=item.get('preview_path')
                source=self.folder_custom.get('favgroup:'+group_id) or (self.folder_custom.get(str(preview)) if preview else None) or (self.folder_cache.get(str(preview)) if preview else None)
            elif item.get('is_dir'):source=self.folder_custom.get(path) or self.folder_cache.get(path)
            if source:visible_sources.setdefault(str(source),[]).append(i)
        for key,f in list(self.thumb_futures.items()):
            if f.done():
                self.thumb_futures.pop(key,None)
                try:
                    img=f.result()
                    if img is not None and not img.isNull():
                        pixmap=QPixmap.fromImage(img)
                        previous=self.cache.pop(key,None)
                        if previous:self.cache_bytes-=self.pixmap_bytes(previous)
                        self.cache[key]=pixmap;self.cache_bytes+=self.pixmap_bytes(pixmap)
                        while self.cache and self.cache_bytes>self.cache_limit_bytes:
                            _,oldest=self.cache.popitem(last=False);self.cache_bytes-=self.pixmap_bytes(oldest)
                except Exception:
                    logger.exception('Thumbnail worker failed for %s',key[0])
                changed_indices.update(visible_sources.get(str(key[0]),()))
        for key,f in list(self.folder_futures.items()):
            if f.done():
                self.folder_futures.pop(key,None)
                if f.cancelled():
                    continue
                try:
                    val=f.result()
                    parent=self.explorer()
                    if val and parent is not None and key in getattr(parent,'pinned_folders',set()):
                        parent.pinned_thumbs[key]=val;parent.save_settings();parent.schedule_disk_thumbnail_cache_trim()
                    self.folder_cache[key]=val
                except Exception:
                    logger.exception('Folder thumbnail worker failed for %s',key)
                    self.folder_cache[key]=None
                changed_indices.update(visible_paths.get(key,()))
        for futures in (self.folder_size_futures,self.hover_folder_size_futures):
            events=self.folder_size_events if futures is self.folder_size_futures else self.hover_folder_size_events
            for key,f in list(futures.items()):
                if not f.done():continue
                futures.pop(key,None);cancel_event=events.pop(key,None)
                if f.cancelled() or (cancel_event is not None and cancel_event.is_set()):continue
                try:
                    size=max(0,int(f.result()));self.folder_size_cache[key]=size;checked_at=time.time();self.folder_size_cache_checked_at[key]=checked_at
                    for item in self.items_by_path.get(key,()):
                        if item.get('is_dir'):item['size']=size
                    changed_indices.update(visible_paths.get(key,()))
                    # If the same path was also queued for sorting, discard that
                    # work now that the interactive result has populated cache.
                    other=self.folder_size_futures if futures is self.hover_folder_size_futures else self.hover_folder_size_futures
                    queued=other.pop(key,None)
                    if queued is not None:queued.cancel()
                    parent=self.explorer()
                    if parent is not None and self.is_persistently_kept(key):
                        parent.pinned_folder_sizes[key]=size;parent.pinned_folder_sizes_checked[key]=checked_at;parent.save_settings()
                except Exception:
                    logger.exception('Folder size calculation failed for %s',key)
                sizes_changed=True
        self.start_next_folder_size()
        if sizes_changed:
            parent=self.explorer()
            if parent is not None:parent.update_selection_button()
        for i in changed_indices:
            if self.mode=='horizontal':rect=QRect(10,i*self.row_h-self.verticalScrollBar().value(),self.viewport().width()-20,self.row_h-7)
            else:
                row,col=divmod(i,self.columns);rect=QRect(9+col*(self.cell_w+9),row*self.row_h-self.verticalScrollBar().value(),self.cell_w,self.cell_w+48)
            self.viewport().update(rect)
        parent=self.explorer()
        # Keep the visible item order stable while the controlled size queue is
        # filling. Re-sort once after its final result, not during each scroll.
        size_queue_drained=not self.folder_size_futures and not self.folder_size_queued
        if sizes_changed and size_queue_drained and parent is not None and getattr(parent,'sort',None)=='size':
            QTimer.singleShot(0,parent.refresh)

    def begin_scroll_diagnostic(self):
        if getattr(self.explorer(),'scroll_test_logging_enabled',False) and self._scroll_diagnostic is None:
            self._scroll_diagnostic={'mode':'smooth' if getattr(self.explorer(),'smooth_wheel_scroll',False) else 'normal','started':time.perf_counter(),'last_scroll':None,'last_qscroll':None,'last_paint':None,'scroll_gaps_ms':[],'qscroll_gaps_ms':[],'paint_gaps_ms':[],'paint_ms':[],'card_ms':[],'damage_areas':[],'slow_paints':[],'shift_ms':[],'scroll_steps':0,'qscroll_events':0,'wheel_events':0,'painted_cards':0,'cache_hits':0,'cache_misses':0,'card_cache_hits':0,'card_cache_misses':0}

    @staticmethod
    def diagnostic_stats(samples):
        if not samples:return (0.0,0.0,0.0)
        ordered=sorted(samples);p95=ordered[min(len(ordered)-1,int((len(ordered)-1)*.95))]
        return (sum(ordered)/len(ordered),p95,ordered[-1])

    def finish_scroll_diagnostic(self):
        diag=self._scroll_diagnostic
        if diag is None:return
        elapsed=(time.perf_counter()-diag['started'])*1000
        paint_avg,paint_p95,paint_max=self.diagnostic_stats(diag['paint_ms'])
        card_avg,card_p95,card_max=self.diagnostic_stats(diag['card_ms'])
        shift_avg,shift_p95,shift_max=self.diagnostic_stats(diag['shift_ms'])
        scroll_gap=self.diagnostic_stats(diag['scroll_gaps_ms'])
        qscroll_gap=self.diagnostic_stats(diag['qscroll_gaps_ms'])
        paint_gap=self.diagnostic_stats(diag['paint_gaps_ms'])
        damage_avg,damage_p95,damage_max=self.diagnostic_stats(diag['damage_areas'])
        slow=';'.join('%.1fms@%dx%d cards=%d thumbs=%d folders=%d'%row for row in diag['slow_paints']) or 'none'
        logger.info('Scroll diagnostic mode=%s elapsed_ms=%.0f wheel_events=%d scroll_updates=%d qscroll_events=%d paints=%d painted_cards=%d paint_ms_avg/p95/max=%.2f/%.2f/%.2f card_loop_ms_avg/p95/max=%.2f/%.2f/%.2f paint_gap_ms_avg/p95/max=%.2f/%.2f/%.2f scroll_update_gap_ms_avg/p95/max=%.2f/%.2f/%.2f qscroll_gap_ms_avg/p95/max=%.2f/%.2f/%.2f damage_area_px_avg/p95/max=%.0f/%.0f/%.0f shift_ms_avg/p95/max=%.3f/%.3f/%.3f thumb_cache_hits=%d misses=%d card_cache_hits=%d misses=%d slow_paints=[%s]',diag['mode'],elapsed,diag['wheel_events'],diag['scroll_steps'],diag['qscroll_events'],len(diag['paint_ms']),diag['painted_cards'],paint_avg,paint_p95,paint_max,card_avg,card_p95,card_max,*paint_gap,*scroll_gap,*qscroll_gap,damage_avg,damage_p95,damage_max,shift_avg,shift_p95,shift_max,diag['cache_hits'],diag['cache_misses'],diag['card_cache_hits'],diag['card_cache_misses'],slow)
        self._scroll_diagnostic=None

    def on_scroll(self,value):
        self.begin_scroll_diagnostic()
        diag=self._scroll_diagnostic
        if diag is not None:
            stamp=time.perf_counter();diag['scroll_steps']+=1
            if diag['last_scroll'] is not None:diag['scroll_gaps_ms'].append((stamp-diag['last_scroll'])*1000)
            diag['last_scroll']=stamp
        now=__import__('time').monotonic()
        dt=now-self.last_scroll_time if self.last_scroll_time else 1
        speed=abs(value-self.last_scroll_value)/max(dt,0.001)
        smooth_active=self.smooth_scroll_animation.state()==QAbstractAnimation.Running
        # QScroller advances in small increments, so velocity alone can miss
        # fast-scroll mode. Avoid starting thumbnail work while its animation
        # is moving; that competes with painting and causes frame hitches.
        self.fast_scroll=smooth_active or speed>900
        self.last_scroll_value=value;self.last_scroll_time=now
        self.scroll_settle_timer.start(220)
        if smooth_active:return
        self.update_scroll_work()

    def update_scroll_work(self):
        # Folder discovery is much more expensive than decoding an already-known image.
        # When the scrollbar jumps, cancel queued folder scans that are no longer near
        # the viewport so the newly visible folders get the worker slots first. Cached
        # thumbnails are never removed, so this does not cause a blink once loaded.
        first,last=self.visible_range()
        visible={str(self.items[i]['path']) for i in range(first,last) if i < len(self.items) and self.items[i].get('is_dir')}
        visible_sources=set()
        for i in range(first,last):
            if i>=len(self.items):continue
            item=self.items[i]
            if item.get('kind') in ('image','video') or (item.get('kind')=='document' and Path(item['path']).suffix.casefold() in PDF_EXTS):visible_sources.add(str(item['path']))
            elif item.get('is_dir'):
                source=self.folder_custom.get(str(item['path'])) or self.folder_cache.get(str(item['path']))
                if source:visible_sources.add(str(source))
        for key,f in list(self.thumb_futures.items()):
            if key[0] not in visible_sources and not f.running():
                try:f.cancel()
                except Exception:pass
                self.thumb_futures.pop(key,None)
        # Hover state is refreshed once scrolling stops. Recomputing it here
        # invalidates the entire viewport on every scrollbar value change.
        self.hover_delay_timer.stop();self.hover_animation_timer.stop()
        for key,f in list(self.folder_futures.items()):
            if key not in visible and not f.running():
                try: f.cancel()
                except: pass
                self.folder_futures.pop(key,None)

    def scroll_settled(self):
        if self.smooth_scroll_animation.state()==QAbstractAnimation.Running:
            self.scroll_settle_timer.start(100)
            return
        self.finish_scroll_diagnostic()
        self.fast_scroll=False
        self.update_scroll_work()
        cursor=self.viewport().mapFromGlobal(QCursor.pos())
        self.set_hover_index(self.thumbnail_hit(cursor) if self.viewport().rect().contains(cursor) else -1)
        self.viewport().update()

    def video_thumbnail_worker(self,path,w,h):
        if imageio_ffmpeg is None:
            return None
        try:
            exe=imageio_ffmpeg.get_ffmpeg_exe();deadline=time.monotonic()+15
            for seek in ('0','1','5'):
                if self._closing:return None
                remaining=deadline-time.monotonic()
                if remaining<=0:break
                try:
                    cmd=[exe,'-hide_banner','-loglevel','error','-ss',seek,'-i',str(path),'-frames:v','1','-vf',f'scale={max(1,int(w))}:{max(1,int(h))}:force_original_aspect_ratio=increase,crop={max(1,int(w))}:{max(1,int(h))}','-f','image2pipe','-vcodec','png','-']
                    data=subprocess.check_output(cmd,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),timeout=remaining)
                    img=QImage.fromData(data,'PNG')
                    if not img.isNull(): return img
                except subprocess.TimeoutExpired:
                    logger.warning('Video thumbnail extraction timed out for %s',path)
                    break
                except Exception:
                    continue
        except Exception:
            logger.exception('Video thumbnail extraction failed for %s',path)
        return None

    def thumbnail_worker(self,path,w,h):
        try:
            if self._closing:return None
            suffix=Path(path).suffix.casefold()
            if suffix in VIDEO_EXTS:
                return self.video_thumbnail_worker(path,w,h)
            if suffix in PDF_EXTS:
                document=load_pdf_document(path)
                if document is None or document.pageCount()<1:return None
                page=document.pagePointSize(0);tw=max(1,int(w));th=max(1,int(h))
                ratio=max(.01,page.width()/max(.01,page.height()))
                if ratio>=1:render_size=QSize(max(tw,int(round(th*ratio))),th)
                else:render_size=QSize(tw,max(th,int(round(tw/ratio))))
                img=document.render(0,render_size)
                if img.isNull():return None
                img=img.scaled(QSize(tw,th),Qt.KeepAspectRatioByExpanding,Qt.SmoothTransformation)
                return img.copy(max(0,(img.width()-tw)//2),max(0,(img.height()-th)//2),tw,th)
            if suffix=='.jxl':
                from PIL import Image
                import pillow_jxl  # registers JPEG XL support with Pillow
                tw=max(1,int(w));th=max(1,int(h))
                with Image.open(path) as source:
                    source.thumbnail((max(tw,th)*2,max(tw,th)*2),Image.Resampling.LANCZOS)
                    source=source.convert('RGBA')
                    image=QImage(source.tobytes(),source.width,source.height,source.width*4,QImage.Format_RGBA8888).copy()
                if image.isNull():return None
                image=image.scaled(QSize(tw,th),Qt.KeepAspectRatioByExpanding,Qt.SmoothTransformation)
                return image.copy(max(0,(image.width()-tw)//2),max(0,(image.height()-th)//2),tw,th)
            reader=QImageReader(str(path));reader.setAutoTransform(True)
            reader.setDecideFormatFromContent(True)
            source_size=reader.size()
            tw=max(1,w);th=max(1,h)
            if source_size.isValid() and source_size.width()>0 and source_size.height()>0:
                ratio=source_size.width()/source_size.height()
                if ratio>=1: target=QSize(max(tw,int(round(th*ratio))),th)
                else: target=QSize(tw,max(th,int(round(tw/ratio))))
                reader.setScaledSize(target)
            img=reader.read()
            if img.isNull() or reader.error()==QImageReader.ImageReaderError.InvalidDataError:return None
            img=img.scaled(QSize(tw,th),Qt.KeepAspectRatioByExpanding,Qt.SmoothTransformation)
            x=max(0,(img.width()-tw)//2);y=max(0,(img.height()-th)//2)
            return img.copy(x,y,tw,th)
        except Exception:
            return None

    def pix(self,path,box):
        if self._closing:return None
        key=(str(path),int(box.width()),int(box.height()))
        if key in self.cache:
            if self._scroll_diagnostic is not None:self._scroll_diagnostic['cache_hits']+=1
            self.cache.move_to_end(key)
            if self._visible_cache_keys is not None:self._visible_cache_keys.add(key)
            return self.cache[key]
        if self._scroll_diagnostic is not None:self._scroll_diagnostic['cache_misses']+=1
        if self.fast_scroll:return self.cache.get(key)
        if key not in self.thumb_futures:
            self.thumb_futures[key]=self.pool.submit(self.thumbnail_worker,str(path),int(box.width()),int(box.height()))
        return None
    @staticmethod
    def pixmap_bytes(pixmap):return pixmap.width()*pixmap.height()*max(1,(pixmap.depth()+7)//8)
    def set_cache_limit_mib(self,limit_mib):
        total_limit=max(16,min(8192,int(limit_mib)))*1024*1024
        self.card_cache_limit_bytes=min(64*1024*1024,total_limit//4)
        self.cache_limit_bytes=total_limit-self.card_cache_limit_bytes
        self.trim_card_cache()
        while self.cache and self.cache_bytes>self.cache_limit_bytes:
            _,oldest=self.cache.popitem(last=False)
            self.cache_bytes=max(0,self.cache_bytes-self.pixmap_bytes(oldest))
        self.viewport().update()

    def icon(self,k):return {'folder':'📁','video':'▶','audio':'♫','document':'PDF','file':'•','image':'🖼','favorite_group':'★'}.get(k,'•')
    def paint_card(self,p,i,r):
        it=self.items[i];meta=self.paint_metadata.get(id(it),('',self.label_prefix(it),False));source=self.card_thumbnail_source(it,meta)
        side=max(1,min(r.width()-2,self.cell_w if self.mode=='vertical' else r.width()-2));thumb_key=(source,side,side) if source else None
        thumb_pix=self.cache.get(thumb_key) if thumb_key else None
        thumb_signature=thumb_pix.cacheKey() if thumb_pix is not None else None
        dpr=float(self.devicePixelRatioF())
        cache_key=(str(it.get('path','')),str(it.get('name','')),it.get('kind'),bool(it.get('is_dir')),r.width(),r.height(),dpr,source,thumb_signature)
        cached=self.card_cache.get(cache_key)
        cache_enabled=getattr(self.explorer(),'thumbnail_cache_enabled',True)
        if cached is not None and cache_enabled:
            self.card_cache.move_to_end(cache_key)
            # A placeholder card can be cached while fast scrolling deliberately
            # defers thumbnail work. Once scrolling settles, keep requesting the
            # missing preview from this cache-hit path so the placeholder is
            # replaced when the worker completes.
            if source and thumb_pix is None and not self.fast_scroll and thumb_key not in self.thumb_futures:
                self.pix(source,QSize(side,side))
            if self._scroll_diagnostic is not None:self._scroll_diagnostic['card_cache_hits']+=1
        else:
            if self._scroll_diagnostic is not None:self._scroll_diagnostic['card_cache_misses']+=1
            cached=self.render_card_pixmap(i,r.size(),side,source,meta,dpr)
            if cached is not None and cache_enabled:
                self.card_cache[cache_key]=cached;self.card_cache_bytes+=self.pixmap_bytes(cached)
                self.trim_card_cache()
        if cached is not None:p.drawPixmap(r.topLeft(),cached)
        if self.image_search_active and getattr(self,'search_hover',-1)==i:
            preview=QRect(r.x()+1,r.y()+1,side,side);p.setPen(QPen(QColor('#ffffff'),3));p.setBrush(Qt.NoBrush);p.drawRect(preview.adjusted(1,1,-1,-1))
    def card_thumbnail_source(self,it,meta):
        if it['kind'] in ('image','video') or (it['kind']=='document' and meta[0] in PDF_EXTS):return str(it['path'])
        if it.get('kind')=='favorite_group':return self.folder_custom.get('favgroup:'+str(it['path']).split('://',1)[1]) or (self.ensure_folder_thumb(it['preview_path']) if it.get('preview_path') else None)
        if it['is_dir']:return self.ensure_folder_thumb(it['path'])
        return None
    def render_card_pixmap(self,index,size,side,source,meta,dpr):
        it=self.items[index];pixmap=QPixmap(max(1,int(round(size.width()*dpr))),max(1,int(round(size.height()*dpr))))
        if pixmap.isNull():return None
        pixmap.setDevicePixelRatio(dpr);pixmap.fill(Qt.transparent);p=QPainter(pixmap);r=QRect(0,0,size.width(),size.height());p.setPen(QPen(QColor('#171a24')));p.setBrush(QColor('#020513'));p.drawRect(r)
        preview=QRect(1,1,side,side)
        if source:
            px=self.pix(source,QSize(preview.width(),preview.height()))
            if px:p.drawPixmap(preview,px)
            else:self.draw_center(p,preview,self.icon(it['kind']),48)
        else:self.draw_center(p,preview,self.icon(it['kind']),48)
        label_y=side+5
        if it.get('kind')=='favorite_group':
            p.setPen(QColor('#d0a32d'));p.setFont(self.paint_fonts['group']);p.drawText(QRect(7,label_y,18,24),Qt.AlignCenter,'★')
        if it['is_dir']:
            p.save();p.setPen(Qt.NoPen);p.setBrush(QColor('#f1f1f1'));p.drawRoundedRect(QRect(7,label_y+5,13,10),2,2);p.drawRect(QRect(9,label_y+3,5,4));p.restore();text_x=25
        else:text_x=29 if it.get('kind')=='favorite_group' else 7
        if meta[2]:p.drawPixmap(QRect(text_x,label_y+4,16,16),self.image_name_icon);text_x+=20
        p.setPen(QColor('#ddd'));p.setFont(self.paint_fonts['label']);p.drawText(QRect(text_x,label_y,size.width()-(text_x-7)-7,38),Qt.TextWordWrap,meta[1]+it['name'])
        p.end();return pixmap
    def paint_horizontal(self,p,i):
        it=self.items[i];y=i*self.row_h-self.verticalScrollBar().value();r=QRect(10,y,self.viewport().width()-20,self.row_h-7);p.setBrush(QColor('#020513'));p.setPen(QPen(QColor('#171a24')));p.drawRect(r)
        box=QRect(r.x()+4,r.y()+4, r.height()-8,r.height()-15)
        paint_meta=self.paint_metadata.get(id(it),('',self.label_prefix(it),False))
        source=str(it['path']) if it['kind'] in ('image','video') or (it['kind']=='document' and paint_meta[0] in PDF_EXTS) else (self.ensure_folder_thumb(it['path']) if it['is_dir'] else None)
        if source:
            px=self.pix(source,box.size())
            if px:p.drawPixmap(box,px)
            else:self.draw_center(p,box,self.icon(it['kind']),32)
        else:self.draw_center(p,box,self.icon(it['kind']),32)
        text_x=box.right()+15
        if it['is_dir']:
            p.save();p.setPen(Qt.NoPen);p.setBrush(QColor('#f1f1f1'))
            fx=text_x; fy=r.y()+15
            p.drawRoundedRect(QRect(fx,fy,13,10),2,2);p.drawRect(QRect(fx+2,fy-2,5,4));p.restore()
            text_x+=20
        if paint_meta[2]:p.drawPixmap(QRect(text_x,r.y()+16,16,16),self.image_name_icon);text_x+=20
        p.setPen(QColor('#eee'));p.setFont(self.paint_fonts['horizontal_label']);p.drawText(QRect(text_x,r.y()+12,r.width()-text_x+r.x()-15,25),Qt.AlignLeft|Qt.AlignVCenter,paint_meta[1]+it['name'])
        p.setPen(QColor('#777'));p.setFont(self.paint_fonts['horizontal_detail']);p.drawText(QRect(box.right()+15,r.y()+40,r.width()-box.width()-30,22),Qt.AlignLeft|Qt.AlignVCenter,('Folder' if it['is_dir'] else it['kind'].title())+' • '+datefmt(it['mtime']))
    def label_prefix(self,item):
        if item.get('is_dir') or item.get('kind')=='favorite_group':return ''
        suffix=Path(str(item.get('path',''))).suffix.casefold()
        if item.get('kind')=='video' or suffix=='.gif':return '▶  '
        if item.get('kind')=='image':return '' if not self.image_name_icon.isNull() else '▧  '
        if item.get('kind')=='audio':return '♫  '
        return '▤  '
    def draw_center(self,p,r,text,size):p.setPen(QColor('#eee'));p.setFont(self.paint_fonts['horizontal_center'] if size==32 else self.paint_fonts['center']);p.drawText(r,Qt.AlignCenter,text)
    def paint_hover_details(self,p,preview,item):
        if item.get('kind')=='favorite_group':
            height=max(28,min(40,int(preview.height()*.18)));elapsed=max(0,time.monotonic()-self.hover_animation_started);progress=min(1.0,elapsed/.18) if self.hover_animation_started else 0.0;y=preview.y()-height+round(height*(1-(1-progress)**3));rect=QRect(preview.x(),y,preview.width(),height);self.hover_overlay_rect=rect;p.save();p.setClipRect(self.viewport().rect());p.fillRect(rect,QColor(0,0,0,190));self.hover_favorite_rect=QRect(preview.right()-32,y+2,28,height-4);p.setPen(QColor('#fff'));p.setFont(QFont('Segoe UI',15,QFont.Bold));p.drawText(self.hover_favorite_rect,Qt.AlignCenter,'−');p.restore();return
        height=max(1,min(68,max(32,int(preview.height()*.34)),max(1,self.viewport().height())))
        elapsed=max(0,time.monotonic()-self.hover_animation_started)
        progress=min(1.0,elapsed/.18) if self.hover_animation_started else 0.0
        eased=1-(1-progress)**3
        placement=getattr(self.explorer(),'hover_details_placement','top')
        if placement=='below':placement='bottom'
        # Grow downward from the hovered thumbnail's bottom edge. Anchoring to
        # the next row made the popup appear to start on the next thumbnail.
        below_y=(preview.y()+self.cell_w+43) if self.mode=='vertical' else (preview.bottom()+1)
        can_below=below_y+height<=self.viewport().height();can_above=preview.y()>=height
        if placement=='bottom':side='below' if can_below else 'above'
        elif placement=='above':side='above' if can_above else 'below'
        else:side='top'
        if side=='top':
            y=preview.y()-height+round(height*eased);target=QRect(preview.x(),y,preview.width(),height);reveal=target;content_y=y
        elif side=='below':
            target_y=below_y;target=QRect(preview.x(),target_y,preview.width(),height);reveal=QRect(target.x(),target_y,target.width(),round(height*eased));content_y=target_y
        else:
            target_y=preview.y()-height;target=QRect(preview.x(),target_y,preview.width(),height);visible_h=round(height*eased);reveal=QRect(target.x(),preview.y()-visible_h,target.width(),visible_h);content_y=target_y
        self.hover_popup_side=side;self.hover_overlay_rect=reveal;self.hover_tooltip_rect=target
        p.save();p.setClipRect(self.viewport().rect());p.setClipRect(reveal,Qt.IntersectClip);p.fillRect(target,QColor(0,0,0,190))
        font_size=max(8,min(11,int(preview.width()/24)));p.setFont(QFont('Segoe UI',font_size));p.setPen(QColor('#fff'))
        raw_size=item.get('size')
        size_text='Calculating…' if raw_size is None else sizefmt(max(0,int(raw_size)))
        lines=(f"Size: {size_text}",f"Date created: {datefmt(item.get('ctime',0))}")
        line_h=max(15,(height-6)//2)
        button_size=max(20,min(30,height-8));has_favorite=item.get('kind')!='favorite_group';self.hover_favorite_rect=QRect(preview.right()-button_size-4,content_y+4,button_size,button_size) if has_favorite else QRect()
        text_width=preview.width()-14-(button_size+5 if has_favorite else 0)
        p.drawText(QRect(preview.x()+7,content_y+3,text_width,line_h),Qt.AlignLeft|Qt.AlignVCenter,lines[0])
        p.drawText(QRect(preview.x()+7,content_y+3+line_h,preview.width()-14,line_h),Qt.AlignLeft|Qt.AlignVCenter,lines[1])
        if has_favorite:
            key=str(item['path']);fav=key in getattr(self.explorer(),'favorites',set());grouped=self.explorer().is_folder_in_favorite_group(key)
            p.setPen(QColor('#ffd34d' if fav or grouped else '#fff'));p.setFont(QFont('Segoe UI',max(13,font_size+4),QFont.Bold));p.drawText(self.hover_favorite_rect,Qt.AlignCenter,'+')
        p.restore()
    def begin_hover_animation(self):
        if self.hover_index<0 or not getattr(self.explorer(),'hover_details_enabled',True):return
        item=self.items[self.hover_index]
        if item.get('is_dir'):self.request_folder_size(item,priority=True)
        self.hover_animation_started=time.monotonic();self.hover_animation_timer.start();self.viewport().update()
    def advance_hover_animation(self):
        if time.monotonic()-self.hover_animation_started>=.18:self.hover_animation_timer.stop()
        self.viewport().update()
    def set_hover_index(self,index):
        if index==self.hover_index:return
        self.hover_delay_timer.stop();self.hover_animation_timer.stop();self.hover_index=index;self.hover_animation_started=0
        self.hover_favorite_rect=QRect();self.hover_overlay_rect=QRect();self.hover_tooltip_rect=QRect()
        keep_key=str(self.items[index]['path']) if 0<=index<len(self.items) else None
        if getattr(self.explorer(),'sort',None)!='size':
            for key,f in list(self.folder_size_futures.items()):
                if not f.running() and key!=keep_key:
                    f.cancel();self.folder_size_futures.pop(key,None);self.folder_size_events.pop(key,None)
        if index>=0 and getattr(self.explorer(),'hover_details_enabled',True):
            self.hover_delay_timer.start(max(0,int(getattr(self.explorer(),'hover_details_delay',500))))
        self.viewport().update()
    def hit(self,pos):
        y=pos.y()+self.verticalScrollBar().value()
        if self.mode=='horizontal':i=y//self.row_h;return i if 0<=i<len(self.items) else -1
        gap=9;cw=self.cell_w; x=pos.x()-9
        if x<0:return -1
        col=min(self.columns-1,max(0,int(x//(cw+gap))));row=int(y//self.row_h);i=row*self.columns+col
        if 0<=i<len(self.items):
            cell_x=9+col*(cw+gap); cell_y=row*self.row_h
            if x > (cell_x-9)+(cw+gap) or y-cell_y > cw+48:return -1
            return i
        return -1
    def thumbnail_hit(self,pos):
        index=self.hit(pos)
        if index<0:return -1
        if self.mode=='horizontal':
            return index
        # Use one contiguous hit region for the preview and caption. Separate
        # image/name rectangles left a two-pixel seam that repeatedly cleared
        # and restarted the hover timer as the pointer crossed it.
        row,col=divmod(index,self.columns)
        x=9+col*(self.cell_w+9)
        y=row*self.row_h-self.verticalScrollBar().value()
        return index if QRect(x,y,self.cell_w,self.row_h).contains(pos) else -1
    def name_hit(self,index,pos):
        if index<0 or index>=len(self.items):return False
        if self.mode=='horizontal':return pos.x()>=self.row_h+10
        row=index//self.columns;local_y=pos.y()+self.verticalScrollBar().value()-row*self.row_h
        return self.cell_w<local_y<self.row_h
    def selection_key(self,item):return str(item.get('path',''))
    def selectable_item(self,item):return item.get('kind')!='favorite_group' or bool(getattr(self.explorer(),'in_favorites_view',False))
    def remember_deselection(self,paths):
        removed=set(paths)
        if removed:self.selection_undo.append((removed,time.monotonic()));self.selection_undo=self.selection_undo[-50:]
    def clear_selection(self,remember=True):
        if self.selected_paths:
            if remember:self.remember_deselection(self.selected_paths)
            self.selected_paths.clear();self.viewport().update();self.explorer().update_selection_button()
    def undo_deselection(self):
        if self.selection_undo:self.selected_paths.update(self.selection_undo.pop()[0]);self.viewport().update();self.explorer().update_selection_button()
    def toggle_selected_path(self,path):
        key=str(path)
        if key in self.selected_paths:self.selected_paths.remove(key);self.remember_deselection({key})
        else:self.selected_paths.add(key)
        self.viewport().update();self.explorer().update_selection_button()
    def select_all_current(self):
        keys={self.selection_key(item) for item in self.items if self.selectable_item(item) and item.get('path')}
        if keys and keys.issubset(self.selected_paths):
            self.remember_deselection(keys);self.selected_paths.difference_update(keys)
        else:self.selected_paths.update(keys)
        self.viewport().update();self.explorer().update_selection_button()
    def begin_rect_selection(self,pos):
        self.setFocus();self.selection_dragging=True;self.selection_scroll_accumulator=0.0;self.selection_drag_moved=False;self.selection_anchor=QPoint(pos.x(),pos.y()+self.verticalScrollBar().value());self.selection_current=QPoint(self.selection_anchor);self.selection_base=set(self.selected_paths);self.last_mouse_pos=QPoint(pos);self.selection_scroll_timer.start();self.navigation_index=-1;self.set_hover_index(-1);self.favorite_picker_delay.stop();self.favorite_picker_key=None;self.viewport().update()
    def rect_selection_items(self,rect):
        if self.mode=='horizontal':
            first=max(0,rect.top()//self.row_h);last=min(len(self.items)-1,rect.bottom()//self.row_h)
            return [i for i in range(first,last+1) if QRect(10,i*self.row_h,self.viewport().width()-20,self.row_h-7).intersects(rect)] if last>=first else []
        gap=9;cw=self.cell_w;first_row=max(0,rect.top()//self.row_h);last_row=min((len(self.items)-1)//self.columns,rect.bottom()//self.row_h);first_col=max(0,(rect.left()-9)//(cw+gap));last_col=min(self.columns-1,(rect.right()-9)//(cw+gap));indices=[]
        for row in range(first_row,last_row+1):
            for col in range(first_col,last_col+1):
                i=row*self.columns+col
                if i<len(self.items) and QRect(9+col*(cw+gap),row*self.row_h,cw,self.row_h-1).intersects(rect):indices.append(i)
        return indices
    def update_rect_selection(self,pos):
        if not self.selection_dragging or self.selection_anchor is None:return
        self.last_mouse_pos=QPoint(pos);self.selection_current=QPoint(pos.x(),pos.y()+self.verticalScrollBar().value())
        if (QPoint(pos.x(),pos.y()+self.verticalScrollBar().value())-self.selection_anchor).manhattanLength()>=QApplication.startDragDistance():self.selection_drag_moved=True
        if not self.selection_drag_moved:return
        rect=QRect(self.selection_anchor,self.selection_current).normalized();inside=set()
        for i in self.rect_selection_items(rect):
            item=self.items[i]
            if self.selectable_item(item):inside.add(self.selection_key(item))
        self.selected_paths=self.selection_base.symmetric_difference(inside);self.viewport().update();self.explorer().update_selection_button()
    def auto_scroll_selection(self):
        if not self.selection_dragging:return
        y=self.last_mouse_pos.y();height=self.viewport().height();edge=240;speed=0.0
        if y<edge:
            intensity=max(0.0,min(1.0,(edge-y)/edge));speed=-(.25+159.75*intensity**2.2)
        elif y>height-edge:
            intensity=max(0.0,min(1.0,(y-(height-edge))/edge));speed=.25+159.75*intensity**2.2
        self.selection_scroll_accumulator+=speed
        delta=int(self.selection_scroll_accumulator)
        if delta:
            self.selection_scroll_accumulator-=delta
            bar=self.verticalScrollBar();old=bar.value();bar.setValue(max(0,min(bar.maximum(),old+delta)))
            if bar.value()!=old:self.update_rect_selection(self.last_mouse_pos)
    def finish_rect_selection(self,pos):
        if not self.selection_dragging:return
        self.update_rect_selection(pos)
        if not self.selection_drag_moved:
            self.selected_paths=set(self.selection_base)
            i=self.hit(pos)
            if 0<=i<len(self.items) and self.selectable_item(self.items[i]):self.toggle_selected_path(self.selection_key(self.items[i]))
        else:self.remember_deselection(self.selection_base-self.selected_paths)
        self.selection_dragging=False;self.selection_scroll_timer.stop();self.selection_anchor=None;self.selection_current=None;self.selection_base.clear();self.viewport().update();self.explorer().update_selection_button()
    def deselect_paths(self,paths):
        keys={str(path) for path in paths};removed=self.selected_paths&keys
        if removed:self.remember_deselection(removed);self.selected_paths.difference_update(removed);self.viewport().update();self.explorer().update_selection_button()
    def ensure_navigation_visible(self,index):
        if index<0 or index>=len(self.items):return
        top=(index*self.row_h if self.mode=='horizontal' else (index//self.columns)*self.row_h);bottom=top+self.row_h;bar=self.verticalScrollBar()
        if top<bar.value():bar.setValue(top)
        elif bottom>bar.value()+self.viewport().height():bar.setValue(max(0,bottom-self.viewport().height()))
        self.viewport().update()
    def handle_navigation_key(self,e):
        key=e.key();explorer=self.explorer()
        if key==Qt.Key_Escape and self.selected_paths:self.clear_selection();return True
        if key==Qt.Key_Z and e.modifiers()&Qt.ControlModifier:explorer.undo_last_action();return True
        if key==Qt.Key_Q and not e.modifiers()&(Qt.ControlModifier|Qt.AltModifier):explorer.toggle_search_navigation_focus();return True
        if key==Qt.Key_Tab:
            count=explorer.tabbar.count()
            if count:explorer.tabbar.setCurrentIndex((explorer.tabbar.currentIndex()+(-1 if e.modifiers()&Qt.ShiftModifier else 1))%count)
            return True
        if Qt.Key_1<=key<=Qt.Key_7 and not e.modifiers()&(Qt.ControlModifier|Qt.AltModifier|Qt.ShiftModifier):explorer.set_sort(('name','mtime','ctime','type','size','random','manual')[key-Qt.Key_1]);return True
        if key in (Qt.Key_Return,Qt.Key_Enter,Qt.Key_Space):
            if 0<=self.navigation_index<len(self.items):explorer.open(str(self.items[self.navigation_index]['path']))
            return True
        if key in (Qt.Key_Backspace,Qt.Key_Back):self.goBack.emit();return True
        if key not in (Qt.Key_Left,Qt.Key_Right,Qt.Key_Up,Qt.Key_Down):return False
        if not self.items:return True
        if self.navigation_index<0:
            self.navigation_index=min(self.visible_range()[0],len(self.items)-1);self.navigation_started=time.monotonic();self.navigation_key=key;self.ensure_navigation_visible(self.navigation_index);return True
        if not e.isAutoRepeat():self.navigation_started=time.monotonic()
        step=min(4,1+int(max(0,time.monotonic()-self.navigation_started-1)//.6)) if e.isAutoRepeat() else 1
        delta=step if key in (Qt.Key_Right,Qt.Key_Down) else -step
        if self.mode=='vertical' and key in (Qt.Key_Up,Qt.Key_Down):delta*=self.columns
        self.navigation_key=key;self.navigation_index=max(0,min(len(self.items)-1,self.navigation_index+delta));self.ensure_navigation_visible(self.navigation_index);return True
    def keyPressEvent(self,e):
        if self.handle_navigation_key(e):e.accept();return
        if e.key()==Qt.Key_Home:
            self.verticalScrollBar().setValue(0);e.accept();return
        if e.key()==Qt.Key_End:
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum());e.accept();return
        super().keyPressEvent(e)
    def keyReleaseEvent(self,e):
        if e.key() in (Qt.Key_Left,Qt.Key_Right,Qt.Key_Up,Qt.Key_Down):self.navigation_key=None
        super().keyReleaseEvent(e)
    def focusNextPrevChild(self,next):
        tabbar=getattr(self.explorer(),'tabbar',None)
        if tabbar is not None and tabbar.count():tabbar.setCurrentIndex((tabbar.currentIndex()+(1 if next else -1))%tabbar.count());return True
        return super().focusNextPrevChild(next)

    def eventFilter(self,obj,e):
        if obj is self.viewport():
            if e.type()==QEvent.KeyPress and self.handle_navigation_key(e):e.accept();return True
            if e.type()==QEvent.KeyRelease and e.key() in (Qt.Key_Left,Qt.Key_Right,Qt.Key_Up,Qt.Key_Down):self.navigation_key=None
            if e.type()==QEvent.MouseButtonPress and e.button()==Qt.LeftButton:
                pos=e.position().toPoint();mods=e.modifiers()
                if mods&Qt.AltModifier:
                    i=self.hit(pos)
                    if 0<=i<len(self.items) and self.items[i].get('kind')!='favorite_group':self.explorer().reveal_in_folder(self.items[i]['path'])
                    else:self.clear_selection()
                    e.accept();return True
                if mods&Qt.ShiftModifier:self.begin_rect_selection(pos);e.accept();return True
                if not self.hover_favorite_rect.contains(pos) and self.hit(pos)<0 and self.selected_paths:self.clear_selection()
            if e.type()==QEvent.MouseButtonRelease and e.button()==Qt.LeftButton and self.selection_dragging:
                self.finish_rect_selection(e.position().toPoint());e.accept();return True
            if e.type()==QEvent.MouseMove:
                pos=e.position().toPoint()
                if self.selection_dragging:self.update_rect_selection(pos);return True
                if self.navigation_index>=0:self.navigation_index=-1;self.viewport().update()
                if getattr(self.explorer(),'favorite_organize_mode',False) and self.drag_source_index>=0 and e.buttons()&Qt.LeftButton:
                    if not self.drag_started and (pos-self.drag_start).manhattanLength()>=QApplication.startDragDistance():self.start_favorite_drag()
                    e.accept();return True
                if self.hover_favorite_rect.contains(pos) and 0<=self.hover_index<len(self.items) and self.items[self.hover_index].get('kind')!='favorite_group':
                    key=str(self.items[self.hover_index]['path'])
                    if key!=self.favorite_picker_hover_key:
                        self.favorite_picker_hover_key=key;self.favorite_picker_key=key;self.favorite_picker_delay.start()
                    return False
                if self.hover_tooltip_rect.contains(pos) or self.hover_overlay_rect.contains(pos):
                    self.favorite_picker_delay.stop();self.favorite_picker_hover_key=None
                    return False
                self.favorite_picker_delay.stop();self.favorite_picker_hover_key=None
                index=self.thumbnail_hit(e.position().toPoint())
                self.set_hover_index(index)
                if self.image_search_active and self.search_hover!=index:
                    self.search_hover=index;self.viewport().update()
                return False
            if e.type()==QEvent.Leave:
                menu=getattr(self.explorer(),'favorite_picker_menu',None)
                cursor=QCursor.pos()
                if menu is not None and menu.isVisible() and (menu.geometry().contains(cursor) or getattr(self.explorer(),'favorite_picker_source_rect',QRect()).adjusted(-2,-2,2,2).contains(cursor)):
                    return False
                self.favorite_picker_delay.stop();self.favorite_picker_hover_key=None;self.set_hover_index(-1);self.search_hover=-1;self.viewport().update();return False
            if e.type() in (QEvent.DragEnter,QEvent.DragMove):
                if getattr(self.explorer(),'favorite_organize_mode',False) and e.mimeData().hasText():
                    pos=e.position().toPoint();index=self.hit(pos);self.drop_indicator=(index,pos.x()<(self.item_rect(index).center().x()) if index>=0 else True) if index>=0 else None;self.viewport().update();e.acceptProposedAction();return True
                return False
            if e.type()==QEvent.Drop:
                self.explorer().handle_favorite_drop(e.mimeData().text(),self.hit(e.position().toPoint()),self.drop_indicator[1] if self.drop_indicator else True);self.drop_indicator=None;self.viewport().update();e.acceptProposedAction();return True
            if e.type()==QEvent.DragLeave:self.drop_indicator=None;self.viewport().update();return False
            if getattr(self.explorer(),'favorite_organize_mode',False):
                if e.type()==QEvent.MouseButtonDblClick and time.monotonic()-self.last_favorite_drag<QApplication.doubleClickInterval()/1000+0.1:
                    e.accept();return True
                if e.type()==QEvent.MouseButtonPress and e.button()==Qt.LeftButton:
                    pos=e.position().toPoint();i=self.hit(pos)
                    delete_group=(self.hover_favorite_rect.contains(pos) and 0<=self.hover_index<len(self.items) and self.items[self.hover_index].get('kind')=='favorite_group')
                    if not delete_group:
                        self.drag_source_index=i;self.drag_start=pos;self.drag_started=False
                        if i>=0:e.accept();return True
                if e.type()==QEvent.MouseMove and self.drag_source_index>=0 and e.buttons()&Qt.LeftButton:
                    if not self.drag_started and (e.position().toPoint()-self.drag_start).manhattanLength()>=QApplication.startDragDistance():self.start_favorite_drag()
                    e.accept();return True
                if e.type()==QEvent.MouseButtonRelease and e.button()==Qt.LeftButton and self.drag_source_index>=0:
                    self.drag_source_index=-1;self.drag_started=False;e.accept();return True
            if self.image_search_active and e.type()==QEvent.MouseButtonPress and e.button()==Qt.LeftButton:
                i=self.hit(e.position().toPoint())
                if 0<=i<len(self.items):
                    item=self.items[i]
                    if item.get('is_dir'):self.openItem.emit(str(item['path']))
                    elif item.get('kind') in ('image','video'):self.imageSearchRequested.emit(str(item['path']))
                # Search mode owns the left click, even when the pointer is not over media.
                # This prevents the normal single-click-open handler from firing.
                return True
        return super().eventFilter(obj,e)

    def start_favorite_drag(self):
        if self.drag_started or not (0<=self.drag_source_index<len(self.items)):return
        self.drag_started=True;item=self.items[self.drag_source_index]
        mime=QMimeData();raw=str(item['path'])
        if item.get('kind')=='favorite_group':payload='favgroup://'+raw.split('://',1)[-1]
        else:
            selected=[str(path) for path in self.selected_paths if not str(path).startswith('favgroup://') and Path(path).exists()]
            if raw not in selected:selected=[raw]
            payload='folder-list://'+json.dumps(selected)
        mime.setText(payload)
        drag=QDrag(self);drag.setMimeData(mime)
        ghost=self.favorite_drag_pixmap(item)
        if not ghost.isNull():drag.setPixmap(ghost);drag.setHotSpot(QPoint(ghost.width()//2,ghost.height()//2))
        drag.exec(Qt.MoveAction)
        self.last_favorite_drag=time.monotonic();self.drag_source_index=-1;self.drag_started=False;self.drop_indicator=None;self.viewport().update()

    def item_rect(self,index):
        if not 0<=index<len(self.items):return QRect()
        if self.mode=='horizontal':return QRect(10,index*self.row_h-self.verticalScrollBar().value(),self.viewport().width()-20,self.row_h-7)
        row,col=divmod(index,self.columns);return QRect(9+col*(self.cell_w+9),row*self.row_h-self.verticalScrollBar().value(),self.cell_w,self.cell_w+48)

    def favorite_drag_pixmap(self,item):
        size=max(72,min(144,getattr(self,'cell_w',120)));preview=QRect(3,3,size-6,size-30)
        pixmap=QPixmap(size,size+22);pixmap.fill(Qt.transparent);p=QPainter(pixmap);p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(220,225,235,190),1));p.setBrush(QColor(18,21,29,220));p.drawRoundedRect(QRect(0,0,size-1,size+20),7,7)
        if item.get('kind')=='favorite_group':source=self.folder_custom.get('favgroup:'+str(item['path']).split('://',1)[-1]) or (self.ensure_folder_thumb(item['preview_path']) if item.get('preview_path') else None)
        elif item.get('is_dir'):source=self.ensure_folder_thumb(item['path'])
        elif item.get('kind') in ('image','video'):source=str(item['path'])
        else:source=None
        thumb=self.pix(source,preview.size()) if source else None
        if thumb:p.drawPixmap(preview,thumb)
        else:self.draw_center(p,preview,self.icon(item.get('kind','folder')),32)
        p.setPen(QColor(240,242,247,230));p.setFont(QFont('Segoe UI',9));p.drawText(QRect(6,size-23,size-12,18),Qt.AlignCenter,item.get('name',''))
        p.end();return pixmap

    def open_hover_favorite_picker(self):
        if self.favorite_picker_key and self.hover_favorite_rect.isValid():
            r=self.hover_favorite_rect;top=self.viewport().mapToGlobal(r.topLeft());anchor=QRect(top,r.size())
            popup=self.hover_tooltip_rect;popup_top=self.viewport().mapToGlobal(popup.topLeft()) if popup.isValid() else top
            tooltip=QRect(popup_top,popup.size()) if popup.isValid() else anchor
            selected=list(self.selected_paths) if self.favorite_picker_key in self.selected_paths else [self.favorite_picker_key]
            self.explorer().show_favorites_picker(self.favorite_picker_key,anchor,tooltip,selected)

    def set_image_search_active(self,active):
        self.image_search_active=bool(active)
        self.search_hover=-1
        self.viewport().update()

    def mouseMoveEvent(self,e):
        if getattr(self.explorer(),'favorite_organize_mode',False) and self.drag_source_index>=0 and e.buttons() & Qt.LeftButton and not self.drag_started and (e.position().toPoint()-self.drag_start).manhattanLength()>=QApplication.startDragDistance():
            self.start_favorite_drag();e.accept();return
        if self.image_search_active:
            self.search_hover=self.thumbnail_hit(e.position().toPoint())
            self.viewport().update()
        super().mouseMoveEvent(e)

    def mousePressEvent(self,e):
        self.setFocus()
        if e.button()==Qt.LeftButton and self.hover_index>=0 and self.hover_favorite_rect.contains(e.position().toPoint()):
            self.favorite_picker_delay.stop();self.toggleItemFavorite.emit(str(self.items[self.hover_index]['path']));e.accept();return
        if e.button()==Qt.LeftButton and getattr(self.explorer(),'favorite_organize_mode',False):
            i=self.hit(e.position().toPoint())
            if i>=0:self.drag_source_index=i;self.drag_start=e.position().toPoint();self.drag_started=False;e.accept();return
        if e.button()==Qt.RightButton:
            explorer=self.explorer()
            if self.image_search_active:
                self.set_image_search_active(False);explorer.image_search_mode=False
                if hasattr(explorer,'status'):explorer.status.setText('Image search off')
                e.accept();return
            if getattr(explorer,'favorite_organize_mode',False):
                explorer.set_favorite_organization_mode(False);e.accept();return
            self.goBack.emit();e.accept();return
        if e.button()==Qt.MiddleButton:
            i=self.hit(e.position().toPoint())
            if i>=0 and self.items[i].get('is_dir'):
                self.middleOpenItem.emit(str(self.items[i]['path']))
            e.accept();return
        if e.button()==Qt.LeftButton:
            i=self.hit(e.position().toPoint())
            if i>=0:
                if e.modifiers()&Qt.AltModifier:
                    self.explorer().reveal_in_folder(str(self.items[i]['path']));e.accept();return
                if getattr(self.explorer(),'in_favorites_view',False) and not getattr(self.explorer(),'favorite_organize_mode',False) and self.items[i].get('kind')=='favorite_group' and self.group_name_hit(self.items[i],e.position().toPoint()):
                    self.favorite_click_timer.stop();self.pending_group_rename_id=str(self.items[i]['path']).split('://',1)[-1];self.group_rename_timer.start(QApplication.doubleClickInterval()+80);e.accept();return
                if getattr(self.explorer(),'favorite_organize_mode',False):self.drag_source_index=i;self.drag_start=e.position().toPoint();self.drag_started=False;e.accept();return
                if self.name_hit(i,e.position().toPoint()) and self.items[i].get('kind')!='favorite_group':
                    path=str(self.items[i]['path'])
                    if self.items[i].get('is_dir'):self.explorer().reveal(path)
                    else:self.explorer().reveal_in_folder(path)
                    e.accept();return
                self.openItem.emit(str(self.items[i]['path']))
            e.accept();return
        super().mousePressEvent(e)
    def mouseReleaseEvent(self,e):
        if e.button()==Qt.LeftButton and self.drag_source_index>=0:
            i=self.drag_source_index;self.drag_source_index=-1
            if self.suppress_favorite_release:self.suppress_favorite_release=False
            self.drag_started=False;e.accept();return
        super().mouseReleaseEvent(e)
    def dispatch_favorite_open(self):
        i=self.pending_favorite_item;self.pending_favorite_item=-1
        if 0<=i<len(self.items):self.openItem.emit(str(self.items[i]['path']))
    def dispatch_group_rename(self):
        group_id=self.pending_group_rename_id;self.pending_group_rename_id=None
        explorer=self.explorer()
        if group_id and getattr(explorer,'in_favorites_view',False) and not getattr(explorer,'favorite_organize_mode',False):explorer.rename_favorite_group(group_id)
    def mouseDoubleClickEvent(self,e):
        if getattr(self.explorer(),'favorite_organize_mode',False):e.accept();return
        if not getattr(self.explorer(),'in_favorites_view',False):super().mouseDoubleClickEvent(e);return
        if time.monotonic()-self.last_favorite_drag<QApplication.doubleClickInterval()/1000+0.1:e.accept();return
        if e.button()==Qt.LeftButton:
            i=self.hit(e.position().toPoint())
            self.favorite_click_timer.stop();self.pending_favorite_item=-1;self.suppress_favorite_release=True
            if 0<=i<len(self.items):
                item=self.items[i]
                if item.get('kind')=='favorite_group' and self.group_name_hit(item,e.position().toPoint()):self.group_rename_timer.stop();self.pending_group_rename_id=None;self.explorer().rename_favorite_group(str(item['path']).split('://',1)[1]);e.accept();return
                if not getattr(self.explorer(),'favorite_organize_mode',False):super().mouseDoubleClickEvent(e);return
                self.openItem.emit(str(item['path']));e.accept();return
        super().mouseDoubleClickEvent(e)
    def group_name_hit(self,item,pos):
        if self.mode=='horizontal':return pos.x()>self.row_h
        return (pos.y()+self.verticalScrollBar().value())%self.row_h>self.cell_w
    def contextMenuEvent(self,e): e.accept()

def make_folder_icon(size=16):
    pm=QPixmap(size,size);pm.fill(Qt.transparent)
    p=QPainter(pm);p.setPen(Qt.NoPen);p.setBrush(QColor('#f1f1f1'))
    w,h=size,size
    p.drawRoundedRect(QRect(1,4,w-2,h-5),2,2)
    p.drawRoundedRect(QRect(2,2,max(4,w//2),5),2,2)
    p.setBrush(QColor('#ffffff'));p.drawRect(QRect(2,6,w-4,h-7));p.end()
    return QIcon(pm)

def make_boat_icon():
    # Small vector-style silhouette; works without an external image file.
    svg='''<svg xmlns="http://www.w3.org/2000/svg" width=64 height=64 viewBox="0 0 64 64"><path fill="#d0a32d" d="M9 42h46l-7 9H18zM18 39V18h3v21zm3-18 18 9H21zm19 9h4v9h-4zM6 54c6 4 12-2 18 0 6 4 12-2 18 0 6 4 12-2 16 0v4c-6-3-11 3-17 0-6-3-12 3-18 0-6-3-12 3-17 0z"/></svg>'''
    return QIcon(QPixmap.fromImage(QImage.fromData(svg.encode('utf-8'))))

class SafeTabBar(QTabBar):
    closeRequested=Signal(int);dragStarted=Signal();dragFinished=Signal()
    def __init__(self,parent=None):super().__init__(parent);self._last_middle_pos=None;self._last_middle_time=0.0;self._hovered_tab=-1;self.selected_tabs=set();self.selection_anchor=-1;self._wheel_remainder=0;self._colored_tab_indices={};self._press_pos=None;self._press_tab=-1;self._dragging_tabs=False;self.setMouseTracking(True);self.setUsesScrollButtons(True)
    def setTabData(self,index,data):
        super().setTabData(index,data)
        if isinstance(data,str) and data:self._colored_tab_indices[index]=data
        else:self._colored_tab_indices.pop(index,None)
        self.update(self.tabRect(index))
    def wheelEvent(self, e):
        delta=e.angleDelta().x() or e.angleDelta().y()
        if not delta and e.pixelDelta().x():delta=e.pixelDelta().x()
        if not delta and e.pixelDelta().y():delta=e.pixelDelta().y()
        if delta:
            self._wheel_remainder+=delta
            while abs(self._wheel_remainder)>=120:
                direction=Qt.RightArrow if self._wheel_remainder<0 else Qt.LeftArrow
                button=next((child for child in self.findChildren(QToolButton) if child.arrowType()==direction and child.isVisible()),None)
                if button is None:self._wheel_remainder=0;break
                button.click();self._wheel_remainder+=120 if self._wheel_remainder<0 else -120
        e.accept()
    def keyPressEvent(self,e):
        if e.key()==Qt.Key_Q and not e.modifiers()&(Qt.ControlModifier|Qt.AltModifier):
            window=self.window()
            if hasattr(window,'toggle_search_navigation_focus'):window.toggle_search_navigation_focus();e.accept();return
        super().keyPressEvent(e)
    def mousePressEvent(self, e):
        if e.button()==Qt.LeftButton:
            pos=e.position().toPoint();idx=self.tabAt(pos)
            if idx>=0 and idx==self._hovered_tab and self.close_rect(idx).contains(pos):
                self.closeRequested.emit(idx);e.accept();return
            if idx>=0:
                self._press_pos=pos;self._press_tab=idx;self._dragging_tabs=False
                if e.modifiers()&Qt.ControlModifier:self.select_range(idx)
                else:self.selection_anchor=idx;self.selected_tabs={idx};self.update()
        if e.button()==Qt.MiddleButton:
            idx=self.tabAt(e.position().toPoint())
            if idx>=0:
                now=time.monotonic();pos=e.position().toPoint()
                if self._last_middle_pos is None or (now-self._last_middle_time>QApplication.doubleClickInterval()/1000 or (pos-self._last_middle_pos).manhattanLength()>QApplication.startDragDistance()):
                    self.closeRequested.emit(idx)
                self._last_middle_pos=pos;self._last_middle_time=now
            e.accept()
            return
        super().mousePressEvent(e)
    def mouseDoubleClickEvent(self,e):
        if e.button()==Qt.MiddleButton:
            e.accept();return
        super().mouseDoubleClickEvent(e)
    def mouseMoveEvent(self, e):
        pos=e.position().toPoint();idx=self.tabAt(pos)
        if idx!=self._hovered_tab:self._hovered_tab=idx;self.update()
        if idx>=0 and self.close_rect(idx).contains(pos):self.setCursor(Qt.PointingHandCursor)
        else:self.setCursor(Qt.ArrowCursor)
        if self._press_tab>=0 and e.buttons()&Qt.LeftButton and not self._dragging_tabs and (pos-self._press_pos).manhattanLength()>=QApplication.startDragDistance():
            self._dragging_tabs=True;self.dragStarted.emit()
        if self._dragging_tabs and e.buttons()&Qt.LeftButton and idx>=0 and idx!=self._press_tab:
            old_index=self._press_tab;self.moveTab(old_index,idx);self._press_tab=idx
        super().mouseMoveEvent(e)
    def mouseReleaseEvent(self,e):
        was_dragging=self._dragging_tabs;self._press_pos=None;self._press_tab=-1;self._dragging_tabs=False
        super().mouseReleaseEvent(e)
        if was_dragging:self.dragFinished.emit()
    def leaveEvent(self,e):
        if self._hovered_tab!=-1:self._hovered_tab=-1;self.update()
        self.setCursor(Qt.ArrowCursor);super().leaveEvent(e)
    def close_rect(self,index):
        rect=self.tabRect(index);return QRect(rect.right()-23,rect.center().y()-8,16,16)
    def paintEvent(self,e):
        super().paintEvent(e)
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing,True)
        # Color only tabs that actually have an outline, plus selected tabs.
        # Walking every tab on each paint made the new decoration scale with the
        # total open-tab count even when most tabs had no custom state.
        for i in self._colored_tab_indices.keys()|self.selected_tabs:
            if i<0 or i>=self.count():continue
            raw_rect=self.tabRect(i)
            if not raw_rect.intersects(e.rect()):continue
            rect=raw_rect.adjusted(1,1,-1,-1)
            color=self._colored_tab_indices.get(i)
            if color:
                p.setPen(QPen(QColor(color),2));p.setBrush(Qt.NoBrush);p.drawRoundedRect(rect,4,4)
            if i in self.selected_tabs:
                p.setPen(QPen(QColor(255,255,255,210),1,Qt.DashLine));p.setBrush(Qt.NoBrush);p.drawRoundedRect(rect.adjusted(3,3,-3,-3),3,3)
        p.end()
        if self._hovered_tab<0 or self._hovered_tab>=self.count():return
        rect=self.close_rect(self._hovered_tab);p=QPainter(self);p.setRenderHint(QPainter.Antialiasing,True);p.setPen(Qt.NoPen);p.setBrush(QColor(45,48,55,220));p.drawRoundedRect(rect,3,3);p.setPen(QColor('#e8e8e8'));p.setFont(QFont('Segoe UI',9,QFont.Bold));p.drawText(rect,Qt.AlignCenter,'X');p.end()
    def select_range(self,index):
        if index<0 or index>=self.count():return
        if self.selection_anchor<0 or self.selection_anchor>=self.count():self.selection_anchor=max(0,self.currentIndex())
        lo,hi=sorted((self.selection_anchor,index));self.selected_tabs=set(range(lo,hi+1));self.update()
    def contextMenuEvent(self,e):
        index=self.tabAt(e.pos())
        if index<0:e.accept();return
        if e.modifiers()&Qt.ControlModifier:self.select_range(index)
        elif index not in self.selected_tabs:self.selection_anchor=index;self.selected_tabs={index};self.update()
        window=self.window()
        if hasattr(window,'show_tab_context_menu'):window.show_tab_context_menu(index,e.globalPos())
        e.accept()
    def reindex_after_remove(self,index):
        self.selected_tabs={i-(i>index) for i in self.selected_tabs if i!=index and 0<=i<self.count()+1}
        self._colored_tab_indices={i-(i>index):color for i,color in self._colored_tab_indices.items() if i!=index and 0<=i<self.count()+1}
        if self.selection_anchor==index:self.selection_anchor=min(index,self.count()-1)
        elif self.selection_anchor>index:self.selection_anchor-=1
        self.update()


class MediaCanvas(QWidget):
    """Clean image surface with controls that appear only near their hit areas."""
    def __init__(self, viewer):
        super().__init__(viewer);self.viewer=viewer;self.setMouseTracking(True);self.setFocusPolicy(Qt.StrongFocus);self.setAttribute(Qt.WA_OpaquePaintEvent,True);self.hover_zone='';self.display_pixmap=QPixmap();self._render_cache=QPixmap();self._render_cache_key=None;icon_dir=getattr(viewer.parent(),'icon_dir',None);icon_path=Path(icon_dir)/'sidebyside.png' if icon_dir else None;self.sidebyside_icon=QPixmap(str(icon_path)) if icon_path and icon_path.is_file() else QPixmap();self.setMinimumSize(1,1);self.pan_offset=QPoint();self.press_pos=None;self.last_pos=QPoint();self.dragging=False;self.pending_click=None;self.click_timer=QTimer(self);self.click_timer.setSingleShot(True);self.click_timer.setInterval(220);self.click_timer.timeout.connect(self.dispatch_click)
    def resizeEvent(self,e):super().resizeEvent(e);self.prepare_frame()
    def prepare_frame(self):
        # Defer resampling and geometric transforms to paint time. Changing a
        # rotate/flip/zoom control now only updates a few scalar values instead
        # of allocating and resampling a new full-size pixmap on the UI thread.
        self._render_cache=QPixmap();self._render_cache_key=None
        self.update()
    def bottom_buttons(self):
        width=40;gap=7;count=7;left=(self.width()-(count*width+(count-1)*gap))//2;top=self.height()-55
        return [QRect(left+i*(width+gap),top,width,width) for i in range(count)]
    def paintEvent(self,e):
        p=QPainter(self);p.fillRect(self.rect(),QColor('#080a10'))
        if self.viewer.double_page:
            self.paint_double_page(p)
        else:
            self.paint_single_page(p)
        if self.hover_zone=='previous':self.paint_control(p,QRect(12,(self.height()-54)//2,48,54),'‹')
        if self.hover_zone=='next':self.paint_control(p,QRect(self.width()-60,(self.height()-54)//2,48,54),'›')
        if self.hover_zone=='metadata':self.paint_metadata(p)
        if self.hover_zone=='bottom':
            glyphs=('↶','↷','↕','↔','□','▣','Ⅱ' if self.viewer.slideshow_active else '▶')
            for index,(rect,glyph) in enumerate(zip(self.bottom_buttons(),glyphs)):
                if index==5 and not self.sidebyside_icon.isNull():self.paint_control_icon(p,rect,self.sidebyside_icon)
                else:self.paint_control(p,rect,glyph)
    def paint_single_page(self,p):
        source=self.viewer.current_frame()
        if not source.isNull():
            quarter_turn=abs(self.viewer.rotation)%180==90
            oriented_w,oriented_h=(source.height(),source.width()) if quarter_turn else (source.width(),source.height())
            area=self.rect()
            fit=min(area.width()/max(1,oriented_w),area.height()/max(1,oriented_h))
            scale=max(.05,min(8.0,fit*self.viewer.zoom));w=source.width()*scale;h=source.height()*scale
            oriented_size=QSize(max(1,round(oriented_w*scale)),max(1,round(oriented_h*scale)))
            if self.viewer.rotation%360==0 and not self.viewer.flip_h and not self.viewer.flip_v and abs(self.viewer.zoom-1.0)<1e-6 and oriented_size==source.size():
                x=(self.width()-source.width())//2+self.pan_offset.x();y=(self.height()-source.height())//2+self.pan_offset.y();p.drawPixmap(x,y,source);return
            cache_key=(source.cacheKey(),self.size(),round(scale,6),self.viewer.rotation,self.viewer.flip_h,self.viewer.flip_v)
            # Resample once when the image or view transform changes, then use
            # an integer-position blit while dragging. This prevents repeated
            # interpolation from making the image shimmer/cut during panning.
            if oriented_size.width()*oriented_size.height()<=16_000_000:
                if self._render_cache_key!=cache_key:
                    frame=QPixmap(oriented_size);frame.fill(Qt.transparent);fp=QPainter(frame);fp.setRenderHint(QPainter.SmoothPixmapTransform,True);fp.translate(frame.width()/2,frame.height()/2);fp.rotate(self.viewer.rotation);fp.scale(-1 if self.viewer.flip_h else 1,-1 if self.viewer.flip_v else 1);fp.drawPixmap(QRectF(-w/2,-h/2,w,h),source,QRectF(source.rect()));fp.end();self._render_cache=frame;self._render_cache_key=cache_key
                x=(self.width()-self._render_cache.width())//2+self.pan_offset.x();y=(self.height()-self._render_cache.height())//2+self.pan_offset.y();p.drawPixmap(x,y,self._render_cache)
            else:
                p.save();p.setRenderHint(QPainter.SmoothPixmapTransform,True);p.translate(self.width()/2+self.pan_offset.x(),self.height()/2+self.pan_offset.y());p.rotate(self.viewer.rotation);p.scale(-1 if self.viewer.flip_h else 1,-1 if self.viewer.flip_v else 1);p.drawPixmap(QRectF(-w/2,-h/2,w,h),source,QRectF(source.rect()));p.restore()
        elif self.viewer.loading:
            p.setPen(QColor('#9da2aa'));p.setFont(QFont('Segoe UI',12));p.drawText(self.rect(),Qt.AlignCenter,'Loading image…')
    def paint_double_page(self,p):
        viewer=self.viewer
        pages=(viewer.index,(viewer.index+1)%len(viewer.paths)) if viewer.paths else ()
        p.setRenderHint(QPainter.SmoothPixmapTransform,True)
        for slot,index in enumerate(pages):
            area=QRect(slot*self.width()//2,0,(slot+1)*self.width()//2-self.width()//2 if slot else self.width()//2,self.height())
            pix=viewer.page_pixmap(index)
            if pix.isNull():
                p.setPen(QColor('#9da2aa'));p.setFont(QFont('Segoe UI',11));p.drawText(area,Qt.AlignCenter,'Loading image…')
                if index!=viewer.index:viewer.request_image(viewer.paths[index],preload=True)
                continue
            scale=min(area.width()/max(1,pix.width()),area.height()/max(1,pix.height()));w=max(1,int(pix.width()*scale));h=max(1,int(pix.height()*scale));target=QRect(area.center().x()-w//2,area.center().y()-h//2,w,h);p.drawPixmap(target,pix)
        p.setPen(QPen(QColor(255,255,255,25),1));p.drawLine(self.width()//2,0,self.width()//2,self.height())
    def paint_metadata(self,p):
        viewer=self.viewer
        if not viewer.paths:return
        path=Path(viewer.paths[viewer.index]);rect=QRect(16,12,min(680,max(220,self.width()-32)),58)
        p.save();p.setPen(QPen(QColor(255,255,255,55),1));p.setBrush(QColor(0,0,0,175));p.drawRoundedRect(rect,6,6)
        name=path.name or str(path);suffix=path.suffix[1:].upper() or 'Unknown';resolution=f'{viewer.original_resolution.width()} × {viewer.original_resolution.height()}' if viewer.original_resolution.isValid() else 'Loading resolution…';size=sizefmt(viewer.current_file_size) if viewer.current_file_size is not None else 'Size unavailable'
        font=QFont('Segoe UI',10);p.setFont(font);metrics=QFontMetricsF(font);p.setPen(QColor('#f2f2f2'))
        first=f'{name}  •  {suffix}';second=f'Resolution: {resolution}    •    Size: {size}'
        p.drawText(QRectF(rect.x()+12,rect.y()+6,rect.width()-24,22),Qt.AlignLeft|Qt.AlignVCenter,metrics.elidedText(first,Qt.ElideMiddle,rect.width()-24))
        p.setPen(QColor('#c8c8c8'));p.drawText(QRectF(rect.x()+12,rect.y()+30,rect.width()-24,20),Qt.AlignLeft|Qt.AlignVCenter,metrics.elidedText(second,Qt.ElideRight,rect.width()-24));p.restore()
    @staticmethod
    def paint_control(p,rect,text):
        p.setPen(QPen(QColor(255,255,255,75),1));p.setBrush(QColor(0,0,0,150));p.drawRoundedRect(rect,7,7);p.setPen(QColor(255,255,255,230));font=QFont('Segoe UI' if len(text)>1 else 'Segoe UI Symbol',10 if len(text)>1 else 20);p.setFont(font);bounds=QFontMetricsF(font).boundingRect(text);p.drawText(QPointF(rect.center().x()-bounds.width()/2-bounds.x(),rect.center().y()-bounds.height()/2-bounds.y()),text)
    def paint_control_icon(self,p,rect,icon):
        self.paint_control(p,rect,'');p.drawPixmap(rect.adjusted(9,9,-9,-9),icon)
    def mouseMoveEvent(self,e):
        pos=e.position().toPoint()
        self.viewer.note_pointer_activity()
        if self.press_pos is not None and e.buttons() & Qt.LeftButton:
            if not self.dragging and (pos-self.press_pos).manhattanLength()>=QApplication.startDragDistance():self.dragging=True;self.setCursor(Qt.ClosedHandCursor)
            if self.dragging:self.pan_offset+=pos-self.last_pos;self.last_pos=pos;self.update();e.accept();return
        center_left=self.width()*.25<=pos.x()<=self.width()*.75
        zone='metadata' if pos.y()<=82 else ('bottom' if pos.y()>=self.height()-72 and center_left else ('previous' if pos.x()<80 else ('next' if pos.x()>self.width()-80 else '')))
        if zone!=self.hover_zone:self.hover_zone=zone;self.update()
    def leaveEvent(self,e):self.hover_zone='';self.update();super().leaveEvent(e)
    def keyPressEvent(self,e):
        if self.viewer.handle_viewer_key(e):e.accept();return
        amount=max(24,round(min(self.width(),self.height())*.06))
        offsets={Qt.Key_W:QPoint(0,amount),Qt.Key_A:QPoint(amount,0),Qt.Key_S:QPoint(0,-amount),Qt.Key_D:QPoint(-amount,0)}
        if e.key() in offsets:self.pan_offset+=offsets[e.key()];self.update();e.accept();return
        super().keyPressEvent(e)
    def keyReleaseEvent(self,e):
        self.viewer.handle_viewer_key_release(e);super().keyReleaseEvent(e)
    def mousePressEvent(self,e):
        if e.button()==Qt.RightButton:self.viewer.close();return
        if e.button()!=Qt.LeftButton:return
        pos=e.position().toPoint();is_control=pos.x()<80 or pos.x()>self.width()-80 or (pos.y()>=self.height()-72 and self.width()*.25<=pos.x()<=self.width()*.75)
        self.press_pos=pos if not is_control else None;self.last_pos=pos;self.dragging=False
    def dispatch_click(self):
        pos=self.pending_click;self.pending_click=None
        if pos is None:return
        if pos.y()>=self.height()-72 and self.width()*.25<=pos.x()<=self.width()*.75:
            for i,rect in enumerate(self.bottom_buttons()):
                if rect.contains(pos):
                    if i==4:self.viewer.fit_to_screen()
                    elif i==5:self.viewer.toggle_double_page()
                    elif i==6:self.viewer.toggle_slideshow()
                    else:self.viewer.transform_image(i)
                    return
        if pos.x()<80:self.viewer.step(-1)
        elif pos.x()>self.width()-80:self.viewer.step(1)
    def mouseReleaseEvent(self,e):
        if e.button()!=Qt.LeftButton:return
        pos=e.position().toPoint();was_dragging=self.dragging;self.press_pos=None;self.dragging=False;self.setCursor(Qt.ArrowCursor)
        if was_dragging:return
        if getattr(self,'suppress_control_release',False):self.suppress_control_release=False;return
        self.pending_click=pos;self.dispatch_click()
    def mouseDoubleClickEvent(self,e):
        if e.button()!=Qt.LeftButton:return
        pos=e.position().toPoint()
        control=(pos.y()>=self.height()-72 and self.width()*.25<=pos.x()<=self.width()*.75 and any(r.contains(pos) for r in self.bottom_buttons())) or pos.x()<80 or pos.x()>self.width()-80
        self.click_timer.stop();self.pending_click=None
        if control:
            # Qt delivers a double-click event instead of the second press.
            # Treat it as another button activation, then consume its release.
            self.suppress_control_release=True;self.pending_click=pos;self.dispatch_click();return
        self.viewer.zoom=2.0 if self.viewer.zoom<=1.01 else 1.0;self.pan_offset=QPoint();self.prepare_frame()
    def wheelEvent(self,e):
        delta=e.angleDelta().y()
        if not delta:delta=e.pixelDelta().y()
        if not delta:return
        factor=1.15**(delta/120.0);old_zoom=self.viewer.zoom;new_zoom=max(.1,min(8.0,old_zoom*factor))
        if abs(new_zoom-old_zoom)<1e-9:e.accept();return
        ratio=new_zoom/old_zoom;anchor=e.position();center=QPointF(self.width()/2,self.height()/2)
        # Preserve the image point under the cursor as the scale changes.
        old_vector=anchor-center-QPointF(self.pan_offset)
        new_pan=anchor-center-old_vector*ratio
        self.viewer.zoom=new_zoom;self.pan_offset=QPoint(round(new_pan.x()),round(new_pan.y()));self.prepare_frame();e.accept()


class MediaViewer(QMainWindow):
    def __init__(self,paths,index=0,parent=None,slideshow_interval_ms=5000):
        super().__init__(parent,Qt.Window);self.paths=list(paths);self.index=max(0,min(int(index),len(self.paths)-1)) if self.paths else -1;self.rotation=0;self.flip_h=False;self.flip_v=False;self.zoom=1.0;self.movie=None;self.full_image=QImage();self.static_pixmap=QPixmap();self.fit_pixmap=QPixmap();self.original_resolution=QSize();self.double_page=False;self.double_page_path=None;self.double_page_frame=QPixmap();self.loading=False;self.current_file_size=None;self.image_cache=OrderedDict();self.image_cache_bytes=0;self.image_cache_limit=256*1024*1024;self.cache_max_dimension=8000;self.cache_max_file_bytes=100*1024*1024;self.pending_image_paths=set();self.image_load_futures={};self.pending_delete_path=None;self.navigation_key=None;self.navigation_started=0.0;self.slideshow_active=False;self.slideshow_timer=QTimer(self);self.slideshow_timer.setInterval(max(250,int(slideshow_interval_ms)));self.slideshow_timer.timeout.connect(lambda:self.step(1));app=QApplication.instance();self.load_bridge=getattr(app,'_viewer_image_load_bridge',None)
        if self.load_bridge is None:self.load_bridge=ViewerImageLoadBridge(app);app._viewer_image_load_bridge=self.load_bridge
        self.pointer_hide_timer=QTimer(self);self.pointer_hide_timer.setSingleShot(True);self.pointer_hide_timer.setInterval(1000);self.pointer_hide_timer.timeout.connect(self.hide_slideshow_cursor)
        self.load_bridge.loaded.connect(self.on_image_loaded);self.setWindowTitle('Magellan Viewer');self.canvas=MediaCanvas(self);self.canvas.setContentsMargins(0,0,0,0);self.setContentsMargins(0,0,0,0);self.setCentralWidget(self.canvas);self.setStyleSheet('QMainWindow{background:#080a10;border:0px}')
    def current_frame(self):
        if self.movie is not None:return self.movie.currentPixmap()
        if self.zoom<=1.0 and not self.fit_pixmap.isNull():return self.fit_pixmap
        if self.static_pixmap.isNull() and not self.full_image.isNull():self.static_pixmap=QPixmap.fromImage(self.full_image)
        return self.static_pixmap
    def page_pixmap(self,index):
        if not self.paths or not 0<=index<len(self.paths):return QPixmap()
        if index==self.index:
            frame=self.current_frame()
            if not frame.isNull():return frame
        path=self.paths[index]
        if self.double_page_path==path and not self.double_page_frame.isNull():return self.double_page_frame
        entry=self.image_cache.get(path)
        if entry is not None:return entry[1]
        return QPixmap()
    @staticmethod
    def pixmap_bytes(pixmap):return pixmap.width()*pixmap.height()*max(1,(pixmap.depth()+7)//8)
    def cache_entry_bytes(self,entry):
        full,fit=entry[:2]
        return full.sizeInBytes()+self.pixmap_bytes(fit)
    def cache_window_contains(self,path):
        count=len(self.paths)
        if not count or not 0<=self.index<count:return False
        return any(self.paths[(self.index+offset)%count]==path for offset in (-1,0,1))
    def stop_movie(self):
        movie=self.movie
        if movie is None:return
        self.movie=None;movie.stop()
        # Detaching QMovie's device closes animated GIF/WebP files immediately.
        movie.setDevice(None);movie.setFileName('');movie.deleteLater()
        QApplication.sendPostedEvents(movie,QEvent.DeferredDelete)
    def release_current_image(self):
        self.stop_movie();self.full_image=QImage();self.static_pixmap=QPixmap();self.fit_pixmap=QPixmap()
        self.double_page_path=None;self.double_page_frame=QPixmap();self.canvas._render_cache=QPixmap();self.canvas._render_cache_key=None
    def load_current(self):
        self.stop_movie()
        if not self.paths:
            self.index=-1;self.full_image=QImage();self.static_pixmap=QPixmap();self.fit_pixmap=QPixmap();self.original_resolution=QSize();self.current_file_size=None;self.loading=False;self.canvas.prepare_frame();return
        self.index=max(0,min(self.index,len(self.paths)-1))
        self.full_image=QImage();self.static_pixmap=QPixmap();self.fit_pixmap=QPixmap();self.original_resolution=QSize();self.double_page_path=None;self.double_page_frame=QPixmap();self.loading=False;self.rotation=0;self.flip_h=False;self.flip_v=False;self.zoom=1.0;self.canvas.pan_offset=QPoint();path=self.paths[self.index]
        self.current_file_size=None
        if Path(path).suffix.casefold() in ('.gif','.webp'):
            movie=QMovie(str(path));movie.setCacheMode(QMovie.CacheNone)
            if movie.isValid():
                self.movie=movie;movie.frameChanged.connect(self.canvas.prepare_frame);movie.start()
                if not self.original_resolution.isValid() and not movie.currentPixmap().isNull():self.original_resolution=movie.currentPixmap().size()
                self.request_file_size(path)
            else:
                entry=self.image_cache.get(path)
                if entry is not None:self.image_cache.move_to_end(path);self.full_image,self.fit_pixmap=entry[:2];self.static_pixmap=QPixmap();self.original_resolution=entry[2] if len(entry)>2 and entry[2].isValid() else self.full_image.size();self.current_file_size=entry[3] if len(entry)>3 else None
                else:self.loading=True;self.request_image(path)
        else:
            entry=self.image_cache.get(path)
            if entry is not None:self.image_cache.move_to_end(path);self.full_image,self.fit_pixmap=entry[:2];self.static_pixmap=QPixmap();self.original_resolution=entry[2] if len(entry)>2 and entry[2].isValid() else self.full_image.size();self.current_file_size=entry[3] if len(entry)>3 else None
            else:self.loading=True;self.request_image(path)
        self.setWindowTitle('Magellan Viewer');self.canvas.prepare_frame();self.preload_neighbours()
    def viewer_preview_size(self):
        size=QSize(self.canvas.size())
        if size.width()<2 or size.height()<2:
            screen=QApplication.primaryScreen();size=screen.size() if screen else QSize(1920,1080)
        return size
    def submit_full_image_decode(self,path,preload=False):
        preview_size=self.viewer_preview_size()
        future=VIEWER_IMAGE_POOL.submit(decode_viewer_image,path,preview_size,self.cache_max_dimension,self.cache_max_file_bytes,preload)
        self.image_load_futures[path]=future
        def completed(result,p=path):
            try:data=result.result()
            except Exception:
                logger.exception('Viewer image load failed for %s',p)
                data=(QImage(),QImage(),False,QSize())
            self.load_bridge.loaded.emit(p,data)
        future.add_done_callback(completed)
    def request_image(self,path,preload=False):
        if path in self.pending_image_paths:return
        self.pending_image_paths.add(path);self.submit_full_image_decode(path,preload=preload)
    def request_file_size(self,path):
        if path in self.pending_image_paths:return
        self.pending_image_paths.add(path)
        future=VIEWER_IMAGE_POOL.submit(lambda p=path:Path(p).stat().st_size);self.image_load_futures[path]=future
        def completed(result,p=path):
            try:data={'stage':'metadata','size':result.result()}
            except OSError:data={'stage':'metadata','size':None}
            except Exception:
                logger.debug('Could not read image size for %s',p,exc_info=True);data={'stage':'metadata','size':None}
            self.load_bridge.loaded.emit(p,data)
        future.add_done_callback(completed)
    def preload_neighbours(self):
        if not self.paths or not 0<=self.index<len(self.paths):return
        current=self.paths[self.index]
        # Preserve the proven 1.3 behavior: prepare both adjacent images so a
        # swipe in either direction normally uses the in-memory cache.
        for offset in (1,-1):
            path=self.paths[(self.index+offset)%len(self.paths)]
            if path!=current and path not in self.image_cache and path not in self.pending_image_paths:self.request_image(path,preload=True)
    def on_image_loaded(self,path,image):
        if not self.paths:return
        self.pending_image_paths.discard(path);self.image_load_futures.pop(path,None)
        if isinstance(image,dict) and image.get('stage')=='metadata':
            if path==self.paths[self.index]:self.current_file_size=image.get('size');self.canvas.update()
            return
        cacheable=True;source_size=QSize()
        file_size=None
        if isinstance(image,tuple):
            full_image,fit_image=image[:2]
            if len(image)>2:cacheable=bool(image[2])
            if len(image)>3 and isinstance(image[3],QSize):source_size=image[3]
            if len(image)>4 and isinstance(image[4],int):file_size=image[4]
        else:full_image=fit_image=image
        if not full_image.isNull():
            fit_pixmap=QPixmap.fromImage(fit_image);source_size=source_size if source_size.isValid() else full_image.size();entry=(full_image,fit_pixmap,source_size,file_size)
            entry_bytes=self.cache_entry_bytes(entry)
            if cacheable and entry_bytes<=self.image_cache_limit and self.cache_window_contains(path):
                previous=self.image_cache.pop(path,None)
                if previous:self.image_cache_bytes-=self.cache_entry_bytes(previous)
                self.image_cache[path]=entry;self.image_cache_bytes+=entry_bytes
            while self.image_cache_bytes>self.image_cache_limit and len(self.image_cache)>1:
                old_path,old_entry=self.image_cache.popitem(last=False);self.image_cache_bytes-=self.cache_entry_bytes(old_entry)
            if path==self.paths[self.index]:
                self.full_image,self.fit_pixmap=entry[:2];self.static_pixmap=QPixmap();self.original_resolution=source_size;self.current_file_size=file_size;self.loading=False;self.canvas.prepare_frame();self.preload_neighbours()
            elif self.double_page and self.paths and path==self.paths[(self.index+1)%len(self.paths)]:self.double_page_path=path;self.double_page_frame=fit_pixmap;self.canvas.update()
        elif path==self.paths[self.index]:
            self.loading=False;self.canvas.prepare_frame();self.preload_neighbours()
    def showEvent(self,e):super().showEvent(e);self.load_current()
    def step(self,amount):
        if not self.paths:return
        self.index=(self.index+amount)%len(self.paths);self.load_current()
    def transform_image(self,button):
        if button==0:self.rotation=(self.rotation-90)%360
        elif button==1:self.rotation=(self.rotation+90)%360
        elif button==2:self.flip_v=not self.flip_v
        else:self.flip_h=not self.flip_h
        self.canvas.prepare_frame()
    def fit_to_screen(self):
        self.zoom=1.0;self.canvas.pan_offset=QPoint();self.canvas.prepare_frame()
    def toggle_double_page(self):
        self.double_page=not self.double_page;self.fit_to_screen();self.canvas.update()
        if self.double_page and self.paths:self.request_image(self.paths[(self.index+1)%len(self.paths)],preload=True)
    def delete_current_to_trash(self):
        if not self.paths or self.index<0:return
        path=self.paths[self.index];future=self.image_load_futures.get(path)
        if future is not None and not future.done():
            if self.pending_delete_path==path:return
            self.pending_delete_path=path;self.slideshow_timer.stop();QTimer.singleShot(40,lambda p=path:self.finish_pending_delete(p));return
        self.delete_path_to_trash(path)
    def finish_pending_delete(self,path):
        if self.pending_delete_path!=path:return
        future=self.image_load_futures.get(path)
        if future is not None and not future.done():QTimer.singleShot(40,lambda p=path:self.finish_pending_delete(p));return
        self.pending_delete_path=None;self.delete_path_to_trash(path)
    def delete_path_to_trash(self,path):
        if path not in self.paths:return
        index=self.paths.index(path);was_current=index==self.index;parent=self.parent()
        if was_current:self.release_current_image()
        if parent is None or not parent.send_paths_to_trash([path]):
            if was_current:self.load_current()
            return
        cached=self.image_cache.pop(path,None)
        if cached is not None:self.image_cache_bytes=max(0,self.image_cache_bytes-self.cache_entry_bytes(cached))
        self.double_page_path=None;self.double_page_frame=QPixmap();self.paths.pop(index)
        if not self.paths:self.index=-1;self.close();return
        if index<self.index:self.index-=1
        elif index==self.index and self.index>=len(self.paths):self.index=len(self.paths)-1
        if was_current:self.load_current()
    def toggle_slideshow(self):
        self.slideshow_active=not self.slideshow_active
        if self.slideshow_active:self.slideshow_timer.start();self.note_pointer_activity()
        else:self.slideshow_timer.stop();self.pointer_hide_timer.stop();self.canvas.unsetCursor()
        self.canvas.update()
    def hide_slideshow_cursor(self):
        if self.slideshow_active:self.canvas.setCursor(Qt.BlankCursor)
    def note_pointer_activity(self):
        if self.slideshow_active:self.canvas.unsetCursor();self.pointer_hide_timer.start()
    def navigate_key(self,direction,event):
        key=event.key()
        if not event.isAutoRepeat() or self.navigation_key!=key:self.navigation_key=key;self.navigation_started=time.monotonic()
        elapsed=max(0,time.monotonic()-self.navigation_started-1.0)
        step=1 if not event.isAutoRepeat() else min(4,1+int(elapsed/.6))
        self.step(direction*step)
    def handle_viewer_key_release(self,event):
        if event.key()==self.navigation_key:self.navigation_key=None
    def handle_viewer_key(self,e):
        if e.key()==Qt.Key_Z and e.modifiers()&Qt.ControlModifier:
            parent=self.parent()
            if parent is not None and hasattr(parent,'undo_last_action'):parent.undo_last_action()
        elif e.key() in (Qt.Key_Escape,Qt.Key_Up):self.close()
        elif e.key()==Qt.Key_F11:
            self.showNormal() if self.isFullScreen() else self.showFullScreen()
        elif e.key() in (Qt.Key_Left,Qt.Key_Q):self.navigate_key(-1,e)
        elif e.key() in (Qt.Key_Right,Qt.Key_E):self.navigate_key(1,e)
        elif e.key()==Qt.Key_R:self.transform_image(1)
        elif e.key()==Qt.Key_F:self.transform_image(3)
        elif e.key()==Qt.Key_G:self.fit_to_screen()
        elif e.key()==Qt.Key_H:self.toggle_slideshow()
        elif e.key()==Qt.Key_Delete:self.delete_current_to_trash()
        elif e.key() in (Qt.Key_Minus,Qt.Key_Equal,Qt.Key_Plus):
            self.zoom=max(.05,min(8.0,self.zoom*(1.15 if e.key() in (Qt.Key_Equal,Qt.Key_Plus) else 1/1.15)));self.canvas.prepare_frame()
        else:return False
        return True
    def keyPressEvent(self,e):
        if self.handle_viewer_key(e):e.accept();return
        super().keyPressEvent(e)
    def keyReleaseEvent(self,e):
        self.handle_viewer_key_release(e)
        super().keyReleaseEvent(e)
    def closeEvent(self,e):
        self.slideshow_timer.stop();self.pointer_hide_timer.stop();self.release_current_image()
        for future in self.image_load_futures.values():future.cancel()
        self.image_load_futures.clear();self.pending_image_paths.clear();self.image_cache.clear();self.image_cache_bytes=0
        self.paths.clear();self.index=-1;self.pending_delete_path=None
        parent=self.parent()
        if parent is not None and hasattr(parent,'viewer_closed'):
            try:parent.viewer_closed(self)
            except RuntimeError:logger.debug('Viewer closed after its parent was deleted')
        super().closeEvent(e)


class Explorer(QMainWindow):
    def __init__(self):
        super().__init__();self._closing=False;self.viewer=None;self.setWindowTitle('Magellan');icon_root=Path(sys._MEIPASS) if getattr(sys,'frozen',False) else Path(__file__).resolve().parent.parent;self.icon_root=icon_root;self.icon_dir=icon_root/'Alternative Icons';icon_path=icon_root/'MainIcon.ico';self.app_icon=QIcon(str(icon_path)) if icon_path.is_file() else make_boat_icon();self.setWindowIcon(self.app_icon);QApplication.setWindowIcon(self.app_icon);self.resize(1900,1000);self.window_cycle=0;self.in_favorites_view=False;self.setStyleSheet(QSS)
        self.hover_details_enabled=True;self.hover_details_delay=500;self.hover_details_placement='top';self.slideshow_interval_ms=5000;self.wheel_scroll_rows=1;self.smooth_wheel_scroll=False;self.scroll_test_logging_enabled=False;self.auto_refresh_folders=False;self.folder_refresh_interval_s=30;self._tab_drag_active=False;self.auto_hide_main=False;self.auto_hide_tabs=False;self.auto_hide_tree=False;self.main_hide_delay=900;self.tree_hide_delay=900;self.right_controls_auto_hide=True;self.right_controls_hide_delay=500;self.favorite_organize_mode=False;self.favorites_navigation_root=None;self.favorite_return_group=None;self.recursive_search_enabled=False;self.recursive_search_results=None;self.recursive_search_result_query='';self.recursive_search_generation=0;self.recursive_search_jobs=[];self.google_lens_query='';self.disk_thumbnail_cache_limit_enabled=False;self.disk_thumbnail_cache_limit_mib=4096;self._disk_cache_trim_lock=threading.Lock();self._disk_cache_trim_future=None
        self.current=Path.home();self.items=[];self.sort='name';self.desc=False;self.columns=5;self.thumb=260;self.mode='vertical';self.thumbnail_cache_enabled=True;self.thumbnail_cache_limit_mib=256;self.favorites=set();self.favorite_groups=[];self.active_favorite_group=None;self.tree_favorites=set();self.tree_expanded_paths=set();self.tree_selected_path='';self.tree_scroll_value=0;self.tree_scroll_anchor_path='';self.tree_scroll_anchor_offset=0;self._tree_scroll_restore_pending=False;self.pinned_folders=set();self._pinned_folders_generation=0;self.pinned_thumbs={};self.pinned_folder_sizes={};self.pinned_folder_sizes_checked={};self.custom={};self.startup_folder=None;self.tabs=[];self.active_tab_index=-1;self.saved_tabs=[];self.saved_active_tab=0;self.scan_generation=0;self.scan_jobs=[];self.return_to_favorites=False;self.favorites_return_path=None;self.scroll_positions={};self.folder_sort_settings={};self.folder_manual_orders={};self.manual_order_created={};self._suppress_scroll_save=False;self.window_mode='fullscreen';self.last_deletion_undo=None;self.cache_dir=Path(QStandardPaths.writableLocation(QStandardPaths.AppDataLocation));self.cache_dir.mkdir(parents=True,exist_ok=True);self.settings_file=self.cache_dir/'settings.json';self.install_dir=Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent;self.persistent_thumb_dir=self.install_dir/'thumbnail_cache';self.persistent_thumb_dir.mkdir(parents=True,exist_ok=True);self.load_settings();self.persistent_thumb_dir=Path(getattr(self,'thumbnail_cache_dir',self.persistent_thumb_dir));self.persistent_thumb_dir.mkdir(parents=True,exist_ok=True);self.back_target=Path(self.startup_folder) if self.startup_folder else Path(self.current);self.image_search_provider='saucenao';self.image_search_mode=False;self.scan=None;self.work=None;self.build();self.grid.set_cache_limit_mib(self.thumbnail_cache_limit_mib);self.grid.persistent_folder_cache_dir=self.persistent_thumb_dir;self.grid.folder_cache.update(self.pinned_thumbs);self.grid.folder_size_cache.update(self.pinned_folder_sizes);self.grid.folder_size_cache_checked_at.update(self.pinned_folder_sizes_checked)
        self._folder_refresh_scan=None;self._folder_refresh_context=None;self._folder_refresh_pending=None;self._folder_refresh_generation=0
        self.folder_refresh_timer=QTimer(self);self.folder_refresh_timer.timeout.connect(self.auto_refresh_folder_page);self.sync_folder_refresh_timer()
        self.folder_refresh_apply_timer=QTimer(self);self.folder_refresh_apply_timer.setSingleShot(True);self.folder_refresh_apply_timer.setInterval(400);self.folder_refresh_apply_timer.timeout.connect(self.apply_pending_folder_refresh)
        self._folder_thumb_bridge=FolderThumbnailBridge(self);self._folder_thumb_bridge.completed.connect(self.handle_pinned_thumbnail_ready)
        self.restore_tabs()
        if self.disk_thumbnail_cache_limit_enabled:self.schedule_disk_thumbnail_cache_trim()
        for _pf in list(self.pinned_folders):
            _cached=self.grid.load_persistent_folder_thumb(_pf)
            if _cached:
                self.pinned_thumbs[_pf]=_cached;self.grid.folder_cache[_pf]=_cached
            elif Path(_pf).is_dir():
                _fut=self.grid.folder_pool.submit(self.grid.resolve_folder_thumbnail,_pf)
                def _warm_done(f,pp=_pf):
                    try:
                        _src=f.result()
                        if _src:self._folder_thumb_bridge.completed.emit(pp,_src)
                    except Exception:
                        if not f.cancelled():logger.exception('Could not warm pinned folder thumbnail for %s',pp)
                _fut.add_done_callback(_warm_done)
        self.apply_window_state();self.apply_auto_hide()
    def load_settings(self):
        self.log_storage_mib=8
        configure_logging(self.cache_dir/'logs',self.log_storage_mib)
        try:
            d=self._sanitize_settings(json.loads(self.settings_file.read_text()));self.favorites=set(d.get('favorites',[]));self.favorite_groups=d.get('favorite_groups',[]);self.tree_favorites=set(d.get('tree_favorites',[]));self.tree_expanded_paths=set(d.get('tree_expanded_paths',[]));self.tree_selected_path=d.get('tree_selected_path','');self.tree_scroll_value=d.get('tree_scroll_value',0);self.tree_scroll_anchor_path=d.get('tree_scroll_anchor_path','');self.tree_scroll_anchor_offset=d.get('tree_scroll_anchor_offset',0);self.custom=d.get('custom',{});self.pinned_folders=set(d.get('pinned_folders',[]));self._pinned_folders_generation+=1;self.pinned_thumbs=d.get('pinned_thumbs',{});self.pinned_folder_sizes=d.get('pinned_folder_sizes',{});self.pinned_folder_sizes_checked=d.get('pinned_folder_sizes_checked',{});self.image_search_provider=d.get('image_search_provider','saucenao');self.saucenao_api_key=d.get('saucenao_api_key','');self.window_mode=d.get('window_mode','fullscreen');self.saved_geometry=d.get('geometry');self.hover_details_enabled=bool(d.get('hover_details_enabled',True));self.hover_details_delay=max(0,min(3000,int(d.get('hover_details_delay',500))));_placement=d.get('hover_details_placement','top');self.hover_details_placement='bottom' if _placement=='below' else (_placement if _placement in ('top','bottom','above') else 'top');self.thumbnail_cache_enabled=bool(d.get('thumbnail_cache_enabled',True));self.thumbnail_cache_limit_mib=max(16,min(8192,int(d.get('thumbnail_cache_limit_mib',256))));self.auto_hide_main=bool(d.get('auto_hide_main',False));self.auto_hide_tabs=bool(d.get('auto_hide_tabs',False));self.auto_hide_tree=bool(d.get('auto_hide_tree',False));self.main_hide_delay=max(0,min(10000,int(d.get('main_hide_delay',900))));self.tree_hide_delay=max(0,min(10000,int(d.get('tree_hide_delay',900))));self.has_saved_tabs='tabs' in d;self.saved_tabs=d.get('tabs',[]);self.saved_active_tab=max(0,int(d.get('active_tab',0)));self.folder_sort_settings=d.get('folder_sort_settings',{});self.folder_manual_orders=d.get('folder_manual_orders',{});self.manual_order_created=d.get('manual_order_created',{});self.thumbnail_cache_dir=d.get('thumbnail_cache_dir') or str(self.persistent_thumb_dir)
            self.slideshow_interval_ms=max(250,min(60000,int(d.get('slideshow_interval_ms',5000))))
            self.wheel_scroll_rows=max(.1,min(12.0,float(d.get('wheel_scroll_rows',1))))
            self.smooth_wheel_scroll=bool(d.get('smooth_wheel_scroll',False))
            self.scroll_test_logging_enabled=bool(d.get('scroll_test_logging_enabled',False))
            self.auto_refresh_folders=bool(d.get('auto_refresh_folders',False))
            self.folder_refresh_interval_s=max(1,min(3600,int(d.get('folder_refresh_interval_s',30))))
            self.log_storage_mib=max(1,min(64,int(d.get('log_storage_mib',8))))
            self.recursive_search_enabled=bool(d.get('recursive_search_enabled',False))
            self.google_lens_query=d.get('google_lens_query','') if isinstance(d.get('google_lens_query',''),str) else ''
            self.disk_thumbnail_cache_limit_enabled=bool(d.get('disk_thumbnail_cache_limit_enabled',False))
            self.disk_thumbnail_cache_limit_mib=max(256,min(262144,int(d.get('disk_thumbnail_cache_limit_mib',4096))))
            configure_logging(self.cache_dir/'logs',self.log_storage_mib)
            self.right_controls_auto_hide=bool(d.get('right_controls_auto_hide',True));self.right_controls_hide_delay=max(0,min(10000,int(d.get('right_controls_hide_delay',500))))
            grouped_paths={self._normalized_path_key(path) for group in self.favorite_groups for path in group.get('folders',[])};self.favorites={path for path in self.favorites if self._normalized_path_key(path) not in grouped_paths}
            startup=d.get('startup'); last=d.get('last');
            if startup and Path(startup).is_dir():
                self.startup_folder=startup; self.current=Path(startup)
            elif last and Path(last).is_dir(): self.current=Path(last)
        except FileNotFoundError:
            self.window_mode='fullscreen';self.saved_geometry=None
        except Exception:
            logger.exception('Could not load settings; using defaults for window state')
            self.window_mode='fullscreen';self.saved_geometry=None
    @staticmethod
    def _sanitize_settings(data):
        if not isinstance(data,dict):raise ValueError('Settings root must be a JSON object')
        def string_list(key):
            value=data.get(key,[])
            return [item for item in value if isinstance(item,str)] if isinstance(value,list) else []
        data['favorites']=string_list('favorites');data['tree_favorites']=string_list('tree_favorites');data['pinned_folders']=string_list('pinned_folders')
        groups=data.get('favorite_groups',[]);clean_groups=[]
        if isinstance(groups,list):
            for group in groups:
                if not isinstance(group,dict):continue
                group['id']=str(group.get('id',''));group['name']=str(group.get('name','Group'))
                members=group.get('folders',[]);group['folders']=[path for path in members if isinstance(path,str)] if isinstance(members,list) else []
                clean_groups.append(group)
        data['favorite_groups']=clean_groups
        for key in ('custom','pinned_thumbs'):
            value=data.get(key,{})
            data[key]={k:v for k,v in value.items() if isinstance(k,str) and isinstance(v,str)} if isinstance(value,dict) else {}
        sizes=data.get('pinned_folder_sizes',{})
        clean_sizes={}
        if isinstance(sizes,dict):
            for path,value in sizes.items():
                if not isinstance(path,str):continue
                try:
                    parsed=int(value)
                    if not isinstance(value,bool) and parsed>=0:clean_sizes[path]=parsed
                except (TypeError,ValueError,OverflowError):continue
        data['pinned_folder_sizes']=clean_sizes
        checked=data.get('pinned_folder_sizes_checked',{});clean_checked={}
        if isinstance(checked,dict):
            for path,value in checked.items():
                if not isinstance(path,str) or isinstance(value,bool):continue
                try:
                    parsed=float(value)
                    if parsed>=0 and parsed<1e12:clean_checked[path]=parsed
                except (TypeError,ValueError,OverflowError):continue
        data['pinned_folder_sizes_checked']=clean_checked
        tabs=data.get('tabs',[]);clean_tabs=[]
        if isinstance(tabs,list):
            for tab in tabs:
                if not isinstance(tab,dict) or not isinstance(tab.get('path'),str) or not tab['path'].strip():continue
                tab['kind']=tab.get('kind') if tab.get('kind') in ('folder','farview') else 'folder'
                tab['sort']=tab.get('sort') if tab.get('sort') in ('name','mtime','ctime','type','size','random','manual') else 'name'
                tab['desc']=bool(tab.get('desc',False));tab['unsorted']=bool(tab.get('unsorted',True))
                color=tab.get('color','')
                tab['color']=color if isinstance(color,str) and re.fullmatch(r'#[0-9a-fA-F]{6}',color) else ''
                try:tab['scroll']=max(0,int(tab.get('scroll',0)))
                except (TypeError,ValueError,OverflowError):tab['scroll']=0
                positions=tab.get('positions',{});clean_positions={}
                if isinstance(positions,dict):
                    for path,value in positions.items():
                        if not isinstance(path,str):continue
                        try:clean_positions[path]=max(0,int(value))
                        except (TypeError,ValueError,OverflowError):continue
                tab['positions']=clean_positions
                selection=tab.get('selection',[]);tab['selection']=[path for path in selection if isinstance(path,str)] if isinstance(selection,list) else []
                source_order=tab.get('source_order',[]);tab['source_order']=[path for path in source_order if isinstance(path,str)][:100000] if isinstance(source_order,list) else []
                tab['view_id']=str(tab.get('view_id') or '')[:64]
                clean_tabs.append(tab)
        data['tabs']=clean_tabs
        folder_sorts=data.get('folder_sort_settings',{});clean_folder_sorts={}
        if isinstance(folder_sorts,dict):
            for folder,value in folder_sorts.items():
                if not isinstance(folder,str) or not folder.strip() or not isinstance(value,dict):continue
                sort=value.get('sort')
                if sort not in ('name','mtime','ctime','type','size','random','manual'):continue
                key=os.path.normcase(os.path.normpath(os.path.abspath(folder)))
                clean_folder_sorts[key]={'sort':sort,'desc':bool(value.get('desc',False))}
                if len(clean_folder_sorts)>5000:clean_folder_sorts.pop(next(iter(clean_folder_sorts)))
        data['folder_sort_settings']=clean_folder_sorts
        manual_orders=data.get('folder_manual_orders',{});clean_manual_orders={}
        if isinstance(manual_orders,dict):
            for key,order in manual_orders.items():
                if isinstance(key,str) and key and isinstance(order,list):clean_manual_orders[key]=[v for v in order[:100000] if isinstance(v,str)]
        data['folder_manual_orders']=clean_manual_orders
        created=data.get('manual_order_created',{});clean_created={}
        if isinstance(created,dict):
            for key,value in created.items():
                if not isinstance(key,str) or isinstance(value,bool):continue
                try:stamp=float(value)
                except (TypeError,ValueError,OverflowError):continue
                if 0<=stamp<1e12:clean_created[key]=stamp
        data['manual_order_created']=clean_created
        data['tree_expanded_paths']=string_list('tree_expanded_paths')[:10000]
        selected_tree=data.get('tree_selected_path','')
        data['tree_selected_path']=selected_tree if isinstance(selected_tree,str) else ''
        try:data['tree_scroll_value']=max(0,int(data.get('tree_scroll_value',0)))
        except (TypeError,ValueError,OverflowError):data['tree_scroll_value']=0
        anchor=data.get('tree_scroll_anchor_path','');data['tree_scroll_anchor_path']=anchor if isinstance(anchor,str) else ''
        try:data['tree_scroll_anchor_offset']=int(data.get('tree_scroll_anchor_offset',0))
        except (TypeError,ValueError,OverflowError):data['tree_scroll_anchor_offset']=0
        cache_path=data.get('thumbnail_cache_dir')
        data['thumbnail_cache_dir']=cache_path if isinstance(cache_path,str) and cache_path.strip() else ''
        for key in ('startup','last','geometry','saucenao_api_key','google_lens_query'):
            if data.get(key) is not None and not isinstance(data.get(key),str):data[key]=None if key not in ('saucenao_api_key','google_lens_query') else ''
        for key,default in {'active_tab':0,'hover_details_delay':500,'thumbnail_cache_limit_mib':256,'disk_thumbnail_cache_limit_mib':4096,'main_hide_delay':900,'tree_hide_delay':900,'slideshow_interval_ms':5000,'log_storage_mib':8,'right_controls_hide_delay':500,'folder_refresh_interval_s':30}.items():
            try:data[key]=int(data.get(key,default))
            except (TypeError,ValueError,OverflowError):data[key]=default
        for key,default in {'hover_details_enabled':True,'thumbnail_cache_enabled':True,'disk_thumbnail_cache_limit_enabled':False,'auto_hide_main':False,'auto_hide_tabs':False,'auto_hide_tree':False,'right_controls_auto_hide':True,'recursive_search_enabled':False,'smooth_wheel_scroll':False,'scroll_test_logging_enabled':False,'auto_refresh_folders':False}.items():
            data[key]=data.get(key,default) if isinstance(data.get(key,default),bool) else default
        try:data['wheel_scroll_rows']=max(.1,min(12.0,float(data.get('wheel_scroll_rows',1))))
        except (TypeError,ValueError,OverflowError):data['wheel_scroll_rows']=1.0
        if data.get('window_mode') not in ('fullscreen','maximized','normal'):data['window_mode']='fullscreen'
        return data
    def save_settings(self):
        geometry=None
        try:
            geometry=bytes(self.saveGeometry().toBase64()).decode('ascii')
        except (RuntimeError,ValueError):
            logger.debug('Could not save window geometry',exc_info=True)
        tabs=[{k:v for k,v in tab.items() if k in ('path','kind','scroll','positions','sort','desc','unsorted','selection','color','source_order','view_id')} for tab in getattr(self,'tabs',[])]
        payload=json.dumps({'favorites':list(self.favorites),'favorite_groups':self.favorite_groups,'tree_favorites':list(self.tree_favorites),'pinned_folders':list(self.pinned_folders),'pinned_thumbs':self.pinned_thumbs,'pinned_folder_sizes':self.pinned_folder_sizes,'pinned_folder_sizes_checked':self.pinned_folder_sizes_checked,'custom':self.custom,'image_search_provider':self.image_search_provider,'saucenao_api_key':getattr(self,'saucenao_api_key',''),'hover_details_enabled':self.hover_details_enabled,'hover_details_delay':self.hover_details_delay,'hover_details_placement':self.hover_details_placement,'thumbnail_cache_enabled':self.thumbnail_cache_enabled,'thumbnail_cache_limit_mib':self.thumbnail_cache_limit_mib,'auto_hide_main':getattr(self,'auto_hide_main',False),'auto_hide_tabs':getattr(self,'auto_hide_tabs',False),'auto_hide_tree':getattr(self,'auto_hide_tree',False),'main_hide_delay':getattr(self,'main_hide_delay',900),'tree_hide_delay':getattr(self,'tree_hide_delay',900),'last':str(self.current),'startup':self.startup_folder,'window_mode':self.window_mode,'geometry':geometry,'tabs':tabs,'active_tab':max(0,self.current_tab_index()) if hasattr(self,'tabbar') else 0})
        payload=json.dumps(json.loads(payload)|{'slideshow_interval_ms':self.slideshow_interval_ms,'wheel_scroll_rows':self.wheel_scroll_rows,'smooth_wheel_scroll':self.smooth_wheel_scroll,'scroll_test_logging_enabled':self.scroll_test_logging_enabled,'auto_refresh_folders':self.auto_refresh_folders,'folder_refresh_interval_s':self.folder_refresh_interval_s,'right_controls_auto_hide':self.right_controls_auto_hide,'right_controls_hide_delay':self.right_controls_hide_delay,'log_storage_mib':self.log_storage_mib,'recursive_search_enabled':self.recursive_search_enabled,'folder_sort_settings':self.folder_sort_settings,'folder_manual_orders':self.folder_manual_orders,'manual_order_created':self.manual_order_created,'tree_expanded_paths':sorted(self.tree_expanded_paths),'tree_selected_path':self.tree_selected_path,'tree_scroll_value':self.tree_scroll_value,'tree_scroll_anchor_path':getattr(self,'tree_scroll_anchor_path',''),'tree_scroll_anchor_offset':getattr(self,'tree_scroll_anchor_offset',0),'thumbnail_cache_dir':str(self.persistent_thumb_dir),'google_lens_query':getattr(self,'google_lens_query',''),'disk_thumbnail_cache_limit_enabled':self.disk_thumbnail_cache_limit_enabled,'disk_thumbnail_cache_limit_mib':self.disk_thumbnail_cache_limit_mib})
        try:
            self.settings_file.parent.mkdir(parents=True,exist_ok=True)
            tmp=self.settings_file.with_suffix('.tmp')
            tmp.write_text(payload,encoding='utf-8')
            os.replace(tmp,self.settings_file)
        except (OSError,TypeError,ValueError):logger.exception('Could not save application settings')
    def configure_hover_details(self):
        dlg=QDialog(self);dlg.setWindowTitle('Thumbnail hover details');layout=QVBoxLayout(dlg)
        enabled=QCheckBox('Show size and date created when hovering over a thumbnail');enabled.setChecked(self.hover_details_enabled);layout.addWidget(enabled)
        row=QHBoxLayout();row.addWidget(QLabel('Show after'));delay=QSpinBox();delay.setRange(0,3000);delay.setSingleStep(100);delay.setSuffix(' ms');delay.setValue(self.hover_details_delay);row.addWidget(delay);row.addStretch();layout.addLayout(row)
        place_row=QHBoxLayout();place_row.addWidget(QLabel('Tooltip placement'));placement=QComboBox();placement.addItem('Over the top of the thumbnail','top');placement.addItem('Below; fall back above','below');placement.addItem('Above; fall back below','above');placement.setCurrentIndex(max(0,placement.findData('below' if self.hover_details_placement=='bottom' else self.hover_details_placement)));place_row.addWidget(placement,1);layout.addLayout(place_row)
        note=QLabel('Folder size includes files in its subfolders and is calculated in the background.');note.setWordWrap(True);layout.addWidget(note)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel);buttons.accepted.connect(dlg.accept);buttons.rejected.connect(dlg.reject);layout.addWidget(buttons)
        if dlg.exec()==QDialog.Accepted:
            self.hover_details_enabled=enabled.isChecked();self.hover_details_delay=delay.value();self.hover_details_placement=placement.currentData();self.grid.set_hover_settings(self.hover_details_enabled,self.hover_details_delay);self.save_settings();QTimer.singleShot(0,self.grid.refresh_hover_from_cursor)
    def configure_slideshow(self):
        dlg=QDialog(self);dlg.setWindowTitle('Slideshow');layout=QVBoxLayout(dlg);row=QHBoxLayout();row.addWidget(QLabel('Time per image'));interval=QSpinBox();interval.setRange(250,60000);interval.setSingleStep(250);interval.setSuffix(' ms');interval.setValue(self.slideshow_interval_ms);row.addWidget(interval);layout.addLayout(row)
        note=QLabel('The viewer advances to the next image at this interval while the slideshow button is active.');note.setWordWrap(True);layout.addWidget(note);buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel);buttons.accepted.connect(dlg.accept);buttons.rejected.connect(dlg.reject);layout.addWidget(buttons)
        if dlg.exec()==QDialog.Accepted:self.slideshow_interval_ms=interval.value();self.save_settings()
    def configure_scroll_behavior(self):
        dlg=QDialog(self);dlg.setWindowTitle('Scroll behavior');layout=QVBoxLayout(dlg)
        row=QHBoxLayout();row.addWidget(QLabel('Rows per mouse-wheel notch'));rows=QDoubleSpinBox();rows.setRange(.1,12);rows.setSingleStep(.1);rows.setDecimals(1);rows.setSuffix(' rows');rows.setValue(self.wheel_scroll_rows);row.addWidget(rows);layout.addLayout(row)
        smooth=QCheckBox('Smooth scrolling with gentle momentum');smooth.setChecked(self.smooth_wheel_scroll);layout.addWidget(smooth)
        note=QLabel('Smooth scrolling eases to a stop and may travel slightly past the first wheel step.');note.setWordWrap(True);layout.addWidget(note)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel);buttons.accepted.connect(dlg.accept);buttons.rejected.connect(dlg.reject);layout.addWidget(buttons)
        if dlg.exec()==QDialog.Accepted:
            self.wheel_scroll_rows=rows.value();self.smooth_wheel_scroll=smooth.isChecked()
            if not self.smooth_wheel_scroll:self.grid.stop_smooth_scroll()
            self.save_settings()
    def configure_folder_auto_refresh(self):
        dlg=QDialog(self);dlg.setWindowTitle('Automatic folder refresh');layout=QVBoxLayout(dlg)
        enabled=QCheckBox('Automatically refresh the active folder page');enabled.setChecked(self.auto_refresh_folders);layout.addWidget(enabled)
        row=QHBoxLayout();row.addWidget(QLabel('Refresh every'));interval=QSpinBox();interval.setRange(1,3600);interval.setSingleStep(5);interval.setSuffix(' seconds');interval.setValue(self.folder_refresh_interval_s);row.addWidget(interval);row.addStretch(1);layout.addLayout(row)
        enabled.toggled.connect(interval.setEnabled);interval.setEnabled(enabled.isChecked())
        note=QLabel('Magellan checks the open folder in a low-priority background scan and applies changes after scrolling or selection is idle. It never navigates away or resets the current scroll position. Checks are skipped while another folder scan is running, or while Favorites, Farview, or the image viewer is active.');note.setWordWrap(True);layout.addWidget(note)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel);buttons.accepted.connect(dlg.accept);buttons.rejected.connect(dlg.reject);layout.addWidget(buttons)
        if dlg.exec()==QDialog.Accepted:
            self.auto_refresh_folders=enabled.isChecked();self.folder_refresh_interval_s=interval.value();self.sync_folder_refresh_timer();self.save_settings()
    def sync_folder_refresh_timer(self):
        timer=getattr(self,'folder_refresh_timer',None)
        if timer is None:return
        if self.auto_refresh_folders and not self._closing:timer.start(self.folder_refresh_interval_s*1000)
        else:
            timer.stop()
            apply_timer=getattr(self,'folder_refresh_apply_timer',None)
            if apply_timer is not None:apply_timer.stop()
            self._folder_refresh_pending=None
            self._folder_refresh_context=None
            job=getattr(self,'_folder_refresh_scan',None)
            if job is not None and job.isRunning():job.requestInterruption()
    def auto_refresh_folder_page(self):
        if self._closing or not self.auto_refresh_folders or self.in_favorites_view or self.viewer is not None:return
        if self._folder_refresh_context is not None:return
        if self._folder_refresh_scan is not None and self._folder_refresh_scan.isRunning():return
        if self._folder_refresh_pending is not None:return
        index=self.current_tab_index()
        if not (0<=index<len(self.tabs)) or self.tabs[index].get('kind')!='folder':return
        grid=self.grid
        if grid.selection_dragging or grid.fast_scroll or grid.smooth_scroll_animation.state()==QAbstractAnimation.Running or grid.scroll_settle_timer.isActive():return
        scan=self.scan
        if scan is not None and scan.isRunning():return
        if self.recursive_search_enabled and self.search.text().strip():return
        try:
            folder=Path(self.current)
            if not folder.is_dir():return
            self._folder_refresh_generation+=1;generation=self._folder_refresh_generation
            self._folder_refresh_context={'folder':str(folder),'tab':self.tabs[index],'scan_generation':self.scan_generation,'generation':generation}
            task=ScanTask(folder,recursive=False,generation=generation,manual_orders=self.folder_manual_orders,manual_sort_settings=self.folder_sort_settings)
            task.signals.done.connect(self.folder_refresh_scanned)
            task.finished.connect(lambda job=task:self.release_folder_refresh_job(job))
            task.finished.connect(lambda job=task:self.release_scan_job(job))
            self._folder_refresh_scan=task
            self.scan_jobs.append(task)
            task.start(QThread.LowPriority)
        except Exception:logger.exception('Automatic folder refresh failed for %s',self.current)
    def release_folder_refresh_job(self,job):
        if self._folder_refresh_scan is job:self._folder_refresh_scan=None
    def folder_refresh_scanned(self,items,error,generation):
        context=self._folder_refresh_context
        if not context or generation!=context.get('generation'):return
        self._folder_refresh_context=None
        if not self.auto_refresh_folders or self._closing:return
        if error:
            logger.warning('Automatic folder refresh could not read %s: %s',context['folder'],error)
            return
        self._folder_refresh_pending=(items,context)
        self.apply_pending_folder_refresh()
    def apply_pending_folder_refresh(self):
        pending=self._folder_refresh_pending
        if pending is None:return
        if self._closing or not self.auto_refresh_folders:
            self._folder_refresh_pending=None
            return
        items,context=pending
        index=self.current_tab_index()
        if (not 0<=index<len(self.tabs) or self.tabs[index] is not context['tab'] or
                str(self.current)!=context['folder'] or self.scan_generation!=context['scan_generation'] or
                self.in_favorites_view or self.viewer is not None):
            self._folder_refresh_pending=None
            return
        grid=self.grid
        if grid.selection_dragging or grid.fast_scroll or grid.smooth_scroll_animation.state()==QAbstractAnimation.Running or grid.scroll_settle_timer.isActive():
            self.folder_refresh_apply_timer.start()
            return
        if self.scan is not None and self.scan.isRunning():
            self.folder_refresh_apply_timer.start()
            return
        previous={str(item.get('path')):(bool(item.get('is_dir')),item.get('mtime'),item.get('ctime'),item.get('size')) for item in self.items}
        updated={str(item.get('path')):(bool(item.get('is_dir')),item.get('mtime'),item.get('ctime'),item.get('size')) for item in items}
        if previous==updated:
            self._folder_refresh_pending=None
            return
        scroll=grid.verticalScrollBar().value()
        selected=set(grid.selected_paths)
        navigation_path=(str(grid.items[grid.navigation_index].get('path'))
                         if 0<=grid.navigation_index<len(grid.items) else None)
        hover_path=(str(grid.items[grid.hover_index].get('path'))
                    if 0<=grid.hover_index<len(grid.items) else None)
        hover_delay_remaining=grid.hover_delay_timer.remainingTime() if grid.hover_delay_timer.isActive() else None
        hover_animation_active=grid.hover_animation_timer.isActive()
        hover_animation_started=grid.hover_animation_started
        self._folder_refresh_pending=None
        self.items=items
        self.refresh()
        new_indices={str(item.get('path')):index for index,item in enumerate(grid.items)}
        valid_paths={str(item.get('path')) for item in items if item.get('path') is not None}
        grid.selected_paths.intersection_update(valid_paths)
        grid.selected_metadata={path:value for path,value in grid.selected_metadata.items() if path in grid.selected_paths}
        if selected!=grid.selected_paths:self.update_selection_button()
        grid.navigation_index=new_indices.get(navigation_path,-1) if navigation_path else -1
        grid.hover_index=new_indices.get(hover_path,-1) if hover_path else -1
        if hover_delay_remaining is not None and grid.hover_index>=0:grid.hover_delay_timer.start(max(0,hover_delay_remaining))
        if hover_animation_active and grid.hover_index>=0:
            grid.hover_animation_started=hover_animation_started;grid.hover_animation_timer.start()
        grid.verticalScrollBar().setValue(min(scroll,grid.verticalScrollBar().maximum()))
        grid.viewport().update()
    def set_scroll_test_logging_enabled(self,enabled):
        self.scroll_test_logging_enabled=bool(enabled)
        if not self.scroll_test_logging_enabled:self.grid._scroll_diagnostic=None
        self.save_settings()
        if hasattr(self,'status'):self.status.setText('Scroll test logging '+('enabled' if enabled else 'disabled'))
    def configure_right_controls(self):
        dlg=QDialog(self);dlg.setWindowTitle('Right-side buttons');layout=QVBoxLayout(dlg)
        hide=QCheckBox('Hide right-side buttons until the pointer reaches the right edge');hide.setChecked(self.right_controls_auto_hide);layout.addWidget(hide)
        row=QHBoxLayout();row.addWidget(QLabel('Keep buttons visible for'));delay=QSpinBox();delay.setRange(0,10000);delay.setSingleStep(100);delay.setSuffix(' ms');delay.setValue(self.right_controls_hide_delay);row.addWidget(delay);layout.addLayout(row)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel);buttons.accepted.connect(dlg.accept);buttons.rejected.connect(dlg.reject);layout.addWidget(buttons)
        if dlg.exec()==QDialog.Accepted:
            self.right_controls_auto_hide=hide.isChecked();self.right_controls_hide_delay=delay.value();self.favorite_overlay_timer.stop()
            if self.right_controls_auto_hide:self.hide_favorite_controls()
            else:self.show_right_controls()
            self.save_settings()
    def set_hover_details_enabled(self,enabled):
        self.grid.set_hover_settings(enabled,self.hover_details_delay);self.save_settings();QTimer.singleShot(0,self.grid.refresh_hover_from_cursor)
    def configure_auto_hide(self):
        dlg=QDialog(self);dlg.setWindowTitle('Auto-hide panels');layout=QVBoxLayout(dlg)
        main=QCheckBox('Hide the main bar until the pointer reaches the top edge');main.setChecked(self.auto_hide_main);layout.addWidget(main)
        mr=QHBoxLayout();mr.addWidget(QLabel('Main bar stays visible for'));md=QSpinBox();md.setRange(0,10000);md.setSingleStep(100);md.setSuffix(' ms');md.setValue(self.main_hide_delay);mr.addWidget(md);layout.addLayout(mr)
        tabs=QCheckBox('Hide the tabs with the main bar, leaving only thumbnails visible');tabs.setChecked(self.auto_hide_tabs);layout.addWidget(tabs)
        tree=QCheckBox('Hide the file tree until the pointer reaches the left edge');tree.setChecked(self.auto_hide_tree);layout.addWidget(tree)
        tr=QHBoxLayout();tr.addWidget(QLabel('File tree stays visible for'));td=QSpinBox();td.setRange(0,10000);td.setSingleStep(100);td.setSuffix(' ms');td.setValue(self.tree_hide_delay);tr.addWidget(td);layout.addLayout(tr)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel);buttons.accepted.connect(dlg.accept);buttons.rejected.connect(dlg.reject);layout.addWidget(buttons)
        if dlg.exec()==QDialog.Accepted:
            self.auto_hide_main=main.isChecked();self.auto_hide_tabs=tabs.isChecked();self.main_hide_delay=md.value();self.auto_hide_tree=tree.isChecked();self.tree_hide_delay=td.value();self.apply_auto_hide();self.save_settings()
    def apply_auto_hide(self):
        if not hasattr(self,'root_widget'):return
        attach_tabs=self.auto_hide_main and self.auto_hide_tabs
        if self.auto_hide_main:
            self.main_bar_layout.removeWidget(self.main_bar);self.main_bar_layout.removeWidget(self.main_bar_line)
            self.main_bar.setParent(self.root_widget);self.main_bar.ensurePolished()
            self.main_bar_overlay_height=max(self.main_bar.height(),self.main_bar.sizeHint().height(),getattr(self,'main_bar_overlay_height',0))
            self.main_bar.setGeometry(0,0,self.root_widget.width(),self.main_bar_overlay_height);self.main_bar.hide();self.main_bar_line.hide()
            if attach_tabs:
                self.main_bar_layout.removeWidget(self.nav_bar);self.nav_bar.setParent(self.root_widget);self.nav_bar.ensurePolished()
                self.nav_bar_overlay_height=max(self.nav_bar.height(),self.nav_bar.sizeHint().height(),getattr(self,'nav_bar_overlay_height',0),34)
                self.nav_bar.setGeometry(0,self.main_bar_overlay_height,self.root_widget.width(),self.nav_bar_overlay_height);self.nav_bar.hide()
            else:
                if self.main_bar_layout.indexOf(self.nav_bar)<0:self.main_bar_layout.insertWidget(0,self.nav_bar)
                else:self.main_bar_layout.insertWidget(0,self.nav_bar)
                self.nav_bar.show()
        else:
            self.main_bar.setParent(self.root_widget);self.main_bar_layout.insertWidget(0,self.main_bar);self.main_bar_layout.insertWidget(1,self.main_bar_line);self.main_bar.show();self.main_bar_line.show()
            if self.main_bar_layout.indexOf(self.nav_bar)<0:self.main_bar_layout.insertWidget(2,self.nav_bar)
            else:self.main_bar_layout.insertWidget(2,self.nav_bar)
            self.nav_bar.show()
        if self.auto_hide_tree and not hasattr(self,'tree_dock'):self.build_file_tree()
        if hasattr(self,'tree_dock'):
            if self.auto_hide_tree:
                self.tree_dock.setFloating(True);self.tree_dock.setWindowFlags(Qt.Tool|Qt.FramelessWindowHint);self.tree_dock.resize(300,max(200,self.height()-100));self.tree_dock.hide()
            else:
                self.tree_dock.setFloating(False);self.tree_dock.show()
        if hasattr(self,'selection_clear_overlay'):
            if self.right_controls_auto_hide:self.hide_favorite_controls()
            else:self.show_right_controls()
        if hasattr(self,'titlebar_hover_timer'):
            if self.auto_hide_main:self.titlebar_hover_timer.start()
            else:self.titlebar_hover_timer.stop()
        self.root_widget.update()
    def check_titlebar_hover(self):
        if not self.auto_hide_main or not hasattr(self,'main_bar') or isinstance(QApplication.activeWindow(),MediaViewer):return
        if self.search_has_focus():
            self.main_hide_timer.stop()
            if not self.main_bar.isVisible():self.set_main_panel_visible(True)
            return
        if getattr(self,'shift_selection_held',False):
            self.main_hide_timer.stop()
            if self.main_bar.isVisible():self.set_main_panel_visible(False)
            return
        cursor=QCursor.pos();frame=self.frameGeometry();client=self.geometry()
        in_titlebar=frame.contains(cursor) and cursor.y()<client.top() and not client.contains(cursor)
        if in_titlebar:
            self.main_hide_timer.stop()
            if not self.main_bar.isVisible():self.set_main_panel_visible(True)
        elif self.main_bar.isVisible():
            if getattr(self,'tab_context_menu_open',False):
                self.main_hide_timer.stop();return
            pos=self.root_widget.mapFromGlobal(cursor)
            over_panel=self.main_bar.geometry().contains(pos) or (self.nav_bar.isVisible() and self.nav_bar.geometry().contains(pos))
            over_menu=any(action.menu() and action.menu().isVisible() and action.menu().geometry().contains(cursor) for action in self.menu_bar.actions()) if hasattr(self,'menu_bar') else False
            if over_panel or over_menu:self.main_hide_timer.stop()
            elif not self.main_hide_timer.isActive():self.main_hide_timer.start(self.main_hide_delay)
    def hide_tree_overlay(self):
        if self.auto_hide_tree and hasattr(self,'tree_dock'):
            if getattr(self,'tree_scrollbar_interacting',False) or self.tree_panel_contains_global(QCursor.pos()):
                self.tree_hide_timer.stop();return
            self.save_tree_navigation_state()
            self.tree_dock.hide()
    def hide_main_panel(self):
        if getattr(self,'tab_context_menu_open',False) or getattr(self,'_tab_drag_active',False):return
        if getattr(self,'tab_reveal_timer',None) and self.tab_reveal_timer.isActive():return
        if self.search_has_focus():return
        if hasattr(self,'menu_bar'):
            for action in self.menu_bar.actions():
                menu=action.menu()
                if menu and menu.isVisible():menu.close()
        if self.auto_hide_main and hasattr(self,'main_bar'):self.set_main_panel_visible(False)
    def set_main_panel_visible(self,visible):
        if not hasattr(self,'main_bar'):return
        if not visible and self.auto_hide_main and self.search_has_focus():
            self.main_hide_timer.stop()
            return
        if self.auto_hide_main:
            self.main_bar_overlay_height=max(getattr(self,'main_bar_overlay_height',0),self.main_bar.sizeHint().height())
            self.main_bar.setGeometry(0,0,self.root_widget.width(),self.main_bar_overlay_height)
            if visible:self.main_bar.show();self.main_bar.raise_()
            else:self.main_bar.hide()
            if self.auto_hide_tabs:
                self.nav_bar_overlay_height=max(getattr(self,'nav_bar_overlay_height',0),self.nav_bar.sizeHint().height(),34)
                self.nav_bar.setGeometry(0,self.main_bar_overlay_height,self.root_widget.width(),self.nav_bar_overlay_height)
                if visible:self.nav_bar.show();self.nav_bar.raise_()
                else:self.nav_bar.hide()
            else:
                self.nav_bar.show();self.nav_bar.raise_()
                if visible:self.main_bar.raise_()
        else:
            self.main_bar.setVisible(visible)
    def open_menu_on_hover(self,action):
        menu=action.menu() if action is not None else None
        if menu is None:return
        for other in self.menu_bar.actions():
            other_menu=other.menu()
            if other_menu is not None and other_menu is not menu and other_menu.isVisible():other_menu.close()
        if not menu.isVisible():menu.popup(self.menu_bar.mapToGlobal(self.menu_bar.actionGeometry(action).bottomLeft()))
    def eventFilter(self,obj,event):
        viewer_active=isinstance(QApplication.activeWindow(),MediaViewer)
        active=QApplication.activeWindow();main_active=active is self or (active is not None and self.isAncestorOf(active));target_main=obj is self or (isinstance(obj,QWidget) and self.isAncestorOf(obj))
        if event.type()==QEvent.MouseButtonPress and event.button()==Qt.LeftButton and isinstance(obj,QWidget) and hasattr(self,'main_bar') and self.main_bar.isAncestorOf(obj) and obj is not getattr(self,'search',None):
            self.grid.setFocus(Qt.MouseFocusReason)
        if hasattr(self,'file_tree') and obj in (self.file_tree,self.file_tree.viewport()) and event.type() in (QEvent.Wheel,QEvent.MouseButtonPress,QEvent.KeyPress):
            if self._tree_scroll_restore_pending and not self._tree_restoring_scroll:
                # Once the user interacts with the visible tree, its saved view
                # is authoritative; a delayed async restore must not jump it.
                self._tree_scroll_restore_pending=False;self._tree_rebuilding=False
        if event.type()==QEvent.ApplicationDeactivate:
            self.ctrl_key_down=False;self.ctrl_organization_timer.stop()
            if self.ctrl_organization_held:self.ctrl_organization_held=False;self.set_favorite_organization_mode(False)
        if event.type()==QEvent.KeyPress and event.key()==Qt.Key_Control and (main_active or target_main) and not event.isAutoRepeat():
            self.ctrl_key_down=True;self.ctrl_organization_timer.start()
        elif event.type()==QEvent.KeyRelease and event.key()==Qt.Key_Control:
            self.ctrl_key_down=False
            self.ctrl_organization_timer.stop()
            if self.ctrl_organization_held:self.ctrl_organization_held=False;self.set_favorite_organization_mode(False)
        elif event.type()==QEvent.KeyPress and event.modifiers()&Qt.ControlModifier:
            self.ctrl_organization_timer.stop()
        if event.type()==QEvent.MouseButtonPress and event.button()==Qt.LeftButton and self.ctrl_key_down and obj is getattr(getattr(self,'grid',None),'viewport',lambda:None)():
            self.ctrl_organization_timer.stop();self.set_favorite_organization_mode(True);self.ctrl_organization_held=True
        if event.type()==QEvent.MouseMove and not viewer_active and hasattr(self,'grid') and self.grid.navigation_index>=0:
            self.grid.navigation_index=-1;self.grid.viewport().update()
        if event.type()==QEvent.KeyPress and QApplication.activeWindow() is self and not viewer_active:
            focus=QApplication.focusWidget();editing=isinstance(focus,(QLineEdit,QSpinBox))
            if not editing and event.key()==Qt.Key_A and event.modifiers()&Qt.ControlModifier:self.grid.select_all_current();event.accept();return True
            if not editing and event.key()==Qt.Key_Delete:
                if event.modifiers()&Qt.ShiftModifier:
                    self.send_paths_to_trash(list(self.grid.selected_paths))
                elif self.in_favorites_view:
                    self.remove_favorites_selection()
                else:return super().eventFilter(obj,event)
                event.accept();return True
            if event.key()==Qt.Key_Z and event.modifiers()&Qt.ControlModifier and not editing:self.undo_last_action();event.accept();return True
            if not editing and event.key()==Qt.Key_Tab:
                count=self.tabbar.count()
                if count:self.tabbar.setCurrentIndex((self.tabbar.currentIndex()+(-1 if event.modifiers()&Qt.ShiftModifier else 1))%count)
                event.accept();return True
            if (not editing or focus is self.search) and event.key()==Qt.Key_Q and not event.modifiers()&(Qt.ControlModifier|Qt.AltModifier):self.toggle_search_navigation_focus();event.accept();return True
            if not editing and Qt.Key_1<=event.key()<=Qt.Key_7 and not event.modifiers()&(Qt.ControlModifier|Qt.AltModifier|Qt.ShiftModifier):self.set_sort(('name','mtime','ctime','type','size','random','manual')[event.key()-Qt.Key_1]);event.accept();return True
        if event.type()==QEvent.KeyPress and event.key()==Qt.Key_Shift:
            self.shift_selection_held=True
            if self.auto_hide_main and not self.search_has_focus():self.main_hide_timer.stop();self.tab_reveal_timer.stop();self.set_main_panel_visible(False)
            elif self.auto_hide_main:self.main_hide_timer.stop();self.tab_reveal_timer.stop();self.set_main_panel_visible(True)
            if self.auto_hide_tree:self.tree_hide_timer.stop();self.hide_tree_overlay()
            if hasattr(self,'favorite_group_overlay'):self.hide_favorite_controls()
        elif event.type()==QEvent.KeyRelease and event.key()==Qt.Key_Shift:self.shift_selection_held=False
        if viewer_active:
            if self.auto_hide_main and (self.main_bar.isVisible() or (self.auto_hide_tabs and self.nav_bar.isVisible())):self.set_main_panel_visible(False)
            if self.auto_hide_tree and hasattr(self,'tree_dock') and self.tree_dock.isVisible():
                self.save_tree_navigation_state();self.tree_dock.hide()
            if hasattr(self,'favorite_group_overlay') and any(widget.isVisible() for widget in self.right_control_widgets()):self.hide_favorite_controls()
        if event.type()==QEvent.Resize and obj is getattr(self,'root_widget',None):
            if self.auto_hide_main:self.main_bar.setGeometry(0,0,self.root_widget.width(),self.main_bar.sizeHint().height())
            if self.auto_hide_main and self.auto_hide_tabs:self.nav_bar.setGeometry(0,self.main_bar.sizeHint().height(),self.root_widget.width(),self.nav_bar.sizeHint().height())
            if hasattr(self,'selection_clear_overlay'):self.position_right_controls()
        if event.type()==QEvent.MouseMove and hasattr(self,'menu_bar'):
            cursor=QCursor.pos()
            for action in self.menu_bar.actions():
                menu=action.menu()
                if menu and menu.isVisible():
                    action_rect=QRect(self.menu_bar.mapToGlobal(self.menu_bar.actionGeometry(action).topLeft()),self.menu_bar.actionGeometry(action).size())
                    if not menu.geometry().contains(cursor) and not action_rect.contains(cursor):menu.close()
        main_menu_hover=False
        if hasattr(self,'menu_bar'):
            cursor=QCursor.pos()
            for action in self.menu_bar.actions():
                menu=action.menu()
                if menu and menu.isVisible():
                    action_rect=QRect(self.menu_bar.mapToGlobal(self.menu_bar.actionGeometry(action).topLeft()),self.menu_bar.actionGeometry(action).size())
                    if menu.geometry().contains(cursor) or action_rect.contains(cursor):main_menu_hover=True;break
        if event.type()==QEvent.MouseMove and (self.auto_hide_main or self.auto_hide_tree) and not viewer_active and not getattr(self,'shift_selection_held',False):
            pos=self.root_widget.mapFromGlobal(QCursor.pos())
            if self.auto_hide_main:
                over_tabs=self.nav_bar.isVisible() and self.nav_bar.geometry().contains(pos)
                if self.search_has_focus():
                    self.main_hide_timer.stop()
                    if not self.main_bar.isVisible():self.set_main_panel_visible(True)
                elif pos.y()<=2:
                    self.main_hide_timer.stop();self.set_main_panel_visible(True)
                elif over_tabs or main_menu_hover or getattr(self,'tab_context_menu_open',False):self.main_hide_timer.stop()
                elif self.main_bar.isVisible() and pos.y()>self.main_bar.height():self.main_hide_timer.start(self.main_hide_delay)
            if self.auto_hide_tree and hasattr(self,'tree_dock'):
                if getattr(self,'tree_scrollbar_interacting',False):
                    self.tree_hide_timer.stop()
                elif self.tree_panel_contains_global(QCursor.pos()):
                    self.tree_hide_timer.stop()
                elif 0<=pos.x()<=10:
                    self.show_tree_overlay()
                elif self.tree_dock.isVisible() and not self.tree_hide_timer.isActive():self.tree_hide_timer.start(self.tree_hide_delay)
        if event.type()==QEvent.MouseMove and hasattr(self,'selection_clear_overlay') and not viewer_active:
            pos=self.root_widget.mapFromGlobal(QCursor.pos());self.position_right_controls()
            rects=[w.geometry().adjusted(-3,-3,3,3) for w in self.right_control_widgets() if w.isVisible()]
            if not self.right_controls_auto_hide:
                self.favorite_overlay_timer.stop();self.show_right_controls()
            elif pos.x()>=self.root_widget.width()-24 and pos.y()>=self.main_bar.height():
                self.favorite_overlay_timer.stop();self.show_right_controls()
            elif any(rect.contains(pos) for rect in rects):self.favorite_overlay_timer.stop()
            elif any(w.isVisible() for w in self.right_control_widgets()):self.favorite_overlay_timer.start(self.right_controls_hide_delay)
        return super().eventFilter(obj,event)
    def apply_window_state(self):
        try:
            if self.saved_geometry:
                from PySide6.QtCore import QByteArray
                self.restoreGeometry(QByteArray.fromBase64(self.saved_geometry.encode('ascii')))
        except Exception:
            logger.warning('Could not restore saved window geometry',exc_info=True)
        if self.window_mode=='fullscreen': self.showFullScreen()
        elif self.window_mode=='maximized': self.showMaximized()
        else: self.showNormal()
        self.update_window_button()
    def update_window_button(self):
        if hasattr(self,'windowbtn'):
            self.windowbtn.setText('□' if self.window_mode!='fullscreen' else '⛶')
            self.windowbtn.setToolTip('Exit full screen' if self.window_mode=='fullscreen' else 'Full screen')
    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.window_mode=getattr(self,'pre_fullscreen_mode','maximized')
            if self.window_mode=='maximized': self.showMaximized()
            else: self.showNormal()
        else:
            self.pre_fullscreen_mode='maximized' if self.isMaximized() else 'normal'
            self.window_mode='fullscreen';self.showFullScreen()
        self.update_window_button();self.save_settings()
    def closeEvent(self,e):
        self._closing=True
        for timer_name in ('folder_refresh_timer','folder_refresh_apply_timer'):
            timer=getattr(self,timer_name,None)
            if timer is not None:timer.stop()
        refresh_job=getattr(self,'_folder_refresh_scan',None)
        if refresh_job is not None and refresh_job.isRunning():refresh_job.requestInterruption()
        self._folder_refresh_pending=None;self._folder_refresh_context=None
        if hasattr(self,'recursive_search_timer'):self.recursive_search_timer.stop()
        for job in list(getattr(self,'recursive_search_jobs',[])):
            if job.isRunning():job.requestInterruption()
        for job in list(getattr(self,'recursive_search_jobs',[])):
            if job.isRunning():job.wait(250)
        if hasattr(self,'_tree_generation'):
            if hasattr(self,'tree_scroll_save_timer'):self.tree_scroll_save_timer.stop()
            self.save_tree_navigation_state();self._tree_generation+=1
        TREE_SCAN_POOL.shutdown(wait=False,cancel_futures=True)
        menu=getattr(self,'favorite_picker_menu',None)
        if menu is not None:menu.close()
        viewer=getattr(self,'viewer',None)
        if viewer is not None:
            try:
                if viewer.isVisible():viewer.close()
            except RuntimeError:
                logger.debug('The image viewer was already deleted during application shutdown')
            self.viewer=None
        if hasattr(self,'grid'):self.grid.shutdown_workers()
        scan_jobs=list(getattr(self,'scan_jobs',[]))
        for job in scan_jobs:
            if job.isRunning():job.requestInterruption()
        for job in scan_jobs:
            if job.isRunning():job.wait(250)
        VIEWER_IMAGE_POOL.shutdown(wait=False,cancel_futures=True)
        THUMB_CACHE_CLEANUP_POOL.shutdown(wait=False,cancel_futures=True)
        if self.isFullScreen(): self.window_mode='fullscreen'
        elif self.isMaximized(): self.window_mode='maximized'
        else: self.window_mode='normal'
        self.save_current_view();self.save_settings();e.accept()
        app=QApplication.instance()
        if app is not None:QTimer.singleShot(0,app.quit)
    def resizeEvent(self,e):
        super().resizeEvent(e)
        self.position_activity_overlay()
        if self.auto_hide_main and hasattr(self,'root_widget') and hasattr(self,'main_bar'):
            self.set_main_panel_visible(self.main_bar.isVisible())
    def build(self):
        root=QWidget();root.setObjectName('root');self.setCentralWidget(root);main=QVBoxLayout(root);main.setContentsMargins(0,0,0,0);main.setSpacing(0)
        mb=QMenuBar();mb.setMouseTracking(True);mb.hovered.connect(self.open_menu_on_hover);self.menu_bar=mb
        for title in ('FILES','VIEW','PERFORMANCE','TOOLS','HELP'):
            m=mb.addMenu(title)
            if title=='FILES':m.addAction('Open folder…',self.choose);m.addAction('Refresh',self.refresh_current_view);m.addSeparator();m.addAction('Exit',self.close)
            if title=='VIEW':
                m.addAction('Vertical grid',lambda checked=False:self.set_mode('vertical'));m.addAction('Horizontal rows',lambda checked=False:self.set_mode('horizontal'));m.addSeparator()
                m.addAction('Thumbnail hover settings…',self.configure_hover_details);m.addAction('Slideshow timer…',self.configure_slideshow);m.addAction('Auto-hide panels…',self.configure_auto_hide);m.addAction('Right-side buttons…',self.configure_right_controls)
                m.addAction('Manage personal orders…',self.manage_personal_orders);m.addAction('Automatically refresh folder pages…',self.configure_folder_auto_refresh);m.addAction('Keep folder thumbnails…',self.manage_pinned_folders)
            if title=='PERFORMANCE':
                recursive_action=m.addAction('Recursive search in subfolders');recursive_action.setCheckable(True);recursive_action.setChecked(self.recursive_search_enabled);recursive_action.toggled.connect(self.set_recursive_search_enabled)
                m.addSeparator()
                cache_action=m.addAction('Cache visible thumbnails in memory');cache_action.setCheckable(True);cache_action.setChecked(self.thumbnail_cache_enabled);cache_action.toggled.connect(self.set_thumbnail_cache_enabled)
                self.thumbnail_cache_limit_action=m.addAction(f'Memory thumbnail cache limit… ({self.thumbnail_cache_limit_mib} MiB)');self.thumbnail_cache_limit_action.triggered.connect(self.configure_thumbnail_cache_limit)
                self.disk_thumbnail_cache_action=m.addAction('Limit on-disk thumbnail cache');self.disk_thumbnail_cache_action.setCheckable(True);self.disk_thumbnail_cache_action.setChecked(self.disk_thumbnail_cache_limit_enabled);self.disk_thumbnail_cache_action.toggled.connect(self.set_disk_thumbnail_cache_limit_enabled)
                self.disk_thumbnail_cache_limit_action=m.addAction(f'On-disk thumbnail cache limit… ({self.disk_thumbnail_cache_limit_mib} MiB)');self.disk_thumbnail_cache_limit_action.triggered.connect(self.configure_disk_thumbnail_cache_limit)
                m.addAction('Thumbnail cache location…',self.configure_thumbnail_cache_location)
                self.log_storage_action=m.addAction(f'Maximum log storage… ({self.log_storage_mib} MiB)');self.log_storage_action.triggered.connect(self.configure_log_storage_limit)
            if title=='TOOLS':m.addAction('Set custom folder thumbnail…',self.set_custom);m.addSeparator();m.addAction('Scroll behavior…',self.configure_scroll_behavior);m.addSeparator();m.addAction('Set startup folder…',self.set_startup);m.addAction('Use last folder on startup',self.clear_startup);m.addSeparator();m.addAction('Reverse image search…',self.choose_search_provider);m.addAction('Google Lens text query…',self.configure_google_lens_query);m.addAction('Set SauceNAO API key…',self.set_saucenao_api_key)
            if title=='HELP':
                self.scroll_test_logging_action=m.addAction('Scroll test logging');self.scroll_test_logging_action.setCheckable(True);self.scroll_test_logging_action.setChecked(self.scroll_test_logging_enabled);self.scroll_test_logging_action.setToolTip('Write detailed scroll timing diagnostics to the application log');self.scroll_test_logging_action.toggled.connect(self.set_scroll_test_logging_enabled)
                m.addAction('Keyboard and mouse controls…',self.show_controls_help)
        top=QFrame();top.setObjectName('top');top_layout=QVBoxLayout(top);top_layout.setContentsMargins(0,0,0,0);top_layout.setSpacing(0);top_layout.addWidget(mb);controls=QWidget(top);tl=QHBoxLayout(controls);tl.setContentsMargins(12,5,12,7);tl.setSpacing(7);top_layout.addWidget(controls)
        for s,t,f in [('☰','File tree',self.toggle_file_tree),('⌂','Back',self.back),('↑','Up',self.up),('⟳','Refresh',self.refresh_current_view)]:
            button=self.addbtn(tl,s,t,f)
            if t=='Back':self.backbtn=button;self.set_button_asset(button,'back.png')
        self.addbtn(tl,'⌕','Image search',self.toggle_image_search)
        self.favorites_list_btn=self.addbtn(tl,'☆','Show favorite folders',self.toggle_favorites_view)
        self.farviewbtn=self.addbtn(tl,'⇢','Create a Farview tab from this folder and its image subfolders',self.create_farview)
        self.set_button_asset(self.farviewbtn,'eye.png')
        self.recursive_search_timer=QTimer(self);self.recursive_search_timer.setSingleShot(True);self.recursive_search_timer.setInterval(250);self.recursive_search_timer.timeout.connect(self.start_recursive_search)
        self.search=QLineEdit();self.search.setObjectName('search');self.search.setPlaceholderText('Search...');self.search.setClearButtonEnabled(False);self.search.textChanged.connect(self.search_text_changed);self.search.setMinimumWidth(260);tl.addWidget(self.search)
        self.search_clear_btn=self.addbtn(tl,'×','Clear search',self.search.clear,'searchclear');self.search_clear_btn.setFixedSize(30,38)
        tl.addStretch(1)
        sb=QHBoxLayout();lab=QLabel('Sorting');lab.setObjectName('section');lab.setAlignment(Qt.AlignVCenter|Qt.AlignRight);sb.addWidget(lab);sr=QHBoxLayout()
        self.sortBtns={}
        for k,s,t in [('name','☷','Name'),('mtime','◩','Last modified'),('ctime','◷','Last created'),('type','◇','Type'),('size','◴','Size'),('random','⤨','Random folders'),('manual','↕','Personal order')]:
            q=self.addbtn(sr,s,t,lambda checked=False,k=k:self.set_sort(k),'sort');q.setCheckable(True);self.sortBtns[k]=q
            q.setToolButtonStyle(Qt.ToolButtonIconOnly);q.setMinimumWidth(56);q.setIconSize(QSize(46,24))
        for key,filename in {'name':'list.png','mtime':'lastmod.png','type':'types.png','size':'size.png'}.items():self.set_button_asset(self.sortBtns[key],filename)
        sb.addLayout(sr);tl.addLayout(sb)
        self.update_sort_buttons()
        lb=QHBoxLayout();lab=QLabel('Layout');lab.setObjectName('section');lab.setAlignment(Qt.AlignVCenter|Qt.AlignRight);lb.addWidget(lab);lr=QHBoxLayout();self.vbtn=self.addbtn(lr,'⋮','Vertical grid',lambda checked=False:self.set_mode('vertical'),'layout');self.vbtn.setCheckable(True);self.hbtn=self.addbtn(lr,'—','Horizontal rows',lambda checked=False:self.set_mode('horizontal'),'layout');self.hbtn.setCheckable(True);self.vbtn.setChecked(True);lb.addLayout(lr);tl.addLayout(lb)
        sz=QHBoxLayout();lab=QLabel('Size');lab.setObjectName('section');lab.setAlignment(Qt.AlignVCenter|Qt.AlignRight);sz.addWidget(lab);self.slider=QSlider(Qt.Horizontal);self.slider.setRange(140,430);self.slider.setValue(self.thumb);self.slider.valueChanged.connect(self.set_thumb);sz.addWidget(self.slider);tl.addLayout(sz)
        self.windowbtn=self.addbtn(tl,'⛶','Full screen',self.toggle_fullscreen);self.main_bar=top;main.addWidget(top);line=QFrame();line.setObjectName('line');main.addWidget(line);self.main_bar_line=line;self.main_bar_layout=main
        nav=QFrame();nav.setObjectName('navigation');nav.setAttribute(Qt.WA_StyledBackground,True);self.nav_bar=nav;nl=QHBoxLayout(nav);nl.setContentsMargins(14,4,14,4);nl.setSpacing(8)
        self.crumb=QLabel();self.crumb.setObjectName('crumb');self.crumb.setFixedWidth(360);self.crumb.setTextInteractionFlags(Qt.TextSelectableByMouse);self.crumb.setCursor(Qt.PointingHandCursor);self.crumb.mousePressEvent=lambda e:self.reveal(self.current) if e.button()==Qt.LeftButton else None;nl.addWidget(self.crumb)
        self.tabbar=SafeTabBar();self.tabbar.setExpanding(False);self.tabbar.setMovable(False);self.tabbar.setTabsClosable(False);self.tabbar.closeRequested.connect(self.close_tab);self.tabbar.dragStarted.connect(self.begin_tab_drag);self.tabbar.dragFinished.connect(self.end_tab_drag);self.tabbar.tabMoved.connect(self.tab_reordered);self.tabbar.setDocumentMode(True);self.tabbar.setElideMode(Qt.ElideMiddle);self.tabbar.currentChanged.connect(self.switch_tab);self.tabbar.setMinimumWidth(300);nl.addWidget(self.tabbar,1)
        self.status=MarqueeLabel();self.status.setObjectName('status');self.status.setFixedWidth(190);self.status.setAlignment(Qt.AlignRight|Qt.AlignVCenter);nl.addWidget(self.status)
        main.addWidget(nav)
        self.grid=VirtualGrid(self);self.grid.openItem.connect(self.open);self.grid.toggleItemFavorite.connect(self.toggle_item_favorite)
        self.grid.imageSearchRequested.connect(self.perform_image_search);self.grid.middleOpenItem.connect(self.open_new_tab);self.grid.goBack.connect(self.up);self.grid.verticalScrollBar().valueChanged.connect(self.remember_scroll);main.addWidget(self.grid,1)
        self.root_widget=root;root.setMouseTracking(True);QApplication.instance().installEventFilter(self);self.main_hide_timer=QTimer(self);self.main_hide_timer.setSingleShot(True);self.main_hide_timer.timeout.connect(self.hide_main_panel);self.ctrl_organization_timer=QTimer(self);self.ctrl_organization_timer.setSingleShot(True);self.ctrl_organization_timer.setInterval(50);self.ctrl_organization_timer.timeout.connect(self.enable_ctrl_organization_mode);self.ctrl_organization_held=False;self.ctrl_key_down=False;self.tab_reveal_timer=QTimer(self);self.tab_reveal_timer.setSingleShot(True);self.tab_reveal_timer.setInterval(2000);self.tab_reveal_timer.timeout.connect(self.finish_tab_reveal);self.titlebar_hover_timer=QTimer(self);self.titlebar_hover_timer.setInterval(60);self.titlebar_hover_timer.timeout.connect(self.check_titlebar_hover);self.tree_hide_timer=QTimer(self);self.tree_hide_timer.setSingleShot(True);self.tree_hide_timer.timeout.connect(self.hide_tree_overlay);self.tab_context_menu_open=False;self.tab_context_menu_timer=QTimer(self);self.tab_context_menu_timer.setInterval(50);self.tab_context_menu_timer.timeout.connect(self.close_tab_context_menu_if_cursor_left)
        self.favorite_overlay_timer=QTimer(self);self.favorite_overlay_timer.setSingleShot(True);self.favorite_overlay_timer.setInterval(500);self.favorite_overlay_timer.timeout.connect(self.hide_favorite_controls)
        self.favorite_picker_close_timer=QTimer(self);self.favorite_picker_close_timer.setInterval(20);self.favorite_picker_close_timer.timeout.connect(self.check_favorite_picker_dismiss);self.favorite_picker_leave_since=None
        self.favorite_group_overlay=QToolButton(root);self.favorite_group_overlay.setText('＋');self.favorite_group_overlay.setToolTip('Create a favorites group');self.favorite_group_overlay.setFixedSize(58,36);self.favorite_group_overlay.clicked.connect(self.create_favorite_group);self.favorite_group_overlay.hide()
        self.favorite_organize_overlay=QToolButton(root);self.favorite_organize_overlay.setText('↔');self.favorite_organize_overlay.setToolTip('Organization mode (Ctrl)');self.favorite_organize_overlay.setCheckable(True);self.favorite_organize_overlay.setFixedSize(58,36);self.favorite_organize_overlay.toggled.connect(self.set_favorite_organization_mode);self.favorite_organize_overlay.hide()
        self.selection_clear_overlay=QToolButton(root);self.selection_clear_overlay.setText('×');self.selection_clear_overlay.setToolTip('Clear selection');self.selection_clear_overlay.setFixedSize(58,36);self.selection_clear_overlay.clicked.connect(lambda:self.grid.clear_selection());self.selection_clear_overlay.hide()
        self.selection_all_overlay=QToolButton(root);self.selection_all_overlay.setText('☑');self.selection_all_overlay.setToolTip('Select all items on this page (Ctrl+A)');self.selection_all_overlay.setFixedSize(58,36);self.selection_all_overlay.clicked.connect(self.grid.select_all_current);self.selection_all_overlay.hide()
        self.selection_undo_overlay=QToolButton(root);self.selection_undo_overlay.setText('↶');self.selection_undo_overlay.setToolTip('Undo last deselection (Ctrl+Z)');self.selection_undo_overlay.setFixedSize(58,36);self.selection_undo_overlay.clicked.connect(self.grid.undo_deselection);self.selection_undo_overlay.hide()
        self.set_button_asset(self.favorite_group_overlay,'createfavoritegroup.png');self.set_button_asset(self.favorite_organize_overlay,'organizationmode.png');self.set_button_asset(self.selection_clear_overlay,'clearselect.png');self.set_button_asset(self.selection_undo_overlay,'undo.png')
        self.grid.verticalScrollBar().rangeChanged.connect(lambda *_:self.position_right_controls())
        self.activity_overlay=ActivityOverlay(root);self.activity_overlay.raise_()
        self.operation_show_timer=QTimer(self);self.operation_show_timer.setSingleShot(True);self.operation_show_timer.setInterval(220);self.operation_show_timer.timeout.connect(self.show_pending_operation)
        self._pending_operation_text=''
        self.update_selection_button()
    def toggle_selection(self):
        self.grid.clear_selection()
        self.grid.setFocus()
    def update_selection_button(self):
        if not hasattr(self,'selection_clear_overlay'):return
        count=len(getattr(self.grid,'selected_paths',())) if hasattr(self,'grid') else 0
        self.selection_clear_overlay.setToolTip(f'Clear selection ({count} selected)' if count else 'Clear selection')
        if hasattr(self,'farviewbtn'):self.farviewbtn.setToolTip('Create a Farview tab from the selected files and folders' if count else f'Create a Farview tab from {self.current} and its image subfolders')
        if hasattr(self,'activity_overlay'):
            files=folders=total=unknown=0
            for key in self.grid.selected_paths:
                if str(key).startswith('favgroup://'):continue
                item=next(iter(self.grid.items_by_path.get(str(key),())),None)
                if item is not None:
                    self.grid.selected_metadata[str(key)]={'is_dir':bool(item.get('is_dir')),'size':item.get('size')}
                metadata=self.grid.selected_metadata.get(str(key),{})
                if (item is not None and item.get('is_dir')) or (item is None and metadata.get('is_dir')):
                    folders+=1;size=self.grid.folder_size_cache.get(str(key),(item.get('size') if item is not None else metadata.get('size')))
                    if size is None:
                        unknown+=1
                        if item is not None:self.grid.request_folder_size(item)
                    else:total+=max(0,int(size))
                else:
                    files+=1
                    size=item.get('size') if item is not None else metadata.get('size')
                    if size is None:unknown+=1
                    else:total+=max(0,int(size))
            self.grid.selected_metadata={key:value for key,value in self.grid.selected_metadata.items() if key in self.grid.selected_paths}
            label=f'{files} file(s) · {folders} folder(s) · {self.format_bytes(total)}'
            if unknown:label+=f' · {unknown} size(s) calculating'
            self.activity_overlay.set_selection(label if files+folders else '')
            self.position_activity_overlay()
    @staticmethod
    def format_bytes(value):
        size=max(0,float(value));units=('B','KiB','MiB','GiB','TiB')
        for unit in units:
            if size<1024 or unit==units[-1]:return f'{size:.0f} {unit}' if unit=='B' else f'{size:.1f} {unit}'
            size/=1024
    def position_activity_overlay(self):
        if not hasattr(self,'activity_overlay') or not hasattr(self,'root_widget'):return
        overlay=self.activity_overlay;overlay.adjustSize();margin=14
        overlay.move(max(margin,self.root_widget.width()-overlay.width()-margin),max(margin,self.root_widget.height()-overlay.height()-margin));overlay.raise_()
    def begin_operation(self,message):
        self._pending_operation_text=str(message)
        if hasattr(self,'activity_overlay'):self.activity_overlay.set_operation('')
        self.operation_show_timer.start()
    def show_pending_operation(self):
        if self._pending_operation_text and hasattr(self,'activity_overlay'):
            self.activity_overlay.set_operation(self._pending_operation_text);self.position_activity_overlay()
    def finish_operation(self):
        self.operation_show_timer.stop();self._pending_operation_text=''
        if hasattr(self,'activity_overlay'):self.activity_overlay.set_operation('');self.position_activity_overlay()
    def hide_favorite_controls(self):
        for widget in self.right_control_widgets():widget.hide()
    def right_control_widgets(self):
        names=('favorite_group_overlay','favorite_organize_overlay','selection_clear_overlay','selection_all_overlay','selection_undo_overlay')
        return [getattr(self,name) for name in names if hasattr(self,name)]
    def position_right_controls(self):
        if not hasattr(self,'selection_clear_overlay'):return
        scrollbar=self.grid.verticalScrollBar();button_width=58;gap=4;x=self.root_widget.width()-button_width-gap
        if scrollbar.isVisible() and scrollbar.width()>0:
            scrollbar_left=scrollbar.mapTo(self.root_widget,QPoint(0,0)).x();x=scrollbar_left-button_width-gap
        y=self.main_bar.height()+self.nav_bar.height()+5
        widgets=([self.favorite_group_overlay] if self.in_favorites_view and self.active_favorite_group is None else [])+[self.favorite_organize_overlay]
        widgets += [self.selection_clear_overlay,self.selection_all_overlay,self.selection_undo_overlay]
        for row,widget in enumerate(widgets):widget.setGeometry(x,y+row*42,button_width,36)
    def show_right_controls(self):
        self.position_right_controls()
        if self.in_favorites_view and self.active_favorite_group is None:self.favorite_group_overlay.show()
        else:self.favorite_group_overlay.hide()
        self.favorite_organize_overlay.show()
        self.selection_clear_overlay.show();self.selection_all_overlay.show();self.selection_undo_overlay.show()
        for widget in self.right_control_widgets():widget.raise_()
    def search_has_focus(self):
        search=getattr(self,'search',None);focus=QApplication.focusWidget()
        return search is not None and (focus is search or (focus is not None and search.isAncestorOf(focus)))
    def toggle_search_navigation_focus(self):
        if QApplication.focusWidget() is self.search:
            self.grid.setFocus()
            if self.grid.navigation_index<0 and self.grid.items:self.grid.navigation_index=min(self.grid.visible_range()[0],len(self.grid.items)-1)
            self.grid.ensure_navigation_visible(self.grid.navigation_index)
            if self.auto_hide_main and not self.main_hide_timer.isActive():self.main_hide_timer.start(self.main_hide_delay)
            return
        if self.auto_hide_main:
            self.main_hide_timer.stop();self.set_main_panel_visible(True)
        else:self.set_main_panel_visible(True)
        self.search.setFocus(Qt.ShortcutFocusReason);self.search.selectAll()
    def viewer_closed(self,viewer=None):
        if viewer is None or getattr(self,'viewer',None) is viewer:self.viewer=None
        if self._closing:return
        if not self.auto_hide_tree and getattr(self,'tree_was_visible_before_viewer',False) and hasattr(self,'tree_dock'):self.tree_dock.show();self.refresh_file_tree()
        self.tree_was_visible_before_viewer=False
    def set_favorite_organization_mode(self,enabled):
        self.favorite_organize_mode=bool(enabled);self.favorite_organize_overlay.setToolTip('Organization mode (Ctrl)')
        if self.favorite_organize_overlay.isChecked()!=self.favorite_organize_mode:self.favorite_organize_overlay.setChecked(self.favorite_organize_mode)
        if enabled and self.sort!='manual':self.set_sort('manual')
        self.grid.viewport().update()

    def enable_ctrl_organization_mode(self):
        active=QApplication.activeWindow()
        if self.ctrl_key_down and not isinstance(active,MediaViewer):
            self.ctrl_organization_held=True;self.set_favorite_organization_mode(True)

    def configure_thumbnail_cache_location(self):
        selected=QFileDialog.getExistingDirectory(self,'Choose thumbnail cache folder',str(self.persistent_thumb_dir))
        if not selected:return
        target=Path(selected).expanduser().resolve()
        try:target.mkdir(parents=True,exist_ok=True)
        except OSError as exc:QMessageBox.warning(self,'Thumbnail cache location',f'Could not use this folder:\n{exc}');return
        if not target.is_dir():QMessageBox.warning(self,'Thumbnail cache location','The selected location is not a folder.');return
        self.persistent_thumb_dir=target;self.thumbnail_cache_dir=str(target)
        self.grid.persistent_folder_cache_dir=target;self.grid._thumb_cache_roots_cache=None;self.grid._thumb_cache_roots_generation=None
        self.save_settings();self.status.setText(f'Thumbnail cache: {target}')
    def configure_thumbnail_cache_limit(self):
        dlg=QDialog(self);dlg.setWindowTitle('Thumbnail memory cache');layout=QVBoxLayout(dlg)
        row=QHBoxLayout();row.addWidget(QLabel('Maximum thumbnail memory'));limit=QSpinBox();limit.setRange(16,8192);limit.setSingleStep(256);limit.setSuffix(' MiB');limit.setValue(self.thumbnail_cache_limit_mib);row.addWidget(limit);layout.addLayout(row)
        note=QLabel('The cache grows only as thumbnails are used. Lowering the limit immediately removes the least recently used thumbnails. The SSD cache for pinned folder previews is separate.');note.setWordWrap(True);layout.addWidget(note)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel);buttons.accepted.connect(dlg.accept);buttons.rejected.connect(dlg.reject);layout.addWidget(buttons)
        if dlg.exec()==QDialog.Accepted:
            self.thumbnail_cache_limit_mib=limit.value();self.grid.set_cache_limit_mib(self.thumbnail_cache_limit_mib);self.thumbnail_cache_limit_action.setText(f'Memory thumbnail cache limit… ({self.thumbnail_cache_limit_mib} MiB)');self.save_settings()
    def configure_disk_thumbnail_cache_limit(self):
        dlg=QDialog(self);dlg.setWindowTitle('On-disk thumbnail cache limit');layout=QVBoxLayout(dlg)
        row=QHBoxLayout();row.addWidget(QLabel('Maximum cached thumbnails'));limit=QSpinBox();limit.setRange(256,262144);limit.setSingleStep(1024);limit.setSuffix(' MiB');limit.setValue(self.disk_thumbnail_cache_limit_mib);row.addWidget(limit);layout.addLayout(row)
        note=QLabel('The limiter is off by default. When enabled, only generated folder-preview JPEGs in Magellan’s thumbnail cache are eligible for removal, starting with the oldest files. Removed previews are regenerated when needed.');note.setWordWrap(True);layout.addWidget(note)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel);buttons.accepted.connect(dlg.accept);buttons.rejected.connect(dlg.reject);layout.addWidget(buttons)
        if dlg.exec()==QDialog.Accepted:
            self.disk_thumbnail_cache_limit_mib=limit.value();self.disk_thumbnail_cache_limit_action.setText(f'On-disk thumbnail cache limit… ({self.disk_thumbnail_cache_limit_mib} MiB)');self.save_settings()
            if self.disk_thumbnail_cache_limit_enabled:self.schedule_disk_thumbnail_cache_trim()
    def set_disk_thumbnail_cache_limit_enabled(self,enabled):
        self.disk_thumbnail_cache_limit_enabled=bool(enabled);self.save_settings()
        if self.disk_thumbnail_cache_limit_enabled:self.schedule_disk_thumbnail_cache_trim()
    def schedule_disk_thumbnail_cache_trim(self):
        if not self.disk_thumbnail_cache_limit_enabled:return
        with self._disk_cache_trim_lock:
            if self._disk_cache_trim_future is not None and not self._disk_cache_trim_future.done():return
            cache_dir=Path(self.persistent_thumb_dir);limit_bytes=self.disk_thumbnail_cache_limit_mib*1024*1024
            def trim():
                try:
                    entries=[];total=0
                    with os.scandir(cache_dir) as items:
                        for entry in items:
                            if self._closing:return
                            # Only remove files created by _persistent_folder_thumb_path.
                            # Cache location is user-selectable and may contain personal photos.
                            if not re.fullmatch(r'[0-9a-f]{40}\.jpg',entry.name,re.IGNORECASE):continue
                            try:
                                if not entry.is_file(follow_symlinks=False):continue
                                stat=entry.stat(follow_symlinks=False);size=max(0,stat.st_size);total+=size;entries.append((stat.st_mtime_ns,size,Path(entry.path)))
                            except OSError:continue
                    if total<=limit_bytes:return
                    for _,size,path in sorted(entries):
                        if self._closing:return
                        try:path.unlink(missing_ok=True);total-=size
                        except OSError:logger.warning('Could not remove cached thumbnail %s',path,exc_info=True)
                        if total<=limit_bytes:break
                except OSError:logger.exception('Could not enforce the on-disk thumbnail cache limit')
            self._disk_cache_trim_future=THUMB_CACHE_CLEANUP_POOL.submit(trim)
    def configure_log_storage_limit(self):
        dlg=QDialog(self);dlg.setWindowTitle('Application log storage');layout=QVBoxLayout(dlg)
        row=QHBoxLayout();row.addWidget(QLabel('Maximum total log storage'));limit=QSpinBox();limit.setRange(1,64);limit.setSingleStep(1);limit.setSuffix(' MiB');limit.setValue(self.log_storage_mib);row.addWidget(limit);layout.addLayout(row)
        note=QLabel('Magellan keeps the current log and one rotated log. Repeated identical messages are coalesced, and older log data is trimmed when you lower this limit.');note.setWordWrap(True);layout.addWidget(note)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel);buttons.accepted.connect(dlg.accept);buttons.rejected.connect(dlg.reject);layout.addWidget(buttons)
        if dlg.exec()==QDialog.Accepted:
            self.log_storage_mib=limit.value();configure_logging(self.cache_dir/'logs',self.log_storage_mib);self.log_storage_action.setText(f'Maximum log storage… ({self.log_storage_mib} MiB)');self.save_settings()
    def set_thumbnail_cache_enabled(self,enabled):
        self.thumbnail_cache_enabled=bool(enabled)
        if hasattr(self,'grid'):
            self.grid.cache.clear();self.grid.cache_bytes=0;self.grid.clear_card_cache();self.grid.viewport().update()
        self.save_settings()
    def toggle_file_tree(self):
        active=QApplication.activeWindow()
        if active is not None and active is not self:return
        created=not hasattr(self,'tree_dock')
        if created:
            self.build_file_tree()
        if created:
            if self.auto_hide_tree:self.show_tree_overlay()
            else:self.tree_dock.show()
        elif self.tree_dock.isVisible():
            self.tree_hide_timer.stop();self.save_tree_navigation_state();self.tree_dock.hide()
        elif self.auto_hide_tree:self.show_tree_overlay()
        else:self.tree_dock.show()
        if created and self.tree_dock.isVisible() and not getattr(self,'_tree_initialized',False):self.refresh_file_tree()

    def show_tree_overlay(self):
        if not hasattr(self,'tree_dock'):return
        origin=self.root_widget.mapToGlobal(QPoint(0,0))
        self.tree_dock.setGeometry(origin.x(),origin.y(),300,self.root_widget.height())
        self.tree_dock.show();self.tree_dock.raise_();self.tree_hide_timer.stop()
        if not getattr(self,'_tree_initialized',False):self.refresh_file_tree()

    def tree_panel_contains_global(self,position):
        if not hasattr(self,'tree_dock') or not self.tree_dock.isVisible():return False
        origin=self.tree_dock.mapToGlobal(QPoint(0,0))
        if QRect(origin,self.tree_dock.size()).adjusted(-4,-4,4,4).contains(position):return True
        if hasattr(self,'file_tree'):
            bar=self.file_tree.verticalScrollBar();bar_origin=bar.mapToGlobal(QPoint(0,0))
            if QRect(bar_origin,bar.size()).adjusted(-5,-3,5,3).contains(position):return True
        return False

    def begin_tree_scrollbar_interaction(self):
        self.tree_scrollbar_interacting=True
        self.tree_hide_timer.stop()

    def finish_tree_scrollbar_interaction(self):
        self.tree_scrollbar_interacting=False
        if self.auto_hide_tree and hasattr(self,'tree_dock') and self.tree_dock.isVisible():
            if self.tree_panel_contains_global(QCursor.pos()):self.tree_hide_timer.stop()
            else:self.tree_hide_timer.start(self.tree_hide_delay)

    def build_file_tree(self):
        self.tree_scroll_save_timer=QTimer(self);self.tree_scroll_save_timer.setSingleShot(True);self.tree_scroll_save_timer.timeout.connect(self.persist_tree_scroll_position)
        self._tree_rebuilding=False;self._tree_restoring_scroll=False;self._tree_initialized=False;self.tree_scrollbar_interacting=False
        self.tree_dock=QDockWidget('File Tree',self)
        self.tree_dock.setObjectName('fileTreeDock')
        self.tree_dock.setAllowedAreas(Qt.LeftDockWidgetArea)
        self.tree_dock.setFeatures(QDockWidget.DockWidgetClosable)
        title=QWidget(self.tree_dock);title_row=QHBoxLayout(title);title_row.setContentsMargins(8,2,4,2);title_row.setSpacing(4)
        title_label=QLabel('File Tree',title);title_label.setStyleSheet('color:#eee;font-weight:600;');title_row.addWidget(title_label);title_row.addStretch(1)
        close_button=QToolButton(title);close_button.setObjectName('treeCloseButton');close_button.setText('×');close_button.setToolTip('Close file tree');close_button.setFixedSize(28,26);close_button.setStyleSheet('QToolButton#treeCloseButton{min-width:0;min-height:0;padding:0;}');close_button.clicked.connect(self.close_file_tree_panel);title_row.addWidget(close_button);self.tree_dock.setTitleBarWidget(title)
        self.file_tree=QTreeWidget()
        self._tree_generation=0;self._tree_scan_active=set();self._tree_bridge=TreeScanBridge(self);self._tree_bridge.completed.connect(self.finish_tree_scan)
        self.file_tree.setHeaderHidden(True)
        self.file_tree.setIndentation(16)
        self.file_tree.setAnimated(True)
        self.file_tree.itemExpanded.connect(self.expand_tree_item)
        self.file_tree.itemExpanded.connect(self.remember_tree_expanded)
        self.file_tree.itemCollapsed.connect(self.remember_tree_collapsed)
        self.file_tree.currentItemChanged.connect(self.remember_tree_selection)
        self.file_tree.verticalScrollBar().valueChanged.connect(self.remember_tree_scroll)
        self.file_tree.verticalScrollBar().sliderPressed.connect(self.begin_tree_scrollbar_interaction)
        self.file_tree.verticalScrollBar().sliderReleased.connect(self.finish_tree_scrollbar_interaction)
        self.file_tree.itemClicked.connect(self.file_tree_clicked)
        self.file_tree.itemDoubleClicked.connect(self.file_tree_double_clicked)
        self.file_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.file_tree.customContextMenuRequested.connect(self.file_tree_context_menu)
        self.tree_dock.setWidget(self.file_tree)
        self.addDockWidget(Qt.LeftDockWidgetArea,self.tree_dock)

    def _tree_path(self,item):
        raw=item.data(0,TREE_PATH_ROLE)
        if not raw or str(raw).startswith('__'): return None
        return Path(str(raw))

    def save_tree_navigation_state(self):
        """Capture tree expansion and selection before the panel is hidden."""
        if not hasattr(self,'file_tree'):
            return
        timer=getattr(self,'tree_scroll_save_timer',None)
        if timer is not None:timer.stop()
        stable=not (getattr(self,'_tree_rebuilding',False) or getattr(self,'_tree_restoring_scroll',False) or getattr(self,'_tree_scroll_restore_pending',False))
        if stable:
            for top_index in range(self.file_tree.topLevelItemCount()):
                stack=[self.file_tree.topLevelItem(top_index)]
                while stack:
                    node=stack.pop();path=self._tree_path(node)
                    if path:
                        key=str(path)
                        if node.isExpanded():self.tree_expanded_paths.add(key)
                        else:self.tree_expanded_paths.discard(key)
                    stack.extend(node.child(i) for i in range(node.childCount()))
            self.tree_scroll_value=self.file_tree.verticalScrollBar().value()
            self.capture_tree_scroll_anchor()
            current=self.file_tree.currentItem();path=self._tree_path(current) if current else None
            if path:self.tree_selected_path=str(path)
        self.save_settings()

    def close_file_tree_panel(self):
        self.save_tree_navigation_state()
        if hasattr(self,'tree_dock'):self.tree_dock.hide()

    def remember_tree_scroll(self,value):
        if getattr(self,'_tree_rebuilding',False) or getattr(self,'_tree_restoring_scroll',False):return
        self.tree_scroll_value=max(0,int(value))
        self.capture_tree_scroll_anchor()
        timer=getattr(self,'tree_scroll_save_timer',None)
        if timer is not None:timer.start(350)

    def capture_tree_scroll_anchor(self):
        """Remember the first visible real folder and its viewport-relative offset."""
        if not hasattr(self,'file_tree'):return
        item=self.file_tree.itemAt(QPoint(8,2))
        path=self._tree_path(item) if item is not None else None
        if path is None:return
        rect=self.file_tree.visualItemRect(item)
        self.tree_scroll_anchor_path=str(path)
        self.tree_scroll_anchor_offset=int(rect.top())

    def persist_tree_scroll_position(self):
        if self._closing:return
        try:self.save_settings()
        except Exception:logger.exception('Could not persist File Tree scroll position')

    def _add_tree_placeholder(self,item):
        child=QTreeWidgetItem([''])
        child.setData(0,TREE_PATH_ROLE,'__placeholder__')
        item.addChild(child)

    def _add_tree_folder(self,parent,path):
        child=QTreeWidgetItem([path.name or str(path)])
        child.setData(0,TREE_PATH_ROLE,str(path))
        self._add_tree_placeholder(child)
        parent.addChild(child)
        return child

    def refresh_file_tree(self):
        if not hasattr(self,'file_tree') or not self.tree_dock.isVisible(): return
        if not getattr(self,'_tree_rebuilding',False) and not getattr(self,'_tree_scroll_restore_pending',False):
            self.tree_scroll_value=self.file_tree.verticalScrollBar().value()
            self.capture_tree_scroll_anchor()
            for top_index in range(self.file_tree.topLevelItemCount()):
                stack=[self.file_tree.topLevelItem(top_index)]
                while stack:
                    node=stack.pop();path=self._tree_path(node)
                    if path:
                        if node.isExpanded():self.tree_expanded_paths.add(str(path))
                        else:self.tree_expanded_paths.discard(str(path))
                    stack.extend(node.child(i) for i in range(node.childCount()))
            current=self.file_tree.currentItem();current_path=self._tree_path(current) if current else None
            if current_path:self.tree_selected_path=str(current_path)
        self._tree_scroll_restore_pending=True
        self._tree_generation+=1
        self._tree_rebuilding=True
        self.file_tree.clear()
        fav_root=QTreeWidgetItem(['★ Tree Favorites'])
        fav_root.setData(0,TREE_PATH_ROLE,'__favorites__')
        self.file_tree.addTopLevelItem(fav_root)
        for raw in sorted(self.tree_favorites,key=natural_key):
            p=Path(raw)
            if p.is_dir():
                item=QTreeWidgetItem([(p.name or str(p))+'  ★'])
                item.setData(0,TREE_PATH_ROLE,str(p))
                self._add_tree_placeholder(item)
                fav_root.addChild(item)
        fav_root.setExpanded(bool(fav_root.childCount()))
        for drive in QDir.drives():
            p=Path(drive.absoluteFilePath())
            item=QTreeWidgetItem([str(p)])
            item.setData(0,TREE_PATH_ROLE,str(p))
            self._add_tree_placeholder(item)
            self.file_tree.addTopLevelItem(item)
        self.file_tree.setStyleSheet('QTreeWidget{background:#15171c;color:#ddd;border:0;padding:8px 5px;font-size:13px;} QTreeWidget::item{padding:5px 4px;} QTreeWidget::item:hover{background:#2b2d31;} QTreeWidget::item:selected{background:#51441d;color:#fff;}')
        self._tree_initialized=True
        generation=self._tree_generation
        QTimer.singleShot(0,lambda token=generation:self.restore_tree_navigation(token))

    def _find_tree_item(self,target):
        if not target or not hasattr(self,'file_tree'):return None
        target_key=self._normalized_path_key(target);stack=[self.file_tree.topLevelItem(i) for i in range(self.file_tree.topLevelItemCount())]
        while stack:
            item=stack.pop();path=self._tree_path(item)
            if path and self._normalized_path_key(path)==target_key:return item
            stack.extend(item.child(i) for i in range(item.childCount()))
        return None

    def restore_tree_navigation(self,generation=None):
        if not hasattr(self,'file_tree'):return
        if generation is not None and generation!=self._tree_generation:return
        targets=sorted(self.tree_expanded_paths,key=lambda path:(str(path).count(os.sep),str(path).casefold()))
        ordered_targets=targets+([self.tree_selected_path] if self.tree_selected_path else [])+([self.tree_scroll_anchor_path] if self.tree_scroll_anchor_path else [])
        for path in ordered_targets:
            item=self._find_tree_item(path)
            if item is None:
                parent=self._find_tree_item(str(Path(path).parent))
                children=parent.data(0,TREE_CHILDREN_ROLE) if parent is not None else None
                if parent is not None and isinstance(children,list):
                    while item is None and int(parent.data(0,TREE_PAGE_ROLE) or 0)<len(children):
                        self.append_tree_page(parent);item=self._find_tree_item(path)
            should_expand=path in self.tree_expanded_paths or path==self.tree_selected_path
            if item is not None and should_expand and not item.isExpanded():item.setExpanded(True)
        selected=self._find_tree_item(self.tree_selected_path)
        if selected is not None:self.file_tree.setCurrentItem(selected)
        if self._tree_scroll_restore_pending:
            if any(scan_generation==self._tree_generation for scan_generation,_ in getattr(self,'_tree_scan_active',())):
                QTimer.singleShot(40,lambda token=self._tree_generation:self.restore_tree_navigation(token))
            else:
                self._tree_restoring_scroll=True
                try:
                    bar=self.file_tree.verticalScrollBar()
                    anchor=self._find_tree_item(self.tree_scroll_anchor_path) if self.tree_scroll_anchor_path else None
                    if anchor is not None:
                        self.file_tree.scrollToItem(anchor,QAbstractItemView.PositionAtTop)
                        bar.setValue(max(0,min(bar.value()-self.tree_scroll_anchor_offset,bar.maximum())))
                    else:bar.setValue(min(self.tree_scroll_value,bar.maximum()))
                    self.tree_scroll_value=bar.value();self.capture_tree_scroll_anchor();self._tree_scroll_restore_pending=False
                finally:
                    self._tree_restoring_scroll=False;self._tree_rebuilding=False

    def remember_tree_expanded(self,item):
        path=self._tree_path(item)
        if path:
            self.tree_expanded_paths.add(str(path));self.save_settings()

    def remember_tree_collapsed(self,item):
        path=self._tree_path(item)
        if path:
            self.tree_expanded_paths.discard(str(path));self.save_settings()

    def remember_tree_selection(self,item,previous):
        path=self._tree_path(item) if item is not None else None
        if path:
            self.tree_selected_path=str(path);self.save_settings()

    def expand_tree_item(self,item):
        role=item.data(0,TREE_PATH_ROLE)
        if role=='__favorites__' or role=='__more__' or role=='__loading__':return
        if item.data(0,TREE_POPULATED_ROLE) or item.data(0,TREE_POPULATING_ROLE): return
        path=self._tree_path(item)
        if not path:return
        if item.childCount()==1 and item.child(0).data(0,TREE_PATH_ROLE)=='__placeholder__':item.takeChild(0)
        generation=self._tree_generation;path_text=str(path);scan_key=(generation,path_text)
        if len(self._tree_scan_active)>=16:
            item.setData(0,TREE_POPULATING_ROLE,False);self._add_tree_placeholder(item);self.status.setText('File Tree is busy; expand this folder again in a moment');return
        self._tree_scan_active.add(scan_key)
        item.setData(0,TREE_POPULATING_ROLE,True)
        loading=QTreeWidgetItem(['Loading…']);loading.setData(0,TREE_PATH_ROLE,'__loading__');item.addChild(loading)
        def scan_children():
            dirs=[];error=''
            try:
                with os.scandir(path_text) as entries:
                    for entry in entries:
                        if self._closing:return
                        try:
                            if entry.is_dir(follow_symlinks=False) and (not entry.name.startswith('.') or entry.name.startswith('. ')):
                                dirs.append(entry.path)
                        except OSError:continue
                dirs.sort(key=lambda value:natural_key(Path(value).name))
            except Exception as exc:
                logger.exception('File Tree scan failed for %s',path_text);error=str(exc)
            if not self._closing:self._tree_bridge.completed.emit(item,path_text,dirs,generation,error)
        TREE_SCAN_POOL.submit(scan_children)

    def finish_tree_scan(self,item,path,dirs,generation,error):
        self._tree_scan_active.discard((generation,path))
        if generation!=self._tree_generation:return
        try:
            if item.treeWidget() is not self.file_tree or str(self._tree_path(item))!=path:return
            while item.childCount():item.takeChild(0)
            item.setData(0,TREE_POPULATING_ROLE,False);item.setData(0,TREE_POPULATED_ROLE,True);item.setData(0,TREE_CHILDREN_ROLE,dirs);item.setData(0,TREE_PAGE_ROLE,0)
            self.append_tree_page(item)
            QTimer.singleShot(0,lambda token=generation:self.restore_tree_navigation(token))
            if error and not dirs:self.status.setText(f'Cannot read File Tree folder: {error}')
        except RuntimeError:
            return

    def append_tree_page(self,parent):
        children=parent.data(0,TREE_CHILDREN_ROLE) or [];start=int(parent.data(0,TREE_PAGE_ROLE) or 0);end=min(len(children),start+TREE_PAGE_SIZE)
        for raw in children[start:end]:self._add_tree_folder(parent,Path(raw))
        parent.setData(0,TREE_PAGE_ROLE,end)
        if end<len(children):
            more=QTreeWidgetItem([f'Load next {min(TREE_PAGE_SIZE,len(children)-end)} folders…']);more.setData(0,TREE_PATH_ROLE,'__more__');parent.addChild(more)

    def file_tree_clicked(self,item,column):
        if item.data(0,TREE_PATH_ROLE)=='__more__':
            parent=item.parent()
            if parent is not None:
                parent.removeChild(item);self.append_tree_page(parent)
            return
        path=self._tree_path(item)
        if path and path.is_dir():self.tree_selected_path=str(path);self.save_settings();self.load(path)

    def file_tree_double_clicked(self,item,column):
        path=self._tree_path(item)
        if path and path.is_dir():
            try:
                os.startfile(str(path))
            except Exception as e:
                self.status.setText(f'Could not open in Explorer: {e}')

    def file_tree_context_menu(self,pos):
        item=self.file_tree.itemAt(pos)
        path=self._tree_path(item) if item else None
        if not path or not path.is_dir():
            return
        menu=QMenu(self.file_tree)
        key=str(path)
        if key in self.tree_favorites:
            act=menu.addAction('Remove from tree favorites')
        else:
            act=menu.addAction('Add to tree favorites')
        chosen=menu.exec(self.file_tree.viewport().mapToGlobal(pos))
        if chosen==act:
            if key in self.tree_favorites:
                self.tree_favorites.remove(key)
            else:
                self.tree_favorites.add(key)
            self.save_settings()
            self.refresh_file_tree()

    def addbtn(self,lay,text,tip,fn,style='top'):
        q=QToolButton();q.setText(text);q.setToolTip(tip);q.setObjectName(style if style else 'toolbtn');q.clicked.connect(fn);lay.addWidget(q);return q
    def set_button_asset(self,button,filename):
        path=self.icon_dir/filename
        if path.is_file():
            button.setIcon(QIcon(str(path)));button.setIconSize(QSize(20,20));button.setText('')
        else:button.setToolTip(f"{button.toolTip()} (icon missing: {filename})")
    def current_tab_index(self):
        return self.tabbar.currentIndex() if hasattr(self,'tabbar') else -1

    def add_tab(self, folder, select=False, tab=None):
        folder=Path(folder)
        if not folder.is_dir(): return
        data=dict(tab or {})
        data.setdefault('kind','folder');data['path']=str(folder)
        if data['kind']=='farview':data.setdefault('view_id',self._normalized_path_key(folder));data.setdefault('source_order',[])
        data.setdefault('scroll',self.scroll_positions.get(str(folder),0));data.setdefault('positions',{str(folder):self.scroll_positions.get(str(folder),0)})
        data.setdefault('sort',self.sort);data.setdefault('desc',self.desc)
        self.tabs.append(data)
        prefix=('Farview selection · ' if data['kind']=='farview' and data.get('selection') else ('Farview · ' if data['kind']=='farview' else ''))
        idx=self.tabbar.addTab(prefix+(folder.name or str(folder)))
        self.tabbar.setTabData(idx,data.get('color',''))
        self.tabbar.setTabToolTip(idx,('Farview selection from chosen items' if data.get('selection') else ('Farview of ' if data['kind']=='farview' else ''))+str(folder))
        if select:
            self.tabbar.setCurrentIndex(idx)

    def restore_tabs(self):
        records=[]
        for value in self.saved_tabs:
            if not isinstance(value,dict):continue
            path=Path(value.get('path',''))
            if path.is_dir():records.append((path,value))
        self.tabbar.blockSignals(True)
        try:
            for path,value in records:self.add_tab(path,tab=value)
            if not self.tabs and not getattr(self,'has_saved_tabs',False):self.add_tab(self.current)
            active=max(0,min(self.saved_active_tab,len(self.tabs)-1)) if self.tabs else -1
            self.tabbar.setCurrentIndex(active)
        finally:self.tabbar.blockSignals(False)
        if self.tabs:self.activate_tab(self.tabbar.currentIndex(),save_previous=False)
        else:self.grid.setItems([]);self.crumb.setText('Magellan');self.status.setText('No open tabs')

    def activate_tab(self, idx, save_previous=True):
        if idx<0 or idx>=len(self.tabs): return
        if save_previous:self.save_current_view()
        self.return_to_favorites=False;self.favorites_navigation_root=None;self.favorite_return_group=None
        self.active_tab_index=idx
        tab=self.tabs[idx];target=Path(tab['path']);self.current=target
        if tab.get('kind')=='farview':
            if not tab.get('unsorted',True):self.sort=tab.get('sort',self.sort);self.desc=bool(tab.get('desc',False))
            self.load_farview(target,save_previous=False,selection=tab.get('selection'))
        else:
            self.sort=tab.get('sort',self.sort);self.desc=bool(tab.get('desc',self.desc));self.load(target, restore_scroll=True, save_previous=False);self.update_sort_buttons()

    def switch_tab(self, idx):
        QTimer.singleShot(0,lambda i=idx:self.activate_tab(i) if i==self.tabbar.currentIndex() else None)

    def begin_tab_drag(self):
        self._tab_drag_active=True
        if self.auto_hide_main:
            self.main_hide_timer.stop();self.tab_reveal_timer.stop();self.set_main_panel_visible(True)

    def end_tab_drag(self):
        self._tab_drag_active=False
        if self.auto_hide_main:
            pos=self.root_widget.mapFromGlobal(QCursor.pos())
            over_panel=self.main_bar.geometry().contains(pos) or (self.nav_bar.isVisible() and self.nav_bar.geometry().contains(pos))
            if not over_panel and not getattr(self,'tab_context_menu_open',False):self.main_hide_timer.start(self.main_hide_delay)

    def tab_reordered(self,source,destination):
        if source<0 or destination<0 or source>=len(self.tabs) or destination>=len(self.tabs):return
        tab=self.tabs.pop(source);self.tabs.insert(destination,tab)
        def remap(index):
            if index==source:return destination
            if source<destination and source<index<=destination:return index-1
            if destination<source and destination<=index<source:return index+1
            return index
        self.tabbar.selected_tabs={remap(index) for index in self.tabbar.selected_tabs if 0<=index<len(self.tabs)}
        self.tabbar.selection_anchor=remap(self.tabbar.selection_anchor) if self.tabbar.selection_anchor>=0 else -1
        for index,record in enumerate(self.tabs):self.tabbar.setTabData(index,record.get('color',''))
        self.active_tab_index=self.tabbar.currentIndex();self.save_settings();self.tabbar.update()

    def close_tab(self, idx):
        if idx is None or idx<0 or idx>=len(self.tabs): return
        now=time.monotonic()
        if getattr(self,'_last_tab_close',None) and self._last_tab_close[0]==idx and now-self._last_tab_close[1]<.18:return
        self._last_tab_close=(idx,now)
        self.save_current_view()
        was_current=(idx==self.current_tab_index())
        old_current=self.current_tab_index()
        new_idx=max(0,min(idx,len(self.tabs)-2)) if was_current else (old_current-1 if idx<old_current else old_current)
        self.tabbar.blockSignals(True)
        try:
            self.tabs.pop(idx)
            self.tabbar.removeTab(idx)
            self.tabbar.reindex_after_remove(idx)
            if self.tabs:
                new_idx=max(0,min(new_idx,len(self.tabs)-1))
                self.tabbar.setCurrentIndex(new_idx)
        finally:
            self.tabbar.blockSignals(False)
        if was_current:
            self.active_tab_index=-1
            if self.tabs:self.activate_tab(new_idx,save_previous=False)
            else:
                self.current=Path.home();self.items=[];self.grid.setItems([]);self.crumb.setText('Magellan');self.status.setText('No open tabs')
        elif idx<old_current:self.active_tab_index=max(0,old_current-1)
        self.save_settings()

    def remember_scroll(self, value):
        if self._suppress_scroll_save or self.in_favorites_view or self.return_to_favorites:return
        path=str(self.current)
        self.scroll_positions[path]=int(value)
        idx=self.active_tab_index
        if 0<=idx<len(self.tabs) and self.tabs[idx].get('path')==path:self.tabs[idx]['scroll']=int(value);self.tabs[idx].setdefault('positions',{})[path]=int(value)

    def save_current_view(self):
        self.remember_scroll(self.grid.verticalScrollBar().value())
        idx=self.current_tab_index()
        if self.in_favorites_view or self.return_to_favorites:return
        if 0<=idx<len(self.tabs):
            self.tabs[idx].update({'sort':self.sort,'desc':self.desc})
            if self.tabs[idx].get('kind')!='farview':self._remember_folder_sort(self.current)
        elif Path(self.current).is_dir():self._remember_folder_sort(self.current)

    def _remember_folder_sort(self,folder):
        if not Path(folder).is_dir():return
        key=self._normalized_path_key(folder)
        self.folder_sort_settings.pop(key,None)
        self.folder_sort_settings[key]={'sort':self.sort,'desc':bool(self.desc)}
        while len(self.folder_sort_settings)>5000:self.folder_sort_settings.pop(next(iter(self.folder_sort_settings)))

    def show_tab_context_menu(self,index,global_pos):
        self.main_hide_timer.stop();self.tab_context_menu_open=True
        if self.auto_hide_main:self.set_main_panel_visible(True)
        menu=QMenu(self);menu.setMouseTracking(True);self._tab_context_menu=menu;menu.addSection('Tab outline color')
        colors=[None,'#e74c3c','#e67e22','#f1c40f','#2ecc71','#1abc9c','#3498db','#1f3a93','#9b59b6','#e84393']
        grid_widget=QWidget();grid=QGridLayout(grid_widget);grid.setContentsMargins(8,6,8,6);grid.setHorizontalSpacing(5);grid.setVerticalSpacing(5)
        action=QWidgetAction(menu);action.setDefaultWidget(grid_widget)
        for offset,color in enumerate(colors):
            button=QPushButton();button.setFixedSize(27,27)
            if color is None:
                button.setText('×');button.setToolTip('Clear tab color');button.setStyleSheet('QPushButton{background:#303238;color:#fff;border:1px solid #aaa;border-radius:2px;font-weight:bold} QPushButton:hover{border:2px solid #fff}')
            else:
                button.setToolTip(color);button.setStyleSheet(f'QPushButton{{background:{color};border:1px solid #aaa;border-radius:2px}} QPushButton:hover{{border:2px solid #fff}}')
            button.clicked.connect(lambda checked=False,value=color:(self.apply_tab_outline_color(value,index),menu.close()))
            grid.addWidget(button,offset//5,offset%5)
        menu.addAction(action)
        menu.aboutToHide.connect(self.on_tab_context_menu_closed)
        self.tab_context_menu_timer.start()
        menu.exec(global_pos)

    def close_tab_context_menu_if_cursor_left(self):
        menu=getattr(self,'_tab_context_menu',None)
        if not self.tab_context_menu_open:return
        if menu is None or not menu.isVisible():
            self.on_tab_context_menu_closed();return
        if not menu.frameGeometry().contains(QCursor.pos()):menu.close()

    def on_tab_context_menu_closed(self):
        if not self.tab_context_menu_open:return
        self.tab_context_menu_timer.stop();self.tab_context_menu_open=False;self._tab_context_menu=None
        if self.auto_hide_main and self.main_bar.isVisible():
            pos=self.root_widget.mapFromGlobal(QCursor.pos())
            over_panel=self.main_bar.geometry().contains(pos) or (self.nav_bar.isVisible() and self.nav_bar.geometry().contains(pos))
            if not over_panel:self.main_hide_timer.start(self.main_hide_delay)

    def apply_tab_outline_color(self,color,fallback_index=-1):
        indices=sorted(i for i in self.tabbar.selected_tabs if 0<=i<len(self.tabs))
        if not indices and 0<=fallback_index<len(self.tabs):indices=[fallback_index]
        value=color or ''
        for i in indices:
            self.tabs[i]['color']=value;self.tabbar.setTabData(i,value)
        if indices:self.save_settings();self.tabbar.update()

    def update_tab_labels(self):
        for i,t in enumerate(self.tabs):
            p=Path(t['path']);prefix=('Farview selection · ' if t.get('kind')=='farview' and t.get('selection') else ('Farview · ' if t.get('kind')=='farview' else ''));self.tabbar.setTabText(i,prefix+(p.name or str(p)));self.tabbar.setTabToolTip(i,('Farview selection from chosen items' if t.get('selection') else ('Farview of ' if t.get('kind')=='farview' else ''))+str(p))

    def load(self,folder,restore_scroll=False,save_previous=True):
        self.in_favorites_view=False;self.active_favorite_group=None
        if hasattr(self,'selection_clear_overlay'):
            if self.right_controls_auto_hide:self.hide_favorite_controls()
            else:self.show_right_controls()
        folder=Path(folder)
        if not folder.is_dir():return
        if not self.tabs:
            self.add_tab(folder,select=False);self.active_tab_index=0
            self.tabbar.blockSignals(True);self.tabbar.setCurrentIndex(0);self.tabbar.blockSignals(False)
        if save_previous:self.save_current_view()
        self.current=folder
        folder_sort=self.folder_sort_settings.get(self._normalized_path_key(folder))
        if isinstance(folder_sort,dict):
            self.sort=folder_sort.get('sort',self.sort);self.desc=bool(folder_sort.get('desc',False))
        self._remember_folder_sort(folder)
        # Clear the old scrollbar immediately. This prevents a newly loaded
        # folder from inheriting the previous folder's maximum scroll value
        # while the asynchronous scan is running.
        self._suppress_scroll_save=True
        try:self.grid.verticalScrollBar().setValue(0)
        finally:self._suppress_scroll_save=False
        self.update_favorite_button()
        self.crumb.setText(str(folder));self.status.setText('Scanning…')
        idx=self.current_tab_index()
        if 0<=idx<len(self.tabs) and not self.return_to_favorites:
            self.tabs[idx]['kind']='folder'
            self.tabs[idx]['path']=str(folder)
            self.tabs[idx].update({'sort':self.sort,'desc':self.desc})
            positions=self.tabs[idx].setdefault('positions',{})
            if restore_scroll:
                target_scroll=positions.get(str(folder),self.scroll_positions.get(str(folder),0))
                self.tabs[idx]['scroll']=int(target_scroll)
            else:
                self.scroll_positions[str(folder)]=0
                positions[str(folder)]=0
                self.tabs[idx]['scroll']=0
            self.update_tab_labels()
        self.update_sort_buttons()
        self.start_scan(folder,recursive=False)
        if self.recursive_search_enabled and self.search.text().strip():self.search_text_changed(self.search.text())

    def load_farview(self,root,save_previous=True,selection=None):
        root=Path(root)
        if not root.is_dir():return
        if save_previous:self.save_current_view()
        self.in_favorites_view=False;self.current=root;self._suppress_scroll_save=True
        try:self.grid.verticalScrollBar().setValue(0)
        finally:self._suppress_scroll_save=False
        self.crumb.setText(f'Farview · {root}');self.status.setText('Collecting images from subfolders…')
        idx=self.current_tab_index();self.farview_unsorted=bool(self.tabs[idx].get('unsorted',True)) if 0<=idx<len(self.tabs) else True
        if 0<=idx<len(self.tabs):self.sort=self.tabs[idx].get('sort',self.sort);self.desc=bool(self.tabs[idx].get('desc',self.desc))
        self.update_favorite_button()
        if 0<=idx<len(self.tabs):
            self.tabs[idx]['kind']='farview';self.tabs[idx]['path']=str(root);self.update_tab_labels()
        self.update_sort_buttons()
        self.start_scan(root,recursive=True,selection=selection)

    def start_scan(self,folder,recursive=False,selection=None):
        if self.scan and self.scan.isRunning():self.scan.requestInterruption()
        self.scan_generation+=1;generation=self.scan_generation
        self.begin_operation('Collecting Farview items…' if recursive else 'Scanning folder…')
        self.scan=ScanTask(folder,recursive=recursive,generation=generation,selection=selection,manual_orders=self.folder_manual_orders,manual_sort_settings=self.folder_sort_settings)
        self.scan_jobs.append(self.scan)
        self.scan.finished.connect(lambda job=self.scan:self.release_scan_job(job))
        self.scan.signals.done.connect(self.scanned)
        self.scan.start()

    def release_scan_job(self,job):
        try:self.scan_jobs.remove(job)
        except ValueError:pass

    def scanned(self,items,error,generation=None):
        if generation is not None and generation!=self.scan_generation:return
        self.finish_operation()
        if error:self.status.setText('Unable to read folder');QMessageBox.warning(self,'Magellan',error);return
        idx=self.current_tab_index()
        if 0<=idx<len(self.tabs) and self.tabs[idx].get('kind')=='farview':
            tab=self.tabs[idx];source_order=[self._manual_item_key(item) for item in items if self._manual_item_key(item)][:100000]
            tab['source_order']=source_order
        self.items=items;self.refresh();self.save_settings()
        if 0<=idx<len(self.tabs):
            target=int(self.tabs[idx].setdefault('positions',{}).get(str(self.current),self.tabs[idx].get('scroll',0)))
        else:
            target=int(self.scroll_positions.get(str(self.current),0))
        # The grid's range is established by refresh(). Restore only after the
        # new range exists, and do it twice across the event loop so late
        # layout/range updates cannot snap the view back to the old maximum.
        expected_generation=self.scan_generation;expected_path=str(self.current)
        def restore():
            if expected_generation!=self.scan_generation or expected_path!=str(self.current):return
            bar=self.grid.verticalScrollBar()
            self._suppress_scroll_save=True
            try:bar.setValue(max(0,min(target,bar.maximum())))
            finally:self._suppress_scroll_save=False
        QTimer.singleShot(0,restore)
        QTimer.singleShot(60,restore)

    def set_sort(self,k):
        idx=self.current_tab_index();farview=0<=idx<len(self.tabs) and self.tabs[idx].get('kind')=='farview'
        if k!='size':self.grid.clear_folder_size_queue()
        preserve_folder_order=farview and self.tabs[idx].get('unsorted',True)
        if k=='manual':
            if self.sort!='manual':self._seed_manual_order()
            self.sort='manual';self.desc=False
        elif k=='random':
            self.sort='random';self.desc=False
        elif self.sort==k and not preserve_folder_order:self.desc=not self.desc
        else:self.sort=k;self.desc=False
        if 0<=idx<len(self.tabs):self.tabs[idx].update({'sort':self.sort,'desc':self.desc})
        if farview:self.tabs[idx]['unsorted']=False
        elif not self.in_favorites_view and not self.return_to_favorites:self._remember_folder_sort(self.current)
        self.update_sort_buttons();self.refresh()
        self.save_settings()

    def update_sort_buttons(self):
        labels={'name':('↥','Name'),'mtime':('↧','Last modified'),'ctime':('◷','Last created'),'type':('◇','Type'),'size':('⇵','Size'),'random':('⤨','Random folders'),'manual':('↕','Personal order')}
        idx=self.current_tab_index();folder_order=0<=idx<len(self.tabs) and self.tabs[idx].get('kind')=='farview' and self.tabs[idx].get('unsorted',True)
        for key,b in self.sortBtns.items():
            icon,title=labels[key]
            active=key==self.sort and not folder_order
            composed=QPixmap(46,24);composed.fill(Qt.transparent);painter=QPainter(composed);painter.setRenderHint(QPainter.SmoothPixmapTransform,True)
            asset={'name':'list.png','mtime':'lastmod.png','type':'types.png','size':'size.png'}.get(key)
            asset_path=self.icon_dir/asset if asset else None
            source=QPixmap(str(asset_path)) if asset_path and asset_path.is_file() else QPixmap()
            if source.isNull():
                painter.setPen(QColor('#e8e8e8'));painter.setFont(QFont('Segoe UI Symbol',16));painter.drawText(QRect(0,0,23,24),Qt.AlignCenter,icon)
            else:
                scaled=source.scaled(22,22,Qt.KeepAspectRatio,Qt.SmoothTransformation)
                painter.drawPixmap((23-scaled.width())//2,1+(22-scaled.height())//2,scaled)
            if active and key!='manual':
                painter.setPen(QColor('#f5f5f5'));painter.setFont(QFont('Segoe UI Symbol',12,QFont.Bold));painter.drawText(QRect(23,0,22,24),Qt.AlignCenter,'▼' if self.desc else '▲')
            painter.end();b.setIcon(QIcon(composed));b.setText('');b.setIconSize(QSize(46,24));b.setToolTip(title if key=='manual' else (f'{title} — {"Descending" if self.desc else "Ascending"}' if active else title));b.setChecked(active)

    def _manual_view_key(self):
        if self.in_favorites_view:return 'favorites:root' if self.active_favorite_group is None else f'favorites:group:{self.active_favorite_group}'
        idx=self.current_tab_index()
        if 0<=idx<len(self.tabs) and self.tabs[idx].get('kind')=='farview':return 'farview:'+str(self.tabs[idx].get('view_id') or self._normalized_path_key(self.tabs[idx].get('path','')))
        return 'folder:'+self._normalized_path_key(self.current)

    def _record_manual_order_created(self,key):
        if key and key in self.folder_manual_orders and key not in self.manual_order_created:self.manual_order_created[key]=time.time()

    @staticmethod
    def _manual_item_key(item):
        path=str(item.get('path',''))
        if path.startswith('favgroup://'):return path
        return os.path.normcase(os.path.normpath(os.path.abspath(path))) if path else ''

    def _seed_manual_order(self):
        key=self._manual_view_key()
        if not self.folder_manual_orders.get(key):
            idx=self.current_tab_index();tab=self.tabs[idx] if 0<=idx<len(self.tabs) else {}
            source=tab.get('source_order',[]) if tab.get('kind')=='farview' else []
            self.folder_manual_orders[key]=list(source) if source else [self._manual_item_key(item) for item in self.grid.items if self._manual_item_key(item)]
            self._record_manual_order_created(key)

    def _apply_manual_order(self,items):
        key=self._manual_view_key();order=self.folder_manual_orders.get(key)
        if not order:
            idx=self.current_tab_index();tab=self.tabs[idx] if 0<=idx<len(self.tabs) else {}
            source=tab.get('source_order',[]) if tab.get('kind')=='farview' else []
            order=list(source) if source else [self._manual_item_key(item) for item in items if self._manual_item_key(item)]
            self.folder_manual_orders[key]=order;self._record_manual_order_created(key)
        ranks={value:index for index,value in enumerate(order)}
        # Stable fallback keeps newly discovered/unrecorded entries visible after existing entries.
        if self.in_favorites_view and self.active_favorite_group is None:
            items.sort(key=lambda item:(0 if item.get('kind')=='favorite_group' else 1,ranks.get(self._manual_item_key(item),len(ranks))))
        else:items.sort(key=lambda item:ranks.get(self._manual_item_key(item),len(ranks)))

    def _reorder_manual_items(self,paths,target_index,before):
        if not (0<=target_index<len(self.grid.items)):return False
        visible_keys={self._manual_item_key(item) for item in self.grid.items}
        moved={os.path.normcase(os.path.normpath(os.path.abspath(path))) for path in paths}&visible_keys
        if not moved:return False
        target=self._manual_item_key(self.grid.items[target_index])
        if target in moved:return False
        order=self.folder_manual_orders.get(self._manual_view_key()) or [self._manual_item_key(item) for item in self.items]
        order=[value for value in order if value]
        moving=[value for value in order if value in moved]
        if not moving:return False
        order=[value for value in order if value not in moved]
        try:insert_at=order.index(target)+(0 if before else 1)
        except ValueError:return False
        order[insert_at:insert_at]=moving
        self.folder_manual_orders[self._manual_view_key()]=order
        self._record_manual_order_created(self._manual_view_key())
        self.refresh();self.save_settings();return True

    def set_mode(self,m):self.mode=m;self.vbtn.setChecked(m=='vertical');self.hbtn.setChecked(m=='horizontal');self.grid.setMode(m);self.refresh()
    def set_thumb(self,n):self.thumb=int(n);self.grid.setThumbSize(n);self.refresh()
    def preset(self,n):self.slider.setValue({1:150,2:200,3:250,4:310,5:390}[n])
    def set_recursive_search_enabled(self,enabled):
        self.recursive_search_enabled=bool(enabled);self.save_settings();self.search_text_changed(self.search.text())
    def search_text_changed(self,text):
        self.recursive_search_generation+=1
        if hasattr(self,'recursive_search_timer'):self.recursive_search_timer.stop()
        for job in list(getattr(self,'recursive_search_jobs',[])):
            if job.isRunning():job.requestInterruption()
        self.recursive_search_results=None;self.recursive_search_result_query=''
        if getattr(self,'_pending_operation_text','').startswith('Searching'):self.finish_operation()
        self.refresh()
        if self.recursive_search_enabled and text.strip() and not self.in_favorites_view:
            self.status.setText('Searching subfolders…');self.begin_operation('Searching subfolders…');self.recursive_search_timer.start()
    def start_recursive_search(self):
        query=self.search.text().casefold().strip()
        if not self.recursive_search_enabled or not query or self.in_favorites_view:return
        self.begin_operation('Searching subfolders…')
        generation=self.recursive_search_generation
        task=RecursiveSearchTask(self.current,query,generation);self.recursive_search_jobs.append(task)
        task.signals.done.connect(self.recursive_search_finished)
        task.finished.connect(lambda job=task:self.release_recursive_search_job(job))
        task.start()
    def release_recursive_search_job(self,job):
        try:self.recursive_search_jobs.remove(job)
        except ValueError:pass
    def recursive_search_finished(self,items,error,generation,limited,folder):
        if generation!=self.recursive_search_generation or folder!=str(self.current) or not self.recursive_search_enabled or self.in_favorites_view:return
        if error:
            self.finish_operation();self.status.setText('Recursive search failed');QMessageBox.warning(self,'Search failed',error);return
        self.recursive_search_results=items;self.recursive_search_result_query=self.search.text().casefold().strip();self.refresh()
        if limited:self.status.setText(f'{len(items)} results — refine search (10,000 result limit)')
        else:self.status.setText(f'{len(items)} recursive search results')
        self.finish_operation()
    def refresh(self):
        q=self.search.text().casefold().strip()
        source=self.recursive_search_results if self.recursive_search_enabled and q and self.recursive_search_result_query==q and self.recursive_search_results is not None else self.items
        items=[x for x in source if not q or q in x['name'].casefold()]
        recursive_results=self.recursive_search_enabled and q and self.recursive_search_result_query==q and self.recursive_search_results is not None
        if self.sort=='size' and not recursive_results:self.grid.apply_cached_folder_sizes(items)
        def name_key(x):return natural_key(x['name'])
        def type_key(x):
            type_names={'folder':'File folder','image':'Image','video':'Video','audio':'Audio','document':'Document','file':'File'}
            return (natural_key(type_names.get(x['kind'],x['kind'])),name_key(x))
        active_idx=self.current_tab_index();is_farview=active_idx>=0 and self.tabs[active_idx].get('kind')=='farview'
        if is_farview and self.tabs[active_idx].get('unsorted',True):
            pass
        elif self.sort=='manual':
            self._apply_manual_order(items)
        elif self.sort=='random':
            if is_farview:
                random.shuffle(items)
            else:
                favorite_groups=[x for x in items if x.get('kind')=='favorite_group']
                folders=[x for x in items if x['is_dir']]
                files=[x for x in items if not x['is_dir'] and x.get('kind')!='favorite_group']
                random.shuffle(favorite_groups);random.shuffle(folders)
                files.sort(key=name_key)
                items=favorite_groups+folders+files
        else:
            if self.sort=='name':key=name_key
            elif self.sort=='type':key=type_key
            elif self.sort in ('mtime','ctime'):key=lambda x:x[self.sort]
            elif self.sort=='size':key=lambda x:-1 if x.get('size') is None else int(x['size'])
            else:key=name_key
            favorite_groups=[item for item in items if item.get('kind')=='favorite_group'] if self.in_favorites_view and self.active_favorite_group is None else []
            folders=[item for item in items if item.get('is_dir')]
            files=[item for item in items if not item.get('is_dir') and item.get('kind')!='favorite_group']
            for bucket in (favorite_groups,folders,files):bucket.sort(key=key,reverse=self.desc)
            items=favorite_groups+folders+files
        self.grid.setFolderThumbs(self.custom);self.grid.setItems(items,reset_scroll=False)
        if self.sort=='size' and not recursive_results:
            first,last=self.grid.visible_range()
            visible_indices=range(first,min(last,first+32))
            visible_paths={str(items[index]['path']) for index in visible_indices if items[index].get('is_dir')}
            self.grid.schedule_folder_sizes(items,visible_paths)
        suffix='' if self.sort in ('random','manual') else (' • '+('Descending' if self.desc else 'Ascending'))
        if is_farview and self.tabs[active_idx].get('unsorted',True):
            self.status.setText(f'{len(items)} images • Farview • folder order')
        else:
            self.status.setText(f'{len(items)} items • {self.sort}'+suffix)

    def refresh_current_view(self):
        """Reload the active view without navigating away from it."""
        if self.in_favorites_view:
            scroll=self.grid.verticalScrollBar().value();group_id=self.active_favorite_group
            self.show_favorites(group_id)
            QTimer.singleShot(0,lambda value=scroll:self.grid.verticalScrollBar().setValue(min(value,self.grid.verticalScrollBar().maximum())))
            return
        idx=self.current_tab_index()
        if 0<=idx<len(self.tabs) and self.tabs[idx].get('kind')=='farview':
            tab=self.tabs[idx]
            self.load_farview(Path(tab['path']),save_previous=False,selection=tab.get('selection'))
            return
        self.load(self.current,restore_scroll=True,save_previous=False)

    def open_new_tab(self,path):
        p=Path(path)
        if p.is_dir():
            self.save_current_view();self.add_tab(p,select=False)
            self.save_settings()
            self.reveal_panel_for_new_tab()

    def reveal_panel_for_new_tab(self):
        if not (self.auto_hide_main and self.auto_hide_tabs):return
        self.main_hide_timer.stop();self.set_main_panel_visible(True);self.tab_reveal_timer.start()

    def finish_tab_reveal(self):
        if not (self.auto_hide_main and self.auto_hide_tabs) or not self.main_bar.isVisible():return
        pos=self.root_widget.mapFromGlobal(QCursor.pos())
        if self.main_bar.geometry().contains(pos) or self.nav_bar.geometry().contains(pos):return
        self.set_main_panel_visible(False)

    def create_farview(self):
        root=Path(self.current)
        selected_keys={self._normalized_path_key(path) for path in self.grid.selected_paths if not str(path).startswith('favgroup://')}
        selected=[str(item.get('path')) for item in self.grid.items if self._normalized_path_key(item.get('path','')) in selected_keys and (Path(item['path']).is_dir() or (Path(item['path']).is_file() and Path(item['path']).suffix.casefold() in PREVIEW_EXTS))]
        selected_group_ids={str(path).split('://',1)[1] for path in self.grid.selected_paths if str(path).startswith('favgroup://')}
        if self.in_favorites_view and self.active_favorite_group is None and selected_group_ids:
            for group in self.favorite_groups:
                if group.get('id') not in selected_group_ids:continue
                for path in group.get('folders',[]):
                    candidate=Path(path)
                    if candidate.is_dir() or (candidate.is_file() and candidate.suffix.casefold() in PREVIEW_EXTS):selected.append(str(candidate))
            selected=list(dict.fromkeys(selected))
            if selected:
                first=Path(selected[0]);root=first if first.is_dir() else first.parent
        if self.in_favorites_view and self.active_favorite_group is not None and not selected:
            group=next((g for g in self.favorite_groups if g.get('id')==self.active_favorite_group),None)
            selected=[str(path) for path in (group or {}).get('folders',[]) if Path(path).is_dir() or (Path(path).is_file() and Path(path).suffix.casefold() in PREVIEW_EXTS)]
            if selected:
                first=Path(selected[0]);root=first if first.is_dir() else first.parent
            else:self.status.setText('This Favorites group has no available folders or previewable files');return
        if not root.is_dir():
            root=next((Path(path) if Path(path).is_dir() else Path(path).parent for path in selected if Path(path).exists()),None)
        if root is None or not root.is_dir():return
        # Keep selection order stable: Favorites groups already have a deliberate
        # order, while ordinary selection uses the visible natural order.
        if selected:
            # Farviews with identical selections are independent tabs so each can keep its own sort.
            self.save_current_view();self.add_tab(root,select=True,tab={'kind':'farview','view_id':hashlib.sha1(os.urandom(16)).hexdigest()[:16],'selection':selected,'sort':self.sort,'desc':self.desc,'unsorted':True,'source_order':[],'scroll':0,'positions':{}});self.save_settings();self.reveal_panel_for_new_tab();return
        self.save_current_view()
        self.add_tab(root,select=True,tab={'kind':'farview','view_id':hashlib.sha1(os.urandom(16)).hexdigest()[:16],'sort':self.sort,'desc':self.desc,'unsorted':True,'source_order':[],'scroll':0,'positions':{}})
        self.save_settings()
        self.reveal_panel_for_new_tab()

    def toggle_item_favorite(self,path):
        key=str(path)
        if key.startswith('favgroup://'):
            group_id=key.split('://',1)[1];self.favorite_groups=[g for g in self.favorite_groups if g.get('id')!=group_id];self.save_settings();self.show_favorites(self.active_favorite_group);return
        if getattr(self,'favorite_picker_key',None)==key and getattr(self,'favorite_picker_menu',None) and self.favorite_picker_menu.isVisible():return
        selected=list(self.grid.selected_paths) if key in self.grid.selected_paths else [key]
        self.show_favorites_picker(key,selection=selected)
    @staticmethod
    def _normalized_path_key(path):
        return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))
    def is_folder_in_favorite_group(self,path):
        key=self._normalized_path_key(path)
        return any(key==self._normalized_path_key(folder) for group in self.favorite_groups for folder in group.get('folders',[]))
    def show_favorites_picker(self,key,anchor=None,tooltip_rect=None,selection=None):
        old=getattr(self,'favorite_picker_menu',None)
        if old:old.close();old.deleteLater()
        is_directory=Path(key).is_dir()
        paths=[str(Path(p)) for p in (selection or [key]) if Path(p).exists()]
        if not paths:paths=[key]
        path_keys={self._normalized_path_key(path) for path in paths}
        menu=QMenu(self);self.favorite_picker_menu=menu;self.favorite_picker_key=key;menu.setWindowFlag(Qt.FramelessWindowHint,True);menu.setAttribute(Qt.WA_TranslucentBackground,True);menu.setStyleSheet('QMenu { background:rgba(0,0,0,190); color:#fff; border:0px; border-radius:0px; padding:4px; } QMenu::item { background:transparent; padding:7px 18px; } QMenu::item:selected { background:rgba(80,80,80,190); } QMenu::separator { height:1px; background:rgba(255,255,255,65); margin:4px 6px; }')
        root_keys={self._normalized_path_key(entry) for entry in self.favorites}
        grouped_keys={self._normalized_path_key(entry) for group in self.favorite_groups for entry in group.get('folders',[])}
        member_paths=[path for path in paths if self._normalized_path_key(path) in root_keys or self._normalized_path_key(path) in grouped_keys]
        nonmembers=[path for path in paths if self._normalized_path_key(path) not in root_keys and self._normalized_path_key(path) not in grouped_keys]
        if member_paths:
            favorite=menu.addAction('Remove selection from Favorites' if len(member_paths)>1 else 'Remove from Favorites')
            favorite.triggered.connect(lambda checked=False,items=list(member_paths):self.remove_favorites_membership(items))
        if nonmembers:
            favorite=menu.addAction('Add selection to Favorites' if len(nonmembers)>1 else 'Add to Favorites')
            favorite.triggered.connect(lambda checked=False,items=list(nonmembers):self.set_root_favorite_membership(items,True))
        if self.favorite_groups:
            menu.addSeparator()
            for group in self.favorite_groups:
                members={self._normalized_path_key(p) for p in group.get('folders',[])};all_in_group=all(path_key in members for path_key in path_keys)
                action=menu.addAction(group.get('name','Group'));action.setCheckable(True);action.setChecked(all_in_group);action.triggered.connect(lambda checked=False,gid=group.get('id'),items=paths,remove=all_in_group:self.set_favorite_group_membership(items,gid,not remove))
        if anchor is None:pos=QCursor.pos();self.favorite_picker_source_rect=QRect(pos-QPoint(8,8),QSize(16,16))
        else:
            source=tooltip_rect if tooltip_rect is not None and tooltip_rect.isValid() else anchor
            screen=QApplication.screenAt(source.center()) or QApplication.primaryScreen();available=screen.availableGeometry();pos=QPoint(source.right()+1,source.top())
            self.favorite_picker_source_rect=QRect(source)
            if pos.x()+menu.sizeHint().width()>available.right()+1:pos=QPoint(source.left()-menu.sizeHint().width()-1,source.top())
            pos.setY(max(available.top(),min(pos.y(),available.bottom()-menu.sizeHint().height())))
        menu.aboutToHide.connect(self.favorite_picker_hidden)
        menu.popup(pos)
        self.favorite_picker_leave_since=None
        if hasattr(self,'favorite_picker_close_timer'):self.favorite_picker_close_timer.start()
    def favorite_picker_hidden(self):
        self.favorite_picker_leave_since=None
        if hasattr(self,'favorite_picker_close_timer'):self.favorite_picker_close_timer.stop()
    def check_favorite_picker_dismiss(self):
        menu=getattr(self,'favorite_picker_menu',None)
        if menu is None or not menu.isVisible():self.favorite_picker_hidden();return
        cursor=QCursor.pos()
        if menu.geometry().contains(cursor) or getattr(self,'favorite_picker_source_rect',QRect()).adjusted(-2,-2,2,2).contains(cursor):
            self.favorite_picker_leave_since=None;return
        self.favorite_picker_leave_since=None
        menu.close()
    def _toggle_root_favorite(self,key):
        if key in self.favorites:self._remove_favorite(key)
        else:self.favorites.add(key);self.save_settings();self.refresh_favorite_view()
    def set_root_favorite_membership(self,paths,enabled):
        keys={self._normalized_path_key(path) for path in paths}
        self.favorites={path for path in self.favorites if self._normalized_path_key(path) not in keys}
        if enabled:self.favorites.update(paths)
        self.grid.deselect_paths(paths)
        self.save_settings();self.refresh_favorite_view()
    def remove_favorites_membership(self,paths):
        snapshot=(set(self.favorites),copy.deepcopy(self.favorite_groups));keys={self._normalized_path_key(path) for path in paths}
        self.favorites={path for path in self.favorites if self._normalized_path_key(path) not in keys}
        for group in self.favorite_groups:group['folders']=[path for path in group.get('folders',[]) if self._normalized_path_key(path) not in keys]
        self.grid.deselect_paths(paths);self.save_settings();self.refresh_favorite_view()
        self.last_deletion_undo={'kind':'favorites','favorites':snapshot,'time':time.monotonic()}
    def set_favorite_group_membership(self,paths,group_id,enabled):
        group=next((g for g in self.favorite_groups if g.get('id')==group_id),None)
        if not group:return
        snapshot=(set(self.favorites),copy.deepcopy(self.favorite_groups)) if not enabled else None
        entries=group.setdefault('folders',[]);keys={self._normalized_path_key(path) for path in paths}
        entries[:]=[path for path in entries if self._normalized_path_key(path) not in keys]
        if enabled:entries.extend(paths)
        self.grid.deselect_paths(paths)
        self.favorites={path for path in self.favorites if self._normalized_path_key(path) not in keys}
        self.save_settings();self.refresh_favorite_view()
        if snapshot is not None:self.last_deletion_undo={'kind':'favorites','favorites':snapshot,'time':time.monotonic()}
    def toggle_folder_favorite_group(self,path,group_id):
        group=next((g for g in self.favorite_groups if g.get('id')==group_id),None)
        if not group:return
        folders=group.setdefault('folders',[])
        path_key=self._normalized_path_key(path);existing=next((entry for entry in folders if self._normalized_path_key(entry)==path_key),None)
        if existing is not None:folders.remove(existing)
        else:folders.append(path)
        self.grid.deselect_paths([path])
        self.favorites={entry for entry in self.favorites if self._normalized_path_key(entry)!=path_key}
        self.save_settings();self.refresh_favorite_view()
    def _remove_favorite(self,key):
        path_key=self._normalized_path_key(key);self.favorites={entry for entry in self.favorites if self._normalized_path_key(entry)!=path_key}
        self.grid.deselect_paths([key])
        for group in self.favorite_groups:group['folders']=[p for p in group.get('folders',[]) if self._normalized_path_key(p)!=path_key]
        self.save_settings();self.refresh_favorite_view()
    def add_folder_to_favorite_group(self,path,group_id):
        group=next((g for g in self.favorite_groups if g.get('id')==group_id),None)
        if group:
            folders=group.setdefault('folders',[]);path_key=self._normalized_path_key(path)
            if not any(self._normalized_path_key(entry)==path_key for entry in folders):folders.append(path)
            self.favorites={entry for entry in self.favorites if self._normalized_path_key(entry)!=path_key};self.grid.deselect_paths([path]);self.save_settings();self.refresh_favorite_view()
    def refresh_favorite_view(self):
        self.grid.viewport().update();self.update_favorite_button()
        if self.in_favorites_view:
            scroll=self.grid.verticalScrollBar().value();group_id=self.active_favorite_group;self.show_favorites(group_id)
            QTimer.singleShot(0,lambda value=scroll:self.grid.verticalScrollBar().setValue(min(value,self.grid.verticalScrollBar().maximum())))
    def rename_favorite_group(self,group_id):
        group=next((g for g in self.favorite_groups if g.get('id')==group_id),None)
        if not group:return
        name,ok=QInputDialog.getText(self,'Rename favorites group','Group name:',text=group.get('name',''))
        if ok and name.strip():group['name']=name.strip();self.save_settings();self.show_favorites(self.active_favorite_group)
    def handle_favorite_drop(self,payload,target_index,before=True):
        if not (0<=target_index<len(self.grid.items)):return
        target=self.grid.items[target_index]
        if target.get('kind')=='favorite_group' and self.in_favorites_view:
            target_id=str(target['path']).split('://',1)[1]
            if payload.startswith('folder-list://'):
                try:paths=[str(Path(path)) for path in json.loads(payload[len('folder-list://'):]) if Path(path).exists()]
                except (TypeError,ValueError):return
                if paths:self.set_favorite_group_membership(paths,target_id,True)
            elif payload.startswith('folder://'):
                self.add_folder_to_favorite_group(payload[len('folder://'):],target_id)
            elif payload.startswith('favgroup://'):
                source_id=payload.split('://',1)[1]
                if source_id==target_id:return
                source=next((g for g in self.favorite_groups if g.get('id')==source_id),None);dest_i=next((i for i,g in enumerate(self.favorite_groups) if g.get('id')==target_id),-1)
                if source and dest_i>=0:
                    self.favorite_groups.remove(source);self.favorite_groups.insert(dest_i,source)
                    root_order=self.folder_manual_orders.get('favorites:root',[]);group_keys=[f"favgroup://{group.get('id')}" for group in self.favorite_groups];others=[value for value in root_order if value not in group_keys]
                    self.folder_manual_orders['favorites:root']=group_keys+others
                    self.save_settings();self.show_favorites()
            return
        if payload.startswith('folder-list://') and self.sort=='manual':
            try:paths=[str(Path(path)) for path in json.loads(payload[len('folder-list://'):]) if Path(path).exists()]
            except (TypeError,ValueError):return
            self._reorder_manual_items(paths,target_index,before)
    def create_favorite_group(self):
        name,ok=QInputDialog.getText(self,'New favorite group','Group name:')
        name=name.strip()
        if not ok or not name:return
        group={'id':hashlib.sha1(os.urandom(16)).hexdigest()[:12],'name':name,'folders':[]}
        self.favorite_groups.append(group);self.active_favorite_group=None;self.in_favorites_view=True;self.save_settings();self.show_favorites()

    def open(self,path):
        raw=str(path)
        if raw.startswith('favgroup://'):
            self.show_favorites(raw.split('://',1)[1]);return
        p=Path(path)
        if p.is_dir():
            if self.in_favorites_view:
                self.return_to_favorites=True;self.favorites_navigation_root=p;self.favorite_return_group=self.active_favorite_group
            self.load(p)
        elif p.suffix.casefold() in (IMAGE_EXTS|PDF_EXTS):
            self.tree_was_visible_before_viewer=hasattr(self,'tree_dock') and self.tree_dock.isVisible()
            if self.tree_was_visible_before_viewer:self.save_tree_navigation_state();self.tree_dock.hide()
            paths=[str(item['path']) for item in self.grid.items if item.get('kind') in ('image','document') and Path(item['path']).suffix.casefold() in (IMAGE_EXTS|PDF_EXTS)]
            if str(p) not in paths:paths.append(str(p))
            self.viewer=MediaViewer(paths,paths.index(str(p)),self,self.slideshow_interval_ms);self.viewer.setAttribute(Qt.WA_DeleteOnClose);self.viewer.showFullScreen();self.viewer.activateWindow()
        elif p.suffix.casefold() in ('.lnk','.url'):
            target=self.resolve_link(p)
            if target and Path(target).is_dir():self.load(Path(target))
            elif target:self.reveal(Path(target))
            else:self.reveal(p)
        else:self.reveal(p)

    def resolve_link(self,p):
        try:
            if p.suffix.casefold()=='.url':
                for line in p.read_text(errors='ignore').splitlines():
                    if line.lower().startswith('url='): return line.split('=',1)[1].strip()
                return None
            if sys.platform.startswith('win'):
                ps=("$s=New-Object -ComObject WScript.Shell;" +
                    "$x=$s.CreateShortcut([Environment]::ExpandEnvironmentVariables('" + str(p).replace("'","''") + "'));" +
                    "[Console]::Write($x.TargetPath)")
                out=subprocess.check_output(['powershell','-NoProfile','-NonInteractive','-Command',ps],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),text=True,timeout=3)
                return out.strip() or None
        except Exception:
            logger.debug('Could not resolve shortcut %s',p,exc_info=True)
        return None

    def reveal(self,p):
        try:
            if sys.platform.startswith('win'):
                explorer=os.path.join(os.environ.get('WINDIR',r'C:\Windows'),'explorer.exe');subprocess.Popen([explorer,str(Path(p).resolve())])
            elif sys.platform=='darwin':subprocess.Popen(['open',str(p)])
            else:subprocess.Popen(['xdg-open',str(p)])
        except Exception as e:
            logger.exception('Could not open %s',p)
            QMessageBox.warning(self,'Open failed',str(e))

    def reveal_in_folder(self,path):
        try:
            target=str(Path(path).expanduser().resolve(strict=False))
            if sys.platform.startswith('win'):
                if os.path.normcase(os.path.dirname(target))==os.path.normcase(target):
                    self.reveal(target);return
                shell32=ctypes.WinDLL('shell32',use_last_error=True);ole32=ctypes.WinDLL('ole32',use_last_error=True)
                parse=shell32.SHParseDisplayName;parse.argtypes=[wintypes.LPCWSTR,ctypes.c_void_p,ctypes.POINTER(ctypes.c_void_p),wintypes.DWORD,ctypes.POINTER(wintypes.DWORD)];parse.restype=ctypes.c_long
                find_last=shell32.ILFindLastID;find_last.argtypes=[ctypes.c_void_p];find_last.restype=ctypes.c_void_p
                open_select=shell32.SHOpenFolderAndSelectItems;open_select.argtypes=[ctypes.c_void_p,wintypes.UINT,ctypes.POINTER(ctypes.c_void_p),wintypes.DWORD];open_select.restype=ctypes.c_long
                free=ole32.CoTaskMemFree;free.argtypes=[ctypes.c_void_p];free.restype=None
                co_initialize=ole32.CoInitializeEx;co_initialize.argtypes=[ctypes.c_void_p,wintypes.DWORD];co_initialize.restype=ctypes.c_long
                co_uninitialize=ole32.CoUninitialize;co_uninitialize.argtypes=[];co_uninitialize.restype=None
                com_result=co_initialize(None,2)
                if com_result<0 and (com_result & 0xffffffff)!=0x80010106:raise OSError(f'COM initialization failed (0x{com_result & 0xffffffff:08X})')
                def parse_pidl(shell_path):
                    pidl=ctypes.c_void_p();hr=parse(shell_path,None,ctypes.byref(pidl),0,None)
                    if hr!=0:raise OSError(f'SHParseDisplayName failed (0x{hr & 0xffffffff:08X})')
                    return pidl
                folder_pidl=ctypes.c_void_p();item_pidl=ctypes.c_void_p()
                try:
                    folder_pidl=parse_pidl(str(Path(target).parent));item_pidl=parse_pidl(target)
                    relative_item=find_last(item_pidl)
                    if not relative_item:raise OSError('Could not identify the selected item in its parent folder')
                    items=(ctypes.c_void_p*1)(relative_item);hr=open_select(folder_pidl,1,items,0)
                    if hr!=0:raise OSError(f'SHOpenFolderAndSelectItems failed (0x{hr & 0xffffffff:08X})')
                finally:
                    if item_pidl:free(item_pidl)
                    if folder_pidl:free(folder_pidl)
                    if com_result>=0:co_uninitialize()
            elif sys.platform=='darwin':subprocess.Popen(['open','-R',target])
            else:subprocess.Popen(['xdg-open',str(Path(target).parent)])
        except Exception as e:
            if sys.platform.startswith('win'):
                try:
                    logger.exception('Windows could not select %s in Explorer; opening its containing folder instead',path)
                    explorer=os.path.join(os.environ.get('WINDIR',r'C:\Windows'),'explorer.exe');subprocess.Popen([explorer,str(Path(path).expanduser().resolve(strict=False).parent)]);return
                except Exception:logger.exception('Could not open the containing folder for %s',path)
            logger.exception('Could not reveal %s',path)
            QMessageBox.warning(self,'Reveal failed',str(e))

    def invalidate_folder_sizes(self,paths):
        """Drop cached totals for affected folders and their ancestors."""
        changed=False
        for raw in paths:
            target=Path(raw);matches=self.grid.items_by_path.get(str(target),())
            is_directory=bool(matches and matches[0].get('is_dir')) or target.is_dir()
            folder=target if is_directory else target.parent
            while True:
                key=str(folder)
                self.grid.folder_size_queued.discard(key)
                for futures in (self.grid.folder_size_futures,self.grid.hover_folder_size_futures):
                    future=futures.pop(key,None)
                    events=self.grid.folder_size_events if futures is self.grid.folder_size_futures else self.grid.hover_folder_size_events
                    cancel_event=events.pop(key,None)
                    if cancel_event is not None:cancel_event.set()
                    if future is not None:future.cancel()
                if key in self.grid.folder_size_cache or key in self.pinned_folder_sizes or key in self.pinned_folder_sizes_checked:
                    self.grid.folder_size_cache.pop(key,None);self.grid.folder_size_cache_checked_at.pop(key,None)
                    self.pinned_folder_sizes.pop(key,None);self.pinned_folder_sizes_checked.pop(key,None)
                    for item in self.grid.items:
                        if item.get('is_dir') and str(item.get('path'))==key:item['size']=None
                    changed=True
                parent=folder.parent
                if parent==folder:break
                folder=parent
        if changed:
            self.save_settings();self.grid.viewport().update()

    def send_paths_to_trash(self,paths):
        unique=[]
        for raw in paths:
            path=str(raw)
            if path and not path.startswith('favgroup://') and path not in unique and Path(path).exists():unique.append(path)
        if not unique:return False
        self.begin_operation('Moving items to the Recycle Bin…')
        self.activity_overlay.set_operation('Moving items to the Recycle Bin…');self.position_activity_overlay()
        QApplication.processEvents(QEventLoop.ExcludeUserInputEvents)
        try:
            favorites_snapshot=(set(self.favorites),copy.deepcopy(self.favorite_groups)) if self.in_favorites_view else None
            try:
                from send2trash import send2trash
            except ImportError:
                send2trash=None
            moved=[];partial_failures=[]
            if send2trash is not None:
                for path in unique:
                    try:send2trash(path);moved.append(path)
                    except Exception as exc:partial_failures.append((path,str(exc)))
            else:
                if not sys.platform.startswith('win'):raise RuntimeError('Recycle Bin support is unavailable on this system.')
                class SHFILEOPSTRUCTW(ctypes.Structure):
                    _fields_=[('hwnd',wintypes.HWND),('wFunc',wintypes.UINT),('pFrom',wintypes.LPCWSTR),('pTo',wintypes.LPCWSTR),('fFlags',wintypes.WORD),('fAnyOperationsAborted',wintypes.BOOL),('hNameMappings',wintypes.LPVOID),('lpszProgressTitle',wintypes.LPCWSTR)]
                for path in unique:
                    source=ctypes.create_unicode_buffer(path+'\0\0')
                    operation=SHFILEOPSTRUCTW();operation.hwnd=int(self.winId());operation.wFunc=3;operation.pFrom=ctypes.cast(source,wintypes.LPCWSTR);operation.pTo=None;operation.fFlags=0x0040|0x0010|0x0004;operation.fAnyOperationsAborted=False;operation.hNameMappings=None;operation.lpszProgressTitle=None
                    result=ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
                    if result==0 and not operation.fAnyOperationsAborted:moved.append(path)
                    else:partial_failures.append((path,f'Shell operation returned {result}; aborted={operation.fAnyOperationsAborted}'))
            unique=moved
            if not unique:raise RuntimeError('Windows could not move any selected item to the Recycle Bin.')
            self.invalidate_folder_sizes(unique)
            self.grid.deselect_paths(unique)
            if self.in_favorites_view:
                keys={self._normalized_path_key(path) for path in unique}
                self.favorites={path for path in self.favorites if self._normalized_path_key(path) not in keys}
                for group in self.favorite_groups:group['folders']=[path for path in group.get('folders',[]) if self._normalized_path_key(path) not in keys]
                self.save_settings();self.refresh_favorite_view()
            elif self.current.is_dir():QTimer.singleShot(0,lambda:self.load(self.current,restore_scroll=True,save_previous=False))
            self.last_deletion_undo={'kind':'recycle','paths':list(unique),'favorites':favorites_snapshot,'time':time.monotonic()}
            if partial_failures:
                logger.warning('Some selected items could not be moved to the Recycle Bin: %s',partial_failures)
                details='\n'.join(f'{path}: {error}' for path,error in partial_failures[:5])
                QMessageBox.warning(self,'Some items could not be moved',f'{len(unique)} item(s) were moved.\n\n{details}')
            return True
        except Exception as e:
            logger.exception('Could not move selected items to the Recycle Bin')
            QMessageBox.warning(self,'Move to Recycle Bin failed',str(e));return False
        finally:self.finish_operation()

    def remove_favorites_selection(self):
        paths=list(self.grid.selected_paths)
        if not paths and 0<=self.grid.navigation_index<len(self.grid.items):
            paths=[str(self.grid.items[self.grid.navigation_index].get('path',''))]
        if not paths and 0<=self.grid.hover_index<len(self.grid.items):
            paths=[str(self.grid.items[self.grid.hover_index].get('path',''))]
        if not paths:return
        favorites_snapshot=(set(self.favorites),copy.deepcopy(self.favorite_groups))
        keys={self._normalized_path_key(path) for path in paths if not path.startswith('favgroup://')}
        group_ids={path.split('://',1)[1] for path in paths if path.startswith('favgroup://')}
        self.favorite_groups=[group for group in self.favorite_groups if str(group.get('id')) not in group_ids]
        self.favorites={path for path in self.favorites if self._normalized_path_key(path) not in keys}
        for group in self.favorite_groups:group['folders']=[path for path in group.get('folders',[]) if self._normalized_path_key(path) not in keys]
        self.grid.clear_selection(remember=False);self.save_settings();self.show_favorites(self.active_favorite_group)
        self.last_deletion_undo={'kind':'favorites','favorites':favorites_snapshot,'time':time.monotonic()}

    def undo_last_action(self):
        deletion=getattr(self,'last_deletion_undo',None)
        selection_time=self.grid.selection_undo[-1][1] if self.grid.selection_undo else -1
        if deletion and deletion.get('time',-1)>selection_time:
            return self.undo_recent_deletion()
        self.grid.undo_deselection()

    def undo_recent_deletion(self):
        record=getattr(self,'last_deletion_undo',None)
        if not record:return False
        if record['kind']=='favorites':
            self.favorites,self.favorite_groups=copy.deepcopy(record['favorites'])
            self.last_deletion_undo=None;self.save_settings();self.show_favorites(self.active_favorite_group);return True
        try:
            if not sys.platform.startswith('win'):
                raise RuntimeError('Recycle Bin restore is only supported on Windows.')
            pending=list(record.get('paths',[]));restored=[];restore_failures=[]
            for raw_path in pending:
                try:self.restore_recycle_bin_paths([raw_path]);restored.append(raw_path)
                except Exception as exc:restore_failures.append((raw_path,str(exc)))
            if restored:self.invalidate_folder_sizes(restored)
            if not restored and restore_failures:
                raise RuntimeError('\n'.join(f'{path}: {error}' for path,error in restore_failures[:5]))
            record['paths']=[path for path in pending if path not in restored]
            if not record['paths'] and record.get('favorites'):
                self.favorites,self.favorite_groups=copy.deepcopy(record['favorites'])
            if not record['paths']:self.last_deletion_undo=None
            self.save_settings()
            if self.in_favorites_view:self.refresh_favorite_view()
            elif self.current.is_dir():self.load(self.current,restore_scroll=True,save_previous=False)
            if restore_failures:
                details='\n'.join(f'{path}: {error}' for path,error in restore_failures[:5])
                QMessageBox.warning(self,'Some items could not be restored',f'{len(restored)} item(s) were restored. The remaining items are still available to undo later.\n\n{details}')
            return True
        except Exception as e:
            logger.exception('Could not undo the most recent Recycle Bin deletion')
            QMessageBox.warning(self,'Undo deletion failed',str(e));return False

    @staticmethod
    def restore_recycle_bin_paths(paths):
        """Restore matching $R payloads using their paired Recycle Bin $I metadata."""
        for raw_path in paths:
            original=Path(raw_path)
            if original.exists():raise FileExistsError(f'The original path already exists: {original}')
            if not original.drive:raise RuntimeError(f'The original drive is unavailable: {original}')
            recycle_root=Path(original.drive+'\\$Recycle.Bin')
            if not recycle_root.is_dir():raise FileNotFoundError(f'No Recycle Bin exists on {original.drive}')
            wanted=os.path.normcase(os.path.normpath(str(original)))
            match=None
            try:owners=list(recycle_root.iterdir())
            except OSError as exc:raise PermissionError(f'Cannot access the Recycle Bin on {original.drive}: {exc}') from exc
            for owner in owners:
                if not owner.is_dir():continue
                try:metadata_files=owner.glob('$I*')
                except OSError:continue
                for metadata in metadata_files:
                    try:
                        raw=metadata.read_bytes()
                        if len(raw)<26:continue
                        version=struct.unpack_from('<Q',raw,0)[0]
                        if version==1:
                            encoded_path=raw[24:544]
                        elif version>=2:
                            char_count=struct.unpack_from('<I',raw,24)[0]
                            byte_count=char_count*2
                            if char_count<=0 or byte_count>len(raw)-28:continue
                            encoded_path=raw[28:28+byte_count]
                        else:continue
                        stored=encoded_path.decode('utf-16le',errors='ignore').split('\0',1)[0]
                        if os.path.normcase(os.path.normpath(stored))!=wanted:continue
                        payload=metadata.with_name('$R'+metadata.name[2:])
                        if payload.exists():match=(metadata,payload);break
                    except (OSError,ValueError,struct.error):continue
                if match:break
            if match is None:raise FileNotFoundError(f'Could not find the Recycle Bin entry for: {original}')
            metadata,payload=match
            original.parent.mkdir(parents=True,exist_ok=True)
            if original.exists():raise FileExistsError(f'The original path already exists: {original}')
            try:
                shutil.move(str(payload),str(original))
                try:metadata.unlink(missing_ok=True)
                except OSError:logger.warning('Restored %s but could not remove its Recycle Bin metadata file',original)
            except OSError as exc:
                raise OSError(f'Could not restore {original}: {exc}') from exc

    def back(self):
        # Magellan's Back button is a quick return to the configured startup folder.
        if self.return_to_favorites:
            self.up();return
        if self.in_favorites_view:
            if self.active_favorite_group is not None:self.show_favorites();return
            target=self.favorites_return_path
            for i,tab in enumerate(self.tabs):
                if target==tab.get('path'):
                    self.active_favorite_group=None;self.in_favorites_view=False;self.tabbar.setCurrentIndex(i);self.activate_tab(i,save_previous=False);return
            self.in_favorites_view=False;self.active_favorite_group=None
        target=Path(self.startup_folder) if self.startup_folder else self.back_target
        if target and target.is_dir() and target != self.current:
            self.load(target)
    def up(self):
        if self.return_to_favorites:
            root=self.favorites_navigation_root
            if root is None or Path(self.current)==root:
                group_id=self.favorite_return_group;self.return_to_favorites=False;self.favorites_navigation_root=None;self.favorite_return_group=None;self.show_favorites(group_id);return
            if Path(self.current).parent!=Path(self.current):self.load(Path(self.current).parent,restore_scroll=True,save_previous=False)
            return
        if self.in_favorites_view:
            if self.active_favorite_group is not None:self.show_favorites();return
            self.back();return
        if self.current.parent != self.current:
            self.save_current_view()
            self.load(self.current.parent, restore_scroll=True)

    def toggle_favorite(self):
        self.toggle_item_favorite(str(self.current))

    def update_favorite_button(self):
        if hasattr(self,'favorites_list_btn'):
            self.favorites_list_btn.setText('☆');self.favorites_list_btn.setToolTip('Exit Favorites' if self.in_favorites_view else 'Show favorite folders')
        if hasattr(self,'farviewbtn'):
            self.farviewbtn.setToolTip('Create a Farview tab from the selected files and folders' if getattr(getattr(self,'grid',None),'selected_paths',set()) else f'Create a Farview tab from {self.current} and its image subfolders')

    def choose(self):
        f=QFileDialog.getExistingDirectory(self,'Choose media folder',str(self.current))
        if f:self.return_to_favorites=False;self.favorites_navigation_root=None;self.favorite_return_group=None;self.load(Path(f))
    def _pin_folder(self, path):
        key=str(Path(path).resolve())
        root=Path(key)
        if not root.is_dir(): return False
        self.pinned_folders.add(key);self._pinned_folders_generation+=1
        self.save_settings()
        # Cache the root and ONLY its immediate child folders. Deeper folders
        # are not added to the persistent cache unless separately pinned.
        targets=[key]
        try:
            targets.extend(str(e.path) for e in os.scandir(root) if e.is_dir() and (not e.name.startswith('.') or e.name.startswith('. ')))
        except OSError:
            logger.warning('Could not scan child folders while pinning %s',root,exc_info=True)
        for target in targets:
            if target in self.grid.folder_size_cache:
                self.pinned_folder_sizes[target]=max(0,int(self.grid.folder_size_cache[target]))
                checked=self.grid.folder_size_cache_checked_at.setdefault(target,time.time())
                self.pinned_folder_sizes_checked[target]=checked
            cached=self.grid.load_persistent_folder_thumb(target)
            if cached:
                self.pinned_thumbs[target]=cached
                self.grid.folder_cache[target]=cached
                continue
            self.pinned_thumbs.setdefault(target,None)
            if target in self.grid.folder_cache:
                source=self.grid.folder_cache[target]
                if source:
                    fut=self.grid.folder_pool.submit(self.grid.persist_folder_thumb,target,source)
                    def persisted(f,pp=target):
                        try:
                            cached=f.result()
                            if cached:self._folder_thumb_bridge.completed.emit(pp,cached)
                        except Exception:
                            if not f.cancelled():logger.exception('Could not persist pinned folder thumbnail for %s',pp)
                    fut.add_done_callback(persisted)
                continue
            existing=self.grid.folder_futures.get(target)
            if existing is not None:
                def persist_existing(f,pp=target):
                    try:
                        source=f.result()
                        cached=self.grid.persist_folder_thumb(pp,source) if source else None
                        if cached:self._folder_thumb_bridge.completed.emit(pp,cached)
                    except Exception:
                        if not f.cancelled():logger.exception('Could not persist pinned folder thumbnail for %s',pp)
                existing.add_done_callback(persist_existing)
                continue
            fut=self.grid.folder_pool.submit(self.grid.resolve_folder_thumbnail,target)
            def done(f,pp=target):
                try:
                    val=f.result()
                    if val:self._folder_thumb_bridge.completed.emit(pp,val)
                except Exception:
                    if not f.cancelled():logger.exception('Could not cache pinned folder thumbnail for %s',pp)
            fut.add_done_callback(done)
        self.save_settings()
        return True

    def handle_pinned_thumbnail_ready(self,path,thumbnail_path):
        if self._closing or not thumbnail_path:return
        if path not in self.pinned_folders:
            cache_path=self.grid._persistent_folder_thumb_path(path)
            if cache_path:
                try:cache_path.unlink(missing_ok=True)
                except OSError:logger.debug('Could not remove stale pinned thumbnail %s',cache_path,exc_info=True)
            return
        self.pinned_thumbs[path]=str(thumbnail_path);self.grid.folder_cache[path]=str(thumbnail_path)
        self.save_settings();self.schedule_disk_thumbnail_cache_trim()

    def style_manager_dialog(self,dialog):
        dialog.setStyleSheet('''
            QDialog{background:#1b1d20;color:#eeeeee;}
            QLabel{color:#e6e6e6;}
            QLineEdit,QListWidget,QComboBox{background:#202226;color:#eeeeee;border:1px solid #46484d;border-radius:3px;padding:7px;}
            QListWidget::item{padding:7px 5px;}
            QListWidget::item:selected{background:#51441d;color:#ffffff;}
            QPushButton,QToolButton{background:#292b30;color:#eeeeee;border:1px solid #494b51;border-radius:3px;padding:7px 13px;min-height:20px;}
            QPushButton:hover,QToolButton:hover{background:#393b40;border-color:#c49a28;}
            QPushButton:checked{background:#51441d;border-color:#c49a28;color:#ffffff;}
            QPushButton:disabled{color:#777777;border-color:#333333;}
        ''')

    def manage_pinned_folders(self):
        dlg=QDialog(self);dlg.setWindowTitle('Keep folder thumbnails');dlg.resize(850,540);self.style_manager_dialog(dlg);lay=QVBoxLayout(dlg);lay.setSpacing(10)
        info=QLabel("Keep selected folders' thumbnails in Magellan's local cache. Their immediate child folders are included; deeper subfolders must be added separately.");info.setWordWrap(True);lay.addWidget(info)
        path_row=QHBoxLayout();path_edit=QLineEdit();path_edit.setPlaceholderText(r'Enter a full folder path, e.g. H:\Pictures');browse=QPushButton('Browse…');add_path=QPushButton('Add path');path_row.addWidget(path_edit,1);path_row.addWidget(browse);path_row.addWidget(add_path);lay.addLayout(path_row)
        sort_row=QHBoxLayout();search=QLineEdit();search.setPlaceholderText('Search pinned folder paths…');sort_row.addWidget(search,1);sort_row.addWidget(QLabel('Sort by'));sort_by=QComboBox();sort_by.addItems(['Path','Date created']);sort_row.addWidget(sort_by)
        ascending=QPushButton('Ascending ↑');ascending.setCheckable(True);descending=QPushButton('Descending ↓');descending.setCheckable(True);sort_row.addWidget(ascending);sort_row.addWidget(descending);sort_row.addStretch(1);lay.addLayout(sort_row)
        lay.addWidget(QLabel('Pinned folders'));current=QListWidget();current.setSelectionMode(QAbstractItemView.ExtendedSelection);current.setToolTip("These folders keep a local thumbnail copy inside Magellan's installation folder.");lay.addWidget(current,1)
        descending_order=False
        def populate():
            current.clear();query=search.text().casefold().strip()
            paths=[path for path in self.pinned_folders if not query or query in path.casefold()]
            if sort_by.currentIndex()==1:
                def created(path):
                    try:return Path(path).stat().st_ctime
                    except OSError:return 0
                paths.sort(key=lambda path:(created(path),natural_key(path)),reverse=descending_order)
            else:paths.sort(key=natural_key,reverse=descending_order)
            for path in paths:current.addItem(path)
            remove_btn.setEnabled(bool(current.count()))
        def add_path_value():
            raw=path_edit.text().strip().strip('"')
            if not raw:return
            if not self._pin_folder(raw):QMessageBox.warning(dlg,'Folder not found',f'Folder does not exist:\n{raw}');return
            path_edit.clear();populate()
        def browse_path():
            folder=QFileDialog.getExistingDirectory(dlg,'Choose folder to keep',str(self.current))
            if folder:path_edit.setText(folder);add_path_value()
        def remove():
            keys=[item.text() for item in current.selectedItems()]
            if not keys:return
            for key in keys:
                self.pinned_folders.discard(key);self._pinned_folders_generation+=1
                targets=[key]
                try:
                    root=Path(key)
                    targets.extend(str(entry.path) for entry in os.scandir(root) if entry.is_dir() and (not entry.name.startswith('.') or entry.name.startswith('. ')))
                except Exception:pass
                for target in targets:
                    self.pinned_thumbs.pop(target,None);self.pinned_folder_sizes.pop(target,None);self.pinned_folder_sizes_checked.pop(target,None);self.grid.folder_size_cache_checked_at.pop(target,None)
                    cache_path=self.grid._persistent_folder_thumb_path(target)
                    if cache_path:
                        try:cache_path.unlink(missing_ok=True)
                        except OSError:logger.warning('Could not remove pinned thumbnail cache for %s',target,exc_info=True)
            self.save_settings();populate()
        def set_direction(descending_order_value):
            nonlocal descending_order
            descending_order=descending_order_value;ascending.setChecked(not descending_order);descending.setChecked(descending_order);populate()
        add_path.clicked.connect(add_path_value);browse.clicked.connect(browse_path);path_edit.returnPressed.connect(add_path_value);search.textChanged.connect(populate)
        sort_by.currentIndexChanged.connect(populate);ascending.clicked.connect(lambda:set_direction(False));descending.clicked.connect(lambda:set_direction(True));ascending.setChecked(True)
        actions=QHBoxLayout();remove_btn=QPushButton('Remove selected');remove_btn.clicked.connect(remove);actions.addWidget(remove_btn);actions.addStretch(1);lay.addLayout(actions)
        close=QDialogButtonBox(QDialogButtonBox.Close);close.rejected.connect(dlg.reject);close.accepted.connect(dlg.accept);lay.addWidget(close);populate();dlg.exec()

    def manage_personal_orders(self):
        dlg=QDialog(self);dlg.setWindowTitle('Manage personal orders');dlg.resize(850,540);self.style_manager_dialog(dlg);layout=QVBoxLayout(dlg)
        info=QLabel('Saved custom orders for folders, Favorites, and Farviews. Removing an order clears its saved sequence.')
        info.setWordWrap(True);layout.addWidget(info)
        filters=QHBoxLayout();search=QLineEdit();search.setPlaceholderText('Search paths and views…');filters.addWidget(search,1)
        sort_by=QComboBox();sort_by.addItems(['Path','Date created']);filters.addWidget(QLabel('Sort by'));filters.addWidget(sort_by)
        direction=QComboBox();direction.addItems(['Ascending','Descending']);filters.addWidget(direction);layout.addLayout(filters)
        orders=QListWidget();orders.setSelectionMode(QAbstractItemView.ExtendedSelection);layout.addWidget(orders,1)
        entries=[]
        def describe(storage_key):
            if storage_key=='favorites:root':return 'Favorites root'
            if storage_key.startswith('favorites:group:'):
                group_id=storage_key.split(':',2)[2];group=next((g for g in self.favorite_groups if g.get('id')==group_id),None)
                return 'Favorites group: '+(group.get('name','Group') if group else group_id)
            if storage_key.startswith('folder:'):return storage_key[len('folder:'):]
            if storage_key.startswith('farview:'):
                view_id=storage_key[len('farview:'):];tab=next((t for t in self.tabs if t.get('kind')=='farview' and str(t.get('view_id') or self._normalized_path_key(t.get('path','')))==view_id),None)
                return 'Farview: '+(str(tab.get('path','')) if tab else view_id)
            return storage_key
        def populate():
            entries.clear()
            for storage_key,order in self.folder_manual_orders.items():
                if isinstance(storage_key,str) and isinstance(order,list) and order:
                    entries.append((storage_key,describe(storage_key),len(order),self.manual_order_created.get(storage_key)))
            query=search.text().casefold().strip();field=sort_by.currentIndex();reverse=direction.currentIndex()==1
            visible=[entry for entry in entries if not query or query in entry[1].casefold() or query in entry[0].casefold()]
            if field==1:
                visible.sort(key=lambda entry:(entry[3] is None,entry[3] or 0,entry[1].casefold()),reverse=reverse)
            else:visible.sort(key=lambda entry:entry[1].casefold(),reverse=reverse)
            orders.clear()
            for storage_key,label,count,created in visible:
                try:date_label=datetime.fromtimestamp(created).strftime('%Y-%m-%d %H:%M') if created else 'date unavailable'
                except (OverflowError,OSError,ValueError):date_label='date unavailable'
                item=QListWidgetItem(f'{label}   ·   {count} items   ·   {date_label}');item.setData(Qt.UserRole,storage_key);orders.addItem(item)
            remove_selected.setEnabled(orders.count()>0)
        def reset_order_users(keys,all_orders=False):
            if all_orders:
                for key in list(self.folder_sort_settings):
                    if self.folder_sort_settings.get(key,{}).get('sort')=='manual':self.folder_sort_settings[key]={'sort':'name','desc':False}
                for tab in self.tabs:
                    if tab.get('sort')=='manual':tab.update({'sort':'name','desc':False,'unsorted':False})
                if self.sort=='manual':self.sort='name';self.desc=False
            else:
                for key in keys:
                    if key.startswith('folder:'):
                        path_key=key[len('folder:'):]
                        setting=self.folder_sort_settings.get(path_key)
                        if isinstance(setting,dict) and setting.get('sort')=='manual':self.folder_sort_settings[path_key]={'sort':'name','desc':False}
                        for tab in self.tabs:
                            if tab.get('kind')!='farview' and self._normalized_path_key(tab.get('path',''))==path_key and tab.get('sort')=='manual':tab.update({'sort':'name','desc':False})
                    elif key.startswith('farview:'):
                        view_id=key[len('farview:'):]
                        for tab in self.tabs:
                            if tab.get('kind')=='farview' and str(tab.get('view_id') or self._normalized_path_key(tab.get('path','')))==view_id and tab.get('sort')=='manual':tab.update({'sort':'name','desc':False})
                if self._manual_view_key() in keys and self.sort=='manual':self.sort='name';self.desc=False
        def remove_keys(keys,all_orders=False):
            for storage_key in keys:
                self.folder_manual_orders.pop(storage_key,None);self.manual_order_created.pop(storage_key,None)
            if all_orders:self.manual_order_created.clear()
            reset_order_users(keys,all_orders)
            self.update_sort_buttons();self.refresh();self.save_settings();populate()
        actions=QHBoxLayout();remove_selected=QPushButton('Exclude selected');clear_all=QPushButton('Exclude all');actions.addWidget(remove_selected);actions.addWidget(clear_all);actions.addStretch();layout.addLayout(actions)
        remove_selected.clicked.connect(lambda:remove_keys([str(item.data(Qt.UserRole)) for item in orders.selectedItems()]))
        def confirm_exclude_all():
            if not self.folder_manual_orders:return
            answer=QMessageBox.question(dlg,'Exclude all personal orders','Remove all saved personal orders? This cannot be undone.',QMessageBox.Yes|QMessageBox.Cancel,QMessageBox.Cancel)
            if answer==QMessageBox.Yes:remove_keys(list(self.folder_manual_orders),all_orders=True)
        clear_all.clicked.connect(confirm_exclude_all)
        search.textChanged.connect(populate);sort_by.currentIndexChanged.connect(populate);direction.currentIndexChanged.connect(populate)
        close=QDialogButtonBox(QDialogButtonBox.Close);close.rejected.connect(dlg.reject);close.accepted.connect(dlg.accept);layout.addWidget(close);populate();dlg.exec()

    def choose_search_provider(self):
        items=['SauceNAO','Google Images / Lens']
        cur=0 if self.image_search_provider=='saucenao' else 1
        val,ok=QInputDialog.getItem(self,'Reverse image search','Provider:',items,cur,False)
        if ok:
            self.image_search_provider='saucenao' if val=='SauceNAO' else 'google'
            self.save_settings();self.status.setText(f'Image search: {val}')

    def configure_google_lens_query(self):
        query,ok=QInputDialog.getText(self,'Google Lens text query','Text to attach to each Google Lens image search:',QLineEdit.Normal,self.google_lens_query)
        if ok:
            self.google_lens_query=query.strip()[:500];self.save_settings()
            self.status.setText('Google Lens text query saved' if self.google_lens_query else 'Google Lens text query cleared')

    def set_saucenao_api_key(self):
        key,ok=QInputDialog.getText(self,'SauceNAO API key','Enter the API key from your SauceNAO account:',QLineEdit.Password,getattr(self,'saucenao_api_key',''))
        if ok:
            self.saucenao_api_key=key.strip()
            self.save_settings()
            self.status.setText('SauceNAO API key saved' if self.saucenao_api_key else 'SauceNAO API key cleared')

    def toggle_image_search(self):
        self.image_search_mode=not self.image_search_mode
        self.status.setText('Image search mode — click an image/video to search' if self.image_search_mode else 'Image search mode off')
        self.grid.set_image_search_active(self.image_search_mode);self.grid.setFocus();self.grid.viewport().update()

    def open_search_bridge(self,html,prefix):
        fd,name=tempfile.mkstemp(suffix='.html',prefix=prefix);bridge=Path(name)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as stream:stream.write(html)
            _PENDING_SEARCH_BRIDGES.add(str(bridge))
            os.startfile(str(bridge))
        except Exception:
            cleanup_search_bridge(bridge)
            raise
        QTimer.singleShot(60000,lambda p=str(bridge):cleanup_search_bridge(p))

    def perform_image_search(self,path):
        generated_upload=None
        try:
            original_path=Path(path);upload_path=original_path;upload_name=original_path.name
            if upload_path.suffix.lower() in VIDEO_EXTS:
                img=self.grid.video_thumbnail_worker(str(upload_path),1200,1200)
                if img is None or img.isNull(): raise RuntimeError('Could not extract a frame from the video.')
                fd,generated_upload=tempfile.mkstemp(suffix='.jpg',prefix='magellan_search_');os.close(fd)
                upload_path=Path(generated_upload);upload_name=original_path.stem+'.jpg'
                if not img.save(str(upload_path),'JPEG',92): raise RuntimeError('Could not create the search image.')
            else:
                try:source_bytes=upload_path.stat().st_size
                except OSError as exc:raise RuntimeError(f'Cannot read the selected image: {exc}') from exc
                reader=QImageReader(str(upload_path));reader.setAutoTransform(True);reader.setDecideFormatFromContent(True);source_size=reader.size()
                if source_bytes>25*1024*1024 or (source_size.isValid() and source_size.width()*source_size.height()>48_000_000):
                    if not source_size.isValid():raise RuntimeError('This image is too large to prepare for search because its dimensions could not be read.')
                    factor=min(1.0,2560/max(source_size.width(),source_size.height()),(24_000_000/(source_size.width()*source_size.height()))**0.5)
                    reader.setScaledSize(QSize(max(1,round(source_size.width()*factor)),max(1,round(source_size.height()*factor))))
                    image=reader.read()
                    if image.isNull():raise RuntimeError(f'Could not prepare this large image: {reader.errorString()}')
                    fd,generated_upload=tempfile.mkstemp(suffix='.jpg',prefix='magellan_search_');os.close(fd)
                    upload_path=Path(generated_upload)
                    if not image.save(str(upload_path),'JPEG',90):raise RuntimeError('Could not prepare the large image for search.')
            provider=self.image_search_provider
            self.status.setText(f'Searching with {"SauceNAO" if provider=="saucenao" else "Google Lens"}…')
            self.grid.setFocus()
            if provider=='google':
                # Google Lens upload sessions can be tied to the browser session.
                # Opening the redirect URL from an unrelated HTTP session can therefore
                # produce "Expired visual search". Instead, hand the actual file bytes to
                # the user's browser and let the browser submit the multipart form itself.
                raw=upload_path.read_bytes()
                b64=base64.b64encode(raw).decode('ascii')
                b64_js=javascript_literal(b64);name_js=javascript_literal(upload_name)
                stamp=int(time.time()*1000)
                lens_query=f'&q={quote(self.google_lens_query.strip(),safe="")}' if self.google_lens_query.strip() else ''
                html=f'''<!doctype html>
<html><head><meta charset="utf-8"><title>Magellan - Google Lens</title></head>
<body>
<script>
(async()=>{{
  const b64={b64_js};
  const bytes=Uint8Array.from(atob(b64), c=>c.charCodeAt(0));
  const blob=new Blob([bytes], {{type:"image/jpeg"}});
  const file=new File([blob], {name_js}, {{type:"image/jpeg"}});
  const form=document.createElement("form");
  form.method="POST";
  form.enctype="multipart/form-data";
  form.action="https://lens.google.com/v3/upload?ep=fntpubb&st={stamp}&vpw=1200&vph=900&hl=en{lens_query}";
  const input=document.createElement("input");
  input.type="file";
  input.name="encoded_image";
  const dt=new DataTransfer();
  dt.items.add(file);
  input.files=dt.files;
  const dims=document.createElement("input");
  dims.type="hidden";
  dims.name="processed_image_dimensions";
  dims.value="1200,1200";
  form.appendChild(input);
  form.appendChild(dims);
  document.body.appendChild(form);
  form.submit();
}})().catch(err=>{{
  document.body.textContent="Magellan could not start Google Lens: "+err;
}});
</script>
</body></html>'''
                self.open_search_bridge(html,'magellan_google_lens_')
            else:
                api_key=getattr(self,'saucenao_api_key','').strip()
                if not api_key:
                    self.image_search_mode=False;self.grid.set_image_search_active(False);self.grid.search_hover=-1;self.grid.viewport().update()
                    QMessageBox.information(self,'SauceNAO API key required','SauceNAO API searches require an API key. Add it under Tools → Set SauceNAO API key…')
                    return
                raw=upload_path.read_bytes(); b64=base64.b64encode(raw).decode('ascii');b64_js=javascript_literal(b64);name_js=javascript_literal(upload_name);api_key_js=javascript_literal(api_key)
                html=f"""<!doctype html><html><head><meta charset='utf-8'><title>Magellan - SauceNAO</title></head><body><script>
(async()=>{{
const bytes=Uint8Array.from(atob({b64_js}),c=>c.charCodeAt(0));
const blob=new Blob([bytes],{{type:'image/jpeg'}});
const file=new File([blob],{name_js},{{type:'image/jpeg'}});
const form=document.createElement('form');form.method='POST';form.enctype='multipart/form-data';form.action='https://saucenao.com/search.php';
const input=document.createElement('input');input.type='file';input.name='file';const dt=new DataTransfer();dt.items.add(file);input.files=dt.files;form.appendChild(input);
const key=document.createElement('input');key.type='hidden';key.name='api_key';key.value={api_key_js};form.appendChild(key);
const out=document.createElement('input');out.type='hidden';out.name='output_type';out.value='0';form.appendChild(out);document.body.appendChild(form);form.submit();
}})().catch(e=>document.body.textContent='Magellan could not start SauceNAO: '+e);</script></body></html>"""
                self.open_search_bridge(html,'magellan_saucenao_')
            self.image_search_mode=False;self.grid.set_image_search_active(False);self.grid.search_hover=-1;self.grid.viewport().update();self.status.setText('Image search opened in your browser')
        except Exception as e:
            logger.exception('Image search failed')
            self.image_search_mode=False;self.grid.set_image_search_active(False);self.grid.search_hover=-1;self.grid.viewport().update()
            QMessageBox.warning(self,'Image search failed',str(e))
        finally:
            try:
                if generated_upload:os.unlink(generated_upload)
            except OSError:logger.debug('Could not remove temporary image-search upload %s',generated_upload,exc_info=True)

    def toggle_favorites_view(self):
        if not self.in_favorites_view:
            self.show_favorites();return
        if self.active_favorite_group is not None:
            self.show_favorites();return
        target=self.favorites_return_path
        self.in_favorites_view=False;self.active_favorite_group=None;self.return_to_favorites=False
        if target:
            for i,tab in enumerate(self.tabs):
                if self._normalized_path_key(tab.get('path',''))==self._normalized_path_key(target):
                    self.tabbar.setCurrentIndex(i);self.activate_tab(i,save_previous=False);return
            if Path(target).is_dir():self.load(Path(target),restore_scroll=True);return
        self.back()
    def show_favorites(self,group_id=None):
        if not self.in_favorites_view:self.save_current_view();self.favorites_return_path=str(self.current)
        self.in_favorites_view=True;self.return_to_favorites=False;self.active_favorite_group=group_id
        if hasattr(self,'selection_clear_overlay') and not self.right_controls_auto_hide:self.show_right_controls()
        fav=[]
        if group_id is None:
            for group in self.favorite_groups:
                members=[raw for raw in group.get('folders',[]) if Path(raw).is_dir()]
                preview=members[0] if members else None
                fav.append({'path':f"favgroup://{group.get('id')}",'name':group.get('name','Group'),'is_dir':False,'kind':'favorite_group','mtime':0,'ctime':0,'size':None,'preview_path':preview})
            favorite_paths=sorted(self.favorites,key=natural_key)
        else:
            group=next((g for g in self.favorite_groups if g.get('id')==group_id),None)
            if group is None:self.active_favorite_group=None;return self.show_favorites()
            favorite_paths=sorted(set(group.get('folders',[])),key=natural_key)
        for raw in favorite_paths:
            p=Path(raw)
            try:
                if not p.exists():continue
                st=p.stat();is_directory=p.is_dir()
                fav.append({'path':p,'name':p.name or str(p),'is_dir':is_directory,'kind':'folder' if is_directory else kind(p),'mtime':st.st_mtime,'ctime':birth(p),'size':None if is_directory else st.st_size})
            except OSError:continue
        self.items=fav;self.search_text_changed(self.search.text());self.grid.setFolderThumbs(self.custom);self.status.setText(f'{len(fav)} favorites'+(' in group' if group_id else ''))
        group=next((g for g in self.favorite_groups if g.get('id')==group_id),None) if group_id else None
        self.crumb.setText('★ Favorites' + (f' / {group["name"]}' if group else ''))

    def collection(self):
        self.show_favorites()
    def set_custom(self):
        if self.in_favorites_view and self.active_favorite_group is not None:
            f,_=QFileDialog.getOpenFileName(self,'Choose favorites group thumbnail',str(self.current),'Images (*.jpg *.jpeg *.png *.webp *.gif *.bmp)')
            if f:self.custom[f"favgroup:{self.active_favorite_group}"]=f;self.save_settings();self.show_favorites(self.active_favorite_group)
            return
        if not self.current:return
        f,_=QFileDialog.getOpenFileName(self,'Choose folder thumbnail',str(self.current),'Images (*.jpg *.jpeg *.png *.webp *.gif *.bmp)')
        if f:self.custom[str(self.current)]=f;self.grid.folder_cache.pop(str(self.current),None);self.save_settings();self.refresh()
    def launch_mode(self):
        f=QFileDialog.getExistingDirectory(self,'Choose Launch Mode folder',str(self.current))
        if f:self.current=Path(f);self.load(self.current)
    def set_startup(self):
        f=QFileDialog.getExistingDirectory(self,'Choose startup folder',str(self.current))
        if f:
            self.startup_folder=str(Path(f)); self.back_target=Path(f); self.save_settings(); self.status.setText(f'Startup folder: {f}')
    def clear_startup(self):
        self.startup_folder=None; self.back_target=Path(self.current); self.save_settings(); self.status.setText('Startup: last folder')
    def premium_info(self):
        QMessageBox.information(self,'Magellan features','Implemented: Favorites, custom folder thumbnails, Collection View, Launch Mode, saved settings/recent folder, tabs, single-click navigation, lazy visible thumbnail rendering, fast-scroll thumbnail throttling, responsive square thumbnails, and configurable startup folder.')

    def show_controls_help(self):
        dlg=QDialog(self);dlg.setWindowTitle('Magellan controls');dlg.resize(690,690);layout=QVBoxLayout(dlg)
        content=QLabel('''<h2>Navigation and tabs</h2>
<ul><li>Left-click a thumbnail to open a folder or view a file. Click a thumbnail name to open its folder in File Explorer or reveal the file there. Alt+left-click reveals an item at its exact location.</li><li>Right-click or Backspace goes up/back. The Back button returns to the previous location. Middle-click a folder to open it in a new tab.</li><li>Tab / Shift+Tab switches tabs. Drag a tab to reorder it. Hold Ctrl and click tabs to select multiple tabs for coloring; right-click a tab for its outline color menu. Scroll over the tab strip to move sideways.</li><li>When enabled, Hide tabs with the main bar hides the navigation strip, path, and status area with the main panel. Moving to the top reveals the panel; it stays open briefly after opening a tab.</li></ul>
<h2>Keyboard and selection</h2>
<ul><li>Arrow keys move the keyboard highlight; holding an arrow accelerates. Enter or Space opens the highlighted item. Mouse movement returns navigation to the pointer. Q toggles focus between search and keyboard navigation.</li><li>Shift+drag draws a selection rectangle and toggles items inside it. Drag near the top or bottom edge to auto-scroll. Selection remains while you navigate between folders.</li><li>Ctrl+A selects all items on the current page. If all are already selected it clears them; if only some are selected it selects the rest. Right-side buttons select all, clear selection, or undo the last deselection.</li><li>Ctrl+Z undoes the latest selection change or deletion. Shift+Delete sends selected files and folders to the Recycle Bin.</li><li>Hold Ctrl while dragging to enable organization mode. Release Ctrl to leave it. You can drag one or multiple items between thumbnails and Favorites groups.</li></ul>
<h2>Favorites and Farview</h2>
<ul><li>The Favorites button opens Favorites; from a group or subfolder it returns to the Favorites root, and from the root it exits Favorites. Use the plus on a thumbnail to add it to Favorites or choose a group.</li><li>In Favorites, Delete removes selected entries. Double-click a group name to rename it. Groups stay at the top of the Favorites list.</li><li>Farview creates a tab that lines up images/files found in subfolders. It can be created from a folder or selection, and multiple Farview tabs can use different sorting. The Personal order sort uses a saved custom sequence when one exists.</li><li>Use Manage personal orders to search, sort, or remove saved sequences. Excluded paths are listed in the personal-order settings.</li></ul>
<h2>Sorting and search</h2>
<ul><li>Keys 1–6 select Name, Last modified, Last created, Type, Size, and Random sorting; 7 selects Personal order. The direction indicator switches ascending and descending. Personal order applies a custom sequence for the current folder or view.</li><li>Use the search field to filter items. Recursive search can be enabled in Performance. The clear button empties the search. Tools → Scroll behavior sets rows per wheel notch (including fractional rows) and optional momentum.</li></ul>
<h2>Image viewer</h2>
<ul><li>Left / right arrows or Q / E show the previous / next image. Holding a navigation key accelerates. Click the left or right edge to change images.</li><li>Right-click, Escape, or Up closes the viewer. Double-click toggles zoom. The wheel zooms at the pointer; drag to pan.</li><li>R rotates clockwise; F flips horizontally; G fits the image; H toggles slideshow; minus / equals zoom out/in; WASD pans. Delete sends the current image to the Recycle Bin.</li><li>Use the bottom controls for rotate, flip, fit, side-by-side pages, and slideshow. Hover the top area for image details.</li></ul>
<h2>Thumbnails and panels</h2>
<ul><li>Hover a thumbnail for size/date details and its Favorites control. Hover the right edge for selection and group controls.</li><li>When auto-hide is enabled, move to the top edge for the main panel or the left edge for File Tree. The tree remembers its open paths and selected location; use its × button to close it.</li><li>Performance settings include recursive search, thumbnail cache controls, and other resource limits. View settings control thumbnail details, panel auto-hide, and automatic folder refresh. Scroll test logging can be toggled from Help when diagnostic timings are needed.</li></ul>''')
        content.setWordWrap(True);content.setTextFormat(Qt.RichText);content.setMargin(14)
        scroll=QScrollArea(dlg);scroll.setWidgetResizable(True);scroll.setWidget(content);layout.addWidget(scroll)
        buttons=QDialogButtonBox(QDialogButtonBox.Close);buttons.rejected.connect(dlg.reject);layout.addWidget(buttons);dlg.exec()

if __name__=='__main__':
    app=QApplication(sys.argv);app.setStyle('Fusion')
    lock_dir=Path(QStandardPaths.writableLocation(QStandardPaths.AppDataLocation));lock_dir.mkdir(parents=True,exist_ok=True)
    _INSTANCE_LOCK=QLockFile(str(lock_dir/'magellan-instance.lock'))
    if not _INSTANCE_LOCK.tryLock(0):
        if _INSTANCE_LOCK.error()==QLockFile.LockFailedError:
            QMessageBox.warning(None,'Magellan is already running','Another Magellan instance is still running. Close it in Task Manager before starting another copy. If it has already exited, wait a moment and try again.')
        else:
            QMessageBox.warning(None,'Magellan could not start','Magellan could not create its single-instance lock file. Check that the application settings folder is writable.')
        sys.exit(0)
    w=Explorer();w.show();sys.exit(app.exec())


