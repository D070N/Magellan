import os, sys, json, subprocess, threading, hashlib, re, random, tempfile, base64, time, ctypes, copy, shutil, struct, logging, atexit
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
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
try:
    import imageio_ffmpeg
except Exception:
    imageio_ffmpeg=None

from PySide6.QtCore import Qt, QSize, QRect, QRectF, QPoint, QPointF, Signal, QObject, QThread, QStandardPaths, QTimer, QDir, QEvent, QMimeData, QEventLoop
from PySide6.QtGui import QPixmap, QPainter, QPen, QColor, QFont, QFontMetricsF, QCursor, QImage, QImageReader, QIcon, QMovie, QDrag
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QAbstractScrollArea,
    QToolButton, QLabel, QFrame, QSlider, QMenu, QMenuBar, QFileDialog,
    QMessageBox, QInputDialog, QDialog, QDialogButtonBox, QListWidget,
    QTabBar, QTreeWidget, QTreeWidgetItem, QDockWidget, QCheckBox, QSpinBox, QComboBox
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
                tail=old_log.read_bytes()[-handler_limit:]
                newline=tail.find(b'\n')
                old_log.write_bytes(tail[newline+1:] if newline>=0 else tail)
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

def decode_viewer_image(path,preview_size,max_dimension=8000,max_file_bytes=100*1024*1024,preload=False):
    suffix=Path(path).suffix.casefold()
    try:file_size=Path(path).stat().st_size
    except OSError:return QImage(),QImage(),False,QSize()
    cacheable=file_size<=max_file_bytes
    source_size=QSize()
    if suffix not in PDF_EXTS and suffix!='.jxl':
        try:
            probe=QImageReader(str(path));probe.setAutoTransform(True);probe.setDecideFormatFromContent(True);size=probe.size()
            if size.isValid():source_size=QSize(size)
            cacheable=cacheable and (not size.isValid() or max(size.width(),size.height())<=max_dimension)
        except Exception:logger.debug('Could not inspect viewer image dimensions',exc_info=True)
    if preload and not cacheable:return QImage(),QImage(),False,source_size
    if suffix in PDF_EXTS:
        document=load_pdf_document(path)
        if document is None or document.pageCount()<1:return QImage(),QImage(),False,source_size
        page=document.pagePointSize(0);cacheable=cacheable and max(page.width(),page.height())<=max_dimension
        if preload and not cacheable:return QImage(),QImage(),False,source_size
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
                if preload and not cacheable:return QImage(),QImage(),False,source_size
                source=source.convert('RGBA')
                image=QImage(source.tobytes(),source.width,source.height,source.width*4,QImage.Format_RGBA8888).copy()
        except Exception:
            logger.debug('JPEG XL viewer decode failed',exc_info=True)
            return QImage(),QImage(),False,source_size
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
            return QImage(),QImage(),False,source_size
    if image.isNull():return image,image,False,source_size
    cacheable=cacheable and max(image.width(),image.height())<=max_dimension
    preview=image if image.width()<=preview_size.width() and image.height()<=preview_size.height() else image.scaled(preview_size,Qt.KeepAspectRatio,Qt.SmoothTransformation)
    if not source_size.isValid():source_size=image.size()
    return image,preview,cacheable,source_size



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
    except:return 0

def datefmt(t):
    try:return datetime.fromtimestamp(t).strftime('%d/%m/%Y %H:%M')
    except:return '—'

def sizefmt(n):
    if n<1024:return f'{n} B'
    for u in ('KB','MB','GB','TB'):
        n/=1024
        if n<1024:return f'{n:.1f} {u}'
    return f'{n:.1f} PB'

class ScanSignals(QObject): done=Signal(list,str,int)
class ScanTask(QThread):
    def __init__(self,folder,recursive=False,generation=0,selection=None):super().__init__();self.folder=Path(folder);self.recursive=recursive;self.generation=generation;self.selection=list(selection or []);self.signals=ScanSignals()
    def run(self):
        out=[]
        try:
            if self.recursive:
                roots=[Path(p) for p in self.selection] if self.selection else [self.folder]
                seen=set()
                for selected_root in roots:
                    if self.isInterruptionRequested():return
                    if selected_root.is_file():
                        candidates=[(selected_root.parent,[selected_root.name])]
                    elif selected_root.is_dir():
                        candidates=os.walk(selected_root,followlinks=False)
                    else:continue
                    for root,dirs,files in candidates:
                        if self.isInterruptionRequested():return
                        dirs[:]=sorted((d for d in dirs if not d.startswith('.') or d.startswith('. ')),key=natural_key)
                        for name in sorted(files,key=natural_key):
                            if self.isInterruptionRequested():return
                            p=Path(root)/name
                            if p.suffix.casefold() not in PREVIEW_EXTS:continue
                            path_key=os.path.normcase(os.path.abspath(str(p)))
                            if path_key in seen:continue
                            seen.add(path_key)
                            try:
                                st=p.stat(follow_symlinks=False)
                                out.append({'path':p,'name':p.name,'is_dir':False,'kind':kind(p),'mtime':st.st_mtime,'ctime':birth(p),'size':st.st_size,'parent':str(p.parent)})
                            except OSError:pass
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

class VirtualGrid(QAbstractScrollArea):
    openItem=Signal(str); middleOpenItem=Signal(str); goBack=Signal(); setThumb=Signal(str); context=Signal(str); imageSearchRequested=Signal(str); toggleItemFavorite=Signal(str)
    def __init__(self,parent=None):
        super().__init__(parent);self._explorer_window=parent.window() if parent is not None else None;self._closing=False;self.items=[];self.thumb=230;self.columns=5;self.mode='vertical';self.cache=OrderedDict();self.cache_bytes=0;self.cache_limit_bytes=256*1024*1024;self._visible_cache_keys=None;self.thumb_futures={};self.folder_cache={};self.folder_custom={};self.folder_futures={};self.folder_last_visible={};self.pool=ThreadPoolExecutor(max_workers=4);self.folder_pool=ThreadPoolExecutor(max_workers=2);self.size_pool=ThreadPoolExecutor(max_workers=1);self.folder_size_cache={};self.folder_size_futures={};self.hover_favorite_rect=QRect();self.hover_overlay_rect=QRect();self.hover_tooltip_rect=QRect();self.persistent_folder_cache_dir=None;self.last_scroll_value=0;self.last_scroll_time=0;self.fast_scroll=False;self.search_hover=-1;self.hover_index=-1;self.hover_animation_started=0;self.drag_source_index=-1;self.drag_started=False;self.drag_start=QPoint();self.last_favorite_drag=0.0;self.pending_favorite_item=-1;self.suppress_favorite_release=False;self.favorite_picker_delay=QTimer(self);self.favorite_picker_delay.setSingleShot(True);self.favorite_picker_delay.setInterval(120);self.favorite_picker_delay.timeout.connect(self.open_hover_favorite_picker);self.favorite_picker_key=None;self.favorite_picker_hover_key=None;self.favorite_click_timer=QTimer(self);self.favorite_click_timer.setSingleShot(True);self.favorite_click_timer.setInterval(220);self.favorite_click_timer.timeout.connect(self.dispatch_favorite_open);self.setAcceptDrops(True);self.viewport().setAcceptDrops(True);self.setFrameShape(QFrame.NoFrame);self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);self.viewport().setMouseTracking(True);self.viewport().installEventFilter(self);self.image_search_active=False
        icon_dir=getattr(parent,'icon_dir',None);image_icon=Path(icon_dir)/'image.png' if icon_dir else None;self.image_name_icon=QPixmap(str(image_icon)) if image_icon and image_icon.is_file() else QPixmap()
        self.selected_paths=set();self.selection_undo=[];self.selection_dragging=False;self.selection_drag_moved=False;self.selection_anchor=None;self.selection_current=None;self.selection_base=set();self.last_mouse_pos=QPoint();self.navigation_index=-1;self.navigation_key=None;self.navigation_started=0.0
        self.selection_scroll_timer=QTimer(self);self.selection_scroll_timer.setInterval(16);self.selection_scroll_timer.timeout.connect(self.auto_scroll_selection)
        self.thumb_timer=QTimer(self);self.thumb_timer.timeout.connect(self.poll_async);self.thumb_timer.start(60)
        self.scroll_settle_timer=QTimer(self);self.scroll_settle_timer.setSingleShot(True);self.scroll_settle_timer.timeout.connect(self.scroll_settled)
        self.hover_delay_timer=QTimer(self);self.hover_delay_timer.setSingleShot(True);self.hover_delay_timer.timeout.connect(self.begin_hover_animation)
        self.hover_animation_timer=QTimer(self);self.hover_animation_timer.setInterval(16);self.hover_animation_timer.timeout.connect(self.advance_hover_animation)
        self.setFocusPolicy(Qt.StrongFocus);self.verticalScrollBar().valueChanged.connect(self.on_scroll)
    def explorer(self):
        """Return the top-level Explorer window, not the grid host widget."""
        return self._explorer_window or self.window()

    def setItems(self,items,reset_scroll=True):
        self.set_hover_index(-1)
        self.navigation_index=-1
        # Navigating to a new view should never leave old folder-discovery jobs ahead of the new viewport.
        for f in list(self.folder_futures.values()):
            try: f.cancel()
            except: pass
        self.folder_futures.clear()
        self.items=items;self.recalc()
        # A new folder/view always begins at the top; the Explorer restores a
        # saved position explicitly after the scan has finished.
        if reset_scroll:self.verticalScrollBar().setValue(0)
    def setFolderThumbs(self,custom):self.folder_custom=custom or {};self.viewport().update()
    def set_hover_settings(self,enabled,delay):
        self.hover_delay_timer.stop();self.hover_animation_timer.stop();self.hover_index=-1;self.hover_animation_started=0
        self.explorer().hover_details_enabled=bool(enabled);self.explorer().hover_details_delay=max(0,int(delay));self.viewport().update()
    def refresh_hover_from_cursor(self):
        pos=self.viewport().mapFromGlobal(QCursor.pos())
        self.set_hover_index(self.thumbnail_hit(pos) if self.viewport().rect().contains(pos) else -1)
    def request_folder_size(self,item):
        if self._closing:return
        key=str(item['path'])
        if key in self.folder_size_cache:
            item['size']=self.folder_size_cache[key];return
        if key in self.folder_size_futures:return
        self.folder_size_futures[key]=self.size_pool.submit(self.folder_size_worker,key)
    def folder_size_worker(self,path):
        total=0
        for root,dirs,files in os.walk(path,followlinks=False):
            if self._closing:break
            for name in files:
                if self._closing:break
                try:total+=(Path(root)/name).stat(follow_symlinks=False).st_size
                except OSError:pass
        return total
    def shutdown_workers(self):
        if self._closing:return
        self._closing=True
        for timer in (self.thumb_timer,self.scroll_settle_timer,self.hover_delay_timer,self.hover_animation_timer,self.favorite_picker_delay,self.favorite_click_timer,self.selection_scroll_timer):timer.stop()
        for futures in (self.thumb_futures,self.folder_futures,self.folder_size_futures):
            for future in futures.values():future.cancel()
            futures.clear()
        for executor in (self.pool,self.folder_pool,self.size_pool):executor.shutdown(wait=False,cancel_futures=True)
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
        # Shift already-painted rows and let Qt repaint only the newly exposed
        # strip. A full viewport update on every scrollbar tick defeats this.
        self.viewport().scroll(dx,dy)
    def wheelEvent(self,e):self.verticalScrollBar().setValue(self.verticalScrollBar().value()-e.angleDelta().y());self.setFocus();e.accept()
    def visible_range(self):
        top=self.verticalScrollBar().value(); bottom=top+self.viewport().height()
        if self.mode=='horizontal':return max(0,top//self.row_h-1),min(len(self.items),(bottom//self.row_h)+2)
        first=max(0,top//self.row_h*self.columns-self.columns); last=min(len(self.items),((bottom//self.row_h)+2)*self.columns);return first,last
    def paintEvent(self,e):
        self._visible_cache_keys=set() if not getattr(self.explorer(),'thumbnail_cache_enabled',True) else None
        p=QPainter(self.viewport());p.fillRect(self.viewport().rect(),QColor('#080a10'))
        if not self.items:
            area=self.viewport().rect();p.setPen(QColor('#d0a32d'));p.setFont(QFont('Segoe UI Symbol',54,QFont.Bold));p.drawText(QRect(0,area.height()//2-100,area.width(),90),Qt.AlignCenter,'◈')
            p.setPen(QColor('#e5e5e5'));p.setFont(QFont('Segoe UI',20,QFont.DemiBold));p.drawText(QRect(0,area.height()//2-8,area.width(),36),Qt.AlignCenter,'MAGELLAN')
            p.setPen(QColor('#92969d'));p.setFont(QFont('Segoe UI',11));p.drawText(QRect(0,area.height()//2+32,area.width(),32),Qt.AlignCenter,'Choose a folder to get started')
            p.end();self._trim_cache_after_paint();return
        first,last=self.paint_range(e)
        if self.mode=='horizontal':
            for i in range(first,last):self.paint_horizontal(p,i)
        else:
            gap=9; cw=self.cell_w
            for i in range(first,last):
                row,col=divmod(i,self.columns);x=9+col*(cw+gap);y=row*self.row_h-self.verticalScrollBar().value();self.paint_card(p,i,QRect(int(x),int(y),int(cw),int(cw+48)))
        if self.selection_dragging and self.selection_anchor is not None and self.selection_current is not None:
            rect=QRect(self.selection_anchor,self.selection_current).normalized();rect.translate(0,-self.verticalScrollBar().value());p.setPen(QPen(QColor(190,190,190,145),1));p.setBrush(QColor(170,170,170,55));p.drawRect(rect)
        for i in range(first,last):
            item=self.items[i];selected=self.selection_key(item) in self.selected_paths
            if not selected and i!=self.navigation_index:continue
            if self.mode=='horizontal':r=QRect(10,i*self.row_h-self.verticalScrollBar().value(),self.viewport().width()-20,self.row_h-7)
            else:
                row,col=divmod(i,self.columns);r=QRect(9+col*(self.cell_w+9),row*self.row_h-self.verticalScrollBar().value(),self.cell_w,self.cell_w+48)
            p.setPen(QPen(QColor('#ffffff'),3));p.setBrush(Qt.NoBrush);p.drawRect(r.adjusted(1,1,-2,-2))
        if self.hover_index>=first and self.hover_index<last and getattr(self.explorer(),'hover_details_enabled',True):
            i=self.hover_index
            if self.mode=='horizontal':
                y=i*self.row_h-self.verticalScrollBar().value();preview=QRect(14,y+4,max(1,self.row_h-15),max(1,self.row_h-15))
            else:
                row,col=divmod(i,self.columns);x=9+col*(self.cell_w+9);y=row*self.row_h-self.verticalScrollBar().value();preview=QRect(x+1,y+1,max(1,self.cell_w-2),max(1,self.cell_w-2))
            self.paint_hover_details(p,preview,self.items[i])
        p.end();self._trim_cache_after_paint()
    def paint_range(self,event):
        # The cache-disabled mode needs a complete visible pass so its RAM cache
        # can retain all displayed items. With caching enabled, limit Python-side
        # card work to rows touching the invalidated region plus nearby prefetch.
        if not getattr(self.explorer(),'thumbnail_cache_enabled',True):return self.visible_range()
        rect=event.rect();scroll=self.verticalScrollBar().value();top=max(0,rect.top()+scroll);bottom=max(top,rect.bottom()+1+scroll)
        if self.mode=='horizontal':return max(0,top//self.row_h-1),min(len(self.items),bottom//self.row_h+2)
        first_row=max(0,top//self.row_h-1);last_row=min((len(self.items)+self.columns-1)//self.columns,bottom//self.row_h+2)
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
                    except: pass
                q=nq; depth+=1
        except Exception:
            logger.debug('Could not resolve a folder preview in %s',folder,exc_info=True)
        return None
    def preview_is_readable(self,path):
        """Reject incomplete/corrupt previews and keep searching for a usable sibling."""
        try:
            return self.thumbnail_worker(str(path),64,64) is not None
        except Exception:
            return False
    def _thumb_cache_roots(self):
        return {str(Path(x).resolve()) for x in getattr(self.explorer(),'pinned_folders',set())} if self.explorer() is not None else set()
    def is_persistently_kept(self, folder):
        try:
            p=Path(folder).resolve()
            for raw in self._thumb_cache_roots():
                root=Path(raw)
                if p == root:
                    return True
                if p.parent == root:
                    return True
        except Exception:
            pass
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
        try:
            p.parent.mkdir(parents=True,exist_ok=True)
            img=self.thumbnail_worker(str(source),512,512)
            if img is not None and not img.isNull():
                ok=img.save(str(p),'JPEG',92)
                if ok and p.exists() and p.stat().st_size>0:
                    return str(p)
        except Exception:
            logger.debug('Could not resolve shortcut %s',p,exc_info=True)
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
        if self.folder_cache.get(key):return self.folder_cache[key]
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
        fut=self.folder_pool.submit(self.folder_image,path);self.folder_futures[key]=fut;return None
    def poll_async(self):
        changed=False;sizes_changed=False
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
                changed=True
        for key,f in list(self.folder_futures.items()):
            if f.done():
                self.folder_futures.pop(key,None)
                if f.cancelled():
                    continue
                try:
                    val=f.result()
                    parent=self.explorer()
                    if val and parent is not None and key in getattr(parent,'pinned_folders',set()):
                        val=self.persist_folder_thumb(key,val); parent.pinned_thumbs[key]=val; parent.save_settings()
                    self.folder_cache[key]=val
                except Exception:
                    logger.exception('Folder thumbnail worker failed for %s',key)
                    self.folder_cache[key]=None
                changed=True
        for key,f in list(self.folder_size_futures.items()):
            if f.done():
                self.folder_size_futures.pop(key,None)
                if f.cancelled():continue
                try:
                    size=max(0,int(f.result()));self.folder_size_cache[key]=size
                    for item in self.items:
                        if item.get('is_dir') and str(item['path'])==key:item['size']=size
                except Exception:
                    logger.exception('Folder size calculation failed for %s',key)
                changed=True;sizes_changed=True
        if changed:
            self.viewport().update()
            parent=self.explorer()
            if sizes_changed and parent is not None and getattr(parent,'sort',None)=='size' and not self.folder_size_futures:
                QTimer.singleShot(0,parent.refresh)

    def on_scroll(self,value):
        now=__import__('time').monotonic()
        dt=now-self.last_scroll_time if self.last_scroll_time else 1
        speed=abs(value-self.last_scroll_value)/max(dt,0.001)
        self.fast_scroll=speed>900
        self.last_scroll_value=value;self.last_scroll_time=now
        self.scroll_settle_timer.start(220)
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
        self.fast_scroll=False
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
            self.cache.move_to_end(key)
            if self._visible_cache_keys is not None:self._visible_cache_keys.add(key)
            return self.cache[key]
        if self.fast_scroll:return self.cache.get(key)
        if key not in self.thumb_futures:
            self.thumb_futures[key]=self.pool.submit(self.thumbnail_worker,str(path),int(box.width()),int(box.height()))
        return None
    @staticmethod
    def pixmap_bytes(pixmap):return pixmap.width()*pixmap.height()*max(1,(pixmap.depth()+7)//8)
    def set_cache_limit_mib(self,limit_mib):
        self.cache_limit_bytes=max(16,min(8192,int(limit_mib)))*1024*1024
        while self.cache and self.cache_bytes>self.cache_limit_bytes:
            _,oldest=self.cache.popitem(last=False)
            self.cache_bytes=max(0,self.cache_bytes-self.pixmap_bytes(oldest))
        self.viewport().update()

    def icon(self,k):return {'folder':'📁','video':'▶','audio':'♫','document':'PDF','file':'•','image':'🖼','favorite_group':'★'}.get(k,'•')
    def paint_card(self,p,i,r):
        it=self.items[i];p.setPen(QPen(QColor('#171a24')));p.setBrush(QColor('#020513'));p.drawRect(r)
        # Keep the preview perfectly square. The slider controls the target
        # thumbnail size; the actual cell width adapts to the available window.
        side=max(1,min(r.width()-2,self.cell_w if self.mode=='vertical' else r.width()-2))
        preview=QRect(r.x()+1,r.y()+1,side,side)
        source=None
        if it['kind'] in ('image','video') or (it['kind']=='document' and Path(it['path']).suffix.casefold() in PDF_EXTS): source=str(it['path'])
        elif it.get('kind')=='favorite_group':source=self.folder_custom.get('favgroup:'+str(it['path']).split('://',1)[1]) or (self.ensure_folder_thumb(it['preview_path']) if it.get('preview_path') else None)
        elif it['is_dir']: source=self.ensure_folder_thumb(it['path'])
        if source:
            px=self.pix(source,QSize(preview.width(),preview.height()))
            if px:p.drawPixmap(preview,px)
            else:self.draw_center(p,preview,self.icon(it['kind']),48)
        else:self.draw_center(p,preview,self.icon(it['kind']),48)
        label_y=r.y()+side+5
        if it.get('kind')=='favorite_group':
            p.setPen(QColor('#d0a32d'));p.setFont(QFont('Segoe UI Symbol',11,QFont.Bold));p.drawText(QRect(r.x()+7,label_y,18,24),Qt.AlignCenter,'★')
        if it['is_dir']:
            # Compact white folder glyph, reduced ~15% from the previous icon.
            p.save();p.setPen(Qt.NoPen);p.setBrush(QColor('#f1f1f1'))
            fx=r.x()+7; fy=label_y+5
            p.drawRoundedRect(QRect(fx,fy,13,10),2,2)
            p.drawRect(QRect(fx+2,fy-2,5,4));p.restore()
            text_x=r.x()+25
        else:
            text_x=r.x()+(29 if it.get('kind')=='favorite_group' else 7)
        if not it['is_dir'] and it.get('kind')=='image' and Path(str(it.get('path',''))).suffix.casefold()!='.gif' and not self.image_name_icon.isNull():
            p.drawPixmap(QRect(text_x,label_y+4,16,16),self.image_name_icon);text_x+=20
        p.setPen(QColor('#ddd'));p.setFont(QFont('Segoe UI',11));p.drawText(QRect(text_x,label_y,r.width()-(text_x-r.x())-7,38),Qt.TextWordWrap,self.label_prefix(it)+it['name'])
        if self.image_search_active and getattr(self,'search_hover',-1)==i:
            p.setPen(QPen(QColor('#ffffff'),3));p.setBrush(Qt.NoBrush);p.drawRect(preview.adjusted(1,1,-1,-1))
    def paint_horizontal(self,p,i):
        it=self.items[i];y=i*self.row_h-self.verticalScrollBar().value();r=QRect(10,y,self.viewport().width()-20,self.row_h-7);p.setBrush(QColor('#020513'));p.setPen(QPen(QColor('#171a24')));p.drawRect(r)
        box=QRect(r.x()+4,r.y()+4, r.height()-8,r.height()-15)
        source=str(it['path']) if it['kind'] in ('image','video') or (it['kind']=='document' and Path(it['path']).suffix.casefold() in PDF_EXTS) else (self.ensure_folder_thumb(it['path']) if it['is_dir'] else None)
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
        if not it['is_dir'] and it.get('kind')=='image' and Path(str(it.get('path',''))).suffix.casefold()!='.gif' and not self.image_name_icon.isNull():
            p.drawPixmap(QRect(text_x,r.y()+16,16,16),self.image_name_icon);text_x+=20
        p.setPen(QColor('#eee'));p.setFont(QFont('Segoe UI',12));p.drawText(QRect(text_x,r.y()+12,r.width()-text_x+r.x()-15,25),Qt.AlignLeft|Qt.AlignVCenter,self.label_prefix(it)+it['name'])
        p.setPen(QColor('#777'));p.setFont(QFont('Segoe UI',10));p.drawText(QRect(box.right()+15,r.y()+40,r.width()-box.width()-30,22),Qt.AlignLeft|Qt.AlignVCenter,('Folder' if it['is_dir'] else it['kind'].title())+' • '+datefmt(it['mtime']))
    def label_prefix(self,item):
        if item.get('is_dir') or item.get('kind')=='favorite_group':return ''
        suffix=Path(str(item.get('path',''))).suffix.casefold()
        if item.get('kind')=='video' or suffix=='.gif':return '▶  '
        if item.get('kind')=='image':return '' if not self.image_name_icon.isNull() else '▧  '
        if item.get('kind')=='audio':return '♫  '
        return '▤  '
    def draw_center(self,p,r,text,size):p.setPen(QColor('#eee'));p.setFont(QFont('Segoe UI',size));p.drawText(r,Qt.AlignCenter,text)
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
        if item.get('is_dir'):self.request_folder_size(item)
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
                    f.cancel();self.folder_size_futures.pop(key,None)
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
        self.selected_paths.update(self.selection_key(item) for item in self.items if self.selectable_item(item) and item.get('path'))
        self.viewport().update();self.explorer().update_selection_button()
    def begin_rect_selection(self,pos):
        self.setFocus();self.selection_dragging=True;self.selection_drag_moved=False;self.selection_anchor=QPoint(pos.x(),pos.y()+self.verticalScrollBar().value());self.selection_current=QPoint(self.selection_anchor);self.selection_base=set(self.selected_paths);self.last_mouse_pos=QPoint(pos);self.selection_scroll_timer.start();self.navigation_index=-1;self.set_hover_index(-1);self.favorite_picker_delay.stop();self.favorite_picker_key=None;self.viewport().update()
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
        y=self.last_mouse_pos.y();height=self.viewport().height();edge=140;delta=0
        if y<edge:delta=-max(1,round((edge-y)/edge*44))
        elif y>height-edge:delta=max(1,round((y-(height-edge))/edge*44))
        if delta:
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
        if Qt.Key_1<=key<=Qt.Key_6 and not e.modifiers()&(Qt.ControlModifier|Qt.AltModifier|Qt.ShiftModifier):explorer.set_sort(('name','mtime','ctime','type','size','random')[key-Qt.Key_1]);return True
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
                if (mods&Qt.ControlModifier) and not self.hover_favorite_rect.contains(pos):
                    i=self.hit(pos)
                    if 0<=i<len(self.items) and self.selectable_item(self.items[i]):self.toggle_selected_path(self.items[i]['path'])
                    e.accept();return True
                if not self.hover_favorite_rect.contains(pos) and self.hit(pos)<0 and self.selected_paths:self.clear_selection()
            if e.type()==QEvent.MouseButtonRelease and e.button()==Qt.LeftButton and self.selection_dragging:
                self.finish_rect_selection(e.position().toPoint());e.accept();return True
            if e.type()==QEvent.MouseMove:
                pos=e.position().toPoint()
                if self.selection_dragging:self.update_rect_selection(pos);return True
                if self.navigation_index>=0:self.navigation_index=-1;self.viewport().update()
                if getattr(self.explorer(),'in_favorites_view',False) and getattr(self.explorer(),'favorite_organize_mode',False) and self.drag_source_index>=0 and e.buttons()&Qt.LeftButton:
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
                if e.mimeData().hasText():e.acceptProposedAction();return True
            if e.type()==QEvent.Drop:
                self.explorer().handle_favorite_drop(e.mimeData().text(),self.hit(e.position().toPoint()));e.acceptProposedAction();return True
            if getattr(self.explorer(),'in_favorites_view',False) and getattr(self.explorer(),'favorite_organize_mode',False):
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
        mime.setText('favgroup://'+raw.split('://',1)[-1] if item.get('kind')=='favorite_group' else 'folder://'+raw)
        drag=QDrag(self);drag.setMimeData(mime)
        ghost=self.favorite_drag_pixmap(item)
        if not ghost.isNull():drag.setPixmap(ghost);drag.setHotSpot(QPoint(ghost.width()//2,ghost.height()//2))
        drag.exec(Qt.MoveAction)
        self.last_favorite_drag=time.monotonic();self.drag_source_index=-1;self.drag_started=False

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
        if self.drag_source_index>=0 and e.buttons() & Qt.LeftButton and not self.drag_started and (e.position().toPoint()-self.drag_start).manhattanLength()>=QApplication.startDragDistance():
            self.start_favorite_drag();e.accept();return
        if self.image_search_active:
            self.search_hover=self.thumbnail_hit(e.position().toPoint())
            self.viewport().update()
        super().mouseMoveEvent(e)

    def mousePressEvent(self,e):
        self.setFocus()
        if e.button()==Qt.LeftButton and self.hover_index>=0 and self.hover_favorite_rect.contains(e.position().toPoint()):
            self.favorite_picker_delay.stop();self.toggleItemFavorite.emit(str(self.items[self.hover_index]['path']));e.accept();return
        if e.button()==Qt.LeftButton and getattr(self.explorer(),'in_favorites_view',False) and getattr(self.explorer(),'favorite_organize_mode',False):
            i=self.hit(e.position().toPoint())
            if i>=0:self.drag_source_index=i;self.drag_start=e.position().toPoint();self.drag_started=False;e.accept();return
        if e.button()==Qt.RightButton:
            explorer=self.explorer()
            if self.image_search_active:
                self.set_image_search_active(False);explorer.image_search_mode=False
                if hasattr(explorer,'status'):explorer.status.setText('Image search off')
                e.accept();return
            if getattr(explorer,'in_favorites_view',False) and getattr(explorer,'favorite_organize_mode',False):
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
                if getattr(self.explorer(),'in_favorites_view',False) and getattr(self.explorer(),'favorite_organize_mode',False):self.drag_source_index=i;self.drag_start=e.position().toPoint();self.drag_started=False;e.accept();return
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
    def mouseDoubleClickEvent(self,e):
        if not getattr(self.explorer(),'in_favorites_view',False):super().mouseDoubleClickEvent(e);return
        if not getattr(self.explorer(),'favorite_organize_mode',False):super().mouseDoubleClickEvent(e);return
        if time.monotonic()-self.last_favorite_drag<QApplication.doubleClickInterval()/1000+0.1:e.accept();return
        if e.button()==Qt.LeftButton:
            i=self.hit(e.position().toPoint())
            self.favorite_click_timer.stop();self.pending_favorite_item=-1;self.suppress_favorite_release=True
            if 0<=i<len(self.items):
                item=self.items[i]
                if item.get('kind')=='favorite_group' and self.explorer().favorite_organize_mode and self.group_name_hit(item,e.position().toPoint()):self.explorer().rename_favorite_group(str(item['path']).split('://',1)[1]);e.accept();return
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
    closeRequested=Signal(int)
    def __init__(self,parent=None):super().__init__(parent);self._last_middle_pos=None;self._last_middle_time=0.0;self._hovered_tab=-1;self.setMouseTracking(True)
    def wheelEvent(self, e):
        # Do not let the mouse wheel mutate or switch the tab model.
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
        e.accept()
    def leaveEvent(self,e):
        if self._hovered_tab!=-1:self._hovered_tab=-1;self.update()
        self.setCursor(Qt.ArrowCursor);super().leaveEvent(e)
    def close_rect(self,index):
        rect=self.tabRect(index);return QRect(rect.right()-23,rect.center().y()-8,16,16)
    def paintEvent(self,e):
        super().paintEvent(e)
        if self._hovered_tab<0 or self._hovered_tab>=self.count():return
        rect=self.close_rect(self._hovered_tab);p=QPainter(self);p.setRenderHint(QPainter.Antialiasing,True);p.setPen(Qt.NoPen);p.setBrush(QColor(45,48,55,220));p.drawRoundedRect(rect,3,3);p.setPen(QColor('#e8e8e8'));p.setFont(QFont('Segoe UI',9,QFont.Bold));p.drawText(rect,Qt.AlignCenter,'X');p.end()


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
        super().__init__(parent,Qt.Window);self.paths=list(paths);self.index=max(0,min(int(index),len(self.paths)-1)) if self.paths else -1;self.rotation=0;self.flip_h=False;self.flip_v=False;self.zoom=1.0;self.movie=None;self.full_image=QImage();self.static_pixmap=QPixmap();self.fit_pixmap=QPixmap();self.original_resolution=QSize();self.double_page=False;self.double_page_path=None;self.double_page_frame=QPixmap();self.loading=False;self.current_file_size=None;self.image_cache=OrderedDict();self.image_cache_bytes=0;self.image_cache_limit=256*1024*1024;self.cache_max_dimension=8000;self.cache_max_file_bytes=100*1024*1024;self.pending_image_paths=set();self.slideshow_active=False;self.slideshow_timer=QTimer(self);self.slideshow_timer.setInterval(max(250,int(slideshow_interval_ms)));self.slideshow_timer.timeout.connect(lambda:self.step(1));app=QApplication.instance();self.load_bridge=getattr(app,'_viewer_image_load_bridge',None)
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
    def load_current(self):
        if self.movie:self.movie.stop();self.movie.deleteLater();self.movie=None
        if not self.paths:
            self.index=-1;self.full_image=QImage();self.static_pixmap=QPixmap();self.fit_pixmap=QPixmap();self.original_resolution=QSize();self.current_file_size=None;self.loading=False;self.canvas.prepare_frame();return
        self.index=max(0,min(self.index,len(self.paths)-1))
        self.full_image=QImage();self.static_pixmap=QPixmap();self.fit_pixmap=QPixmap();self.original_resolution=QSize();self.double_page_path=None;self.double_page_frame=QPixmap();self.loading=False;self.rotation=0;self.flip_h=False;self.flip_v=False;self.zoom=1.0;self.canvas.pan_offset=QPoint();path=self.paths[self.index]
        try:self.current_file_size=Path(path).stat().st_size
        except OSError:self.current_file_size=None
        if Path(path).suffix.casefold() in ('.gif','.webp'):
            movie=QMovie(str(path));movie.setCacheMode(QMovie.CacheNone)
            if movie.isValid():
                self.movie=movie;movie.frameChanged.connect(self.canvas.prepare_frame);movie.start()
                if not self.original_resolution.isValid() and not movie.currentPixmap().isNull():self.original_resolution=movie.currentPixmap().size()
            else:
                entry=self.image_cache.get(path)
                if entry is not None:self.image_cache.move_to_end(path);self.full_image,self.fit_pixmap=entry[:2];self.static_pixmap=QPixmap();self.original_resolution=entry[2] if len(entry)>2 and entry[2].isValid() else self.full_image.size()
                else:self.loading=True;self.request_image(path)
        else:
            entry=self.image_cache.get(path)
            if entry is not None:self.image_cache.move_to_end(path);self.full_image,self.fit_pixmap=entry[:2];self.static_pixmap=QPixmap();self.original_resolution=entry[2] if len(entry)>2 and entry[2].isValid() else self.full_image.size()
            else:self.loading=True;self.request_image(path)
        self.setWindowTitle('Magellan Viewer');self.canvas.prepare_frame();self.preload_neighbours()
    def request_image(self,path,preload=False):
        if path in self.pending_image_paths:return
        self.pending_image_paths.add(path);preview_size=QSize(self.canvas.size());
        if preview_size.width()<2 or preview_size.height()<2:
            screen=QApplication.primaryScreen();preview_size=screen.size() if screen else QSize(1920,1080)
        future=VIEWER_IMAGE_POOL.submit(decode_viewer_image,path,preview_size,self.cache_max_dimension,self.cache_max_file_bytes,preload)
        def completed(result,p=path):
            try:data=result.result()
            except Exception:
                logger.exception('Viewer image load failed for %s',p)
                data=(QImage(),QImage(),False,QSize())
            self.load_bridge.loaded.emit(p,data)
        future.add_done_callback(completed)
    def preload_neighbours(self):
        if not self.paths:return
        # Prioritize the next image for the common forward-swipe direction.
        for offset in (1,-1):
            path=self.paths[(self.index+offset)%len(self.paths)]
            if path not in self.image_cache and path not in self.pending_image_paths and path!=self.paths[self.index]:self.request_image(path,preload=True)
    def on_image_loaded(self,path,image):
        self.pending_image_paths.discard(path)
        if not self.paths:return
        cacheable=True;source_size=QSize()
        if isinstance(image,tuple):
            full_image,fit_image=image[:2]
            if len(image)>2:cacheable=bool(image[2])
            if len(image)>3 and isinstance(image[3],QSize):source_size=image[3]
        else:full_image=fit_image=image
        if not full_image.isNull():
            fit_pixmap=QPixmap.fromImage(fit_image);source_size=source_size if source_size.isValid() else full_image.size();entry=(full_image,fit_pixmap,source_size)
            entry_bytes=self.cache_entry_bytes(entry)
            if cacheable and entry_bytes<=self.image_cache_limit and self.cache_window_contains(path):
                previous=self.image_cache.pop(path,None)
                if previous:self.image_cache_bytes-=self.cache_entry_bytes(previous)
                self.image_cache[path]=entry;self.image_cache_bytes+=entry_bytes
            while self.image_cache_bytes>self.image_cache_limit and len(self.image_cache)>1:
                old_path,old_entry=self.image_cache.popitem(last=False);self.image_cache_bytes-=self.cache_entry_bytes(old_entry)
            if path==self.paths[self.index]:self.full_image,self.fit_pixmap=entry[:2];self.static_pixmap=QPixmap();self.original_resolution=source_size;self.loading=False;self.canvas.prepare_frame()
            elif self.double_page and self.paths and path==self.paths[(self.index+1)%len(self.paths)]:self.double_page_path=path;self.double_page_frame=fit_pixmap;self.canvas.update()
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
        if not self.paths:return
        path=self.paths[self.index];parent=self.parent()
        if parent is None or not parent.send_paths_to_trash([path]):return
        self.image_cache.pop(path,None)
        if len(self.paths)==1:self.paths=[];self.close();return
        self.paths.pop(self.index)
        if self.index>=len(self.paths):self.index=len(self.paths)-1
        self.load_current()
    def toggle_slideshow(self):
        self.slideshow_active=not self.slideshow_active
        if self.slideshow_active:self.slideshow_timer.start();self.note_pointer_activity()
        else:self.slideshow_timer.stop();self.pointer_hide_timer.stop();self.canvas.unsetCursor()
        self.canvas.update()
    def hide_slideshow_cursor(self):
        if self.slideshow_active:self.canvas.setCursor(Qt.BlankCursor)
    def note_pointer_activity(self):
        if self.slideshow_active:self.canvas.unsetCursor();self.pointer_hide_timer.start()
    def handle_viewer_key(self,e):
        if e.key()==Qt.Key_Z and e.modifiers()&Qt.ControlModifier:
            parent=self.parent()
            if parent is not None and hasattr(parent,'undo_last_action'):parent.undo_last_action()
        elif e.key() in (Qt.Key_Escape,Qt.Key_Up):self.close()
        elif e.key()==Qt.Key_F11:
            self.showNormal() if self.isFullScreen() else self.showFullScreen()
        elif e.key()==Qt.Key_Left:self.step(-1)
        elif e.key()==Qt.Key_Right:self.step(1)
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
    def closeEvent(self,e):
        self.slideshow_timer.stop();self.pointer_hide_timer.stop()
        if self.movie:self.movie.stop()
        parent=self.parent()
        if parent is not None and hasattr(parent,'viewer_closed'):parent.viewer_closed()
        super().closeEvent(e)


class Explorer(QMainWindow):
    def __init__(self):
        super().__init__();self._closing=False;self.setWindowTitle('Magellan');icon_root=Path(sys._MEIPASS) if getattr(sys,'frozen',False) else Path(__file__).resolve().parent.parent;self.icon_root=icon_root;self.icon_dir=icon_root/'Alternative Icons';icon_path=icon_root/'MainIcon.ico';self.app_icon=QIcon(str(icon_path)) if icon_path.is_file() else make_boat_icon();self.setWindowIcon(self.app_icon);QApplication.setWindowIcon(self.app_icon);self.resize(1900,1000);self.window_cycle=0;self.in_favorites_view=False;self.setStyleSheet(QSS)
        self.hover_details_enabled=True;self.hover_details_delay=500;self.hover_details_placement='top';self.slideshow_interval_ms=5000;self.auto_hide_main=False;self.auto_hide_tabs=False;self.auto_hide_tree=False;self.main_hide_delay=900;self.tree_hide_delay=900;self.right_controls_auto_hide=True;self.right_controls_hide_delay=500;self.favorite_organize_mode=False;self.favorites_navigation_root=None;self.favorite_return_group=None;self.recursive_search_enabled=False;self.recursive_search_results=None;self.recursive_search_result_query='';self.recursive_search_generation=0;self.recursive_search_jobs=[]
        self.current=Path.home();self.items=[];self.sort='name';self.desc=False;self.columns=5;self.thumb=260;self.mode='vertical';self.thumbnail_cache_enabled=True;self.thumbnail_cache_limit_mib=256;self.favorites=set();self.favorite_groups=[];self.active_favorite_group=None;self.tree_favorites=set();self.pinned_folders=set();self.pinned_thumbs={};self.custom={};self.startup_folder=None;self.tabs=[];self.active_tab_index=-1;self.saved_tabs=[];self.saved_active_tab=0;self.scan_generation=0;self.scan_jobs=[];self.return_to_favorites=False;self.favorites_return_path=None;self.scroll_positions={};self._suppress_scroll_save=False;self.window_mode='fullscreen';self.last_deletion_undo=None;self.cache_dir=Path(QStandardPaths.writableLocation(QStandardPaths.AppDataLocation));self.cache_dir.mkdir(parents=True,exist_ok=True);self.settings_file=self.cache_dir/'settings.json';self.install_dir=Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent;self.persistent_thumb_dir=self.install_dir/'thumbnail_cache';self.persistent_thumb_dir.mkdir(parents=True,exist_ok=True);self.load_settings();self.back_target=Path(self.startup_folder) if self.startup_folder else Path(self.current);self.image_search_provider='saucenao';self.image_search_mode=False;self.scan=None;self.work=None;self.build();self.grid.set_cache_limit_mib(self.thumbnail_cache_limit_mib);self.grid.persistent_folder_cache_dir=self.persistent_thumb_dir;self.grid.folder_cache.update(self.pinned_thumbs)
        self.restore_tabs()
        for _pf in list(self.pinned_folders):
            _cached=self.grid.load_persistent_folder_thumb(_pf)
            if _cached:
                self.pinned_thumbs[_pf]=_cached;self.grid.folder_cache[_pf]=_cached
            elif Path(_pf).is_dir():
                _fut=self.grid.folder_pool.submit(self.grid.folder_image,_pf)
                def _warm_done(f,pp=_pf):
                    try:
                        _src=f.result()
                        if _src:
                            _cached2=self.grid.persist_folder_thumb(pp,_src)
                            if _cached2:
                                self.pinned_thumbs[pp]=_cached2;self.grid.folder_cache[pp]=_cached2;self.save_settings()
                    except Exception: pass
                _fut.add_done_callback(_warm_done)
        self.apply_window_state();self.apply_auto_hide()
    def load_settings(self):
        self.log_storage_mib=8
        configure_logging(self.cache_dir/'logs',self.log_storage_mib)
        try:
            d=self._sanitize_settings(json.loads(self.settings_file.read_text()));self.favorites=set(d.get('favorites',[]));self.favorite_groups=d.get('favorite_groups',[]);self.tree_favorites=set(d.get('tree_favorites',[]));self.custom=d.get('custom',{});self.pinned_folders=set(d.get('pinned_folders',[]));self.pinned_thumbs=d.get('pinned_thumbs',{});self.image_search_provider=d.get('image_search_provider','saucenao');self.saucenao_api_key=d.get('saucenao_api_key','');self.window_mode=d.get('window_mode','fullscreen');self.saved_geometry=d.get('geometry');self.hover_details_enabled=bool(d.get('hover_details_enabled',True));self.hover_details_delay=max(0,min(3000,int(d.get('hover_details_delay',500))));_placement=d.get('hover_details_placement','top');self.hover_details_placement='bottom' if _placement=='below' else (_placement if _placement in ('top','bottom','above') else 'top');self.thumbnail_cache_enabled=bool(d.get('thumbnail_cache_enabled',True));self.thumbnail_cache_limit_mib=max(16,min(8192,int(d.get('thumbnail_cache_limit_mib',256))));self.auto_hide_main=bool(d.get('auto_hide_main',False));self.auto_hide_tabs=bool(d.get('auto_hide_tabs',False));self.auto_hide_tree=bool(d.get('auto_hide_tree',False));self.main_hide_delay=max(0,min(10000,int(d.get('main_hide_delay',900))));self.tree_hide_delay=max(0,min(10000,int(d.get('tree_hide_delay',900))));self.has_saved_tabs='tabs' in d;self.saved_tabs=d.get('tabs',[]);self.saved_active_tab=max(0,int(d.get('active_tab',0)))
            self.slideshow_interval_ms=max(250,min(60000,int(d.get('slideshow_interval_ms',5000))))
            self.log_storage_mib=max(1,min(64,int(d.get('log_storage_mib',8))))
            self.recursive_search_enabled=bool(d.get('recursive_search_enabled',False))
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
        tabs=data.get('tabs',[]);clean_tabs=[]
        if isinstance(tabs,list):
            for tab in tabs:
                if not isinstance(tab,dict) or not isinstance(tab.get('path'),str) or not tab['path'].strip():continue
                tab['kind']=tab.get('kind') if tab.get('kind') in ('folder','farview') else 'folder'
                tab['sort']=tab.get('sort') if tab.get('sort') in ('name','mtime','ctime','type','size','random') else 'name'
                tab['desc']=bool(tab.get('desc',False));tab['unsorted']=bool(tab.get('unsorted',True))
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
                clean_tabs.append(tab)
        data['tabs']=clean_tabs
        for key in ('startup','last','geometry','saucenao_api_key'):
            if data.get(key) is not None and not isinstance(data.get(key),str):data[key]=None if key!='saucenao_api_key' else ''
        for key,default in {'active_tab':0,'hover_details_delay':500,'thumbnail_cache_limit_mib':256,'main_hide_delay':900,'tree_hide_delay':900,'slideshow_interval_ms':5000,'log_storage_mib':8,'right_controls_hide_delay':500}.items():
            try:data[key]=int(data.get(key,default))
            except (TypeError,ValueError,OverflowError):data[key]=default
        for key,default in {'hover_details_enabled':True,'thumbnail_cache_enabled':True,'auto_hide_main':False,'auto_hide_tabs':False,'auto_hide_tree':False,'right_controls_auto_hide':True,'recursive_search_enabled':False}.items():
            data[key]=data.get(key,default) if isinstance(data.get(key,default),bool) else default
        if data.get('window_mode') not in ('fullscreen','maximized','normal'):data['window_mode']='fullscreen'
        return data
    def save_settings(self):
        geometry=None
        try:
            geometry=bytes(self.saveGeometry().toBase64()).decode('ascii')
        except: pass
        tabs=[{k:v for k,v in tab.items() if k in ('path','kind','scroll','positions','sort','desc','unsorted','selection')} for tab in getattr(self,'tabs',[])]
        payload=json.dumps({'favorites':list(self.favorites),'favorite_groups':self.favorite_groups,'tree_favorites':list(self.tree_favorites),'pinned_folders':list(self.pinned_folders),'pinned_thumbs':self.pinned_thumbs,'custom':self.custom,'image_search_provider':self.image_search_provider,'saucenao_api_key':getattr(self,'saucenao_api_key',''),'hover_details_enabled':self.hover_details_enabled,'hover_details_delay':self.hover_details_delay,'hover_details_placement':self.hover_details_placement,'thumbnail_cache_enabled':self.thumbnail_cache_enabled,'thumbnail_cache_limit_mib':self.thumbnail_cache_limit_mib,'auto_hide_main':getattr(self,'auto_hide_main',False),'auto_hide_tabs':getattr(self,'auto_hide_tabs',False),'auto_hide_tree':getattr(self,'auto_hide_tree',False),'main_hide_delay':getattr(self,'main_hide_delay',900),'tree_hide_delay':getattr(self,'tree_hide_delay',900),'last':str(self.current),'startup':self.startup_folder,'window_mode':self.window_mode,'geometry':geometry,'tabs':tabs,'active_tab':max(0,self.current_tab_index()) if hasattr(self,'tabbar') else 0})
        payload=json.dumps(json.loads(payload)|{'slideshow_interval_ms':self.slideshow_interval_ms,'right_controls_auto_hide':self.right_controls_auto_hide,'right_controls_hide_delay':self.right_controls_hide_delay,'log_storage_mib':self.log_storage_mib,'recursive_search_enabled':self.recursive_search_enabled})
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
        cursor=QCursor.pos();frame=self.frameGeometry();client=self.geometry()
        in_titlebar=frame.contains(cursor) and cursor.y()<client.top() and not client.contains(cursor)
        if in_titlebar:
            self.main_hide_timer.stop()
            if not self.main_bar.isVisible():self.set_main_panel_visible(True)
        elif self.main_bar.isVisible() and not self.root_widget.rect().contains(self.root_widget.mapFromGlobal(cursor)) and QApplication.focusWidget() is not self.search:
            if not self.main_hide_timer.isActive():self.main_hide_timer.start(self.main_hide_delay)
    def hide_tree_overlay(self):
        if self.auto_hide_tree and hasattr(self,'tree_dock'):self.tree_dock.hide()
    def hide_main_panel(self):
        if getattr(self,'tab_reveal_timer',None) and self.tab_reveal_timer.isActive():return
        if hasattr(self,'menu_bar'):
            for action in self.menu_bar.actions():
                menu=action.menu()
                if menu and menu.isVisible():menu.close()
        if self.auto_hide_main and hasattr(self,'main_bar') and QApplication.focusWidget() is not self.search:self.set_main_panel_visible(False)
    def set_main_panel_visible(self,visible):
        if not hasattr(self,'main_bar'):return
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
        if event.type()==QEvent.MouseMove and not viewer_active and hasattr(self,'grid') and self.grid.navigation_index>=0:
            self.grid.navigation_index=-1;self.grid.viewport().update()
        if event.type()==QEvent.KeyPress and QApplication.activeWindow() is self and not viewer_active:
            focus=QApplication.focusWidget();editing=isinstance(focus,(QLineEdit,QSpinBox))
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
            if not editing and Qt.Key_1<=event.key()<=Qt.Key_6 and not event.modifiers()&(Qt.ControlModifier|Qt.AltModifier|Qt.ShiftModifier):self.set_sort(('name','mtime','ctime','type','size','random')[event.key()-Qt.Key_1]);event.accept();return True
        if event.type()==QEvent.KeyPress and event.key()==Qt.Key_Shift:
            self.shift_selection_held=True
            if self.auto_hide_main:self.main_hide_timer.stop();self.hide_main_panel()
            if self.auto_hide_tree:self.tree_hide_timer.stop();self.hide_tree_overlay()
            if hasattr(self,'favorite_group_overlay'):self.hide_favorite_controls()
        elif event.type()==QEvent.KeyRelease and event.key()==Qt.Key_Shift:self.shift_selection_held=False
        if viewer_active:
            if self.auto_hide_main:self.set_main_panel_visible(False)
            if self.auto_hide_tree and hasattr(self,'tree_dock'):self.tree_dock.hide()
            if hasattr(self,'favorite_group_overlay'):self.hide_favorite_controls()
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
                if pos.y()<=2:
                    self.main_hide_timer.stop();self.set_main_panel_visible(True)
                elif over_tabs or main_menu_hover:self.main_hide_timer.stop()
                elif self.main_bar.isVisible() and pos.y()>self.main_bar.height():self.main_hide_timer.start(self.main_hide_delay)
            if self.auto_hide_tree and hasattr(self,'tree_dock'):
                if 0<=pos.x()<=10:
                    self.tree_hide_timer.stop();self.tree_dock.setGeometry(self.root_widget.mapToGlobal(QPoint(0,0)).x(),self.root_widget.mapToGlobal(QPoint(0,0)).y(),300,self.root_widget.height());self.tree_dock.show();self.tree_dock.raise_()
                elif self.tree_dock.isVisible() and not QRect(0,0,300,self.root_widget.height()).contains(pos):self.tree_hide_timer.start(self.tree_hide_delay)
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
        except: pass
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
        if hasattr(self,'recursive_search_timer'):self.recursive_search_timer.stop()
        for job in list(getattr(self,'recursive_search_jobs',[])):
            if job.isRunning():job.requestInterruption()
        for job in list(getattr(self,'recursive_search_jobs',[])):
            if job.isRunning():job.wait(250)
        if hasattr(self,'_tree_generation'):self._tree_generation+=1
        TREE_SCAN_POOL.shutdown(wait=False,cancel_futures=True)
        menu=getattr(self,'favorite_picker_menu',None)
        if menu is not None:menu.close()
        viewer=getattr(self,'viewer',None)
        if viewer is not None and viewer.isVisible():viewer.close()
        if hasattr(self,'grid'):self.grid.shutdown_workers()
        for job in list(getattr(self,'scan_jobs',[])):
            if job.isRunning():job.requestInterruption()
        active_scan=getattr(self,'scan',None)
        if active_scan is not None and active_scan.isRunning():active_scan.wait(750)
        VIEWER_IMAGE_POOL.shutdown(wait=False,cancel_futures=True)
        if self.isFullScreen(): self.window_mode='fullscreen'
        elif self.isMaximized(): self.window_mode='maximized'
        else: self.window_mode='normal'
        self.save_current_view();self.save_settings();e.accept()
        app=QApplication.instance()
        if app is not None:QTimer.singleShot(0,app.quit)
    def resizeEvent(self,e):
        super().resizeEvent(e)
        if self.auto_hide_main and hasattr(self,'root_widget') and hasattr(self,'main_bar'):
            self.set_main_panel_visible(self.main_bar.isVisible())
    def build(self):
        root=QWidget();root.setObjectName('root');self.setCentralWidget(root);main=QVBoxLayout(root);main.setContentsMargins(0,0,0,0);main.setSpacing(0)
        mb=QMenuBar();mb.setMouseTracking(True);mb.hovered.connect(self.open_menu_on_hover);self.menu_bar=mb
        for title in ('FILES','VIEW','PERFORMANCE','TOOLS'):
            m=mb.addMenu(title)
            if title=='FILES':m.addAction('Open folder…',self.choose);m.addAction('Refresh',lambda:self.load(self.current,restore_scroll=True));m.addSeparator();m.addAction('Exit',self.close)
            if title=='VIEW':
                m.addAction('Vertical grid',lambda checked=False:self.set_mode('vertical'));m.addAction('Horizontal rows',lambda checked=False:self.set_mode('horizontal'));m.addSeparator()
                m.addAction('Thumbnail hover settings…',self.configure_hover_details);m.addAction('Slideshow timer…',self.configure_slideshow);m.addAction('Auto-hide panels…',self.configure_auto_hide);m.addAction('Right-side buttons…',self.configure_right_controls)
            if title=='PERFORMANCE':
                recursive_action=m.addAction('Recursive search in subfolders');recursive_action.setCheckable(True);recursive_action.setChecked(self.recursive_search_enabled);recursive_action.toggled.connect(self.set_recursive_search_enabled)
                m.addSeparator()
                cache_action=m.addAction('Cache visible thumbnails in memory');cache_action.setCheckable(True);cache_action.setChecked(self.thumbnail_cache_enabled);cache_action.toggled.connect(self.set_thumbnail_cache_enabled)
                self.thumbnail_cache_limit_action=m.addAction(f'Memory thumbnail cache limit… ({self.thumbnail_cache_limit_mib} MiB)');self.thumbnail_cache_limit_action.triggered.connect(self.configure_thumbnail_cache_limit)
                self.log_storage_action=m.addAction(f'Maximum log storage… ({self.log_storage_mib} MiB)');self.log_storage_action.triggered.connect(self.configure_log_storage_limit)
                m.addAction('Keep folder thumbnails…',self.manage_pinned_folders)
            if title=='TOOLS':m.addAction('Set custom folder thumbnail…',self.set_custom);m.addSeparator();m.addAction('Set startup folder…',self.set_startup);m.addAction('Use last folder on startup',self.clear_startup);m.addSeparator();m.addAction('Reverse image search…',self.choose_search_provider);m.addAction('Set SauceNAO API key…',self.set_saucenao_api_key)
        top=QFrame();top.setObjectName('top');top_layout=QVBoxLayout(top);top_layout.setContentsMargins(0,0,0,0);top_layout.setSpacing(0);top_layout.addWidget(mb);controls=QWidget(top);tl=QHBoxLayout(controls);tl.setContentsMargins(12,5,12,7);tl.setSpacing(7);top_layout.addWidget(controls)
        for s,t,f in [('☰','File tree',self.toggle_file_tree),('⌂','Back',self.back),('↑','Up',self.up),('⟳','Refresh',lambda:self.load(self.current))]:
            button=self.addbtn(tl,s,t,f)
            if t=='Back':self.backbtn=button;self.set_button_asset(button,'back.png')
        self.addbtn(tl,'⌕','Image search',self.toggle_image_search)
        self.favorites_list_btn=self.addbtn(tl,'☆','Show favorite folders',self.show_favorites)
        self.farviewbtn=self.addbtn(tl,'⇢','Create a Farview tab from this folder and its image subfolders',self.create_farview)
        self.set_button_asset(self.farviewbtn,'eye.png')
        self.recursive_search_timer=QTimer(self);self.recursive_search_timer.setSingleShot(True);self.recursive_search_timer.setInterval(250);self.recursive_search_timer.timeout.connect(self.start_recursive_search)
        self.search=QLineEdit();self.search.setObjectName('search');self.search.setPlaceholderText('Search...');self.search.setClearButtonEnabled(False);self.search.textChanged.connect(self.search_text_changed);self.search.setMinimumWidth(260);tl.addWidget(self.search)
        self.search_clear_btn=self.addbtn(tl,'×','Clear search',self.search.clear,'searchclear');self.search_clear_btn.setFixedSize(30,38)
        tl.addStretch(1)
        sb=QHBoxLayout();lab=QLabel('Sorting');lab.setObjectName('section');lab.setAlignment(Qt.AlignVCenter|Qt.AlignRight);sb.addWidget(lab);sr=QHBoxLayout()
        self.sortBtns={}
        for k,s,t in [('name','☷','Name'),('mtime','◩','Last modified'),('ctime','◷','Last created'),('type','◇','Type'),('size','◴','Size'),('random','⤨','Random folders')]:
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
        self.tabbar=SafeTabBar();self.tabbar.setExpanding(False);self.tabbar.setMovable(False);self.tabbar.setTabsClosable(False);self.tabbar.closeRequested.connect(self.close_tab);self.tabbar.setDocumentMode(True);self.tabbar.setElideMode(Qt.ElideMiddle);self.tabbar.currentChanged.connect(self.switch_tab);self.tabbar.setMinimumWidth(300);nl.addWidget(self.tabbar,1)
        self.status=MarqueeLabel();self.status.setObjectName('status');self.status.setFixedWidth(190);self.status.setAlignment(Qt.AlignRight|Qt.AlignVCenter);nl.addWidget(self.status)
        main.addWidget(nav)
        self.grid=VirtualGrid(self);self.grid.openItem.connect(self.open);self.grid.toggleItemFavorite.connect(self.toggle_item_favorite)
        self.grid.imageSearchRequested.connect(self.perform_image_search);self.grid.middleOpenItem.connect(self.open_new_tab);self.grid.goBack.connect(self.up);self.grid.verticalScrollBar().valueChanged.connect(self.remember_scroll);main.addWidget(self.grid,1)
        self.root_widget=root;root.setMouseTracking(True);QApplication.instance().installEventFilter(self);self.main_hide_timer=QTimer(self);self.main_hide_timer.setSingleShot(True);self.main_hide_timer.timeout.connect(self.hide_main_panel);self.tab_reveal_timer=QTimer(self);self.tab_reveal_timer.setSingleShot(True);self.tab_reveal_timer.setInterval(2000);self.tab_reveal_timer.timeout.connect(self.finish_tab_reveal);self.titlebar_hover_timer=QTimer(self);self.titlebar_hover_timer.setInterval(60);self.titlebar_hover_timer.timeout.connect(self.check_titlebar_hover);self.tree_hide_timer=QTimer(self);self.tree_hide_timer.setSingleShot(True);self.tree_hide_timer.timeout.connect(self.hide_tree_overlay)
        self.favorite_overlay_timer=QTimer(self);self.favorite_overlay_timer.setSingleShot(True);self.favorite_overlay_timer.setInterval(500);self.favorite_overlay_timer.timeout.connect(self.hide_favorite_controls)
        self.favorite_picker_close_timer=QTimer(self);self.favorite_picker_close_timer.setInterval(20);self.favorite_picker_close_timer.timeout.connect(self.check_favorite_picker_dismiss);self.favorite_picker_leave_since=None
        self.favorite_group_overlay=QToolButton(root);self.favorite_group_overlay.setText('＋');self.favorite_group_overlay.setToolTip('Create a favorites group');self.favorite_group_overlay.setFixedSize(58,36);self.favorite_group_overlay.clicked.connect(self.create_favorite_group);self.favorite_group_overlay.hide()
        self.favorite_organize_overlay=QToolButton(root);self.favorite_organize_overlay.setText('↔');self.favorite_organize_overlay.setToolTip('Enable favorites organization mode');self.favorite_organize_overlay.setCheckable(True);self.favorite_organize_overlay.setFixedSize(58,36);self.favorite_organize_overlay.toggled.connect(self.set_favorite_organization_mode);self.favorite_organize_overlay.hide()
        self.selection_clear_overlay=QToolButton(root);self.selection_clear_overlay.setText('×');self.selection_clear_overlay.setToolTip('Clear selection');self.selection_clear_overlay.setFixedSize(58,36);self.selection_clear_overlay.clicked.connect(lambda:self.grid.clear_selection());self.selection_clear_overlay.hide()
        self.selection_all_overlay=QToolButton(root);self.selection_all_overlay.setText('☑');self.selection_all_overlay.setToolTip('Select all items on this page');self.selection_all_overlay.setFixedSize(58,36);self.selection_all_overlay.clicked.connect(self.grid.select_all_current);self.selection_all_overlay.hide()
        self.selection_undo_overlay=QToolButton(root);self.selection_undo_overlay.setText('↶');self.selection_undo_overlay.setToolTip('Undo last deselection (Ctrl+Z)');self.selection_undo_overlay.setFixedSize(58,36);self.selection_undo_overlay.clicked.connect(self.grid.undo_deselection);self.selection_undo_overlay.hide()
        self.set_button_asset(self.favorite_group_overlay,'createfavoritegroup.png');self.set_button_asset(self.favorite_organize_overlay,'organizationmode.png');self.set_button_asset(self.selection_clear_overlay,'clearselect.png');self.set_button_asset(self.selection_undo_overlay,'undo.png')
        self.grid.verticalScrollBar().rangeChanged.connect(lambda *_:self.position_right_controls())
        self.update_selection_button()
    def toggle_selection(self):
        self.grid.clear_selection()
        self.grid.setFocus()
    def update_selection_button(self):
        if not hasattr(self,'selection_clear_overlay'):return
        count=len(getattr(self.grid,'selected_paths',())) if hasattr(self,'grid') else 0
        self.selection_clear_overlay.setToolTip(f'Clear selection ({count} selected)' if count else 'Clear selection')
        if hasattr(self,'farviewbtn'):self.farviewbtn.setToolTip('Create a Farview tab from the selected files and folders' if count else f'Create a Farview tab from {self.current} and its image subfolders')
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
        widgets=[self.favorite_group_overlay,self.favorite_organize_overlay] if self.in_favorites_view else []
        widgets += [self.selection_clear_overlay,self.selection_all_overlay,self.selection_undo_overlay]
        for row,widget in enumerate(widgets):widget.setGeometry(x,y+row*42,button_width,36)
    def show_right_controls(self):
        self.position_right_controls()
        if self.in_favorites_view:self.favorite_group_overlay.show();self.favorite_organize_overlay.show()
        else:self.favorite_group_overlay.hide();self.favorite_organize_overlay.hide()
        self.selection_clear_overlay.show();self.selection_all_overlay.show();self.selection_undo_overlay.show()
        for widget in self.right_control_widgets():widget.raise_()
    def toggle_search_navigation_focus(self):
        if QApplication.focusWidget() is self.search and (not self.auto_hide_main or self.main_bar.isVisible()):
            self.grid.setFocus()
            if self.grid.navigation_index<0 and self.grid.items:self.grid.navigation_index=min(self.grid.visible_range()[0],len(self.grid.items)-1)
            self.grid.ensure_navigation_visible(self.grid.navigation_index)
            return
        if self.auto_hide_main:
            self.main_hide_timer.stop();self.set_main_panel_visible(True);self.main_hide_timer.start(self.main_hide_delay)
        else:self.set_main_panel_visible(True)
        self.search.setFocus(Qt.ShortcutFocusReason);self.search.selectAll()
    def viewer_closed(self):
        if self._closing:return
        if not self.auto_hide_tree and getattr(self,'tree_was_visible_before_viewer',False) and hasattr(self,'tree_dock'):self.tree_dock.show();self.refresh_file_tree()
        self.tree_was_visible_before_viewer=False
    def set_favorite_organization_mode(self,enabled):
        self.favorite_organize_mode=bool(enabled);self.favorite_organize_overlay.setToolTip('Disable favorites organization mode' if enabled else 'Enable favorites organization mode')
        if self.favorite_organize_overlay.isChecked()!=self.favorite_organize_mode:self.favorite_organize_overlay.setChecked(self.favorite_organize_mode)
    def configure_thumbnail_cache_limit(self):
        dlg=QDialog(self);dlg.setWindowTitle('Thumbnail memory cache');layout=QVBoxLayout(dlg)
        row=QHBoxLayout();row.addWidget(QLabel('Maximum thumbnail memory'));limit=QSpinBox();limit.setRange(16,8192);limit.setSingleStep(256);limit.setSuffix(' MiB');limit.setValue(self.thumbnail_cache_limit_mib);row.addWidget(limit);layout.addLayout(row)
        note=QLabel('The cache grows only as thumbnails are used. Lowering the limit immediately removes the least recently used thumbnails. The SSD cache for pinned folder previews is separate.');note.setWordWrap(True);layout.addWidget(note)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel);buttons.accepted.connect(dlg.accept);buttons.rejected.connect(dlg.reject);layout.addWidget(buttons)
        if dlg.exec()==QDialog.Accepted:
            self.thumbnail_cache_limit_mib=limit.value();self.grid.set_cache_limit_mib(self.thumbnail_cache_limit_mib);self.thumbnail_cache_limit_action.setText(f'Memory thumbnail cache limit… ({self.thumbnail_cache_limit_mib} MiB)');self.save_settings()
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
            self.grid.cache.clear();self.grid.cache_bytes=0;self.grid.viewport().update()
        self.save_settings()
    def toggle_file_tree(self):
        active=QApplication.activeWindow()
        if active is not None and active is not self:return
        if not hasattr(self,'tree_dock'):
            self.build_file_tree()
        self.tree_dock.setVisible(not self.tree_dock.isVisible())
        if self.tree_dock.isVisible(): self.refresh_file_tree()

    def build_file_tree(self):
        self.tree_dock=QDockWidget('File Tree',self)
        self.tree_dock.setObjectName('fileTreeDock')
        self.tree_dock.setAllowedAreas(Qt.LeftDockWidgetArea)
        self.tree_dock.setFeatures(QDockWidget.DockWidgetClosable)
        self.file_tree=QTreeWidget()
        self._tree_generation=0;self._tree_scan_active=set();self._tree_bridge=TreeScanBridge(self);self._tree_bridge.completed.connect(self.finish_tree_scan)
        self.file_tree.setHeaderHidden(True)
        self.file_tree.setIndentation(16)
        self.file_tree.setAnimated(True)
        self.file_tree.itemExpanded.connect(self.expand_tree_item)
        self.file_tree.itemClicked.connect(self.file_tree_clicked)
        self.file_tree.itemDoubleClicked.connect(self.file_tree_double_clicked)
        self.file_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.file_tree.customContextMenuRequested.connect(self.file_tree_context_menu)
        self.tree_dock.setWidget(self.file_tree)
        self.addDockWidget(Qt.LeftDockWidgetArea,self.tree_dock)
        self.tree_dock.visibilityChanged.connect(lambda visible: self.refresh_file_tree() if visible else None)

    def _tree_path(self,item):
        raw=item.data(0,TREE_PATH_ROLE)
        if not raw or str(raw).startswith('__'): return None
        return Path(str(raw))

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
        self._tree_generation+=1
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
        if path and path.is_dir(): self.load(path)

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
        data.setdefault('scroll',self.scroll_positions.get(str(folder),0));data.setdefault('positions',{str(folder):self.scroll_positions.get(str(folder),0)})
        data.setdefault('sort',self.sort);data.setdefault('desc',self.desc)
        self.tabs.append(data)
        prefix=('Farview selection · ' if data['kind']=='farview' and data.get('selection') else ('Farview · ' if data['kind']=='farview' else ''))
        idx=self.tabbar.addTab(prefix+(folder.name or str(folder)))
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
            positions=self.tabs[idx].setdefault('positions',{})
            if restore_scroll:
                target_scroll=positions.get(str(folder),self.scroll_positions.get(str(folder),0))
                self.tabs[idx]['scroll']=int(target_scroll)
            else:
                self.scroll_positions[str(folder)]=0
                positions[str(folder)]=0
                self.tabs[idx]['scroll']=0
            self.update_tab_labels()
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
        self.update_favorite_button()
        if 0<=idx<len(self.tabs):
            self.tabs[idx]['kind']='farview';self.tabs[idx]['path']=str(root);self.update_tab_labels()
        self.update_sort_buttons()
        self.start_scan(root,recursive=True,selection=selection)

    def start_scan(self,folder,recursive=False,selection=None):
        if self.scan and self.scan.isRunning():self.scan.requestInterruption()
        self.scan_generation+=1;generation=self.scan_generation
        self.scan=ScanTask(folder,recursive=recursive,generation=generation,selection=selection)
        self.scan_jobs.append(self.scan)
        self.scan.finished.connect(lambda job=self.scan:self.release_scan_job(job))
        self.scan.signals.done.connect(self.scanned)
        self.scan.start()

    def release_scan_job(self,job):
        try:self.scan_jobs.remove(job)
        except ValueError:pass

    def scanned(self,items,error,generation=None):
        if generation is not None and generation!=self.scan_generation:return
        if error:self.status.setText('Unable to read folder');QMessageBox.warning(self,'Magellan',error);return
        self.items=items;self.refresh();self.save_settings()
        idx=self.current_tab_index()
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
        if k!='size':
            for key,future in list(self.grid.folder_size_futures.items()):
                if not future.running():future.cancel();self.grid.folder_size_futures.pop(key,None)
        preserve_folder_order=farview and self.tabs[idx].get('unsorted',True)
        if k=='random':
            self.sort='random';self.desc=False
        elif self.sort==k and not preserve_folder_order:self.desc=not self.desc
        else:self.sort=k;self.desc=False
        if 0<=idx<len(self.tabs):self.tabs[idx].update({'sort':self.sort,'desc':self.desc})
        if farview:self.tabs[idx]['unsorted']=False
        self.update_sort_buttons();self.refresh()
        self.save_settings()

    def update_sort_buttons(self):
        labels={'name':('↥','Name'),'mtime':('↧','Last modified'),'ctime':('◷','Last created'),'type':('◇','Type'),'size':('⇵','Size'),'random':('⤨','Random folders')}
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
            if active:
                painter.setPen(QColor('#f5f5f5'));painter.setFont(QFont('Segoe UI Symbol',12,QFont.Bold));painter.drawText(QRect(23,0,22,24),Qt.AlignCenter,'▼' if self.desc else '▲')
            painter.end();b.setIcon(QIcon(composed));b.setText('');b.setIconSize(QSize(46,24));b.setToolTip(f'{title} — {"Descending" if self.desc else "Ascending"}' if active else title);b.setChecked(active)

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
        self.refresh()
        if self.recursive_search_enabled and text.strip() and not self.in_favorites_view:
            self.status.setText('Searching subfolders…');self.recursive_search_timer.start()
    def start_recursive_search(self):
        query=self.search.text().casefold().strip()
        if not self.recursive_search_enabled or not query or self.in_favorites_view:return
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
            self.status.setText('Recursive search failed');QMessageBox.warning(self,'Search failed',error);return
        self.recursive_search_results=items;self.recursive_search_result_query=self.search.text().casefold().strip();self.refresh()
        if limited:self.status.setText(f'{len(items)} results — refine search (10,000 result limit)')
        else:self.status.setText(f'{len(items)} recursive search results')
    def refresh(self):
        q=self.search.text().casefold().strip()
        source=self.recursive_search_results if self.recursive_search_enabled and q and self.recursive_search_result_query==q and self.recursive_search_results is not None else self.items
        items=[x for x in source if not q or q in x['name'].casefold()]
        recursive_results=self.recursive_search_enabled and q and self.recursive_search_result_query==q and self.recursive_search_results is not None
        if self.sort=='size' and not recursive_results:
            for item in items:
                if item.get('is_dir'):self.grid.request_folder_size(item)
        def name_key(x):return natural_key(x['name'])
        def type_key(x):
            type_names={'folder':'File folder','image':'Image','video':'Video','audio':'Audio','document':'Document','file':'File'}
            return (natural_key(type_names.get(x['kind'],x['kind'])),name_key(x))
        active_idx=self.current_tab_index();is_farview=active_idx>=0 and self.tabs[active_idx].get('kind')=='farview'
        if is_farview and self.tabs[active_idx].get('unsorted',True):
            pass
        elif self.sort=='random':
            if is_farview:
                random.shuffle(items)
            else:
                folders=[x for x in items if x['is_dir']]
                files=[x for x in items if not x['is_dir']]
                random.shuffle(folders)
                files.sort(key=name_key)
                items=folders+files
        else:
            if self.sort=='name':key=name_key
            elif self.sort=='type':key=type_key
            elif self.sort in ('mtime','ctime'):key=lambda x:x[self.sort]
            elif self.sort=='size':key=lambda x:-1 if x.get('size') is None else int(x['size'])
            else:key=name_key
            items.sort(key=lambda x:0 if x['is_dir'] else 1);items.sort(key=key,reverse=self.desc);items.sort(key=lambda x:0 if x['is_dir'] else 1)
        self.grid.setFolderThumbs(self.custom);self.grid.setItems(items,reset_scroll=False)
        suffix='' if self.sort=='random' else (' • '+('Descending' if self.desc else 'Ascending'))
        if is_farview and self.tabs[active_idx].get('unsorted',True):
            self.status.setText(f'{len(items)} images • Farview • folder order')
        else:
            self.status.setText(f'{len(items)} items • {self.sort}'+suffix)

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
        if self.main_bar.geometry().contains(pos) or self.nav_bar.geometry().contains(pos) or QApplication.focusWidget() is self.search:return
        self.set_main_panel_visible(False)

    def create_farview(self):
        root=Path(self.current)
        if not root.is_dir():return
        selected=[path for path in self.grid.selected_paths if Path(path).is_dir() or (Path(path).is_file() and Path(path).suffix.casefold() in PREVIEW_EXTS)]
        if selected:
            selected.sort(key=natural_key);selection_key=tuple(self._normalized_path_key(p) for p in selected)
            for idx,tab in enumerate(self.tabs):
                if tab.get('kind')=='farview' and tuple(self._normalized_path_key(p) for p in tab.get('selection',[]))==selection_key:
                    self.tabbar.setCurrentIndex(idx);return
            self.save_current_view();self.add_tab(root,select=True,tab={'kind':'farview','selection':selected,'sort':self.sort,'desc':self.desc,'unsorted':True,'scroll':0,'positions':{}});self.save_settings();self.reveal_panel_for_new_tab();return
        for idx,tab in enumerate(self.tabs):
            if tab.get('kind')=='farview' and Path(tab['path'])==root and not tab.get('selection'):
                self.tabbar.setCurrentIndex(idx);return
        self.save_current_view()
        self.add_tab(root,select=True,tab={'kind':'farview','sort':self.sort,'desc':self.desc,'unsorted':True,'scroll':0,'positions':{}})
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
    def handle_favorite_drop(self,payload,target_index):
        if not self.in_favorites_view or not (0<=target_index<len(self.grid.items)):return
        target=self.grid.items[target_index]
        if target.get('kind')!='favorite_group':return
        target_id=str(target['path']).split('://',1)[1]
        if payload.startswith('folder://'):
            self.add_folder_to_favorite_group(payload[len('folder://'):],target_id)
        elif payload.startswith('favgroup://'):
            source_id=payload.split('://',1)[1]
            if source_id==target_id:return
            source=next((g for g in self.favorite_groups if g.get('id')==source_id),None);dest_i=next((i for i,g in enumerate(self.favorite_groups) if g.get('id')==target_id),-1)
            if source and dest_i>=0:
                self.favorite_groups.remove(source);self.favorite_groups.insert(dest_i,source);self.save_settings();self.show_favorites()
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
            if self.tree_was_visible_before_viewer:self.tree_dock.hide()
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
        except Exception: pass
        return None

    def reveal(self,p):
        try:
            if sys.platform.startswith('win'):os.startfile(str(p))
            elif sys.platform=='darwin':subprocess.Popen(['open',str(p)])
            else:subprocess.Popen(['xdg-open',str(p)])
        except Exception as e:
            logger.exception('Could not open %s',p)
            QMessageBox.warning(self,'Open failed',str(e))

    def reveal_in_folder(self,path):
        try:
            target=str(Path(path).resolve())
            if sys.platform.startswith('win'):
                explorer=os.path.join(os.environ.get('WINDIR',r'C:\Windows'),'explorer.exe')
                subprocess.Popen(f'explorer.exe /select,"{target}"',executable=explorer,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            elif sys.platform=='darwin':subprocess.Popen(['open','-R',target])
            else:subprocess.Popen(['xdg-open',str(Path(target).parent)])
        except Exception as e:
            logger.exception('Could not reveal %s',path)
            QMessageBox.warning(self,'Reveal failed',str(e))

    def send_paths_to_trash(self,paths):
        unique=[]
        for raw in paths:
            path=str(raw)
            if path and not path.startswith('favgroup://') and path not in unique and Path(path).exists():unique.append(path)
        if not unique:return False
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
            self.favorites_list_btn.setText('☆');self.favorites_list_btn.setToolTip('Show favorite folders')
        if hasattr(self,'farviewbtn'):
            self.farviewbtn.setToolTip('Create a Farview tab from the selected files and folders' if getattr(getattr(self,'grid',None),'selected_paths',set()) else f'Create a Farview tab from {self.current} and its image subfolders')

    def choose(self):
        f=QFileDialog.getExistingDirectory(self,'Choose media folder',str(self.current))
        if f:self.return_to_favorites=False;self.favorites_navigation_root=None;self.favorite_return_group=None;self.load(Path(f))
    def _pin_folder(self, path):
        key=str(Path(path).resolve())
        root=Path(key)
        if not root.is_dir(): return False
        self.pinned_folders.add(key)
        self.save_settings()
        # Cache the root and ONLY its immediate child folders. Deeper folders
        # are not added to the persistent cache unless separately pinned.
        targets=[key]
        try:
            targets.extend(str(e.path) for e in os.scandir(root) if e.is_dir() and (not e.name.startswith('.') or e.name.startswith('. ')))
        except Exception:
            pass
        for target in targets:
            cached=self.grid.load_persistent_folder_thumb(target)
            if cached:
                self.pinned_thumbs[target]=cached
                self.grid.folder_cache[target]=cached
                continue
            self.pinned_thumbs.setdefault(target,None)
            if target in self.grid.folder_futures or target in self.grid.folder_cache:
                continue
            fut=self.grid.folder_pool.submit(self.grid.folder_image,target)
            def done(f,pp=target):
                try:
                    val=f.result()
                    if val:
                        cached=self.grid.persist_folder_thumb(pp,val)
                        if cached:
                            self.pinned_thumbs[pp]=cached
                            self.grid.folder_cache[pp]=cached
                            self.save_settings()
                except Exception: pass
            fut.add_done_callback(done)
        return True

    def manage_pinned_folders(self):
        dlg=QDialog(self); dlg.setWindowTitle('Keep folder thumbnails'); dlg.resize(700,430); lay=QVBoxLayout(dlg)
        info=QLabel("Add folders whose thumbnails should be kept permanently in Magellan's local thumbnail cache."); info.setWordWrap(True); lay.addWidget(info)
        path_row=QHBoxLayout(); path_edit=QLineEdit(); path_edit.setPlaceholderText(r'Enter a full folder path, e.g. H:\Pictures'); browse=QToolButton(); browse.setText('Browse…'); add_path=QToolButton(); add_path.setText('Add path'); path_row.addWidget(path_edit,1); path_row.addWidget(browse); path_row.addWidget(add_path); lay.addLayout(path_row)
        lay.addWidget(QLabel('Pinned folders')); current=QListWidget(); current.setToolTip("These folders keep a local thumbnail copy inside Magellan's installation folder."); lay.addWidget(current,1)
        def populate():
            current.clear()
            for x in sorted(self.pinned_folders,key=natural_key): current.addItem(x)
        def add_path_value():
            raw=path_edit.text().strip().strip('"')
            if not raw: return
            if not self._pin_folder(raw): QMessageBox.warning(dlg,'Folder not found',f'Folder does not exist:\n{raw}'); return
            populate(); path_edit.clear()
        def browse_path():
            f=QFileDialog.getExistingDirectory(dlg,'Choose folder to keep',str(self.current))
            if f: path_edit.setText(f); add_path_value()
        def remove():
            it=current.currentItem()
            if not it: return
            key=it.text(); self.pinned_folders.discard(key)
            # Remove the root cache and the caches for its immediate child folders.
            targets=[key]
            try:
                root=Path(key)
                targets.extend(str(e.path) for e in os.scandir(root) if e.is_dir() and (not e.name.startswith('.') or e.name.startswith('. ')))
            except Exception: pass
            for target in targets:
                self.pinned_thumbs.pop(target,None)
                cp=self.grid._persistent_folder_thumb_path(target)
                if cp:
                    try: cp.unlink(missing_ok=True)
                    except Exception: pass
            self.save_settings(); populate()
        add_path.clicked.connect(add_path_value); browse.clicked.connect(browse_path)
        row=QHBoxLayout(); addb=QToolButton(); addb.setText('Add path'); addb.clicked.connect(add_path_value); remb=QToolButton(); remb.setText('Remove selected'); remb.clicked.connect(remove); row.addWidget(addb); row.addWidget(remb); row.addStretch(); lay.addLayout(row)
        close=QDialogButtonBox(QDialogButtonBox.Close); close.rejected.connect(dlg.reject); close.accepted.connect(dlg.accept); lay.addWidget(close); populate(); dlg.exec()

    def choose_search_provider(self):
        items=['SauceNAO','Google Images / Lens']
        cur=0 if self.image_search_provider=='saucenao' else 1
        val,ok=QInputDialog.getItem(self,'Reverse image search','Provider:',items,cur,False)
        if ok:
            self.image_search_provider='saucenao' if val=='SauceNAO' else 'google'
            self.save_settings();self.status.setText(f'Image search: {val}')

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
        try:
            upload_path=Path(path)
            if upload_path.suffix.lower() in VIDEO_EXTS:
                img=self.grid.video_thumbnail_worker(str(upload_path),1200,1200)
                if img is None or img.isNull(): raise RuntimeError('Could not extract a frame from the video.')
                fd,tmp=tempfile.mkstemp(suffix='.jpg',prefix='magellan_search_');os.close(fd)
                upload_path=Path(tmp)
                if not img.save(str(upload_path),'JPEG',92): raise RuntimeError('Could not create the search image.')
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
                stamp=int(time.time()*1000)
                html=f'''<!doctype html>
<html><head><meta charset="utf-8"><title>Magellan - Google Lens</title></head>
<body>
<script>
(async()=>{{
  const b64={json.dumps(b64)};
  const bytes=Uint8Array.from(atob(b64), c=>c.charCodeAt(0));
  const blob=new Blob([bytes], {{type:"image/jpeg"}});
  const file=new File([blob], {json.dumps(upload_path.name)}, {{type:"image/jpeg"}});
  const form=document.createElement("form");
  form.method="POST";
  form.enctype="multipart/form-data";
  form.action="https://lens.google.com/v3/upload?ep=fntpubb&st={stamp}&vpw=1200&vph=900&hl=en";
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
                raw=upload_path.read_bytes(); b64=base64.b64encode(raw).decode('ascii')
                html=f"""<!doctype html><html><head><meta charset='utf-8'><title>Magellan - SauceNAO</title></head><body><script>
(async()=>{{
const bytes=Uint8Array.from(atob({json.dumps(b64)}),c=>c.charCodeAt(0));
const blob=new Blob([bytes],{{type:'image/jpeg'}});
const file=new File([blob],{json.dumps(upload_path.name)},{{type:'image/jpeg'}});
const form=document.createElement('form');form.method='POST';form.enctype='multipart/form-data';form.action='https://saucenao.com/search.php';
const input=document.createElement('input');input.type='file';input.name='file';const dt=new DataTransfer();dt.items.add(file);input.files=dt.files;form.appendChild(input);
const key=document.createElement('input');key.type='hidden';key.name='api_key';key.value={json.dumps(api_key)};form.appendChild(key);
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
                if 'tmp' in locals(): os.unlink(tmp)
            except Exception: pass

    def toggle_favorites_view(self):
        self.show_favorites()
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

if __name__=='__main__':
    app=QApplication(sys.argv);app.setStyle('Fusion');w=Explorer();w.show();sys.exit(app.exec())


