import queue, heapq
import os, sys, json, subprocess, threading, hashlib, re, random, tempfile, base64, time, ctypes, copy, shutil, struct, logging, atexit, math, zipfile, bisect, io, gzip
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from settings_storage import SettingsWriter
from ctypes import wintypes
from pathlib import Path, PurePosixPath

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
from concurrent.futures import ThreadPoolExecutor, CancelledError, Future
from urllib.parse import quote

from PySide6.QtCore import Qt, QSize, QSizeF, QRect, QRectF, QPoint, QPointF, Signal, QObject, QThread, QStandardPaths, QTimer, QDir, QEvent, QMimeData, QEventLoop, QLockFile, QAbstractAnimation, QUrl, QBuffer, QIODevice, QByteArray
from PySide6.QtGui import QPixmap, QPainter, QPainterPath, QPen, QColor, QLinearGradient, QFont, QFontMetricsF, QCursor, QImage, QImageReader, QIcon, QMovie, QDrag, QIntValidator, QShortcut, QKeySequence, QPalette, QRegion, QPaintEvent, QSurfaceFormat
from PySide6.QtPdf import QPdfDocument, QPdfSearchModel
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QAbstractScrollArea,
    QToolButton, QLabel, QFrame, QSlider, QMenu, QMenuBar, QFileDialog,
    QMessageBox, QInputDialog, QDialog, QDialogButtonBox, QListWidget,
    QTabBar, QTreeWidget, QTreeWidgetItem, QListWidgetItem, QDockWidget, QCheckBox, QSpinBox, QDoubleSpinBox, QComboBox, QScrollArea, QAbstractItemView,
    QWidgetAction, QGridLayout, QPushButton, QScroller, QScrollerProperties, QScrollBar, QTabWidget, QProgressBar, QStyle, QStyleOptionSlider, QStyleOptionMenuItem
)
from PySide6.QtGui import QDesktopServices
from PySide6.QtNetwork import QLocalServer, QLocalSocket

# Large JPEGs can need a little headroom for decoder scratch buffers even when
# QImageReader is asked for a bounded scaled output. Keep this capped; the
# viewer scales very large sources before decoding them.
QImageReader.setAllocationLimit(512)

IMAGE_EXTS={'.jpg','.jpeg','.jfif','.png','.webp','.gif','.bmp','.tif','.tiff','.avif','.heic','.heif','.jxl','.ico'}
PDF_EXTS={'.pdf'}
COMIC_BOOK_EXTS={'.cbr','.cbz'}
COMIC_EXTS=COMIC_BOOK_EXTS|{'.rar','.zip'}
MULTIPAGE_EXTS=PDF_EXTS|COMIC_EXTS
PREVIEW_EXTS=IMAGE_EXTS|MULTIPAGE_EXTS
NESTED_ARCHIVE_MAX_BYTES=4*1024*1024*1024
ARCHIVE_VIEW_PREFIX='magellan-archive://'
ARCHIVE_THUMB_PREFIX='magellan-archive-thumb://'
PDF_PAGE_MEMBER_PREFIX='__magellan_pdf_page__/'
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
_FFMPEG_EXTRACT_LOCK=threading.Lock()

def resolve_ffmpeg_executable():
    """Find FFmpeg without making image-only cold launches extract it."""
    if getattr(sys,'frozen',False):
        app_dir=Path(sys.executable).resolve().parent
        for candidate in (app_dir/'Magellan_ffmpeg.exe',app_dir/'ffmpeg.exe'):
            if candidate.is_file():return str(candidate)
        compressed=app_dir/'Magellan_ffmpeg.exe.gz'
        if compressed.is_file():
            cache_dir=Path(tempfile.gettempdir())/'MagellanRuntime'
            cached=cache_dir/'Magellan_ffmpeg.exe'
            try:
                with _FFMPEG_EXTRACT_LOCK:
                    if not cached.is_file():
                        cache_dir.mkdir(parents=True,exist_ok=True)
                        temp_path=None
                        try:
                            with gzip.open(compressed,'rb') as source, tempfile.NamedTemporaryFile(dir=cache_dir,prefix='ffmpeg-',suffix='.tmp',delete=False) as target:
                                temp_path=Path(target.name);shutil.copyfileobj(source,target,1024*1024)
                            os.replace(temp_path,cached)
                        finally:
                            if temp_path is not None:
                                try:temp_path.unlink(missing_ok=True)
                                except OSError:pass
                if cached.is_file():return str(cached)
            except (OSError,EOFError,gzip.BadGzipFile):
                logger.warning('Could not prepare FFmpeg for video thumbnails',exc_info=True)
    return shutil.which('ffmpeg') or ''
FOLDER_SIZE_REFRESH_INTERVAL=300
THUMB_CACHE_CLEANUP_POOL=ThreadPoolExecutor(max_workers=1,thread_name_prefix='thumb-cache-cleanup')

logger=logging.getLogger('magellan')
logger.addHandler(logging.NullHandler())
_PENDING_SEARCH_BRIDGES=set()
_NESTED_ARCHIVE_TEMP_ROOT=None

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

PDF_RENDER_LOCK=threading.RLock()

class PdfRenderDocument:
    """Keep background PDF rendering independent of Qt's search engine."""
    def __init__(self,path):
        import pypdfium2
        with PDF_RENDER_LOCK:self.document=pypdfium2.PdfDocument(str(path))
    def pageCount(self):
        with PDF_RENDER_LOCK:return len(self.document)
    def pagePointSize(self,index):
        with PDF_RENDER_LOCK:
            width,height=self.document.get_page_size(index);return QSizeF(width,height)
    def render(self,index,size):
        with PDF_RENDER_LOCK:
            page=self.document[index]
            try:
                width,height=page.get_size();bitmap=page.render(scale=max(.01,size.width()/max(.01,width)))
                try:
                    image=bitmap.to_pil().convert('RGBA');raw=image.tobytes();result=QImage(raw,image.width,image.height,image.width*4,QImage.Format_RGBA8888).copy()
                    return result if result.size()==size else result.scaled(size,Qt.IgnoreAspectRatio,Qt.SmoothTransformation)
                finally:bitmap.close()
            finally:page.close()
    def close(self):
        with PDF_RENDER_LOCK:
            document=getattr(self,'document',None);self.document=None
            if document is not None:document.close()
    def __del__(self):
        try:self.close()
        except Exception:pass

def load_pdf_document(path,timeout_ms=10000):
    try:return PdfRenderDocument(path)
    except Exception:logger.warning('Could not load PDF %s',path,exc_info=True);return None


def multipage_archive_names(path):
    """Return image members in archive display order without extracting files."""
    suffix=Path(path).suffix.casefold()
    if suffix not in COMIC_EXTS:return []
    with multipage_archive(path) as archive:
        names=[info.filename for info in archive.infolist() if not (getattr(info,'is_dir',lambda:False)() if callable(getattr(info,'is_dir',None)) else getattr(info,'is_dir',False)) and getattr(info,'file_size',0)<=256*1024*1024 and Path(info.filename).suffix.casefold() in IMAGE_EXTS]
    return sorted(names,key=natural_key)

def encode_archive_path(path,member='',is_dir=False,nested=False):
    payload=json.dumps({'archive':str(path),'member':str(member),'is_dir':bool(is_dir),'nested':bool(nested)},ensure_ascii=False,separators=(',',':')).encode('utf-8')
    return ARCHIVE_VIEW_PREFIX+base64.urlsafe_b64encode(payload).decode('ascii').rstrip('=')

def decode_archive_path(value):
    if not isinstance(value,str) or not value.startswith(ARCHIVE_VIEW_PREFIX):return None
    try:
        raw=value[len(ARCHIVE_VIEW_PREFIX):];raw+=('='*(-len(raw)%4));payload=json.loads(base64.urlsafe_b64decode(raw.encode('ascii')).decode('utf-8'))
        archive=payload.get('archive');member=payload.get('member','');is_dir=payload.get('is_dir',False);nested=payload.get('nested',False)
        if not isinstance(archive,str) or not isinstance(member,str) or not isinstance(is_dir,bool) or not isinstance(nested,bool):return None
        normalized=member.replace('\\','/').lstrip('/')
        if any(part=='..' for part in normalized.split('/')):return None
        return archive,normalized,is_dir,nested
    except (ValueError,TypeError,UnicodeError,json.JSONDecodeError):return None

def encode_archive_thumbnail(path,member):
    payload=json.dumps({'archive':str(path),'member':str(member)},ensure_ascii=False,separators=(',',':')).encode('utf-8')
    return ARCHIVE_THUMB_PREFIX+base64.urlsafe_b64encode(payload).decode('ascii').rstrip('=')

def encode_pdf_page_member(page):return PDF_PAGE_MEMBER_PREFIX+str(max(0,int(page)))
def decode_pdf_page_member(member):
    raw=str(member or '')
    if not raw.startswith(PDF_PAGE_MEMBER_PREFIX):return None
    try:return max(0,min(100000,int(raw[len(PDF_PAGE_MEMBER_PREFIX):])))
    except (TypeError,ValueError):return None

def decode_archive_thumbnail(value):
    if not isinstance(value,str) or not value.startswith(ARCHIVE_THUMB_PREFIX):return None
    try:
        raw=value[len(ARCHIVE_THUMB_PREFIX):];raw+=('='*(-len(raw)%4));payload=json.loads(base64.urlsafe_b64decode(raw.encode('ascii')).decode('utf-8'))
        archive,member=payload.get('archive'),payload.get('member')
        if not isinstance(archive,str) or not isinstance(member,str):return None
        member=member.replace('\\','/').lstrip('/')
        if any(part=='..' for part in member.split('/')):return None
        return archive,member
    except (ValueError,TypeError,UnicodeError,json.JSONDecodeError):return None

def list_archive_folder(path,prefix=''):
    """List one virtual archive directory without extracting archive members."""
    prefix=prefix.replace('\\','/').strip('/')
    prefix=(prefix+'/') if prefix else ''
    folders={};files=[]
    with multipage_archive(path) as archive:
        for info in archive.infolist():
            name=str(getattr(info,'filename','')).replace('\\','/').lstrip('/')
            if not name or any(part=='..' for part in name.split('/')) or not name.startswith(prefix):continue
            rest=name[len(prefix):]
            if not rest:continue
            head,*tail=rest.split('/',1)
            is_dir=bool(tail) or (getattr(info,'is_dir',lambda:False)() if callable(getattr(info,'is_dir',None)) else getattr(info,'is_dir',False))
            try:
                date_parts=getattr(info,'date_time',None);timestamp=time.mktime(tuple(date_parts)+(0,0,-1)) if date_parts and len(date_parts)==6 else 0
            except (OverflowError,TypeError,ValueError):timestamp=0
            if is_dir:
                member=prefix+head+'/'
                folders.setdefault(member,(head,timestamp))
                continue
            size=int(getattr(info,'file_size',0) or 0)
            if size<0:continue
            suffix=Path(head).suffix.casefold()
            if suffix in COMIC_EXTS:
                member=prefix+head
                folders.setdefault(member,(head,timestamp,True))
                continue
            if suffix in IMAGE_EXTS|PDF_EXTS and size>512*1024*1024:continue
            kind='image' if suffix in IMAGE_EXTS else ('document' if suffix in PDF_EXTS else 'file')
            files.append({'path':encode_archive_path(path,prefix+head,False),'name':head,'is_dir':False,'kind':kind,'mtime':timestamp,'ctime':timestamp,'size':size,'archive_path':str(path),'archive_member':prefix+head,'archive_virtual':True})
    out=[{'path':encode_archive_path(path,member,True,bool(details[2]) if len(details)>2 else False),'name':details[0],'is_dir':True,'kind':'folder','mtime':details[1],'ctime':details[1],'size':None,'archive_path':str(path),'archive_member':member,'archive_virtual':True} for member,details in folders.items()]
    out.extend(files);out.sort(key=lambda item:(not item['is_dir'],natural_key(item['name'])))
    return out[:50000]

def iter_archive_preview_images(path,prefix='',depth=0,visited=None,budget=None):
    """Yield image members recursively, including images inside nested archives."""
    if depth>8:return
    visited=set() if visited is None else visited
    budget=[0] if budget is None else budget
    try:
        identity=os.path.normcase(os.path.abspath(os.path.normpath(os.fspath(path))))+'\0'+str(prefix)
    except (OSError,TypeError,ValueError):return
    if identity in visited:return
    visited.add(identity)
    try:children=list_archive_folder(path,prefix)
    except Exception:
        logger.debug('Could not inspect archive while resolving a preview: %s',path,exc_info=True);return
    # Prefer images in this folder before descending, matching normal folder previews.
    children.sort(key=lambda item:(bool(item.get('is_dir')),natural_key(str(item.get('name','')))))
    for item in children:
        budget[0]+=1
        if budget[0]>4096:return
        if item.get('kind')=='image' and not item.get('is_dir'):
            decoded=decode_archive_path(item.get('path'))
            if decoded:yield decoded[0],decoded[1]
            continue
        if not item.get('is_dir'):continue
        decoded=decode_archive_path(item.get('path'))
        if not decoded:continue
        _archive,member,_is_dir,nested=decoded
        try:
            if nested:
                nested_path=materialize_nested_archive(path,member)
                yield from iter_archive_preview_images(nested_path,'',depth+1,visited,budget)
            else:
                yield from iter_archive_preview_images(path,member,depth+1,visited,budget)
        except Exception:
            # A damaged nested member should not prevent later siblings from
            # providing a usable folder preview.
            logger.debug('Could not inspect nested archive preview %s in %s',member,path,exc_info=True)

def render_archive_preview(path,prefix='',width=512,height=512):
    """Decode the first usable image, descending into archive folders as needed."""
    for archive_path,member in iter_archive_preview_images(path,prefix):
        try:
            data=read_archive_image(archive_path,member)
            image,_=decode_archive_image(data,Path(member).suffix,max(1,int(width))*2)
            if image.isNull():continue
            tw=max(1,int(width));th=max(1,int(height));image=image.scaled(QSize(tw,th),Qt.KeepAspectRatioByExpanding,Qt.SmoothTransformation)
            return image.copy(max(0,(image.width()-tw)//2),max(0,(image.height()-th)//2),tw,th)
        except Exception:
            logger.debug('Could not decode archive preview image %s in %s',member,archive_path,exc_info=True)
    return None

def copy_archive_member_to_stream(path,member,destination,max_bytes=NESTED_ARCHIVE_MAX_BYTES,should_cancel=None):
    """Stream one ZIP/RAR member to disk without holding its full contents in memory."""
    normalized=str(member).replace('\\','/')
    with multipage_archive(path) as archive:
        info=next((item for item in archive.infolist() if str(getattr(item,'filename','')).replace('\\','/')==normalized),None)
        if info is None:raise FileNotFoundError(f'Archive member not found: {member}')
        expected_size=int(getattr(info,'file_size',0) or 0)
        if expected_size<0:raise RuntimeError('Archive member has an invalid expanded size.')
        if expected_size>max_bytes:raise RuntimeError(f'Nested archive exceeds the {max_bytes//(1024*1024)} MiB browsing limit.')
        digest=hashlib.sha256();written=0
        if isinstance(archive,zipfile.ZipFile):
            with archive.open(info,'r') as source:
                while True:
                    if should_cancel is not None and should_cancel():raise InterruptedError('Extraction cancelled.')
                    chunk=source.read(1024*1024)
                    if not chunk:break
                    written+=len(chunk)
                    if written>max_bytes:raise RuntimeError(f'Nested archive exceeds the {max_bytes//(1024*1024)} MiB browsing limit.')
                    destination.write(chunk);digest.update(chunk)
            return written,int(getattr(info,'CRC',0) or 0),digest.hexdigest()
        try:from unrar.cffi.unrarlib import RarArchive
        except (ImportError,OSError) as error:raise RuntimeError(f'Could not initialize Magellan’s bundled RAR reader: {error}') from error
        class LimitedWriter:
            __slots__=('stream','limit','written','digest','too_large','should_cancel','cancelled')
            def __init__(self,stream,limit,cancel_check):self.stream=stream;self.limit=limit;self.written=0;self.digest=hashlib.sha256();self.too_large=False;self.should_cancel=cancel_check;self.cancelled=False
            def __call__(self,chunk):
                if self.too_large or self.cancelled:return
                if self.should_cancel is not None and self.should_cancel():self.cancelled=True;return
                if self.written+len(chunk)>self.limit:self.too_large=True;return
                self.stream.write(chunk);self.digest.update(chunk);self.written+=len(chunk)
        writer=LimitedWriter(destination,max_bytes,should_cancel);matched=False
        with RarArchive.open_for_processing(archive.filename,pwd=archive.pwd) as rar:
            for header in rar.iterate_headers():
                if str(header.FileNameW).replace('\\','/')==normalized:
                    matched=True
                    header.test(writer)
                    break
                header.skip()
        if not matched:raise FileNotFoundError(f'Archive member not found: {member}')
        if writer.cancelled:raise InterruptedError('Extraction cancelled.')
        if writer.too_large:raise RuntimeError(f'Nested archive exceeds the {max_bytes//(1024*1024)} MiB browsing limit.')
        return writer.written,int(getattr(info,'CRC',0) or 0),writer.digest.hexdigest()

def materialize_nested_archive(path,member):
    """Stream a nested archive/PDF member to a bounded temporary cache."""
    global _NESTED_ARCHIVE_TEMP_ROOT
    if _NESTED_ARCHIVE_TEMP_ROOT is None:
        _NESTED_ARCHIVE_TEMP_ROOT=Path(tempfile.mkdtemp(prefix='MagellanNestedArchives-'))
        atexit.register(shutil.rmtree,_NESTED_ARCHIVE_TEMP_ROOT,ignore_errors=True)
    cache=_NESTED_ARCHIVE_TEMP_ROOT
    source=Path(path);source_stat=source.stat()
    source_identity='\\0'.join((str(source.resolve()),str(member),str(source_stat.st_size),str(getattr(source_stat,'st_mtime_ns',int(source_stat.st_mtime*1_000_000_000)))))
    source_digest=hashlib.sha256(source_identity.encode('utf-8','surrogatepass')).hexdigest()
    normalized=str(member).replace('\\','/')
    with multipage_archive(path) as archive:
        info=next((item for item in archive.infolist() if str(getattr(item,'filename','')).replace('\\','/')==normalized),None)
        if info is None:raise FileNotFoundError(f'Archive member not found: {member}')
        expected_size=int(getattr(info,'file_size',0) or 0);crc=int(getattr(info,'CRC',0) or 0)
    if expected_size<0:raise RuntimeError('Archive member has an invalid expanded size.')
    if expected_size>NESTED_ARCHIVE_MAX_BYTES:raise RuntimeError(f'Nested archive exceeds the {NESTED_ARCHIVE_MAX_BYTES//(1024*1024)} MiB browsing limit.')
    identity_digest=hashlib.sha256(f'{source_digest}\\0{expected_size}\\0{crc}'.encode('ascii')).hexdigest()
    target=cache/(identity_digest+Path(member).suffix.casefold())
    if target.is_file() and target.stat().st_size==expected_size:return str(target)
    # Several thumbnail and navigation workers can request the same nested
    # archive at once. Use unique staging files and a no-overwrite rename so
    # Windows never has to replace a target currently opened by the RAR reader.
    fd,temporary_name=tempfile.mkstemp(prefix=source_digest+'-',suffix='.tmp',dir=cache)
    temporary=Path(temporary_name)
    try:
        with os.fdopen(fd,'wb') as stream:
            written,_crc,_content_digest=copy_archive_member_to_stream(path,member,stream)
            stream.flush()
        if written!=expected_size:raise RuntimeError(f'Archive member size changed while extracting {member}.')
        try:os.rename(temporary,target)
        except OSError:
            if not target.is_file() or target.stat().st_size!=expected_size:raise
    finally:
        try:temporary.unlink(missing_ok=True)
        except OSError:logger.debug('Could not remove nested archive staging file %s',temporary,exc_info=True)
    return str(target)

def open_multipage_archive(path):
    suffix=Path(path).suffix.casefold()
    if suffix in ('.cbz','.zip'):return zipfile.ZipFile(path)
    if suffix in ('.cbr','.rar'):
        try:from unrar.cffi import rarfile
        except (ImportError,OSError) as error:raise RuntimeError(f'Could not initialize Magellan’s bundled RAR reader: {error}') from error
        try:return rarfile.RarFile(path)
        except Exception as error:
            # A few comic archives are ZIP containers with a .cbr extension.
            # Accept those before reporting an invalid/unsupported RAR.
            if zipfile.is_zipfile(path):return zipfile.ZipFile(path)
            if getattr(error,'code',None)==13:
                raise RuntimeError('The archive is damaged, incomplete, or uses an unsupported RAR format (RAR error 13).') from error
            raise
    raise ValueError('Unsupported comic archive type')

@contextmanager
def multipage_archive(path):
    """Open ZIP or RAR members with the correct lifetime semantics.

    zipfile.ZipFile is a context manager. unrar.cffi.RarFile is not; it opens
    and closes its native handle inside each operation, so it needs no close.
    """
    archive=open_multipage_archive(path)
    try:yield archive
    finally:
        close=getattr(archive,'close',None)
        if callable(close):close()

def read_archive_image(path,member):
    with multipage_archive(path) as archive:
        normalized=str(member).replace('\\','/')
        info=next((item for item in archive.infolist() if str(getattr(item,'filename','')).replace('\\','/')==normalized),None)
        if info is None:raise FileNotFoundError(f'Archive member not found: {member}')
        size=int(getattr(info,'file_size',0) or 0)
        if size<0 or size>512*1024*1024:raise RuntimeError('Archive member exceeds the 512 MiB read limit.')
        data=archive.read(info.filename)
        if len(data)>512*1024*1024:raise RuntimeError('Expanded archive member exceeds the 512 MiB read limit.')
        return data

def decode_archive_image(data,suffix,target_width=0):
    if suffix.casefold()=='.jxl':
        from PIL import Image
        import pillow_jxl  # noqa: F401 -- registers the JPEG XL Pillow decoder
        with Image.open(io.BytesIO(data),formats=['JXL']) as source:
            ratio=source.width/max(1,source.height)
            if target_width and source.width>target_width:source.thumbnail((target_width,max(1,round(target_width/ratio))),Image.Resampling.LANCZOS)
            source=source.convert('RGBA')
            return QImage(source.tobytes(),source.width,source.height,source.width*4,QImage.Format_RGBA8888).copy(),ratio
    buffer=QBuffer();buffer.setData(data)
    if not buffer.open(QIODevice.ReadOnly):return QImage(),.7
    reader=QImageReader(buffer);reader.setAutoTransform(True);source=reader.size()
    if not source.isValid():return QImage(),.7
    ratio=source.width()/max(1,source.height())
    if target_width and source.width()>target_width:reader.setScaledSize(QSize(target_width,max(1,round(target_width/ratio))))
    return reader.read(),ratio

def prepare_archive_search_upload(archive,member):
    """Prepare only the selected image, using the same bounded archive reader."""
    suffix=Path(member).suffix.casefold()
    if suffix not in IMAGE_EXTS:raise ValueError('Select an image inside the archive to search.')
    data=read_archive_image(archive,member)
    if suffix=='.jxl':image,_=decode_archive_image(data,suffix,2560)
    else:
        source=QBuffer();source.setData(data);source.open(QIODevice.ReadOnly)
        reader=QImageReader(source);reader.setAutoTransform(True);reader.setDecideFormatFromContent(True)
        size=reader.size()
        if size.isValid() and max(size.width(),size.height())>2560:
            reader.setScaledSize(size.scaled(QSize(2560,2560),Qt.KeepAspectRatio))
        image=reader.read()
    if image.isNull():raise RuntimeError('Could not decode the selected archive image.')
    if max(image.width(),image.height())>2560:image=image.scaled(QSize(2560,2560),Qt.KeepAspectRatio,Qt.SmoothTransformation)
    # JPEG uploads need an opaque background for transparent PNG/WebP images.
    if image.hasAlphaChannel():
        opaque=QImage(image.size(),QImage.Format_RGB32);opaque.fill(Qt.white)
        painter=QPainter(opaque);painter.drawImage(0,0,image);painter.end();image=opaque
    output=QBuffer();output.open(QIODevice.WriteOnly)
    if not image.save(output,'JPEG',92):raise RuntimeError('Could not prepare the archive image for search.')
    return bytes(output.data()),PurePosixPath(member.replace(chr(92),'/')).stem+'.jpg'


def render_pdf_page_thumbnail(path,page_index,width,height):
    document=load_pdf_document(path)
    if document is None:return None
    try:
        if page_index<0 or page_index>=document.pageCount():return None
        page=document.pagePointSize(page_index);ratio=max(.01,page.width()/max(.01,page.height()));tw=max(1,int(width));th=max(1,int(height))
        size=QSize(max(tw,int(round(th*ratio))),th) if ratio>=1 else QSize(tw,max(th,int(round(tw/ratio))))
        image=flatten_pdf_page(document.render(page_index,size))
        if image.isNull():return None
        image=image.scaled(QSize(tw,th),Qt.KeepAspectRatioByExpanding,Qt.SmoothTransformation)
        return image.copy(max(0,(image.width()-tw)//2),max(0,(image.height()-th)//2),tw,th)
    finally:document.close()

def load_multipage_info(path):
    """Load page count and proportions off the UI thread."""
    suffix=Path(path).suffix.casefold()
    if suffix=='.pdf':
        document=load_pdf_document(path)
        if document is None:raise RuntimeError('Magellan could not open this PDF.')
        try:
            count=min(document.pageCount(),100000);ratios=[];page_sizes=[]
            for page in range(count):
                size=document.pagePointSize(page);ratios.append(size.width()/max(.01,size.height()));page_sizes.append((size.width(),size.height()))
            if count<1:raise RuntimeError('This PDF contains no pages.')
            return {'count':count,'members':None,'ratios':ratios,'page_sizes':page_sizes}
        finally:document.close()
    members=multipage_archive_names(path)
    if len(members)>100000:members=members[:100000]
    if not members:raise RuntimeError('This archive contains no supported image pages.')
    return {'count':len(members),'members':members,'ratios':[.72]*len(members)}

def flatten_pdf_page(image):
    """Composite PDF output over white so transparent page areas stay white."""
    if image is None or image.isNull():return image
    opaque=QImage(image.size(),QImage.Format_RGB32);opaque.fill(Qt.white)
    painter=QPainter(opaque);painter.drawImage(0,0,image);painter.end()
    return opaque

def decode_multipage_pages(path,page_requests,target_width=1600):
    suffix=Path(path).suffix.casefold()
    if suffix=='.pdf':
        document=load_pdf_document(path)
        if document is None:raise RuntimeError('Magellan could not render this PDF.')
        try:
            results=[];width=max(1,min(6000,int(target_width)))
            for index,_ in page_requests:
                if index<0 or index>=document.pageCount():results.append((index,QImage(),.7));continue
                page_size=document.pagePointSize(index);ratio=page_size.width()/max(.01,page_size.height());height=max(1,int(width/max(.01,ratio)))
                rendered=document.render(index,QSize(width,height));results.append((index,flatten_pdf_page(rendered),ratio))
            return results
        finally:document.close()
    results=[]
    with multipage_archive(path) as archive:
        for index,member in page_requests:
            try:
                data=archive.read(member);image,ratio=decode_archive_image(data,Path(member).suffix,target_width);results.append((index,image,ratio,'' if not image.isNull() else 'The page image could not be decoded.'))
            except Exception as error:
                # One corrupt member must not prevent the other pages in the
                # same requested batch from being displayed.
                logger.warning('Could not decode page %s in %s: %s',member,path,error)
                results.append((index,QImage(),.72,str(error)))
    return results

class ViewerImageLoadBridge(QObject):
    loaded=Signal(str,object)

class FolderThumbnailBridge(QObject):
    completed=Signal(str,object)

class ArchiveScanBridge(QObject):
    completed=Signal(str,str,object,str,int)

def decode_viewer_preview(path,preview_size):
    """Decode a screen-sized first frame quickly, then let the full decoder run."""
    suffix=Path(path).suffix.casefold()
    try:file_size=Path(path).stat().st_size
    except OSError:return QImage(),QImage(),False,QSize(),None
    source_size=QSize()
    try:
        if suffix=='.jxl':
            from PIL import Image
            import pillow_jxl  # noqa: F401 -- registers the JPEG XL Pillow decoder
            with Image.open(path,formats=['JXL']) as source:
                source_size=QSize(*source.size);source.thumbnail((max(1,preview_size.width()),max(1,preview_size.height())),Image.Resampling.BILINEAR);source=source.convert('RGBA')
                image=QImage(source.tobytes(),source.width,source.height,source.width*4,QImage.Format_RGBA8888).copy()
        else:
            reader=QImageReader(str(path));reader.setAutoTransform(True);reader.setDecideFormatFromContent(True);source_size=QSize(reader.size()) if reader.size().isValid() else QSize()
            if source_size.isValid():
                scale=min(1.0,preview_size.width()/max(1,source_size.width()),preview_size.height()/max(1,source_size.height()))
                reader.setScaledSize(QSize(max(1,round(source_size.width()*scale)),max(1,round(source_size.height()*scale))))
            image=reader.read()
    except Exception:
        logger.debug('Fast viewer preview failed for %s',path,exc_info=True);return QImage(),QImage(),False,source_size,file_size
    if image.isNull():return QImage(),QImage(),False,source_size,file_size
    fit=image if image.width()<=preview_size.width() and image.height()<=preview_size.height() else image.scaled(preview_size,Qt.KeepAspectRatio,Qt.FastTransformation)
    return image,fit,False,source_size if source_size.isValid() else image.size(),file_size

def decode_viewer_image(path,preview_size,max_dimension=8000,max_file_bytes=100*1024*1024,preload=False,known_source_size=None,known_file_size=None):
    suffix=Path(path).suffix.casefold()
    if isinstance(known_file_size,int) and known_file_size>=0:file_size=known_file_size
    else:
        try:file_size=Path(path).stat().st_size
        except OSError:return QImage(),QImage(),False,QSize(),None
    cacheable=file_size<=max_file_bytes
    source_size=QSize(known_source_size) if isinstance(known_source_size,QSize) and known_source_size.isValid() else QSize()
    if suffix not in PDF_EXTS and suffix!='.jxl' and not source_size.isValid():
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
        image=flatten_pdf_page(document.render(0,size));source_size=image.size()
    elif suffix=='.jxl':
        try:
            from PIL import Image
            import pillow_jxl  # noqa: F401 -- registers the JPEG XL Pillow decoder
            with Image.open(path,formats=['JXL']) as source:
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
QScrollArea{border:0;background:#080a10} QScrollBar:vertical{background:#15171c;width:12px;margin:2px 2px 2px 0;border-radius:0px} QScrollBar::handle:vertical{background:#737982;min-height:42px;border-radius:0px} QScrollBar::handle:vertical:hover{background:#747a83} QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0} QScrollBar:horizontal{background:#15171c;height:12px;margin:0 2px;border-radius:0px} QScrollBar::handle:horizontal{background:#737982;min-width:42px;border-radius:0px} QScrollBar::handle:horizontal:hover{background:#747a83} QScrollBar::add-line:horizontal,QScrollBar::sub-line:horizontal{width:0}
QDockWidget#fileTreeDock{background:#15171c;color:#eee;border-right:1px solid #3b3e44} QDockWidget#fileTreeDock::title{background:#202124;color:#eee;padding:7px;font-weight:600} QTabBar::tab{background:#2b2d31;color:#aaa;border:1px solid #44474c;border-bottom:none;padding:5px 34px 5px 14px;min-width:170px;max-width:170px;margin-right:2px;border-top-left-radius:5px;border-top-right-radius:5px} QTabBar::tab:selected{background:#3a3c41;color:#fff;border-color:#65686d} QTabBar::tab:hover{background:#34373c}
'''

def kind(p):
    if p.is_dir(): return 'folder'
    e=p.suffix.lower()
    if e in IMAGE_EXTS:return 'image'
    if e in VIDEO_EXTS:return 'video'
    if e in AUDIO_EXTS:return 'audio'
    if e in DOC_EXTS or e in COMIC_EXTS:return 'document'
    return 'file'

def birth(p):
    try:
        s=p.stat()
        if hasattr(s,'st_birthtime'):return s.st_birthtime
        if sys.platform.startswith('win'):return s.st_ctime
        return s.st_ctime
    except (OSError,ValueError,OverflowError):return 0

def datefmt(t):
    try:return datetime.fromtimestamp(float(t or 0)).strftime('%d/%m/%Y %H:%M')
    except (TypeError,OSError,ValueError,OverflowError):return '—'

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
                roots=list(self.selection) if self.selection else [self.folder]
                seen=set();seen_dirs=set();seen_archives=set();seen_pdfs=set();seen_virtual=set();result_limit=100000
                def append_virtual(item):
                    key=str(item.get('path',''))
                    if not key or key in seen_virtual or len(out)>=result_limit:return
                    seen_virtual.add(key);out.append(item)
                def add_pdf_pages(path,label=None,page_filter=None):
                    if len(out)>=result_limit or self.isInterruptionRequested():return
                    try:
                        path=Path(path);key=os.path.normcase(os.path.abspath(str(path)))
                        if key in seen_pdfs and page_filter is None:return
                        if page_filter is None:seen_pdfs.add(key)
                        st=path.stat(follow_symlinks=False);document=load_pdf_document(path)
                        if document is None:return
                        try:count=min(max(0,int(document.pageCount())),20000,result_limit-len(out))
                        finally:document.close()
                        pages=range(count) if page_filter is None else [page for page in page_filter if 0<=page<count]
                        for page in pages:
                            if self.isInterruptionRequested():return
                            member=encode_pdf_page_member(page);virtual=encode_archive_path(str(path),member,False)
                            append_virtual({'path':virtual,'name':f'{label or path.name} · {page+1}','is_dir':False,'kind':'document','mtime':st.st_mtime,'ctime':birth(path),'size':st.st_size,'parent':str(path.parent),'archive_path':str(path),'archive_member':member,'archive_virtual':True,'pdf_page_index':page})
                    except Exception as error:logger.warning('Farview could not read PDF pages from %s: %s',path,error)
                def add_archive_pages(path,depth=0,label=None,member_prefix='',exact_member=None):
                    if depth>8 or len(out)>=result_limit or self.isInterruptionRequested():return
                    try:
                        path=Path(path);key=os.path.normcase(os.path.abspath(str(path)))
                        prefix=str(member_prefix or '').replace(chr(92),'/').strip('/')
                        exact=str(exact_member or '').replace(chr(92),'/').strip('/') if exact_member is not None else None
                        scope=(key,'exact',exact) if exact is not None else (key,'prefix',prefix)
                        if scope in seen_archives:return
                        seen_archives.add(scope)
                        with multipage_archive(path) as archive:infos=list(archive.infolist())
                        infos.sort(key=lambda info:natural_key(str(getattr(info,'filename',''))))
                        for info in infos:
                            if self.isInterruptionRequested() or len(out)>=result_limit:break
                            member=str(getattr(info,'filename','')).replace(chr(92),'/').strip('/')
                            if not member or (exact is not None and member!=exact) or (exact is None and prefix and not member.startswith(prefix.rstrip('/')+'/')):continue
                            directory=getattr(info,'is_dir',False);directory=bool(directory() if callable(directory) else directory)
                            if directory:continue
                            suffix=Path(member).suffix.casefold();size=max(0,int(getattr(info,'file_size',getattr(info,'uncompressed_size',0)) or 0));stamp=0.0
                            try:
                                date_value=getattr(info,'date_time',None)
                                if hasattr(date_value,'timestamp'):stamp=float(date_value.timestamp())
                                elif isinstance(date_value,(tuple,list)) and len(date_value)>=6:stamp=datetime(*date_value[:6]).timestamp()
                            except (OSError,OverflowError,TypeError,ValueError):pass
                            name=Path(member).name;shown=f'{label} / {name}' if label else name
                            if suffix in IMAGE_EXTS:
                                virtual=encode_archive_path(str(path),member,False)
                                append_virtual({'path':virtual,'name':shown,'is_dir':False,'kind':'image','mtime':stamp,'ctime':stamp,'size':size,'parent':str(path.parent),'archive_path':str(path),'archive_member':member,'archive_virtual':True})
                            elif suffix in PDF_EXTS or suffix in COMIC_EXTS:
                                try:
                                    nested=materialize_nested_archive(path,member)
                                    if suffix in PDF_EXTS:add_pdf_pages(nested,shown)
                                    else:add_archive_pages(nested,depth+1,shown)
                                except Exception as error:logger.warning('Farview could not expand %s in %s: %s',member,path,error)
                    except Exception as error:logger.warning('Farview could not read archive %s: %s',path,error)
                def add_archive_selection(raw):
                    decoded=decode_archive_path(str(raw))
                    if not decoded:return False
                    archive,member,is_dir,nested_flag=decoded;member=member.replace(chr(92),'/').strip('/');page=decode_pdf_page_member(member)
                    if page is not None and Path(archive).suffix.casefold() in PDF_EXTS:
                        add_pdf_pages(archive,page_filter=[page]);return True
                    if is_dir:
                        if nested_flag:
                            try:add_archive_pages(materialize_nested_archive(archive,member))
                            except Exception as error:logger.warning('Farview could not open nested archive %s in %s: %s',member,archive,error)
                        else:add_archive_pages(archive,member_prefix=(member.rstrip('/')+'/' if member else ''))
                        return True
                    suffix=Path(member).suffix.casefold()
                    if suffix in IMAGE_EXTS:add_archive_pages(archive,exact_member=member)
                    elif suffix in PDF_EXTS:
                        try:add_pdf_pages(materialize_nested_archive(archive,member))
                        except Exception as error:logger.warning('Farview could not read PDF %s in %s: %s',member,archive,error)
                    elif suffix in COMIC_EXTS:
                        try:add_archive_pages(materialize_nested_archive(archive,member))
                        except Exception as error:logger.warning('Farview could not open nested archive %s in %s: %s',member,archive,error)
                    return True
                def add_media(path):
                    suffix=path.suffix.casefold()
                    if suffix not in PREVIEW_EXTS:return
                    path_key=os.path.normcase(os.path.abspath(str(path)))
                    if path_key in seen:return
                    seen.add(path_key)
                    try:
                        st=path.stat(follow_symlinks=False)
                        if suffix in PDF_EXTS:add_pdf_pages(path)
                        elif suffix in COMIC_EXTS:add_archive_pages(path)
                        else:append_virtual({'path':path,'name':path.name,'is_dir':False,'kind':kind(path),'mtime':st.st_mtime,'ctime':birth(path),'size':st.st_size,'parent':str(path.parent)})
                    except OSError:pass
                for selected_root in roots:
                    if self.isInterruptionRequested():return
                    if add_archive_selection(selected_root):continue
                    try:selected_root=Path(selected_root)
                    except (TypeError,ValueError):continue
                    if selected_root.is_file():add_media(selected_root);continue
                    if not selected_root.is_dir():continue
                    pending=[selected_root]
                    while pending:
                        if self.isInterruptionRequested():return
                        folder=pending.pop();folder_key=os.path.normcase(os.path.normpath(os.path.abspath(str(folder))))
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
                                elif path.suffix.casefold() in PREVIEW_EXTS:pending.append(('file',path))
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
                        out.append({'path':p,'name':e.name,'is_dir':d,'kind':'folder' if d else kind(p),'mtime':st.st_mtime,'ctime':getattr(st,'st_birthtime',st.st_ctime),'size':None if d else st.st_size})
                    except:pass
            self.signals.done.emit(out,'',self.generation)
        except Exception as ex:
            logger.exception('Directory scan failed for %s',self.folder)
            self.signals.done.emit([],str(ex),self.generation)

class FileOperationSignals(QObject):
    progress=Signal(str)
    done=Signal(object,object)

class FileOperationCompletionBridge(QObject):
    completed=Signal(object,object,object,object,object)
    task_finished=Signal(object)

class ExtractionDialog(QDialog):
    """Frameless confirmation and live feedback for archive extraction."""
    confirmed=Signal()
    cancel_requested=Signal()
    def __init__(self,parent,archives):
        super().__init__(parent,Qt.Dialog|Qt.FramelessWindowHint)
        self._running=False
        self.setModal(True);self.setAttribute(Qt.WA_TranslucentBackground,True);self.setWindowTitle('Extract archives')
        self.setMinimumWidth(390);self.setMaximumWidth(560)
        self.setStyleSheet('QDialog{background:rgba(18,20,25,242);color:#f0f0f0;border:1px solid rgba(255,255,255,75);border-radius:8px;} QLabel{color:#f0f0f0;} QListWidget{background:rgba(5,7,11,175);color:#e8e8e8;border:1px solid rgba(255,255,255,35);padding:4px;} QPushButton{background:rgba(55,58,64,220);color:#f0f0f0;border:1px solid rgba(255,255,255,65);padding:7px 18px;} QPushButton:hover{background:rgba(75,78,84,235);} QProgressBar{background:rgba(5,7,11,180);border:1px solid rgba(255,255,255,45);height:10px;text-align:center;} QProgressBar::chunk{background:#d0a32d;}')
        outer=QVBoxLayout(self);outer.setContentsMargins(0,0,0,0)
        self.panel=QFrame(self);self.panel.setObjectName('extractionPanel');self.panel.setStyleSheet('#extractionPanel{background:rgba(18,20,25,242);border:1px solid rgba(255,255,255,75);border-radius:8px;}')
        outer.addWidget(self.panel)
        layout=QVBoxLayout(self.panel);layout.setContentsMargins(20,18,20,18);layout.setSpacing(12)
        self.title=QLabel('Extract selected archives?',self.panel);self.title.setStyleSheet('font-size:15px;font-weight:600;');layout.addWidget(self.title)
        self.archive_list=QListWidget(self.panel);self.archive_list.setSelectionMode(QAbstractItemView.NoSelection);self.archive_list.setMaximumHeight(180)
        for archive in archives:self.archive_list.addItem(Path(archive).name)
        layout.addWidget(self.archive_list)
        self.progress_label=QLabel('Files will be extracted into the current folder.',self.panel);self.progress_label.setWordWrap(True);layout.addWidget(self.progress_label)
        self.progress=QProgressBar(self.panel);self.progress.setRange(0,0);self.progress.hide();layout.addWidget(self.progress)
        buttons=QHBoxLayout();buttons.addStretch(1);self.cancel_button=QPushButton('Cancel',self.panel);self.cancel_button.clicked.connect(self.cancel);buttons.addWidget(self.cancel_button);self.extract_button=QPushButton('Extract',self.panel);self.extract_button.setDefault(True);self.extract_button.clicked.connect(self.begin_extraction);buttons.addWidget(self.extract_button);layout.addLayout(buttons)
    def begin_extraction(self):
        if self._running:return
        self._running=True;self.title.setText('Extracting archives');self.archive_list.hide();self.progress_label.setText('Starting…');self.progress.show();self.extract_button.hide();self.cancel_button.setEnabled(True);self.confirmed.emit();self.accept()
    def set_progress(self,message):
        if self._running:self.progress_label.setText(str(message))
    def cancel(self):
        if self._running:
            self.cancel_button.setEnabled(False);self.progress_label.setText('Cancelling after the current file…');self.cancel_requested.emit()
        else:self.reject()
    def finish(self,completed,errors,cancelled=False):
        self._running=False;self.progress.hide();self.cancel_button.hide()
        if cancelled:self.title.setText('Extraction cancelled');self.progress_label.setText(f'{len(completed)} archive(s) finished before cancellation.')
        elif errors:self.title.setText('Extraction finished with errors');self.progress_label.setText(f'{len(completed)} archive(s) extracted; {len(errors)} could not be extracted.')
        else:self.title.setText('Extraction complete');self.progress_label.setText(f'{len(completed)} archive(s) extracted into the current folder.')
        QTimer.singleShot(450,self.accept)
    def reject(self):
        if self._running:self.cancel();return
        super().reject()

class FileOperationTask(QThread):
    def __init__(self,operation,sources,destination):
        super().__init__();self.operation=str(operation);self.sources=list(sources) if self.operation=='undo_move' else [str(value) for value in sources];self.destination=Path(destination);self.signals=FileOperationSignals();self.cancelled=False
    @staticmethod
    def unique_target(parent,name):
        base=Path(name);target=Path(parent)/base.name
        if not target.exists():return target
        stem=base.stem if base.suffix else base.name;suffix=base.suffix
        for index in range(1,100000):
            target=Path(parent)/f'{stem} ({index}){suffix}'
            if not target.exists():return target
        raise FileExistsError(f'No free target name for {name}')
    @staticmethod
    def safe_extract_target(root,member):
        normalized=str(member or '').replace(chr(92),'/');parts=PurePosixPath(normalized).parts
        if not normalized or normalized.startswith('/') or not parts or (len(parts[0])>=2 and parts[0][1]==':') or any(part in ('..','.') for part in parts):raise ValueError(f'Unsafe archive path: {member}')
        if any(any(ord(char)<32 or char in '<>:"|?*' for char in part) for part in parts):raise ValueError(f'Unsafe archive path: {member}')
        if os.name=='nt':
            reserved={'CON','PRN','AUX','NUL','CONIN$','CONOUT$'}|{prefix+digit for prefix in ('COM','LPT') for digit in '123456789¹²³'}
            if any(part.endswith((' ','.')) or part.split('.')[0].upper() in reserved for part in parts):raise ValueError(f'Unsafe Windows archive path: {member}')
        base=Path(root).resolve();target=base.joinpath(*parts).resolve()
        if os.path.commonpath((str(base),str(target)))!=str(base):raise ValueError(f'Archive path escapes the destination: {member}')
        return target
    def copy_file_contents(self,source,stream):
        with Path(source).open('rb') as reader:
            while True:
                if self.isInterruptionRequested():raise InterruptedError('Copy cancelled.')
                chunk=reader.read(1024*1024)
                if not chunk:break
                stream.write(chunk)
    def copy_regular_file(self,source,target,*,follow_symlinks=True):
        with Path(target).open('xb') as stream:self.copy_file_contents(source,stream)
        shutil.copystat(source,target,follow_symlinks=follow_symlinks)
        return str(target)
    def check_copy_directory(self,path,names):
        if self.isInterruptionRequested():raise InterruptedError('Copy cancelled.')
        return []
    def run(self):
        completed=[];errors=[]
        def discard_created(path):
            if path is None:return
            candidate=Path(path)
            try:
                if candidate.is_dir():shutil.rmtree(candidate,ignore_errors=True)
                elif candidate.exists():candidate.unlink()
            except OSError:logger.debug('Could not clean partial operation output %s',candidate,exc_info=True)
        try:
            if self.operation=='undo_move':
                for original,target in self.sources:
                    original=Path(original);target=Path(target)
                    try:
                        if self.isInterruptionRequested():self.cancelled=True;break
                        if not target.exists():raise FileNotFoundError(target)
                        if original.exists():raise FileExistsError(f'The original location is occupied: {original}')
                        original.parent.mkdir(parents=True,exist_ok=True);self.signals.progress.emit(f'Restoring {target.name}…');shutil.move(str(target),str(original));completed.append((str(target),str(original)))
                    except Exception as error:errors.append((str(target),str(error)));logger.warning('Could not undo move from %s to %s: %s',target,original,error,exc_info=True)
                self.signals.done.emit(completed,errors);return
            self.destination.mkdir(parents=True,exist_ok=True)
            for raw in self.sources:
                decoded_archive=decode_archive_path(raw);source=Path(decoded_archive[0]) if decoded_archive else Path(raw);display_source=f'{source}!/{decoded_archive[1]}' if decoded_archive else str(source);created_output=None
                try:
                    if self.isInterruptionRequested():self.cancelled=True;break
                    if decoded_archive is not None:
                        archive_path,member,is_dir,_nested=decoded_archive
                        if self.operation!='copy':raise ValueError('Only copying individual files out of an archive is supported.')
                        if is_dir or decode_pdf_page_member(member) is not None:raise ValueError('Select an individual file inside the archive to copy it.')
                        if not source.is_file():raise FileNotFoundError(source)
                        member_name=PurePosixPath(member).name
                        if not member_name:raise ValueError('The selected archive member has no file name.')
                        self.safe_extract_target(self.destination,member_name)
                        target=self.unique_target(self.destination,member_name)
                        self.signals.progress.emit(f'Copying {member_name} from {source.name}…')
                        with target.open('xb') as stream:
                            created_output=target
                            copy_archive_member_to_stream(archive_path,member,stream,max_bytes=4*1024**3,should_cancel=self.isInterruptionRequested)
                        created_output=None;completed.append((raw,str(target)));continue
                    if not source.exists():raise FileNotFoundError(source)
                    if self.operation in ('copy','move'):
                        self.signals.progress.emit(('Moving ' if self.operation=='move' else 'Copying ')+source.name+'…');target=self.unique_target(self.destination,source.name)
                        source_resolved=str(source.resolve());destination_resolved=str(self.destination.resolve());source_drive=os.path.splitdrive(source_resolved)[0].casefold();destination_drive=os.path.splitdrive(destination_resolved)[0].casefold();cross_volume=bool(source_drive and destination_drive and source_drive!=destination_drive)
                        if source.is_dir() and not cross_volume and os.path.commonpath((source_resolved,destination_resolved))==source_resolved:raise ValueError('Cannot place a folder inside itself or one of its subfolders.')
                        if self.operation=='copy' or (self.operation=='move' and cross_volume):
                            if source.is_dir():
                                target.mkdir(exist_ok=False);created_output=target
                                shutil.copytree(source,target,symlinks=True,dirs_exist_ok=True,copy_function=self.copy_regular_file,ignore=self.check_copy_directory)
                            else:
                                with target.open('xb') as stream:
                                    created_output=target;self.copy_file_contents(source,stream)
                                shutil.copystat(source,target)
                            if self.isInterruptionRequested():raise InterruptedError('Copy cancelled.')
                            if self.operation=='move':
                                # A completed copy is now the recovery copy if
                                # deleting the source fails partway through.
                                created_output=None
                                try:
                                    if source.is_dir():shutil.rmtree(source)
                                    else:source.unlink()
                                except OSError as error:raise OSError(f'Copy preserved at {target}; source removal incomplete: {error}') from error
                            created_output=None
                        else:
                            # shutil.move can itself fall back to copy/delete across junctions.
                            # Preserve its output on failure instead of risking the only copy.
                            shutil.move(str(source),str(target));created_output=None
                        completed.append((raw,str(target)))
                    elif self.operation=='extract':
                        if source.suffix.casefold() not in COMIC_EXTS:raise ValueError(f'{source.name} is not a supported ZIP or RAR archive.')
                        output=self.unique_target(self.destination,source.stem);output.mkdir(parents=True,exist_ok=False);created_output=output;total=0;interrupted=False
                        with multipage_archive(source) as archive:infos=list(archive.infolist())
                        for info in infos:
                            if self.isInterruptionRequested():interrupted=True;break
                            member=str(getattr(info,'filename','')).replace(chr(92),'/');target=self.safe_extract_target(output,member)
                            flag=getattr(info,'is_dir',False);is_dir=bool(flag() if callable(flag) else flag)
                            if is_dir:target.mkdir(parents=True,exist_ok=True);continue
                            if total>=64*1024**3:raise ValueError('Archive exceeds the safe extraction size limit.')
                            target.parent.mkdir(parents=True,exist_ok=True);self.signals.progress.emit(f'Extracting {source.name}: {Path(member).name}…')
                            with target.open('wb') as stream:written,_,_=copy_archive_member_to_stream(source,member,stream,max_bytes=min(4*1024**3,64*1024**3-total),should_cancel=self.isInterruptionRequested)
                            total+=written
                        if interrupted:
                            self.cancelled=True
                            shutil.rmtree(output,ignore_errors=True);created_output=None;break
                        completed.append((raw,str(output)));created_output=None
                    else:raise ValueError('Unsupported file operation.')
                except InterruptedError:
                    self.cancelled=True
                    discard_created(created_output)
                    break
                except Exception as error:
                    discard_created(created_output)
                    if self.isInterruptionRequested():self.cancelled=True;break
                    errors.append((raw,str(error)));logger.warning('File operation %s failed for %s: %s',self.operation,display_source,error,exc_info=True)
            if self.isInterruptionRequested():self.cancelled=True
            self.signals.done.emit(completed,errors)
        except Exception as error:
            logger.exception('File operation %s failed before processing items',self.operation);self.signals.done.emit(completed,errors+[(str(self.destination),str(error))])

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
    cancel_requested=Signal()
    def __init__(self,parent):
        super().__init__(parent);self.selection_text='';self.operation_text='';self.clipboard_text='';self.spinner_phase=0
        self.setAttribute(Qt.WA_TranslucentBackground,True)
        self._timer=QTimer(self);self._timer.setInterval(80);self._timer.timeout.connect(self._animate)
        self.cancel_button=QToolButton(self);self.cancel_button.setObjectName('activityCancelButton');self.cancel_button.setText('×');self.cancel_button.setToolTip('Cancel this operation');self.cancel_button.setFixedSize(23,23);self.cancel_button.setFocusPolicy(Qt.NoFocus);self.cancel_button.setStyleSheet('QToolButton#activityCancelButton{background:rgba(42,45,51,220);color:#f2f2f2;border:1px solid rgba(255,255,255,75);border-radius:4px;font-size:16px;padding:0;min-width:0;min-height:0;} QToolButton#activityCancelButton:hover{background:rgba(75,78,84,245);border-color:rgba(255,255,255,150);}')
        self.cancel_button.clicked.connect(self.cancel_requested.emit);self.cancel_button.hide();self.hide()
    def _animate(self):
        self.spinner_phase=(self.spinner_phase+1)%12;self.update()
    def set_selection(self,text):
        text=text or ''
        if text==self.selection_text:return
        self.selection_text=text;self._sync()
    def set_operation(self,text):
        self.operation_text=text or '';self.cancel_button.setVisible(bool(self.operation_text))
        if self.operation_text:self._timer.start()
        else:self._timer.stop()
        self._sync()
    def set_clipboard(self,text):
        self.clipboard_text=text or '';self._sync()
    def _sync(self):
        visible=bool(self.selection_text or self.operation_text or self.clipboard_text)
        self.setVisible(visible)
        if visible:self.adjustSize();self._layout_cancel_button();self.update()
    def sizeHint(self):
        fm=QFontMetricsF(self.font());lines=[t for t in (self.operation_text,self.clipboard_text,self.selection_text) if t]
        width=max((fm.horizontalAdvance(t) for t in lines),default=0)+(78 if self.operation_text else 52)
        height=(48+len(lines)*22) if self.operation_text else max(42,16+len(lines)*22)
        return QSize(max(210,min(460,int(width))),height)
    def _layout_cancel_button(self):
        self.cancel_button.move(max(0,self.width()-29),4)
    def resizeEvent(self,event):
        super().resizeEvent(event);self._layout_cancel_button()
    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing);p.setPen(QPen(QColor(255,255,255,45),1));p.setBrush(QColor(12,14,19,205));p.drawRoundedRect(self.rect().adjusted(0,0,-1,-1),8,8)
        y=40 if self.operation_text else 18
        if self.operation_text:
            center=QPointF(17,y-5)
            for i in range(8):
                alpha=45+int(210*((i-self.spinner_phase)%8)/7);p.setPen(QPen(QColor(235,190,65,alpha),2));angle=i*math.pi/4
                p.drawLine(QPointF(center.x()+math.cos(angle)*4,center.y()+math.sin(angle)*4),QPointF(center.x()+math.cos(angle)*8,center.y()+math.sin(angle)*8))
            p.setPen(QColor('#eeeeee'));p.drawText(32,y,self.operation_text);y+=22
        if self.clipboard_text:
            p.setPen(QColor('#eeeeee'));p.drawText(12,y,self.clipboard_text);y+=22
        if self.selection_text:
            p.setPen(QColor('#eeeeee'));p.drawText(12,y,self.selection_text)
        p.end()
def prepare_thumbnail_pixels(image):
    """Finish display-format conversion in the existing decoder worker."""
    if image is None or image.isNull() or image.depth()>32:return image
    fmt=QImage.Format_ARGB32_Premultiplied if image.hasAlphaChannel() else QImage.Format_RGB32
    return image if image.format()==fmt else image.convertToFormat(fmt)

def create_gpu_thumbnail_viewport(grid):
    # Keep OpenGL imports out of Explorer's image-only cold-start path.
    from PySide6.QtOpenGLWidgets import QOpenGLWidget
    class ThumbnailGpuViewport(QOpenGLWidget):
        def __init__(self):
            super().__init__(grid);fmt=QSurfaceFormat();fmt.setSwapInterval(1);self.setFormat(fmt);self.setUpdateBehavior(QOpenGLWidget.NoPartialUpdate);self.setProperty('magellan_gpu_viewport',True);self.frameSwapped.connect(self.frame_presented)
        def initializeGL(self):
            context=self.context();fmt=context.format();logger.info('Experimental thumbnail GPU context ready: OpenGL %s.%s, requested swap interval 1, context interval %s',fmt.majorVersion(),fmt.minorVersion(),fmt.swapInterval())
        def paintGL(self):
            if hasattr(grid,'paint_metadata') and not grid._closing:grid.paintEvent(QPaintEvent(self.rect()))
        def frame_presented(self):
            driver=getattr(grid,'smooth_scroll_animation',None)
            if driver is not None and driver.state()==QAbstractAnimation.Running:
                remaining=max(0.0,driver.period-(time.perf_counter()-driver.last_time));driver.timer.start(max(1,round(remaining*1000)))
        def grab(self,rect=None):
            image=self.grabFramebuffer();pixmap=QPixmap.fromImage(image);ratio=self.devicePixelRatioF();pixmap.setDevicePixelRatio(ratio)
            if rect is not None:
                pixmap=pixmap.copy(QRect(round(rect.x()*ratio),round(rect.y()*ratio),round(rect.width()*ratio),round(rect.height()*ratio)));pixmap.setDevicePixelRatio(ratio)
            return pixmap
    return ThumbnailGpuViewport()

class SmoothScrollDriver(QObject):
    """Elapsed-time motion with continuous velocity and one pending frame tick."""
    def __init__(self,grid):
        super().__init__(grid);self.grid=grid;self.bar=grid.verticalScrollBar();self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.setTimerType(Qt.PreciseTimer);self.timer.timeout.connect(self.tick);self.running=False;self.position=0.0;self.velocity=0.0;self.target=0.0;self.last_time=0.0;self.deadline=0.0;self.duration=600;self.writing=False;self.bar.valueChanged.connect(self.external_change);self.bar.rangeChanged.connect(self.range_changed)
    def state(self):return QAbstractAnimation.Running if self.running else QAbstractAnimation.Stopped
    def stop(self):
        self.timer.stop();self.running=False;self.velocity=0.0;self.position=float(self.bar.value());self.target=self.position
    def setDuration(self,value):self.duration=max(100,min(3000,int(value)))
    def setStartValue(self,value):self.position=float(value)
    def setEndValue(self,value):self.target=float(value)
    def start(self):self.running=False;self.retarget(self.target)
    def external_change(self,value):
        if self.running and not self.writing:self.stop()
    def range_changed(self,minimum,maximum):
        self.target=max(minimum,min(maximum,self.target))
        if maximum<=minimum:self.stop()
    def retarget(self,target):
        now=time.perf_counter();target=max(self.bar.minimum(),min(self.bar.maximum(),float(target)))
        if self.running:self.advance(now,False)
        else:self.position=float(self.bar.value());self.velocity=(target-self.position)*2.0/(self.duration/1000.0);self.last_time=now
        self.target=target;self.grid.smooth_scroll_target=target
        if (self.target-self.position)*self.velocity<0:self.velocity=0.0
        if abs(self.target-self.position)<.05:self.stop();return
        if not self.running:
            self.running=True;screen=self.grid.screen();hz=screen.refreshRate() if screen is not None else 60;self.period=1/max(30,min(240,hz or 60));self.deadline=now+self.period
            # Start motion before requesting the first GPU frame. Subsequent
            # frames retain the existing swap-paced scheduling and watchdog.
            if self.grid.viewport().property('magellan_gpu_viewport'):self.timer.start(1)
            else:self.schedule(now)
    def schedule(self,now):
        if self.grid.viewport().property('magellan_gpu_viewport'):
            self.timer.start(100);self.grid.viewport().update();return
        if self.deadline<=now:self.deadline+=max(1,math.floor((now-self.deadline)/self.period)+1)*self.period
        self.timer.start(max(1,round((self.deadline-now)*1000)))
    def advance(self,now,present=True):
        dt=max(0.0,now-self.last_time);self.last_time=now;omega=9.0/(self.duration/1000.0);error=self.position-self.target;coefficient=self.velocity+omega*error;decay=math.exp(-omega*dt);self.position=self.target+(error+coefficient*dt)*decay;self.velocity=(self.velocity-omega*coefficient*dt)*decay
        self.position=max(self.bar.minimum(),min(self.bar.maximum(),self.position))
        if present:
            self.writing=True
            try:self.bar.setValue(round(self.position))
            finally:self.writing=False
    def tick(self):
        if not self.running:return
        if not self.grid.isVisible():self.stop();return
        now=time.perf_counter();self.advance(now)
        if abs(self.target-self.position)<.5 and abs(self.velocity)<12:
            self.writing=True
            try:self.bar.setValue(round(self.target))
            finally:self.writing=False
            self.stop();return
        self.deadline+=self.period;self.schedule(time.perf_counter())

class ThumbnailPriorityPool:
    """Visible work jumps ahead of maintenance and speculative requests."""
    def __init__(self,max_workers):
        self.queue=queue.PriorityQueue();self.lock=threading.Lock();self.serial=0;self.closed=False;self.threads=[]
        for index in range(max_workers):
            thread=threading.Thread(target=self.run,name=f'thumbnail-priority-{id(self)}-{index}',daemon=True);thread.start();self.threads.append(thread)
    def submit(self,fn,*args,priority=10):
        with self.lock:
            if self.closed:raise RuntimeError('Thumbnail pool is closed')
            future=Future();future._thumbnail_pool=self;future._thumbnail_job=(fn,args);future._thumbnail_priority=priority;future._thumbnail_version=0;self.serial+=1;self.queue.put((priority,self.serial,0,future));return future
    def promote(self,future,priority=0):
        with self.lock:
            if self.closed or future.done() or future.running() or priority>=future._thumbnail_priority:return
            future._thumbnail_priority=priority;future._thumbnail_version+=1;self.serial+=1;self.queue.put((priority,self.serial,future._thumbnail_version,future))
    def run(self):
        while True:
            _,_,version,future=self.queue.get()
            if future is None:return
            with self.lock:
                if version!=future._thumbnail_version:continue
                if not future.set_running_or_notify_cancel():future._thumbnail_job=None;continue
                fn,args=future._thumbnail_job;future._thumbnail_job=None
            try:future.set_result(fn(*args))
            except BaseException as error:future.set_exception(error)
    def shutdown(self,wait=False,cancel_futures=True):
        with self.lock:
            if not self.closed:
                self.closed=True
                if cancel_futures:
                    while True:
                        try:_,_,_,future=self.queue.get_nowait()
                        except queue.Empty:break
                        if future is not None:future.cancel();future._thumbnail_job=None
                for _ in self.threads:self.serial+=1;self.queue.put((float('inf'),self.serial,0,None))
        if wait:
            for thread in self.threads:thread.join()

class ThumbnailCompletionBridge(QObject):
    ready=Signal()

class VirtualGrid(QAbstractScrollArea):
    openItem=Signal(str); middleOpenItem=Signal(str); goBack=Signal(); setThumb=Signal(str); context=Signal(str); imageSearchRequested=Signal(str); toggleItemFavorite=Signal(str)
    def __init__(self,parent=None):
        super().__init__(parent);self._explorer_window=parent.window() if parent is not None else None;self._closing=False;self.items=[];self.items_by_path={};self._thumb_cache_roots_cache=None;self._thumb_cache_roots_generation=None;self.thumb=230;self.columns=5;self.mode='vertical';self.cache=OrderedDict();self.cache_bytes=0;self.cache_limit_bytes=256*1024*1024;self._visible_cache_keys=None;self.thumb_futures={};self.failed_thumbnails=OrderedDict();self.folder_cache={};self.folder_custom={};self.folder_futures={};self.folder_last_visible={};self._folder_display_events={};self._thumbnail_backpressure=False;self._scroll_direction=1;self._scroll_speed=0.0;self._prefetching=False;self.pool=ThumbnailPriorityPool(max_workers=4);self.cache_read_pool=ThumbnailPriorityPool(max_workers=2);self.folder_pool=ThumbnailPriorityPool(max_workers=2);self.file_cache_write_pool=ThumbnailPriorityPool(max_workers=1);self._file_cache_write_lock=threading.Lock();self._file_cache_write_jobs={};self.size_pool=ThreadPoolExecutor(max_workers=1);self.hover_size_pool=ThreadPoolExecutor(max_workers=1);self.resolution_pool=ThreadPoolExecutor(max_workers=1);self.resolution_cache=OrderedDict();self.resolution_futures={};self.folder_size_cache={};self.folder_size_cache_checked_at={};self.folder_size_futures={};self.hover_folder_size_futures={};self.folder_size_events={};self.hover_folder_size_events={};self.pinned_size_tree_futures={};self.pinned_size_tree_events={};self.pinned_size_tree_targets={};self.folder_size_queue=deque();self.folder_size_queued=set();self.hover_favorite_rect=QRect();self.hover_overlay_rect=QRect();self.hover_tooltip_rect=QRect();self.persistent_folder_cache_dir=None;self.last_scroll_value=0;self.last_scroll_time=0;self.fast_scroll=False;self.search_hover=-1;self.hover_index=-1;self.hover_animation_started=0;self.drag_source_index=-1;self.drag_started=False;self.drag_start=QPoint();self.drop_indicator=None;self.organization_drag_pos=QPoint();self.organization_scroll_accumulator=0.0;self.last_favorite_drag=0.0;self.pending_favorite_item=-1;self.suppress_favorite_release=False;self.pending_group_rename_id=None;self.favorite_picker_delay=QTimer(self);self.favorite_picker_delay.setSingleShot(True);self.favorite_picker_delay.setInterval(120);self.favorite_picker_delay.timeout.connect(self.open_hover_favorite_picker);self.favorite_picker_key=None;self.favorite_picker_hover_key=None;self.favorite_click_timer=QTimer(self);self.favorite_click_timer.setSingleShot(True);self.favorite_click_timer.setInterval(220);self.favorite_click_timer.timeout.connect(self.dispatch_favorite_open);self.group_rename_timer=QTimer(self);self.group_rename_timer.setSingleShot(True);self.group_rename_timer.timeout.connect(self.dispatch_group_rename);self.organization_scroll_timer=QTimer(self);self.organization_scroll_timer.setInterval(16);self.organization_scroll_timer.timeout.connect(self.auto_scroll_organization);self.drop_target_label=QLabel(self.viewport());self.drop_target_label.setAttribute(Qt.WA_TransparentForMouseEvents,True);self.drop_target_label.setStyleSheet('QLabel{background:rgba(10,12,16,205);color:#f1f1f1;border:1px solid rgba(255,255,255,90);padding:4px 7px;font-size:10px;}');self.drop_target_label.hide();self.setAcceptDrops(True);self.viewport().setAcceptDrops(True);self.setFrameShape(QFrame.NoFrame);self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);self.viewport().setMouseTracking(True);self.viewport().installEventFilter(self);self.image_search_active=False
        # Keep Qt's internal scrollbar as the scroll model, but suppress its
        # layout gutter. A separate child scrollbar overlays the viewport so
        # hiding it gives the thumbnail grid the full width back.
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.verticalScrollBar().setProperty('magellan_internal_scroll_model',True)
        self.overlay_scrollbar=QScrollBar(Qt.Vertical,self.viewport());self.overlay_scrollbar.setObjectName('gridOverlayScrollBar');self.overlay_scrollbar.setProperty('magellan_grid_overlay',True);self.overlay_scrollbar.hide()
        self.overlay_scrollbar.valueChanged.connect(self.verticalScrollBar().setValue)
        self.overlay_scrollbar.sliderPressed.connect(lambda:self.stop_smooth_scroll())
        self.verticalScrollBar().valueChanged.connect(self.sync_overlay_scrollbar_value)
        self.verticalScrollBar().rangeChanged.connect(self.sync_overlay_scrollbar_range)
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
        self.selected_paths=set();self.selection_order=[];self.selected_metadata={};self.selection_undo=[];self.selection_dragging=False;self.selection_drag_moved=False;self.selection_anchor=None;self.selection_current=None;self.selection_base=set();self.selection_base_order=[];self.last_mouse_pos=QPoint();self.navigation_index=-1;self.navigation_key=None;self.navigation_started=0.0
        self.selection_scroll_accumulator=0.0;self.selection_scroll_timer=QTimer(self);self.selection_scroll_timer.setInterval(16);self.selection_scroll_timer.timeout.connect(self.auto_scroll_selection)
        self.wheel_scroller=QScroller.scroller(self);wheel_properties=self.wheel_scroller.scrollerProperties();wheel_properties.setScrollMetric(QScrollerProperties.FrameRate,QScrollerProperties.Standard);wheel_properties.setScrollMetric(QScrollerProperties.VerticalOvershootPolicy,QScrollerProperties.OvershootAlwaysOff);self.wheel_scroller.setScrollerProperties(wheel_properties)
        self.smooth_scroll_animation=SmoothScrollDriver(self);self.smooth_scroll_target=0
        self._scroll_diagnostic=None
        self.thumbnail_bridge=ThumbnailCompletionBridge(self);self.thumbnail_bridge.ready.connect(self.schedule_thumbnail_poll,Qt.QueuedConnection);self.thumbnail_completion_timer=QTimer(self);self.thumbnail_completion_timer.setSingleShot(True);self.thumbnail_completion_timer.setInterval(8);self.thumbnail_completion_timer.timeout.connect(self.poll_async)
        self.thumb_timer=QTimer(self);self.thumb_timer.timeout.connect(self.poll_async);self.thumb_timer.start(60)
        self.thumbnail_prefetch_timer=QTimer(self);self.thumbnail_prefetch_timer.setSingleShot(True);self.thumbnail_prefetch_timer.setInterval(120);self.thumbnail_prefetch_timer.timeout.connect(self.prefetch_directional_thumbnails)
        self.scroll_settle_timer=QTimer(self);self.scroll_settle_timer.setSingleShot(True);self.scroll_settle_timer.timeout.connect(self.scroll_settled)
        self.hover_delay_timer=QTimer(self);self.hover_delay_timer.setSingleShot(True);self.hover_delay_timer.timeout.connect(self.begin_hover_animation);self.hover_exit_timer=QTimer(self);self.hover_exit_timer.setSingleShot(True);self.hover_exit_timer.setInterval(75);self.hover_exit_timer.timeout.connect(lambda:self.set_hover_index(-1))
        self.hover_animation_timer=QTimer(self);self.hover_animation_timer.setInterval(16);self.hover_animation_timer.timeout.connect(self.advance_hover_animation)
        self.setFocusPolicy(Qt.StrongFocus);self.viewport().setAttribute(Qt.WA_OpaquePaintEvent,True);self.verticalScrollBar().valueChanged.connect(self.on_scroll)
        if getattr(self.explorer(),'gpu_thumbnail_rendering_enabled',False):
            try:self.replace_thumbnail_viewport(create_gpu_thumbnail_viewport(self));QTimer.singleShot(1800,self.check_gpu_viewport)
            except Exception:logger.warning('Could not initialize experimental GPU viewport; using software rendering',exc_info=True)
    def explorer(self):
        """Return the top-level Explorer window, not the grid host widget."""
        return self._explorer_window or self.window()

    def replace_thumbnail_viewport(self,viewport):
        old=self.viewport();children=[(widget,not widget.isHidden()) for widget in old.findChildren(QWidget,options=Qt.FindDirectChildrenOnly)]
        for widget,_visible in children:widget.setParent(viewport)
        self.setViewport(viewport);viewport.setMouseTracking(True);viewport.installEventFilter(self);viewport.setAcceptDrops(True);viewport.setAttribute(Qt.WA_OpaquePaintEvent,True)
        for widget,visible in children:widget.setVisible(visible)
        self.position_overlay_scrollbar();self.clear_card_cache();viewport.update()
    def check_gpu_viewport(self):
        viewport=self.viewport()
        if self._closing or not viewport.property('magellan_gpu_viewport'):return
        if not viewport.isVisible():QTimer.singleShot(1000,self.check_gpu_viewport);return
        if not viewport.isValid():
            logger.warning('Experimental GPU context is unavailable; falling back to software thumbnail rendering')
            self.stop_smooth_scroll();self.replace_thumbnail_viewport(QWidget(self));owner=self.explorer();owner.gpu_thumbnail_rendering_enabled=False
            if getattr(owner,'gpu_composition_enabled',False):owner.disable_gpu_composition('GPU thumbnail rendering unavailable')
            owner.close_gpu_blur_backend();owner.gpu_blur_enabled=False
            action=getattr(owner,'gpu_blur_action',None)
            if action is not None:
                blocked=action.blockSignals(True);action.setChecked(False);action.setEnabled(False);action.blockSignals(blocked)
            owner.save_settings()
            if hasattr(owner,'status'):owner.status.setText('GPU rendering unavailable; software rendering restored')
    def viewportEvent(self,event):
        if self.viewport().property('magellan_gpu_viewport') and event.type()==QEvent.Paint:return False
        return super().viewportEvent(event)

    def schedule_thumbnail_poll(self):
        if not self._closing and not self.thumbnail_completion_timer.isActive():self.thumbnail_completion_timer.start()
    def watch_thumbnail_future(self,future):
        bridge=self.thumbnail_bridge
        def completed(_):
            try:bridge.ready.emit()
            except RuntimeError:pass
        future.add_done_callback(completed)
        return future

    def setItems(self,items,reset_scroll=True):
        old_hover_path=str(self.items[self.hover_index].get('path','')) if 0<=self.hover_index<len(self.items) else None
        hover_delay_remaining=self.hover_delay_timer.remainingTime() if self.hover_delay_timer.isActive() else None
        hover_animation_active=self.hover_animation_timer.isActive();hover_animation_started=self.hover_animation_started
        if hasattr(self,'wheel_scroller'):self.wheel_scroller.stop()
        if hasattr(self,'smooth_scroll_animation'):
            self.smooth_scroll_animation.stop();self.smooth_scroll_target=self.verticalScrollBar().value()
        self.hover_delay_timer.stop();self.hover_exit_timer.stop();self.hover_animation_timer.stop();self.hover_index=-1;self.hover_favorite_rect=QRect();self.hover_overlay_rect=QRect();self.hover_tooltip_rect=QRect()
        self.favorite_picker_delay.stop();self.favorite_picker_key=None;self.favorite_picker_hover_key=None
        self.navigation_index=-1
        # Navigating to a new view should never leave old folder-discovery jobs ahead of the new viewport.
        for event in self._folder_display_events.values():event.set()
        self._folder_display_events.clear()
        for f in list(self.folder_futures.values()):
            try: f.cancel()
            except: pass
        self.folder_futures.clear()
        self.thumbnail_prefetch_timer.stop();self.clear_card_cache();self.items=items;items_by_path={};paint_metadata={}
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
        cursor=self.viewport().mapFromGlobal(QCursor.pos());new_hover=self.thumbnail_hit(cursor) if self.viewport().rect().contains(cursor) else -1
        new_hover_path=str(self.items[new_hover].get('path','')) if 0<=new_hover<len(self.items) else None
        if old_hover_path and new_hover_path==old_hover_path:
            self.hover_index=new_hover
            if hover_delay_remaining is not None:self.hover_delay_timer.start(max(0,hover_delay_remaining))
            if hover_animation_active:self.hover_animation_started=hover_animation_started;self.hover_animation_timer.start()
        else:self.set_hover_index(new_hover)
    def clear_card_cache(self):
        self.card_cache.clear();self.card_cache_bytes=0
        owner=self.explorer()
        if owner is not None:owner._appearance_source_revision=getattr(owner,'_appearance_source_revision',0)+1
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
        if self._closing or self.selection_dragging or self.folder_size_futures or self.pinned_size_tree_futures or self.hover_index>=0:return
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
    def request_pinned_folder_sizes(self,root,targets):
        """Cache the kept root and its direct children with one recursive walk."""
        if self._closing:return
        root_key=str(root)
        if root_key in self.pinned_size_tree_futures:return
        targets=[str(path) for path in targets if self.folder_size_needs_refresh(str(path))]
        if not targets:return
        target_keys={os.path.normcase(os.path.abspath(os.path.normpath(path))) for path in targets}
        # Remove overlapping queued sort work; the aggregate scan will fill
        # these same folder sizes without reopening each subtree separately.
        if self.folder_size_queue:
            retained=deque()
            for key in self.folder_size_queue:
                normalized=os.path.normcase(os.path.abspath(os.path.normpath(key)))
                if normalized in target_keys:self.folder_size_queued.discard(key)
                else:retained.append(key)
            self.folder_size_queue=retained
        for key,future in list(self.folder_size_futures.items()):
            normalized=os.path.normcase(os.path.abspath(os.path.normpath(key)))
            if normalized in target_keys:
                event=self.folder_size_events.get(key)
                if event:event.set()
                if future.cancel():self.folder_size_futures.pop(key,None);self.folder_size_events.pop(key,None)
        event=threading.Event();future=self.size_pool.submit(self.folder_size_tree_worker,root_key,event)
        self.pinned_size_tree_futures[root_key]=future;self.pinned_size_tree_events[root_key]=event;self.pinned_size_tree_targets[root_key]=targets
    def folder_size_tree_worker(self,root,cancel_event=None):
        """Measure a kept tree once and derive its descendant folder totals."""
        root_path=Path(root)
        direct_bytes={};parents={};pending=[(root_path,None)]
        while pending and not self._closing and not (cancel_event and cancel_event.is_set()):
            current,parent=pending.pop();key=os.path.normcase(os.path.abspath(os.path.normpath(str(current))))
            parents[key]=parent;total=0;children=[]
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        if self._closing or (cancel_event and cancel_event.is_set()):return None
                        try:
                            if entry.is_dir(follow_symlinks=False):children.append((Path(entry.path),key))
                            elif entry.is_file(follow_symlinks=False):total+=entry.stat(follow_symlinks=False).st_size
                        except OSError:continue
            except OSError:pass
            direct_bytes[key]=total;pending.extend(children)
        if cancel_event and cancel_event.is_set():return None
        for key in sorted(direct_bytes,key=lambda item:item.count(os.sep),reverse=True):
            parent=parents.get(key)
            if parent in direct_bytes:direct_bytes[parent]+=direct_bytes[key]
        return direct_bytes
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
        for event in self._folder_display_events.values():event.set()
        self.thumbnail_completion_timer.stop();self.thumbnail_prefetch_timer.stop()
        if self._closing:return
        self._closing=True
        for timer in (self.thumb_timer,self.scroll_settle_timer,self.hover_delay_timer,self.hover_exit_timer,self.hover_animation_timer,self.favorite_picker_delay,self.favorite_click_timer,self.group_rename_timer,self.selection_scroll_timer,self.organization_scroll_timer):timer.stop()
        self.wheel_scroller.stop();self.smooth_scroll_animation.stop()
        for events in (self.folder_size_events,self.hover_folder_size_events,self.pinned_size_tree_events):
            for event in events.values():event.set()
            events.clear()
        for futures in (self.thumb_futures,self.folder_futures,self.folder_size_futures,self.hover_folder_size_futures,self.pinned_size_tree_futures,self.resolution_futures):
            for future in futures.values():future.cancel()
            futures.clear()
        self.folder_size_queue.clear();self.folder_size_queued.clear()
        for executor in (self.pool,self.cache_read_pool,self.folder_pool,self.file_cache_write_pool,self.size_pool,self.hover_size_pool,self.resolution_pool):executor.shutdown(wait=False,cancel_futures=True)
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
    def resizeEvent(self,e):
        super().resizeEvent(e);self.recalc();self.position_overlay_scrollbar()
    def position_overlay_scrollbar(self):
        if not hasattr(self,'overlay_scrollbar'):return
        self.overlay_scrollbar.setGeometry(max(0,self.viewport().width()-12),0,12,self.viewport().height());self.overlay_scrollbar.raise_()
    def sync_overlay_scrollbar_range(self,minimum,maximum):
        bar=self.overlay_scrollbar;bar.blockSignals(True);bar.setRange(minimum,maximum);bar.setPageStep(self.verticalScrollBar().pageStep());bar.setValue(self.verticalScrollBar().value());bar.blockSignals(False);self.position_overlay_scrollbar()
        if maximum<=minimum:bar.hide()
        elif not getattr(self.explorer(),'scrollbars_auto_hide',True):bar.show()
    def sync_overlay_scrollbar_value(self,value):
        bar=self.overlay_scrollbar
        if bar.value()!=value:bar.blockSignals(True);bar.setValue(value);bar.blockSignals(False)
    def remember_failed_thumbnail(self,key):
        self.failed_thumbnails[key]=time.monotonic()+30.0
        self.failed_thumbnails.move_to_end(key)
        while len(self.failed_thumbnails)>2048:self.failed_thumbnails.popitem(last=False)
    def scrollContentsBy(self,dx,dy):
        if self.viewport().property('magellan_gpu_viewport'):
            self.viewport().update();return
        # Move the existing viewport and let Qt repaint only the newly exposed
        # strip. Repainting all cards for each animation frame increased paint
        # cost without removing the reported tearing.
        started=time.perf_counter() if self._scroll_diagnostic is not None else None
        self.viewport().scroll(dx,dy)
        # QWidget.scroll() also shifts child widgets with the painted content.
        # The overlay scrollbar belongs to the viewport, but its track must stay
        # fixed while the thumbnails move underneath it.
        if hasattr(self,'overlay_scrollbar'):
            expected=QPoint(max(0,self.viewport().width()-12),0)
            if self.overlay_scrollbar.pos()!=expected:self.position_overlay_scrollbar()
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
                    if not animation_active:
                        configured=getattr(self.explorer(),'scroll_glide_duration_ms',0)
                        animation.setDuration(configured or max(380,min(1050,int(380+abs(target-bar.value())*.28))))
                    animation.retarget(target)
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
        self._card_build_deadline=time.perf_counter()+.008
        self._cards_built_this_frame=0
        # Avoid repeated deferred redraws if the visible cards cannot fit in RAM.
        estimated_visible_bytes=self.viewport().width()*(self.viewport().height()+2*max(1,self.row_h))*4*float(self.devicePixelRatioF())**2
        self._card_budget_enabled=estimated_visible_bytes<=self.card_cache_limit_bytes
        diag=self._scroll_diagnostic
        paint_started=time.perf_counter() if diag is not None else None
        if diag is not None:
            if diag['last_paint'] is not None:diag['paint_gaps_ms'].append((paint_started-diag['last_paint'])*1000)
            diag['last_paint']=paint_started
        card_started=None;painted_cards=0
        self._visible_cache_keys=set() if not getattr(self.explorer(),'thumbnail_cache_enabled',True) else None
        self._appearance_recording=True
        if not hasattr(self,'_appearance_card_pixels'):self._appearance_card_pixels={}
        p=QPainter(self.viewport());p.fillRect(self.viewport().rect(),QColor('#080a10'))
        if not self.items:
            area=self.viewport().rect();p.setPen(QColor('#d0a32d'));p.setFont(QFont('Segoe UI Symbol',54,QFont.Bold));p.drawText(QRect(0,area.height()//2-100,area.width(),90),Qt.AlignCenter,'◈')
            p.setPen(QColor('#e5e5e5'));p.setFont(QFont('Segoe UI',20,QFont.DemiBold));p.drawText(QRect(0,area.height()//2-8,area.width(),36),Qt.AlignCenter,'MAGELLAN')
            p.setPen(QColor('#92969d'));p.setFont(QFont('Segoe UI',11));p.drawText(QRect(0,area.height()//2+32,area.width(),32),Qt.AlignCenter,'Choose a folder to get started')
            p.end();self.publish_appearance_frame();self._trim_cache_after_paint()
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
            index,before,*rest=self.drop_indicator;inside=bool(rest[0]) if rest else False
            if first<=index<last:
                if self.mode=='horizontal':
                    r=QRect(10,index*self.row_h-self.verticalScrollBar().value(),self.viewport().width()-20,self.row_h-7)
                    if inside:p.setPen(QPen(QColor('#ffffff'),2));p.setBrush(Qt.NoBrush);p.drawRect(r.adjusted(2,2,-3,-3))
                    else:y=r.top()-4 if before else r.bottom()+4;p.fillRect(QRect(r.left()+2,y-1,max(1,r.width()-4),3),QColor('#ffffff'))
                else:
                    row,col=divmod(index,self.columns);r=QRect(9+col*(self.cell_w+9),row*self.row_h-self.verticalScrollBar().value(),self.cell_w,self.cell_w+48)
                    if inside:p.setPen(QPen(QColor('#ffffff'),2));p.setBrush(Qt.NoBrush);p.drawRect(r.adjusted(2,2,-3,-3))
                    else:x=r.left()-5 if before else r.right()+5;p.fillRect(QRect(x,r.top()+2,3,max(1,r.height()-4)),QColor('#ffffff'))
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
        p.end();self.publish_appearance_frame();self._trim_cache_after_paint()
        if diag is not None:self.record_scroll_paint(diag,e,paint_started,painted_cards,(time.perf_counter()-card_started)*1000 if card_started is not None else 0.0)

    def publish_appearance_frame(self):
        self._appearance_recording=False
        first,last=self.visible_range()
        pixels=getattr(self,'_appearance_card_pixels',{})
        self._appearance_card_pixels={i:value for i,value in pixels.items() if first<=i<last}
        key=(self.verticalScrollBar().value(),self.viewport().size().width(),self.viewport().size().height(),self.mode,tuple((i,value[0],value[1].cacheKey()) for i,value in self._appearance_card_pixels.items()))
        self._appearance_frame_scroll=self.verticalScrollBar().value()
        if key!=getattr(self,'_appearance_frame_key',None):
            self._appearance_frame_key=key
            owner=self.explorer()
            if hasattr(owner,'schedule_appearance_capture'):owner.schedule_appearance_capture()

    def render_appearance_source(self):
        """Reuse the visible frame's cards; never repeat thumbnail discovery."""
        image=QPixmap(self.viewport().size());image.fill(QColor('#080a10'))
        painter=QPainter(image)
        first,last=self.visible_range()
        for index in range(first,min(last,len(self.items))):
            if self.mode=='horizontal':self.paint_horizontal(painter,index)
            else:
                cached=getattr(self,'_appearance_card_pixels',{}).get(index)
                if cached is not None and cached[0]==id(self.items[index]):painter.drawPixmap(self.card_rect(index).topLeft(),cached[1])
        painter.end()
        return image

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
    def folder_image(self, folder,with_image=False,target_size=512,cancel_event=None):
        # Breadth-first-ish: check direct children first, then descend. Stop at the first image.
        try:
            if self._closing or (cancel_event is not None and cancel_event.is_set()):return None
            root=Path(folder)
            def preview(candidate):
                if self._closing or (cancel_event is not None and cancel_event.is_set()):return None
                if not with_image:
                    if candidate.suffix.casefold() in VIDEO_EXTS or self.preview_is_readable(candidate):return str(candidate)
                    return None
                try:image=self.thumbnail_request_worker(str(candidate),target_size) if isinstance(target_size,tuple) else self.thumbnail_worker(str(candidate),target_size,target_size)
                except Exception:
                    logger.debug('Could not decode a pinned folder preview candidate %s',candidate,exc_info=True);return None
                if image is None or image.isNull():return None
                return str(candidate),image
            direct=[]
            for e in os.scandir(root):
                if self._closing or (cancel_event is not None and cancel_event.is_set()):return None
                if e.is_file() and Path(e.name).suffix.lower() in (MEDIA_EXTS|MULTIPAGE_EXTS): direct.append(Path(e.path))
            candidates=[(natural_key(candidate.name),index,candidate) for index,candidate in enumerate(direct)];heapq.heapify(candidates)
            while candidates:
                _,_,candidate=heapq.heappop(candidates);found=preview(candidate)
                if found:return found
            q=[root]
            depth=0
            while q and depth<8:
                nq=[]
                for d in q:
                    if self._closing or (cancel_event is not None and cancel_event.is_set()):return None
                    try:
                        for e in os.scandir(d):
                            if self._closing or (cancel_event is not None and cancel_event.is_set()):return None
                            p=Path(e.path)
                            if e.is_file() and p.suffix.lower() in (MEDIA_EXTS|MULTIPAGE_EXTS):
                                found=preview(p)
                                if found:return found
                            if e.is_dir() and (not e.name.startswith('.') or e.name.startswith('. ')): nq.append(p)
                    except OSError:
                        logger.debug('Could not inspect folder preview directory %s',d,exc_info=True)
                q=nq; depth+=1
        except Exception:
            logger.debug('Could not resolve a folder preview in %s',folder,exc_info=True)
        return None
    def resolve_folder_thumbnail(self,folder):
        """Resolve and persist a pinned preview on a worker, never during paint/poll."""
        if self.is_persistently_kept(folder):
            preview=self.folder_image(folder,with_image=True)
            if preview:
                source,image=preview
                return self.persist_folder_thumb(folder,source,image=image)
            return None
        source=self.folder_image(folder)
        return source
    def resolve_folder_display(self,folder,size,cancel_event):
        kept=self.is_persistently_kept(folder);width,height=size
        cached=self.load_persistent_folder_thumb(folder) if kept else None
        image=self.thumbnail_worker(cached,width,height) if cached and not cancel_event.is_set() else None
        if image is not None and not image.isNull():
            if cancel_event.is_set():return None
            return cached,prepare_thumbnail_pixels(image),size
        preview=self.folder_image(folder,with_image=True,target_size=512 if kept else size,cancel_event=cancel_event)
        if not preview or cancel_event.is_set():return None
        source,image=preview
        if kept:
            source=self.persist_folder_thumb(folder,source,image=image)
            if not source:return None
        if image.width()!=width or image.height()!=height:
            image=image.scaled(QSize(width,height),Qt.KeepAspectRatioByExpanding,Qt.SmoothTransformation);image=image.copy(max(0,(image.width()-width)//2),max(0,(image.height()-height)//2),width,height)
        return source,prepare_thumbnail_pixels(image),size

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
            return any(p==root or p.startswith(root.rstrip(os.sep)+os.sep) for root in roots)
        except (OSError,TypeError,ValueError):
            logger.debug('Could not check pinned-cache membership for %s',folder,exc_info=True)
        return False

    def _persistent_folder_thumb_path(self, folder):
        if not self.persistent_folder_cache_dir: return None
        try:
            identity=(str(self.persistent_folder_cache_dir),str(folder))
            paths=getattr(self,'_persistent_folder_paths',None)
            if paths is None:paths=self._persistent_folder_paths={}
            if identity in paths:return paths[identity]
            key=hashlib.sha1(os.path.normcase(str(Path(folder).resolve())).encode('utf-8')).hexdigest()
            result=Path(self.persistent_folder_cache_dir)/(key+'.jpg')
            if len(paths)>=4096:paths.clear()
            paths[identity]=result
            return result
        except Exception: return None
    def load_persistent_folder_thumb(self, folder):
        p=self._persistent_folder_thumb_path(folder)
        return str(p) if p and p.exists() else None
    def persist_folder_thumb(self, folder, source,image=None):
        p=self._persistent_folder_thumb_path(folder)
        if not p or not source: return source
        temporary=None
        try:
            p.parent.mkdir(parents=True,exist_ok=True)
            img=image if image is not None else self.thumbnail_worker(str(source),512,512)
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
    def persistent_archive_thumb_path(self, source):
        """Return the SSD-cache path for an archive page inside a kept folder."""
        decoded=decode_archive_thumbnail(str(source))
        if not decoded:return None
        archive,member=decoded
        if not self.is_persistently_kept(archive):return None
        try:
            identity=os.path.normcase(os.path.abspath(os.path.normpath(archive)))+'\0'+member
            key=hashlib.sha1(identity.encode('utf-8')).hexdigest()
            return Path(self.persistent_folder_cache_dir)/(key+'.jpg') if self.persistent_folder_cache_dir else None
        except (OSError,TypeError,ValueError):
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
            checks=getattr(self,'_folder_cache_validation',None)
            if checks is None:checks=self._folder_cache_validation={}
            now=time.monotonic();previous=checks.get(key)
            if previous and previous[0]==cached and now-previous[1]<5:return cached
            if len(checks)>=4096:checks.clear()
            checks[key]=(cached,now)
            if self.is_persistently_kept(key) and cached==str(self._persistent_folder_thumb_path(key)) and not Path(cached).is_file():
                self.folder_cache.pop(key,None);checks.pop(key,None)
            else:return cached
        if key in self.folder_cache:return self.folder_cache[key]
        if key in self.folder_futures:
            future=self.folder_futures[key];pool=getattr(future,'_thumbnail_pool',None)
            if pool is not None and not self._prefetching:pool.promote(future)
            return None
        if self.fast_scroll and (self._scroll_speed>1600 or len(self.folder_futures)>=1):return None
        if len(self.folder_futures)>=16:self._thumbnail_backpressure=True;return None
        size=(max(1,self.row_h-15),max(1,self.row_h-22)) if self.mode=='horizontal' else (max(1,self.cell_w-2),max(1,self.cell_w-2));event=threading.Event();self._folder_display_events[key]=event
        fut=self.folder_pool.submit(self.resolve_folder_display,path,size,event,priority=20 if self._prefetching else 0);self.folder_futures[key]=fut;self.watch_thumbnail_future(fut);return None
    def poll_async(self):
        if getattr(self,'_file_cache_dirty',False) and time.monotonic()-getattr(self,'_file_cache_last_trim',0)>5:
            parent=self.explorer()
            if getattr(parent,'disk_thumbnail_cache_limit_enabled',False):
                self._file_cache_dirty=False;self._file_cache_last_trim=time.monotonic();parent.schedule_disk_thumbnail_cache_trim()
        changed_indices=set();sizes_changed=False;resolution_updated=False;thumbnail_deadline=time.perf_counter()+(.0008 if self.fast_scroll else (.0015 if time.monotonic()-self.last_scroll_time<.18 else .003));thumbnail_progress=False;thumbnail_remaining=False;thumbnail_indices=set()
        for key,future in list(self.resolution_futures.items()):
            if not future.done():continue
            self.resolution_futures.pop(key,None)
            try:
                width,height=future.result();resolution=(int(width),int(height)) if width>0 and height>0 else None
            except Exception:
                resolution=None
            self.resolution_cache[key]=resolution;self.resolution_cache.move_to_end(key)
            while len(self.resolution_cache)>512:self.resolution_cache.popitem(last=False)
            for item in self.items_by_path.get(key,()):
                item['resolution']=resolution
            resolution_updated=True
        first,last=self.visible_range() if self.items and getattr(self,'row_h',0)>0 else (0,0);visible_paths={};visible_sources={}
        for i in range(first,min(last,len(self.items))):
            item=self.items[i];path=str(item.get('path',''))
            if path:visible_paths.setdefault(path,[]).append(i)
            source=None
            if item.get('kind') in ('image','video') or (item.get('kind')=='document' and Path(path).suffix.casefold() in MULTIPAGE_EXTS):source=path
            elif item.get('kind')=='favorite_group':
                group_id=path.split('://',1)[-1];preview=item.get('preview_path')
                source=self.folder_custom.get('favgroup:'+group_id) or (self.folder_custom.get(str(preview)) if preview else None) or (self.folder_cache.get(str(preview)) if preview else None)
            elif item.get('is_dir'):source=self.folder_custom.get(path) or self.folder_cache.get(path)
            if source:visible_sources.setdefault(str(source),[]).append(i)
        for key,f in list(self.thumb_futures.items()):
            if f.done():
                if time.perf_counter()>=thumbnail_deadline:thumbnail_remaining=True;break
                thumbnail_progress=True;self.thumb_futures.pop(key,None)
                try:
                    img=f.result()
                    if img is not None and not img.isNull():
                        pixmap=QPixmap.fromImage(img)
                        previous=self.cache.pop(key,None)
                        if previous:self.cache_bytes-=self.pixmap_bytes(previous)
                        self.cache[key]=pixmap;self.cache_bytes+=self.pixmap_bytes(pixmap)
                        while self.cache and self.cache_bytes>self.cache_limit_bytes:
                            _,oldest=self.cache.popitem(last=False);self.cache_bytes-=self.pixmap_bytes(oldest)
                        self.failed_thumbnails.pop(key,None)
                    else:self.remember_failed_thumbnail(key)
                except Exception:
                    logger.exception('Thumbnail worker failed for %s',key[0])
                    self.remember_failed_thumbnail(key)
                thumbnail_indices.update(visible_sources.get(str(key[0]),()))
        for key,f in list(self.folder_futures.items()):
            if f.done():
                if time.perf_counter()>=thumbnail_deadline:thumbnail_remaining=True;break
                thumbnail_progress=True;self.folder_futures.pop(key,None)
                cancel_event=self._folder_display_events.pop(key,None)
                if cancel_event is not None and cancel_event.is_set():
                    changed_indices.update(visible_paths.get(key,()));continue
                if f.cancelled():
                    continue
                try:
                    val=f.result()
                    if isinstance(val,tuple):
                        val,image,size=val;cache_key=(str(val),*size);pixmap=QPixmap.fromImage(image);previous=self.cache.pop(cache_key,None)
                        if previous:self.cache_bytes-=self.pixmap_bytes(previous)
                        self.cache[cache_key]=pixmap;self.cache_bytes+=self.pixmap_bytes(pixmap)
                        while self.cache and self.cache_bytes>self.cache_limit_bytes:
                            _,old=self.cache.popitem(last=False);self.cache_bytes-=self.pixmap_bytes(old)
                    parent=self.explorer()
                    if val and parent is not None and key in getattr(parent,'pinned_folders',set()):
                        parent.pinned_thumbs[key]=val;parent.save_settings();parent.schedule_disk_thumbnail_cache_trim()
                    self.folder_cache[key]=val
                except Exception:
                    logger.exception('Folder thumbnail worker failed for %s',key)
                    self.folder_cache[key]=None
                thumbnail_indices.update(visible_paths.get(key,()))
        for index in thumbnail_indices-changed_indices:
            rect=self.card_rect(index)
            # Thumbnail completion can switch between a deferred and cached card.
            # Repaint the label too, so the old text pixels cannot survive.
            rect=QRect(rect.x()+4,rect.y()+4,rect.height()-8,rect.height()-15) if self.mode=='horizontal' else rect
            self.viewport().update(rect)
        if thumbnail_remaining:self.schedule_thumbnail_poll()
        if thumbnail_progress and self._thumbnail_backpressure:
            self._thumbnail_backpressure=False;self.viewport().update()
        for root,f in list(self.pinned_size_tree_futures.items()):
            if not f.done():continue
            self.pinned_size_tree_futures.pop(root,None);cancel_event=self.pinned_size_tree_events.pop(root,None);targets=self.pinned_size_tree_targets.pop(root,())
            if f.cancelled() or (cancel_event is not None and cancel_event.is_set()):continue
            try:
                measured=f.result() or {};checked_at=time.time();parent=self.explorer();settings_changed=False
                for target in targets:
                    key=os.path.normcase(os.path.abspath(os.path.normpath(target)));raw_size=measured.get(key)
                    if raw_size is None:continue
                    size=max(0,int(raw_size));size_changed=self.folder_size_cache.get(target)!=size
                    self.folder_size_cache[target]=size;self.folder_size_cache_checked_at[target]=checked_at
                    for item in self.items_by_path.get(target,()):
                        if item.get('is_dir'):item['size']=size
                    changed_indices.update(visible_paths.get(target,()))
                    if parent is not None and self.is_persistently_kept(target):
                        parent.pinned_folder_sizes[target]=size;parent.pinned_folder_sizes_checked[target]=checked_at;settings_changed=True
                    sizes_changed=sizes_changed or size_changed
                if settings_changed and parent is not None:parent.save_settings()
            except Exception:
                logger.exception('Kept folder size cache failed for %s',root)
        for futures in (self.folder_size_futures,self.hover_folder_size_futures):
            events=self.folder_size_events if futures is self.folder_size_futures else self.hover_folder_size_events
            for key,f in list(futures.items()):
                if not f.done():continue
                futures.pop(key,None);cancel_event=events.pop(key,None)
                if f.cancelled() or (cancel_event is not None and cancel_event.is_set()):
                    parent=self.explorer()
                    if futures is self.folder_size_futures and not self._closing and getattr(parent,'sort',None)=='size' and key not in self.folder_size_queued:
                        self.folder_size_queue.appendleft(key);self.folder_size_queued.add(key)
                    continue
                try:
                    size=max(0,int(f.result()));size_changed=self.folder_size_cache.get(key)!=size;self.folder_size_cache[key]=size;checked_at=time.time();self.folder_size_cache_checked_at[key]=checked_at
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
                    sizes_changed=sizes_changed or size_changed
                except Exception:
                    logger.exception('Folder size calculation failed for %s',key)
        self.start_next_folder_size()
        if resolution_updated:self.viewport().update()
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
        size_queue_drained=not self.folder_size_futures and not self.folder_size_queued and not self.pinned_size_tree_futures
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
        self.thumbnail_prefetch_timer.stop();self._scroll_direction=1 if value>=getattr(self,'_previous_priority_scroll',value) else -1;self._previous_priority_scroll=value;self._scroll_speed=speed
        if self.fast_scroll and now-getattr(self,'_priority_update_time',0)<.04:return
        self._priority_update_time=now;self.update_scroll_work()

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
            if item.get('kind') in ('image','video') or (item.get('kind')=='document' and Path(item['path']).suffix.casefold() in MULTIPAGE_EXTS):visible_sources.add(str(item['path']))
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
            if key not in visible:
                event=self._folder_display_events.get(key)
                if event is not None:event.set()
            if key not in visible and not f.running():
                self._folder_display_events.pop(key,None)
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
        self.viewport().update();self.thumbnail_prefetch_timer.start()

    def prefetch_directional_thumbnails(self):
        if self._closing or self.fast_scroll or not getattr(self.explorer(),'thumbnail_cache_enabled',True):return
        if self.thumb_futures or self.folder_futures:self.thumbnail_prefetch_timer.start(150);return
        first,last=self.visible_range();count=min(4,max(1,self.columns));indices=range(last,min(len(self.items),last+count)) if self._scroll_direction>0 else range(max(0,first-count),first)
        self._prefetching=True
        try:
            for index in indices:
                if len(self.thumb_futures)+len(self.folder_futures)>=2:break
                item=self.items[index];meta=self.paint_metadata.get(id(item),('',self.label_prefix(item),False));source=self.card_thumbnail_source(item,meta)
                if source:self.pix(source,QSize(max(1,self.row_h-15),max(1,self.row_h-22)) if self.mode=='horizontal' else QSize(max(1,self.cell_w-2),max(1,self.cell_w-2)))
        finally:self._prefetching=False

    def video_thumbnail_worker(self,path,w,h):
        try:
            # Keep the 87 MB ffmpeg executable out of the one-file application
            # archive. Its compressed companion is expanded on first video
            # thumbnail use, never during image-only startup.
            exe=resolve_ffmpeg_executable()
            if not exe:
                try:
                    import imageio_ffmpeg
                    exe=imageio_ffmpeg.get_ffmpeg_exe()
                except Exception:
                    logger.debug('Video thumbnails are unavailable: ffmpeg was not found')
                    return None
            deadline=time.monotonic()+15
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
            archive_member=decode_archive_thumbnail(str(path))
            if archive_member:
                archive_path,member=archive_member
                pdf_page=decode_pdf_page_member(member)
                if pdf_page is not None:return render_pdf_page_thumbnail(archive_path,pdf_page,w,h)
                suffix=Path(member).suffix.casefold()
                if not member:return render_archive_preview(archive_path,'',w,h)
                if member.endswith('/'):
                    return render_archive_preview(archive_path,member,w,h)
                if suffix in COMIC_EXTS:
                    nested_path=materialize_nested_archive(archive_path,member)
                    return render_archive_preview(nested_path,'',w,h)
                data=read_archive_image(archive_path,member);image,_=decode_archive_image(data,Path(member).suffix,max(1,int(w))*2)
                if image.isNull():return None
                tw=max(1,int(w));th=max(1,int(h));image=image.scaled(QSize(tw,th),Qt.KeepAspectRatioByExpanding,Qt.SmoothTransformation)
                return image.copy(max(0,(image.width()-tw)//2),max(0,(image.height()-th)//2),tw,th)
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
            if suffix in COMIC_EXTS:
                return render_archive_preview(path,'',w,h)
            if suffix=='.jxl':
                from PIL import Image
                import pillow_jxl  # noqa: F401 -- registers JPEG XL support with Pillow
                tw=max(1,int(w));th=max(1,int(h))
                with Image.open(path,formats=['JXL']) as source:
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

    def ordinary_file_cache_path(self,source,size):
        if not getattr(self.explorer(),'file_thumbnail_disk_cache_enabled',True) or not self.persistent_folder_cache_dir:return None
        if str(source).startswith(ARCHIVE_THUMB_PREFIX):return None
        try:
            path=Path(source)
            root=os.path.normcase(os.path.abspath(self.persistent_folder_cache_dir));raw=os.path.normcase(os.path.abspath(path))
            if os.path.splitdrive(root)[0]==os.path.splitdrive(raw)[0] and os.path.commonpath((root,raw))==root:return None
            stat=path.stat();identity=f'{raw}\0{stat.st_mtime_ns}\0{stat.st_size}\0{size[0]}x{size[1]}'
            return Path(self.persistent_folder_cache_dir)/('file-'+hashlib.sha256(identity.encode('utf-8')).hexdigest()+'.png')
        except (OSError,ValueError,TypeError):return None

    def schedule_file_cache_write(self,path,image):
        with self._file_cache_write_lock:
            if self._closing or path in self._file_cache_write_jobs or len(self._file_cache_write_jobs)>=32:return
            self._file_cache_write_jobs[path]=None
            try:self._file_cache_write_jobs[path]=self.file_cache_write_pool.submit(self.write_file_cache,path,image)
            except RuntimeError:self._file_cache_write_jobs.pop(path,None)
    def write_file_cache(self,path,image):
        temporary=path.with_name(path.name+'.'+str(threading.get_ident())+'.tmp')
        try:
            if self._closing or not getattr(self.explorer(),'file_thumbnail_disk_cache_enabled',True):return
            path.parent.mkdir(parents=True,exist_ok=True)
            if image.save(str(temporary),'PNG'):
                os.replace(temporary,path);self._file_cache_dirty=True
        except OSError:logger.debug('Could not cache file thumbnail %s',path,exc_info=True)
        finally:
            try:temporary.unlink(missing_ok=True)
            except OSError:pass
            with self._file_cache_write_lock:self._file_cache_write_jobs.pop(path,None)

    def thumbnail_request_worker(self,source,size,persistent=None):
        disk_cache=self.ordinary_file_cache_path(source,size)
        if disk_cache is not None and disk_cache.is_file():
            cached=QImage(str(disk_cache))
            if not cached.isNull():return prepare_thumbnail_pixels(cached)
        image=self.thumbnail_worker(source,*size)
        if disk_cache is not None and image is not None and not image.isNull() and not self._closing:
            self.schedule_file_cache_write(disk_cache,image)
        if persistent is not None and image is not None and not image.isNull():
            temporary=None
            try:
                persistent.parent.mkdir(parents=True,exist_ok=True)
                if not persistent.is_file():
                    temporary=persistent.with_name(persistent.name+'.'+str(threading.get_ident())+'.tmp')
                    if image.save(str(temporary),'JPEG',92):os.replace(temporary,persistent)
            except OSError:logger.debug('Could not persist archive thumbnail %s',persistent,exc_info=True)
            finally:
                if temporary is not None:
                    try:temporary.unlink(missing_ok=True)
                    except OSError:pass
        return prepare_thumbnail_pixels(image)

    def pix(self,path,box):
        if self._closing:return None
        key=(str(path),int(box.width()),int(box.height()))
        if key in self.cache:
            if self._scroll_diagnostic is not None:self._scroll_diagnostic['cache_hits']+=1
            self.cache.move_to_end(key)
            if self._visible_cache_keys is not None:self._visible_cache_keys.add(key)
            return self.cache[key]
        if self._scroll_diagnostic is not None:self._scroll_diagnostic['cache_misses']+=1
        failed=self.failed_thumbnails.get(key)
        if failed is not None:
            if time.monotonic()<failed:
                self.failed_thumbnails.move_to_end(key)
                return None
            # Give a failed decoder a cooldown between attempts so damaged
            # archives cannot spin on every repaint. Changed files and later
            # explicit redraws can retry after the cooldown expires.
            self.failed_thumbnails.pop(key,None)
        if self.fast_scroll and (self._scroll_speed>1600 or len(self.thumb_futures)>=2):return self.cache.get(key)
        if key in self.thumb_futures:
            future=self.thumb_futures[key];pool=getattr(future,'_thumbnail_pool',None)
            if pool is not None and not self._prefetching:pool.promote(future)
        if key not in self.thumb_futures:
            if len(self.thumb_futures)>=64:self._thumbnail_backpressure=True;return None
            persistent=self.persistent_archive_thumb_path(path) if str(path).startswith(ARCHIVE_THUMB_PREFIX) else None
            source=str(persistent) if persistent is not None and persistent.is_file() else str(path)
            # Store kept archive pages at one stable high-quality size so they
            # can be reused across thumbnail layouts without re-decompressing.
            size=(512,512) if persistent is not None and source==str(path) else (int(box.width()),int(box.height()))
            cache_read=False
            cache_root=getattr(self,'persistent_folder_cache_dir',None)
            if cache_root:
                try:
                    root_key=os.path.normcase(os.path.abspath(cache_root));source_key=os.path.normcase(os.path.abspath(source))
                    cache_read=Path(source).is_file() and os.path.commonpath((root_key,source_key))==root_key
                except (OSError,ValueError,TypeError):cache_read=False
            worker_pool=self.cache_read_pool if cache_read else self.pool
            self.thumb_futures[key]=self.watch_thumbnail_future(worker_pool.submit(self.thumbnail_request_worker,source,size,persistent if persistent is not None and source==str(path) else None,priority=20 if self._prefetching else 0))
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
            if source and thumb_pix is None and thumb_key not in self.thumb_futures:
                self.pix(source,QSize(side,side))
            if self._scroll_diagnostic is not None:self._scroll_diagnostic['card_cache_hits']+=1
        else:
            if self._scroll_diagnostic is not None:self._scroll_diagnostic['card_cache_misses']+=1
            estimate=getattr(self,'_card_build_cost',.0003)
            defer=cache_enabled and getattr(self,'_card_budget_enabled',False) and getattr(self,'_cards_built_this_frame',0)>0 and time.perf_counter()+estimate>getattr(self,'_card_build_deadline',float('inf'))
            if defer:
                timer=getattr(self,'_card_repaint_timer',None)
                if timer is None:
                    timer=self._card_repaint_timer=QTimer(self);timer.setSingleShot(True);timer.setInterval(16);timer.timeout.connect(self.viewport().update)
                if not timer.isActive():timer.start()
                previous=getattr(self,'_appearance_card_pixels',{}).get(i)
                if previous and previous[0]==id(it):cached=previous[1]
                else:
                    p.fillRect(r,QColor('#020513'))
                    if thumb_pix is not None:p.drawPixmap(QRect(r.x()+1,r.y()+1,side,side),thumb_pix)
                    p.save();p.translate(r.topLeft());p.setClipRect(QRect(QPoint(0,0),r.size()),Qt.IntersectClip)
                    self.paint_card_label(p,it,r.size(),side,meta);p.restore()
            else:
                started=time.perf_counter();cached=self.render_card_pixmap(i,r.size(),side,source,meta,dpr)
                self._card_build_cost=min(.008,.75*estimate+.25*(time.perf_counter()-started));self._cards_built_this_frame=getattr(self,'_cards_built_this_frame',0)+1
            if cached is not None and cache_enabled and not defer:
                self.card_cache[cache_key]=cached;self.card_cache_bytes+=self.pixmap_bytes(cached)
                self.trim_card_cache()
        if cached is not None:
            p.drawPixmap(r.topLeft(),cached)
            if getattr(self,'_appearance_recording',False):self._appearance_card_pixels[i]=(id(it),cached)
        if getattr(self.explorer(),'file_clipboard_cut',False) and getattr(self.explorer(),'file_clipboard_set',None):
            try:cut_key=os.path.normcase(os.path.abspath(str(it.get('path',''))))
            except (OSError,ValueError,TypeError):cut_key=''
            if cut_key and cut_key in self.explorer().file_clipboard_set:p.fillRect(r,QColor(2,5,19,105))
        if self.image_search_active and getattr(self,'search_hover',-1)==i:
            preview=QRect(r.x()+1,r.y()+1,side,side);p.setPen(QPen(QColor('#ffffff'),3));p.setBrush(Qt.NoBrush);p.drawRect(preview.adjusted(1,1,-1,-1))
    def card_thumbnail_source(self,it,meta):
        if it.get('archive_virtual'):
            if it.get('is_dir'):return encode_archive_thumbnail(it.get('archive_path',''),it.get('archive_member',''))
            if it.get('kind')=='image' or (it.get('kind')=='document' and Path(str(it.get('archive_member',''))).suffix.casefold() in PDF_EXTS):return encode_archive_thumbnail(it.get('archive_path',''),it.get('archive_member',''))
            return None
        suffix=Path(str(it.get('path',''))).suffix.casefold()
        if it['kind']=='document' and suffix in COMIC_EXTS:return encode_archive_thumbnail(it['path'],'')
        if it['kind'] in ('image','video') or (it['kind']=='document' and meta[0] in MULTIPAGE_EXTS):return str(it['path'])
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
        self.paint_card_label(p,it,size,side,meta)
        p.end();return pixmap
    def paint_card_label(self,p,it,size,side,meta):
        # Shared coordinates keep budget-deferred cards identical to cached ones.
        p.save();p.setClipRect(QRect(0,0,size.width(),size.height()),Qt.IntersectClip)
        label_y=side+5
        if it.get('kind')=='favorite_group':
            p.setPen(QColor('#d0a32d'));p.setFont(self.paint_fonts['group']);p.drawText(QRect(7,label_y,18,24),Qt.AlignCenter,'★')
        if it['is_dir']:
            p.save();p.setPen(Qt.NoPen);p.setBrush(QColor('#f1f1f1'));p.drawRoundedRect(QRect(7,label_y+5,13,10),2,2);p.drawRect(QRect(9,label_y+3,5,4));p.restore();text_x=25
        else:text_x=29 if it.get('kind')=='favorite_group' else 7
        if meta[2]:p.drawPixmap(QRect(text_x,label_y+4,16,16),self.image_name_icon);text_x+=20
        p.setPen(QColor('#ddd'));p.setFont(self.paint_fonts['label']);p.drawText(QRect(text_x,label_y,size.width()-(text_x-7)-7,38),Qt.TextWordWrap,meta[1]+it['name'])
        p.restore()
    def paint_horizontal(self,p,i):
        it=self.items[i];y=i*self.row_h-self.verticalScrollBar().value();r=QRect(10,y,self.viewport().width()-20,self.row_h-7);p.setBrush(QColor('#020513'));p.setPen(QPen(QColor('#171a24')));p.drawRect(r)
        box=QRect(r.x()+4,r.y()+4, r.height()-8,r.height()-15)
        paint_meta=self.paint_metadata.get(id(it),('',self.label_prefix(it),False))
        source=self.card_thumbnail_source(it,paint_meta)
        if source:
            px=self.pix(source,box.size())
            if px:
                p.drawPixmap(box,px)
                if getattr(self,'_appearance_recording',False):self._appearance_card_pixels[i]=(id(it),px)
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
        if getattr(self.explorer(),'file_clipboard_cut',False) and getattr(self.explorer(),'file_clipboard_set',None):
            try:cut_key=os.path.normcase(os.path.abspath(str(it.get('path',''))))
            except (OSError,ValueError,TypeError):cut_key=''
            if cut_key and cut_key in self.explorer().file_clipboard_set:p.fillRect(r,QColor(2,5,19,105))
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
        resolution=item.get('resolution')
        resolution_text=(f'Res: {resolution[0]}×{resolution[1]}' if resolution else ('Res: …' if item.get('kind')=='image' else ''))
        lines=(f"Size: {size_text}"+(f' · {resolution_text}' if resolution_text else ''),f"Created: {datefmt(item.get('ctime',0))}")
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
        self.request_item_resolution(item)
        # Archive entries are virtual paths, not disk directories. Scanning
        # them with folder_size_worker can return bogus zeroes and needlessly
        # compete with archive thumbnail decompression.
        if item.get('is_dir') and not item.get('archive_virtual'):self.request_folder_size(item,priority=True)
        self.hover_animation_started=time.monotonic();self.hover_animation_timer.start();self.viewport().update()
    def request_item_resolution(self,item):
        if item.get('kind')!='image' or item.get('archive_virtual'):return
        raw=str(item.get('path',''))
        if not raw:return
        if raw in self.resolution_cache:item['resolution']=self.resolution_cache[raw];return
        if raw in self.resolution_futures:return
        try:
            if not Path(raw).is_file():return
        except (OSError,ValueError,TypeError):return
        def read_size(path):
            reader=QImageReader(path);reader.setDecideFormatFromContent(True);size=reader.size()
            return size.width(),size.height()
        self.resolution_futures[raw]=self.resolution_pool.submit(read_size,raw)
    def advance_hover_animation(self):
        if time.monotonic()-self.hover_animation_started>=.18:self.hover_animation_timer.stop()
        self.viewport().update()
    def set_hover_index(self,index):
        if index==self.hover_index:return
        if index>=0:self.hover_exit_timer.stop()
        self.hover_delay_timer.stop();self.hover_animation_timer.stop();self.hover_index=index;self.hover_animation_started=0
        self.hover_favorite_rect=QRect();self.hover_overlay_rect=QRect();self.hover_tooltip_rect=QRect()
        keep_key=str(self.items[index]['path']) if 0<=index<len(self.items) else None
        for key,future in list(self.resolution_futures.items()):
            if key!=keep_key and future.cancel():self.resolution_futures.pop(key,None)
        # Hover size calculations can be expensive for deep folders. Cancel
        # them as soon as the pointer moves so later tooltip requests stay fast.
        for key,future in list(self.hover_folder_size_futures.items()):
            if key==keep_key:continue
            event=self.hover_folder_size_events.get(key)
            if event:event.set()
            if future.cancel():
                self.hover_folder_size_futures.pop(key,None)
                self.hover_folder_size_events.pop(key,None)
        if getattr(self.explorer(),'sort',None)=='size' and index>=0:
            # Size sorting can enqueue thousands of recursive directory walks.
            # Pause that background stream while the user is inspecting a
            # thumbnail and let the dedicated hover worker handle its size.
            for key,future in list(self.folder_size_futures.items()):
                if key==keep_key:continue
                event=self.folder_size_events.get(key)
                if event:event.set()
                if future.cancel():
                    self.folder_size_futures.pop(key,None);self.folder_size_events.pop(key,None)
                    if key not in self.folder_size_queued:
                        self.folder_size_queue.appendleft(key);self.folder_size_queued.add(key)
        elif getattr(self.explorer(),'sort',None)!='size':
            for key,f in list(self.folder_size_futures.items()):
                if not f.running() and key!=keep_key:
                    f.cancel();self.folder_size_futures.pop(key,None);self.folder_size_events.pop(key,None)
        if index>=0 and getattr(self.explorer(),'hover_details_enabled',True):
            self.hover_delay_timer.start(max(0,int(getattr(self.explorer(),'hover_details_delay',500))))
        elif index<0:self.start_next_folder_size()
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
    def reconcile_selection_order(self,preferred=()):
        selected={str(path) for path in self.selected_paths};ordered=[];seen=set()
        for raw in (*self.selection_order,*preferred):
            key=str(raw)
            if key in selected and key not in seen:ordered.append(key);seen.add(key)
        missing=selected-seen
        if missing:
            for item in self.items:
                key=self.selection_key(item)
                if key in missing:
                    ordered.append(key);missing.remove(key)
                    if not missing:break
        ordered.extend(sorted(missing,key=natural_key));self.selection_order=ordered
    def remember_deselection(self,paths,order=None):
        removed=set(paths)
        if removed:
            original=[str(path) for path in (order if order is not None else self.selection_order) if str(path) in removed]
            original.extend(sorted(removed-set(original),key=natural_key))
            self.selection_undo.append((removed,time.monotonic(),tuple(original)));self.selection_undo=self.selection_undo[-50:]
    def clear_selection(self,remember=True):
        if self.selected_paths:
            if remember:self.remember_deselection(self.selected_paths)
            self.selected_paths.clear();self.selection_order.clear();self.viewport().update()
        self.explorer().update_selection_button()
    def undo_deselection(self):
        if self.selection_undo:
            entry=self.selection_undo.pop();self.selected_paths.update(entry[0]);self.reconcile_selection_order(entry[2] if len(entry)>2 else ())
            self.viewport().update();self.explorer().update_selection_button()
    def toggle_selected_path(self,path):
        key=str(path)
        if key in self.selected_paths:
            self.remember_deselection({key});self.selected_paths.remove(key)
            self.selection_order=[item for item in self.selection_order if item!=key]
        else:self.selected_paths.add(key);self.selection_order.append(key)
        self.viewport().update();self.explorer().update_selection_button()
    def select_all_current(self):
        ordered=list(dict.fromkeys(self.selection_key(item) for item in self.items if self.selectable_item(item) and item.get('path')));keys=set(ordered)
        if keys and keys.issubset(self.selected_paths):
            self.remember_deselection(keys);self.selected_paths.difference_update(keys);self.selection_order=[key for key in self.selection_order if key not in keys]
        else:
            self.selected_paths.update(keys);self.reconcile_selection_order(ordered)
        self.viewport().update();self.explorer().update_selection_button()
    def begin_rect_selection(self,pos):
        self.setFocus();self.selection_dragging=True
        timer=getattr(self.explorer(),'selection_size_timer',None)
        if timer is not None:timer.stop()
        self.selection_scroll_accumulator=0.0;self.selection_drag_moved=False
        self.selection_anchor=QPoint(pos.x(),pos.y()+self.verticalScrollBar().value());self.selection_current=QPoint(self.selection_anchor)
        self.selection_base=set(self.selected_paths);self.selection_base_order=list(self.selection_order);self._selection_last_inside=None
        self.last_mouse_pos=QPoint(pos);self.selection_scroll_timer.start();self.navigation_index=-1;self.set_hover_index(-1)
        self.favorite_picker_delay.stop();self.favorite_picker_key=None;self.viewport().update()
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
        rect=QRect(self.selection_anchor,self.selection_current).normalized();inside=set();inside_order=[]
        for i in self.rect_selection_items(rect):
            item=self.items[i]
            if self.selectable_item(item):
                key=self.selection_key(item);inside.add(key);inside_order.append(key)
        if inside==getattr(self,'_selection_last_inside',None) and self.selected_paths==self.selection_base.symmetric_difference(inside):
            self.viewport().update();return
        self._selection_last_inside=inside
        self.selected_paths=self.selection_base.symmetric_difference(inside);self.selection_order=list(self.selection_base_order);self.reconcile_selection_order(inside_order);self.viewport().update();self.explorer().update_selection_button()
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
            self.selected_paths=set(self.selection_base);self.selection_order=list(self.selection_base_order);self.reconcile_selection_order()
            i=self.hit(pos)
            if 0<=i<len(self.items) and self.selectable_item(self.items[i]):self.toggle_selected_path(self.selection_key(self.items[i]))
        else:self.remember_deselection(self.selection_base-self.selected_paths,self.selection_base_order)
        self.selection_dragging=False;self.selection_scroll_timer.stop();self.selection_anchor=None;self.selection_current=None;self.selection_base.clear();self.selection_base_order.clear();self._selection_last_inside=None;self.viewport().update();self.explorer().update_selection_button()
    def deselect_paths(self,paths):
        keys={str(path) for path in paths};removed=self.selected_paths&keys
        if removed:
            self.remember_deselection(removed);self.selected_paths.difference_update(removed);self.selection_order=[key for key in self.selection_order if key not in removed]
            self.viewport().update();self.explorer().update_selection_button()
    def ensure_navigation_visible(self,index):
        if index<0 or index>=len(self.items):return
        top=(index*self.row_h if self.mode=='horizontal' else (index//self.columns)*self.row_h);bottom=top+self.row_h;bar=self.verticalScrollBar()
        if top<bar.value():bar.setValue(top)
        elif bottom>bar.value()+self.viewport().height():bar.setValue(max(0,bottom-self.viewport().height()))
        self.viewport().update()
    def handle_navigation_key(self,e):
        key=e.key();explorer=self.explorer()
        if e.modifiers()&Qt.ControlModifier:
            if key==Qt.Key_X:explorer.copy_selected_files(cut=True);return True
            if key==Qt.Key_C:explorer.copy_selected_files(cut=False);return True
            if key==Qt.Key_V:explorer.paste_files();return True
            if key==Qt.Key_E:explorer.extract_selected_files();return True
        if key==Qt.Key_Escape and self.selected_paths:self.clear_selection();return True
        if key==Qt.Key_Z and e.modifiers()&Qt.ControlModifier:explorer.undo_last_action();return True
        if key==Qt.Key_S and e.modifiers()&Qt.ControlModifier: self.undo_deselection();return True
        if key==Qt.Key_R and e.modifiers()&Qt.ShiftModifier and not e.modifiers()&(Qt.ControlModifier|Qt.AltModifier):
            selected=list(self.selected_paths)
            if len(selected)==1:self.begin_inline_rename(selected[0])
            return True
        if key==Qt.Key_Q and not e.modifiers()&(Qt.ControlModifier|Qt.AltModifier):explorer.toggle_search_navigation_focus();return True
        if key==Qt.Key_Tab:
            count=explorer.tabbar.count()
            if count:explorer.tabbar.setCurrentIndex((explorer.tabbar.currentIndex()+(-1 if e.modifiers()&Qt.ShiftModifier else 1))%count)
            return True
        if Qt.Key_1<=key<=Qt.Key_7 and not e.modifiers()&(Qt.ControlModifier|Qt.AltModifier|Qt.ShiftModifier):explorer.set_sort(('name','mtime','ctime','type','size','random','manual')[key-Qt.Key_1]);return True
        if key==Qt.Key_Space and self.selected_paths:self.clear_selection();return True
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
        return super().focusNextPrevChild(next)

    def eventFilter(self,obj,e):
        editor=getattr(self,'inline_rename_editor',None)
        if obj is editor:
            if e.type()==QEvent.MouseButtonPress and e.button()==Qt.RightButton and e.modifiers()&Qt.ControlModifier:
                path=getattr(self,'inline_rename_path',None);index=next((i for i,item in enumerate(self.items) if str(item.get('path',''))==path),-1)
                if index>=0:self.show_item_context_menu(index,e.globalPosition().toPoint())
                e.accept();return True
            if e.type()==QEvent.KeyPress and e.key()==Qt.Key_Escape:
                self.cancel_inline_rename();e.accept();return True
            if e.type()==QEvent.KeyPress and e.key() in (Qt.Key_Return,Qt.Key_Enter):
                self.commit_inline_rename(restore_focus=True);e.accept();return True
            if e.type()==QEvent.FocusOut:QTimer.singleShot(0,self.commit_inline_rename)
        if obj is self.viewport():
            if e.type()==QEvent.KeyPress and self.handle_navigation_key(e):e.accept();return True
            if e.type()==QEvent.KeyRelease and e.key() in (Qt.Key_Left,Qt.Key_Right,Qt.Key_Up,Qt.Key_Down):self.navigation_key=None
            if e.type()==QEvent.MouseButtonPress and e.button()==Qt.LeftButton:
                pos=e.position().toPoint();mods=e.modifiers()
                if mods&Qt.AltModifier:
                    i=self.hit(pos)
                    if 0<=i<len(self.items) and self.items[i].get('kind')!='favorite_group':self.explorer().reveal_in_folder(self.items[i].get('archive_path') or self.items[i]['path'])
                    else:self.clear_selection()
                    e.accept();return True
                if mods&Qt.ShiftModifier:self.begin_rect_selection(pos);e.accept();return True
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
                if index>=0:self.set_hover_index(index)
                elif self.hover_index>=0 and not self.hover_exit_timer.isActive():self.hover_exit_timer.start()
                if self.image_search_active and self.search_hover!=index:
                    self.search_hover=index;self.viewport().update()
                return False
            if e.type()==QEvent.Leave:
                menu=getattr(self.explorer(),'favorite_picker_menu',None)
                cursor=QCursor.pos()
                if menu is not None and menu.isVisible() and (menu.geometry().contains(cursor) or getattr(self.explorer(),'favorite_picker_source_rect',QRect()).adjusted(-2,-2,2,2).contains(cursor)):
                    return False
                # QAbstractScrollArea sends Leave when the pointer transfers
                # between its viewport and child scrollbar. Keep the active
                # tooltip alive during that internal transition.
                grid_rect=QRect(self.mapToGlobal(QPoint(0,0)),self.size())
                if grid_rect.contains(cursor):return False
                self.favorite_picker_delay.stop();self.favorite_picker_hover_key=None
                if self.hover_index>=0 and not self.hover_exit_timer.isActive():self.hover_exit_timer.start()
                self.search_hover=-1;self.viewport().update();return False
            if e.type() in (QEvent.DragEnter,QEvent.DragMove):
                if getattr(self.explorer(),'favorite_organize_mode',False) and e.mimeData().hasText():
                    pos=e.position().toPoint();self.organization_drag_pos=QPoint(pos);self.update_organization_drop_target(pos)
                    if not self.organization_scroll_timer.isActive():self.organization_scroll_timer.start()
                    e.acceptProposedAction();return True
                return False
            if e.type()==QEvent.Drop:
                indicator=self.drop_indicator or (self.hit(e.position().toPoint()),True,False);self.organization_scroll_timer.stop();self.drop_target_label.hide();self.explorer().handle_favorite_drop(e.mimeData().text(),indicator[0],indicator[1],inside=bool(len(indicator)>2 and indicator[2]));self.drop_indicator=None;self.viewport().update();e.acceptProposedAction();return True
            if e.type()==QEvent.DragLeave:self.organization_scroll_timer.stop();self.drop_target_label.hide();self.drop_indicator=None;self.viewport().update();return False
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
        card=self.card_rect(self.drag_source_index)
        if self.mode=='horizontal':ghost_rect=QRect(card.x()+4,card.y()+4,max(1,card.height()-8),max(1,card.height()-15))
        else:ghost_rect=QRect(card.x()+1,card.y()+1,max(1,min(card.width()-2,self.cell_w-2)),max(1,min(card.width()-2,self.cell_w-2)))
        ghost_rect=ghost_rect.intersected(self.viewport().rect());ghost=self.viewport().grab(ghost_rect) if not ghost_rect.isEmpty() else QPixmap()
        if ghost.isNull():ghost=self.favorite_drag_pixmap(item)
        dpr=max(.1,float(ghost.devicePixelRatioF()));self.drag_ghost_size=QSize(max(1,round(ghost.width()/dpr)),max(1,round(ghost.height()/dpr)));self.drag_ghost_hotspot=QPoint(self.drag_ghost_size.width()//2,self.drag_ghost_size.height()//2)
        if not ghost.isNull():
            scale=.68;scaled=ghost.scaled(max(1,round(ghost.width()*scale)),max(1,round(ghost.height()*scale)),Qt.KeepAspectRatio,Qt.SmoothTransformation);scaled.setDevicePixelRatio(dpr);self.drag_ghost_size=QSize(max(1,round(scaled.width()/dpr)),max(1,round(scaled.height()/dpr)));self.drag_ghost_hotspot=QPoint(self.drag_ghost_size.width()//2,self.drag_ghost_size.height()//2);faded=QPixmap(scaled.size());faded.setDevicePixelRatio(dpr);faded.fill(Qt.transparent);p=QPainter(faded);p.setOpacity(.56);p.drawPixmap(0,0,scaled);p.end();ghost=faded
        if not ghost.isNull():drag.setPixmap(ghost);drag.setHotSpot(self.drag_ghost_hotspot)
        explorer=self.explorer()
        ghost_timer=QTimer(self);ghost_timer.setInterval(8);ghost_timer.timeout.connect(lambda:self.update_organization_drop_target(self.viewport().mapFromGlobal(QCursor.pos())));ghost_timer.start()
        try:drag.exec(Qt.MoveAction)
        finally:
            ghost_timer.stop();ghost_timer.deleteLater()
            self.last_favorite_drag=time.monotonic();self.drag_source_index=-1;self.drag_started=False;self.drop_indicator=None;self.organization_scroll_timer.stop();self.drop_target_label.hide();self.viewport().update()
            if getattr(explorer,'ctrl_organization_held',False) and not (QApplication.keyboardModifiers()&Qt.ControlModifier):
                explorer.ctrl_key_down=False;explorer.ctrl_organization_held=False;explorer.set_favorite_organization_mode(False)

    def update_organization_drop_target(self,pos):
        index=self.hit(pos)
        if index<0 or index>=len(self.items):
            self.drop_indicator=None;self.drop_target_label.hide();self.viewport().update();return
        rect=self.card_rect(index);fraction=(pos.x()-rect.left())/max(1,rect.width());item=self.items[index]
        inside=bool(item.get('is_dir') and item.get('kind')!='favorite_group' and .2<=fraction<=.8 and not getattr(self.explorer(),'in_favorites_view',False) and not getattr(self.explorer(),'in_archive_view',False))
        if inside:
            target_key=os.path.normcase(os.path.abspath(str(item.get('path',''))));source_item=str(self.items[self.drag_source_index].get('path','')) if 0<=getattr(self,'drag_source_index',-1)<len(self.items) else '';sources=[str(path) for path in self.selected_paths if not str(path).startswith('favgroup://')]
            if source_item and source_item not in sources:sources=[source_item]
            for source_path in sources:
                try:
                    source=Path(source_path);source_key=os.path.normcase(os.path.abspath(str(source)))
                    if source_key==target_key or (source.is_dir() and os.path.commonpath((source_key,target_key))==source_key):inside=False;break
                except (OSError,ValueError,TypeError):continue
        before=fraction<.5
        self.drop_indicator=(index,before,inside)
        if inside:
            label=self.drop_target_label;ghost_size=getattr(self,'drag_ghost_size',QSize(max(140,rect.width()),120));ghost_width=max(96,min(self.viewport().width()-8,ghost_size.width()));label.setAlignment(Qt.AlignCenter);label.setFixedWidth(ghost_width);font=label.fontMetrics();name=font.elidedText(str(item.get('name','folder')),Qt.ElideMiddle,max(30,ghost_width-68));label.setText('Move to '+name);label.setFixedHeight(25);x=max(4,min(self.viewport().width()-ghost_width-4,pos.x()-getattr(self,'drag_ghost_hotspot',QPoint()).x()));y=max(4,min(self.viewport().height()-label.height()-4,pos.y()-getattr(self,'drag_ghost_hotspot',QPoint()).y()+ghost_size.height()+1));label.move(x,y);label.show();label.raise_()
        else:self.drop_target_label.hide()
        self.viewport().update()
    def auto_scroll_organization(self):
        if not self.organization_scroll_timer.isActive():return
        pos=self.organization_drag_pos;y=pos.y();height=max(1,self.viewport().height());edge=240;speed=0.0
        if y<edge:
            intensity=max(0.0,min(1.0,(edge-y)/edge));speed=-(.25+159.75*intensity**2.2)
        elif y>height-edge:
            intensity=max(0.0,min(1.0,(y-(height-edge))/edge));speed=.25+159.75*intensity**2.2
        self.organization_scroll_accumulator+=speed;delta=int(self.organization_scroll_accumulator)
        if delta:
            self.organization_scroll_accumulator-=delta;bar=self.verticalScrollBar();bar.setValue(max(bar.minimum(),min(bar.maximum(),bar.value()+delta)));self.update_organization_drop_target(pos)

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

    def name_area_rect(self,index):
        if not 0<=index<len(self.items):return QRect()
        item=self.items[index];rect=self.card_rect(index);meta=self.paint_metadata.get(id(item),('',self.label_prefix(item),False))
        if self.mode=='horizontal':
            box=QRect(rect.x()+4,rect.y()+4,rect.height()-8,rect.height()-15);text_x=box.right()+15+(20 if item.get('is_dir') else 0)+(20 if meta[2] else 0)
            return QRect(text_x,rect.y()+9,max(1,rect.right()-text_x-8),32)
        text_x=25 if item.get('is_dir') else (29 if item.get('kind')=='favorite_group' else 7)
        if meta[2]:text_x+=20
        return QRect(rect.x()+text_x,rect.y()+self.cell_w+5,max(1,rect.width()-text_x-8),38)

    def begin_inline_rename(self,raw_path,new_folder=False):
        path=str(raw_path)
        if getattr(self,'inline_rename_path',None):
            self.inline_rename_editor.setFocus(Qt.OtherFocusReason);self.inline_rename_editor.selectAll();return
        index=next((i for i,item in enumerate(self.items) if str(item.get('path',''))==path),-1)
        if index<0:return
        item=self.items[index]
        if item.get('archive_virtual') or item.get('kind')=='favorite_group':return
        if not hasattr(self,'inline_rename_editor'):
            self.inline_rename_editor=QLineEdit(self.viewport());self.inline_rename_editor.setObjectName('thumbnailInlineRename');self.inline_rename_editor.setStyleSheet('QLineEdit{background:rgba(8,10,14,245);color:#f2f2f2;border:1px solid rgba(255,255,255,190);padding:2px 4px;font-size:11px;}');self.inline_rename_editor.setContextMenuPolicy(Qt.PreventContextMenu);self.inline_rename_editor.installEventFilter(self);self.inline_rename_editor.hide()
        self.inline_rename_path=path;self.inline_rename_new_folder=bool(new_folder);self.inline_rename_editor.setText(Path(path).name);self.inline_rename_editor.setGeometry(self.name_area_rect(index));self.inline_rename_editor.show();self.inline_rename_editor.raise_();self.inline_rename_editor.setFocus(Qt.ShortcutFocusReason);self.inline_rename_editor.selectAll()

    def cancel_inline_rename(self):
        editor=getattr(self,'inline_rename_editor',None);path=getattr(self,'inline_rename_path',None);was_new=getattr(self,'inline_rename_new_folder',False)
        if editor is not None:editor.hide()
        self.inline_rename_path=None;self.inline_rename_new_folder=False
        if was_new and path:
            explorer=self.explorer()
            if getattr(explorer,'pending_new_folder_path',None)==path:explorer.pending_new_folder_path=None;explorer.refresh()

    def commit_inline_rename(self,restore_focus=False):
        path=getattr(self,'inline_rename_path',None);editor=getattr(self,'inline_rename_editor',None)
        if not path or editor is None:return
        name=editor.text();was_new=getattr(self,'inline_rename_new_folder',False)
        result=self.explorer().apply_inline_rename(path,name,new_folder=was_new)
        if not result:
            editor.setFocus(Qt.OtherFocusReason);return
        editor.hide();self.inline_rename_path=None;self.inline_rename_new_folder=False
        if restore_focus:self.setFocus(Qt.OtherFocusReason)

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
            if e.modifiers()&Qt.ControlModifier:
                index=self.hit(e.position().toPoint())
                self.show_item_context_menu(index,e.globalPosition().toPoint())
                e.accept();return
            if self.image_search_active:
                self.set_image_search_active(False);explorer.image_search_mode=False
                if hasattr(explorer,'status'):explorer.status.setText('Image search off')
                e.accept();return
            if getattr(explorer,'favorite_organize_mode',False):
                explorer.set_favorite_organization_mode(False);e.accept();return
            self.goBack.emit();e.accept();return
        if e.button()==Qt.MiddleButton:
            i=self.hit(e.position().toPoint())
            if i>=0 and (self.items[i].get('is_dir') or Path(str(self.items[i].get('path',''))).suffix.casefold() in PREVIEW_EXTS):
                self.middleOpenItem.emit(str(self.items[i]['path']))
            e.accept();return
        if e.button()==Qt.LeftButton:
            i=self.hit(e.position().toPoint())
            if i>=0:
                if e.modifiers()&Qt.AltModifier:
                    self.explorer().reveal_in_folder(str(self.items[i].get('archive_path') or self.items[i]['path']));e.accept();return
                if getattr(self.explorer(),'in_favorites_view',False) and not getattr(self.explorer(),'favorite_organize_mode',False) and self.items[i].get('kind')=='favorite_group' and self.group_name_hit(self.items[i],e.position().toPoint()):
                    self.favorite_click_timer.stop();self.pending_group_rename_id=str(self.items[i]['path']).split('://',1)[-1];self.group_rename_timer.start(QApplication.doubleClickInterval()+80);e.accept();return
                if getattr(self.explorer(),'favorite_organize_mode',False):self.drag_source_index=i;self.drag_start=e.position().toPoint();self.drag_started=False;e.accept();return
                if self.name_hit(i,e.position().toPoint()) and self.items[i].get('kind')!='favorite_group':
                    path=str(self.items[i]['path'])
                    if self.items[i].get('archive_virtual'):self.explorer().open(path);e.accept();return
                    if self.items[i].get('is_dir'):self.explorer().reveal(path)
                    else:self.explorer().reveal_in_folder(path)
                    e.accept();return
                self.openItem.emit(str(self.items[i]['path']))
            e.accept();return
        super().mousePressEvent(e)
    def show_item_context_menu(self,index,global_pos):
        item=self.items[index] if 0<=index<len(self.items) else None
        if item is not None and item.get('kind')=='favorite_group':return
        raw=str(item.get('path','')) if item is not None else ''
        explorer=self.explorer();selected=set(self.selected_paths)
        if raw and raw in selected:targets=[str(path) for path in self.selection_order if str(path) in selected]
        elif raw:targets=[raw]
        else:targets=[str(path) for path in self.selection_order if str(path) in selected]
        paths=[]
        for target in targets:
            decoded=decode_archive_path(target)
            if decoded:
                if not decoded[2] and decode_pdf_page_member(decoded[1]) is None:paths.append(target)
            else:
                try:
                    if Path(target).exists():paths.append(target)
                except (OSError,TypeError,ValueError):continue
        menu=AppearanceMenu(self);menu.configure(explorer)
        undo=menu.addAction('Undo');undo.setShortcut('Ctrl+Z');undo.setEnabled(bool(getattr(explorer,'undo_history',[])));undo.triggered.connect(lambda checked=False:explorer.undo_last_action())
        menu.addSeparator()
        cut=menu.addAction('Cut');cut.setShortcut('Ctrl+X');cut.setEnabled(bool(paths) and any(not decode_archive_path(path) for path in paths));cut.triggered.connect(lambda checked=False,items=list(paths):explorer.copy_paths(items,cut=True))
        copy_action=menu.addAction('Copy');copy_action.setShortcut('Ctrl+C');copy_action.setEnabled(bool(paths));copy_action.triggered.connect(lambda checked=False,items=list(paths):explorer.copy_paths(items,cut=False))
        paste_target=Path(raw) if item is not None and item.get('is_dir') and not item.get('archive_virtual') else explorer.current
        paste=menu.addAction('Paste');paste.setShortcut('Ctrl+V');paste.setEnabled(bool(getattr(explorer,'file_clipboard',[])) and paste_target.is_dir() and not getattr(explorer,'in_favorites_view',False) and not getattr(explorer,'in_archive_view',False));paste.triggered.connect(lambda checked=False,target=paste_target:explorer.paste_files(target))
        menu.addSeparator()
        deletable=[path for path in paths if decode_archive_path(path) is None]
        delete_action=menu.addAction('Delete');delete_action.setShortcut('Shift+Delete');delete_action.setEnabled(bool(deletable));delete_action.setToolTip('Send the selected item(s) to the Recycle Bin');delete_action.triggered.connect(lambda checked=False,items=list(deletable):explorer.send_paths_to_trash(items))
        select_all=menu.addAction('Select All');select_all.setShortcut('Ctrl+A');select_all.triggered.connect(lambda checked=False:self.select_all_current())
        menu.addSeparator()
        rename=menu.addAction('Rename');rename.setEnabled(len(paths)==1 and not decode_archive_path(paths[0]));rename.triggered.connect(lambda checked=False,path=paths[0] if paths else '':self.begin_inline_rename(path) if path else None)
        leave_timer=QTimer(menu);leave_timer.setInterval(55)
        def inside_menu_tree(current,point):
            if current.isVisible() and current.frameGeometry().contains(point):return True
            for action in current.actions():
                child=action.menu()
                if child is not None and child.isVisible() and inside_menu_tree(child,point):return True
            return False
        entered={'value':False};opened=time.monotonic()
        def check_menu_leave():
            inside=inside_menu_tree(menu,QCursor.pos())
            if inside:entered['value']=True
            elif entered['value'] or time.monotonic()-opened>1.0:menu.close()
        leave_timer.timeout.connect(check_menu_leave)
        menu.aboutToShow.connect(leave_timer.start);menu.aboutToHide.connect(leave_timer.stop)
        menu.exec(global_pos)
    def mouseReleaseEvent(self,e):
        if e.button()==Qt.LeftButton and self.drag_source_index>=0:
            self.drag_source_index=-1
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
        if e.button()==Qt.LeftButton and self.hit(e.position().toPoint())<0:
            self.clear_selection();e.accept();return
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

class AppearanceMenuBar(QMenuBar):
    def paintEvent(self,event):
        super().paintEvent(event)
        action=self.actionAt(self.mapFromGlobal(QCursor.pos())) if self.rect().contains(self.mapFromGlobal(QCursor.pos())) else self.activeAction()
        if action is None:return
        if not self.rect().contains(self.mapFromGlobal(QCursor.pos())) and not (action.menu() and action.menu().isVisible()):return
        painter=QPainter(self);rect=self.actionGeometry(action);painter.fillRect(rect,getattr(self,'accent_color',QColor('#34363b')));painter.setPen(getattr(self,'text_color',QColor('#ffffff')));painter.setFont(self.font());painter.drawText(rect,Qt.AlignCenter|Qt.TextShowMnemonic,action.text());painter.end()
    def mouseMoveEvent(self,event):
        super().mouseMoveEvent(event);self.update()
    def leaveEvent(self,event):
        super().leaveEvent(event);self.update()

class FixedTreeTitleBar(QWidget):
    """Consume title gestures instead of forwarding them to QDockWidget."""
    def mousePressEvent(self,event):event.accept()
    def mouseMoveEvent(self,event):event.accept()
    def mouseReleaseEvent(self,event):event.accept()
    def mouseDoubleClickEvent(self,event):event.accept()

class AppearanceMenu(QMenu):
    """Paint the sampled background separately so menu text remains sharp."""
    def __init__(self,*args):
        super().__init__(*args)
        # Effects are composited from a desktop snapshot, not native window alpha.
        # A raster popup stays visible above an OpenGL-composited main window.
        self.setAttribute(Qt.WA_TranslucentBackground,False)
        self.setAttribute(Qt.WA_NoSystemBackground,False)
        self.setAttribute(Qt.WA_OpaquePaintEvent,True)
        self._sample=QPixmap();self._blurred=QPixmap();self._blur_key=None;self._owner=None
    def configure(self,owner):
        self._owner=owner
        accent=QColor(getattr(owner,'appearance_menus_accent_color','') or '#d0a32d');accent.setHsv(int(owner.appearance_menus_accent_hue)%360,max(1,accent.saturation()),min(255,round(accent.value()*owner.appearance_menus_accent_brightness/100)))
        css=f'QMenu{{background:transparent;color:{owner.appearance_font_color};border:1px solid #4b4e53;}} QMenu::item{{background:transparent;padding:7px 24px 7px 10px;}} QMenu::item:selected{{background:{accent.name()};}}'
        if self.styleSheet()!=css:self.setStyleSheet(css)
        self.update()
    def showEvent(self,event):
        screen=self.screen() or QApplication.primaryScreen()
        if screen is not None:self._sample=screen.grabWindow(0,self.x(),self.y(),self.width(),self.height());self._blur_key=None
        super().showEvent(event)
    def paintEvent(self,event):
        owner=self._owner
        if owner is None:return super().paintEvent(event)
        painter=QPainter(self)
        # Keep a readable background even if Windows cannot capture the desktop.
        painter.fillRect(self.rect(),QColor(owner.appearance_menus_background_color))
        radius=int(owner.appearance_menus_blur);key=(self._sample.cacheKey(),radius,self.size().width(),self.size().height())
        if key!=self._blur_key:
            self._blurred=owner.blur_appearance_sample(self._sample,radius);self._blur_key=key
        if not self._blurred.isNull():painter.drawPixmap(self.rect(),self._blurred)
        def adjusted(raw,hue,brightness):
            color=QColor(raw);color.setHsv(int(hue)%360,color.saturation(),max(0,min(255,round(color.value()*brightness/100))));return color
        base=adjusted(owner.appearance_menus_background_color,owner.appearance_menus_background_hue,owner.appearance_menus_background_brightness);base.setAlpha(round(255*(100-owner.appearance_menus_transparency)/100));painter.fillRect(self.rect(),base)
        raw=owner.appearance_menus_color;area=owner.appearance_menus_gradient_area
        if raw and area>0:
            color=adjusted(raw,owner.appearance_menus_gradient_hue,owner.appearance_menus_gradient_brightness);color.setAlpha(round(255*(100-owner.appearance_menus_gradient_transparency)/100));clear=QColor(color);clear.setAlpha(0);angle=math.radians(owner.appearance_menus_gradient_rotation);span=max(.005,area/200);cx=owner.appearance_menus_gradient_distance/100;cy=1-owner.appearance_menus_gradient_position_y/100;dx=math.cos(angle)*span;dy=math.sin(angle)*span;gradient=QLinearGradient((cx-dx)*self.width(),(cy-dy)*self.height(),(cx+dx)*self.width(),(cy+dy)*self.height());gradient.setColorAt(0,clear);gradient.setColorAt(.5,color);gradient.setColorAt(1,clear);painter.fillRect(self.rect(),gradient)
        for action in self.actions():
            if not action.isVisible():continue
            option=QStyleOptionMenuItem();self.initStyleOption(option,action);option.rect=self.actionGeometry(action);self.style().drawControl(QStyle.CE_MenuItem,option,painter,self)
        painter.setPen(QColor('#4b4e53'));painter.setBrush(Qt.NoBrush);painter.drawRect(self.rect().adjusted(0,0,-1,-1));painter.end()

class AppearanceSlider(QSlider):
    """Appearance controls use predictable five-point wheel increments."""
    def wheelEvent(self,event):
        delta=event.angleDelta().y()
        if not delta:
            event.ignore();return
        notches=max(1,round(abs(delta)/120))
        self.setValue(self.value()+(5*notches if delta>0 else -5*notches))
        event.accept()

class ViewerZoomSlider(QSlider):
    """A viewer zoom track that jumps directly to the clicked level."""
    def __init__(self,orientation,parent=None):
        super().__init__(orientation,parent);self._track_drag_active=False
    def _handle_rect(self):
        option=QStyleOptionSlider();self.initStyleOption(option)
        return self.style().subControlRect(QStyle.CC_Slider,option,QStyle.SC_SliderHandle,self)
    def _value_at(self,position):
        option=QStyleOptionSlider();self.initStyleOption(option)
        groove=self.style().subControlRect(QStyle.CC_Slider,option,QStyle.SC_SliderGroove,self)
        handle=self.style().subControlRect(QStyle.CC_Slider,option,QStyle.SC_SliderHandle,self)
        span=max(1,groove.width()-handle.width())
        offset=max(0,min(span,position.x()-groove.left()-handle.width()//2))
        return QStyle.sliderValueFromPosition(self.minimum(),self.maximum(),offset,span,option.upsideDown)
    def mousePressEvent(self,event):
        position=event.position().toPoint()
        if event.button()==Qt.LeftButton and not self._handle_rect().contains(position):
            self._track_drag_active=True;self.setSliderDown(True);self.setValue(self._value_at(position));event.accept();return
        super().mousePressEvent(event)
    def mouseMoveEvent(self,event):
        if self._track_drag_active and event.buttons()&Qt.LeftButton:
            self.setValue(self._value_at(event.position().toPoint()));event.accept();return
        super().mouseMoveEvent(event)
    def mouseReleaseEvent(self,event):
        if self._track_drag_active and event.button()==Qt.LeftButton:
            self.setValue(self._value_at(event.position().toPoint()));self._track_drag_active=False;self.setSliderDown(False);event.accept();return
        super().mouseReleaseEvent(event)

class AppearancePaletteButton(QPushButton):
    doubleClicked=Signal()
    def mouseDoubleClickEvent(self,event):
        self.doubleClicked.emit();event.accept()

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
    closeRequested=Signal(int);dragStarted=Signal();dragFinished=Signal();scrollChanged=Signal()
    def __init__(self,parent=None):super().__init__(parent);self._last_middle_pos=None;self._last_middle_time=0.0;self._hovered_tab=-1;self.selected_tabs=set();self.selection_anchor=-1;self._wheel_remainder=0;self._colored_tab_indices={};self._press_pos=None;self._press_tab=-1;self._dragging_tabs=False;self.setMouseTracking(True);self.setUsesScrollButtons(True);QTimer.singleShot(0,self.bind_scroll_buttons)
    def bind_scroll_buttons(self):
        for button in self.findChildren(QToolButton):
            if button.arrowType() not in (Qt.LeftArrow,Qt.RightArrow) or button.property('magellan_scroll_bound'):continue
            button.setProperty('magellan_scroll_bound',True);button.clicked.connect(self.report_scroll_changed)
    def report_scroll_changed(self):QTimer.singleShot(0,self.scrollChanged.emit)
    def setTabData(self,index,data):
        super().setTabData(index,data)
        if isinstance(data,str) and data:self._colored_tab_indices[index]=data
        else:self._colored_tab_indices.pop(index,None)
        self.update(self.tabRect(index))
    def wheelEvent(self, e):
        self.bind_scroll_buttons()
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
        super().paintEvent(e);self.bind_scroll_buttons()
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



class SearchMarkerScrollBar(QScrollBar):
    def __init__(self,orientation,parent=None):
        super().__init__(orientation,parent);self.search_markers=[]
    def set_search_markers(self,positions):
        markers=set()
        for value in positions:
            if not isinstance(value,(int,float)) or not math.isfinite(float(value)):continue
            markers.add(round(max(0.0,min(1.0,float(value))),5))
        self.search_markers=sorted(markers);self.update()
    def paintEvent(self,event):
        super().paintEvent(event)
        if not self.search_markers:return
        painter=QPainter(self);painter.setRenderHint(QPainter.Antialiasing,False);painter.setPen(QPen(QColor('#ffd84a'),2))
        if self.orientation()==Qt.Vertical:
            span=max(1,self.height()-4)
            for value in self.search_markers:
                y=2+round(value*span);painter.drawLine(1,y,max(2,self.width()-2),y)
        else:
            span=max(1,self.width()-4)
            for value in self.search_markers:
                x=2+round(value*span);painter.drawLine(x,1,x,max(2,self.height()-2))
        painter.end()

class MediaCanvas(QWidget):
    """Clean image surface with controls that appear only near their hit areas."""
    def __init__(self, viewer):
        super().__init__(viewer);self.viewer=viewer;self.setMouseTracking(True);self.setFocusPolicy(Qt.StrongFocus);self.setAttribute(Qt.WA_OpaquePaintEvent,True);self.hover_zone='';self.metadata_rect=QRect(16,12,320,58);self.metadata_progress=0.0;self.metadata_target=0.0;self.metadata_created_cache=OrderedDict();self.metadata_animation_timer=QTimer(self);self.metadata_animation_timer.setInterval(16);self.metadata_animation_timer.timeout.connect(self.advance_metadata_animation);icon_dir=getattr(viewer.application_owner(),'icon_dir',None);icon_path=Path(icon_dir)/'sidebyside.png' if icon_dir else None;self.sidebyside_icon=QPixmap(str(icon_path)) if icon_path and icon_path.is_file() else QPixmap();compare_icon_path=Path(icon_dir)/'compare.png' if icon_dir else None;self.compare_icon=QPixmap(str(compare_icon_path)) if compare_icon_path and compare_icon_path.is_file() else QPixmap();self.compare_gesture=False;self.compare_dragging=False;self.compare_drag_path=None;self.compare_divider_drag=False;self.compare_box_drag=False;self.compare_box_press=None;self.right_close_pending=False;self.setMinimumSize(1,1);self.pan_offset=QPoint();self.press_pos=None;self.last_pos=QPoint();self.dragging=False;self.pending_click=None;self.click_timer=QTimer(self);self.click_timer.setSingleShot(True);self.click_timer.setInterval(220);self.click_timer.timeout.connect(self.dispatch_click);self.counter_editor=QLineEdit(self);self.counter_editor.setAlignment(Qt.AlignCenter);self.counter_editor.setStyleSheet('QLineEdit{background:rgba(10,11,14,190);color:#f2f2f2;border:1px solid rgba(255,255,255,75);border-radius:0px;padding:2px;}');self.counter_editor.installEventFilter(self);self.counter_editor.returnPressed.connect(self.submit_page_counter);self.counter_editor.hide();self.pdf_search_edit=QLineEdit(self);self.pdf_search_edit.setPlaceholderText('Search PDF…');self.pdf_search_edit.setStyleSheet('QLineEdit{background:rgba(10,11,14,170);color:#f2f2f2;border:1px solid rgba(255,255,255,70);border-radius:0px;padding:8px 12px;} QLineEdit:focus{border-color:rgba(208,163,45,210);}');self.pdf_search_edit.returnPressed.connect(self.viewer.next_pdf_search_result);self.pdf_search_edit.textChanged.connect(self.viewer.set_pdf_search_query);self.pdf_search_edit.installEventFilter(self);self.pdf_search_edit.hide();self.zoom_slider=ViewerZoomSlider(Qt.Horizontal,self);self.zoom_slider.setRange(10,800);self.zoom_slider.setSingleStep(10);self.zoom_slider.setPageStep(25);self.zoom_slider.setToolTip('Zoom level (mouse wheel also adjusts zoom here)');self.zoom_slider.setStyleSheet('QSlider::groove:horizontal{height:16px;background:rgba(10,11,14,175);border:0px;border-radius:0px;} QSlider::handle:horizontal{width:16px;height:16px;margin:-1px 0;background:rgba(130,133,140,165);border:1px solid rgba(255,255,255,165);border-radius:0px;}');self.zoom_slider.valueChanged.connect(self.zoom_slider_changed);self.zoom_slider.hide();self.container_scrollbar=SearchMarkerScrollBar(Qt.Vertical,self);self.container_scrollbar.setStyleSheet('QScrollBar:vertical{background:#15171c;width:12px;margin:2px 2px 2px 0;border-radius:0px} QScrollBar::handle:vertical{background:#737982;min-height:42px;border-radius:0px} QScrollBar::handle:vertical:hover{background:#747a83} QScrollBar:horizontal{background:#15171c;height:12px;margin:0 2px;border-radius:0px} QScrollBar::handle:horizontal{background:#737982;min-width:42px;border-radius:0px} QScrollBar::handle:horizontal:hover{background:#747a83} QScrollBar::add-line:horizontal,QScrollBar::sub-line:horizontal{width:0} QScrollBar::add-page:horizontal,QScrollBar::sub-page:horizontal{background:transparent}');self.container_scrollbar.setGeometry(max(0,self.width()-14),0,12,self.height());self.container_scrollbar.hide();self.container_scrollbar.valueChanged.connect(self.container_scroll_changed);self.page_layout=[];self.page_tops=[];self.page_content_height=0;self.page_content_width=0;self.horizontal_pages=[];self.layout_update_timer=QTimer(self);self.layout_update_timer.setSingleShot(True);self.layout_update_timer.setInterval(40);self.layout_update_timer.timeout.connect(self.rebuild_page_layout)
        icon_dir=getattr(viewer.application_owner(),'icon_dir',None)
        self.mode_icons={}
        if icon_dir:
            for mode,filename in (('normal','imagemode.png'),('double_page','sidebyside.png'),('reader','readmodevtc.png'),('double_reader','readmode.png')):
                candidate=Path(icon_dir)/filename
                self.mode_icons[mode]=QPixmap(str(candidate)) if candidate.is_file() else QPixmap()
        self.sequence_pages=[];self.visible_sequence_paths=set();self.double_page_drag_path=None;self.page_render_cache=OrderedDict();self.page_render_cache_bytes=0;self.page_render_cache_limit=256*1024*1024;self.page_render_cache_max_pixels=64_000_000
    def resizeEvent(self,e):
        super().resizeEvent(e)
        if not hasattr(self,'container_scrollbar') or not hasattr(self,'zoom_slider'):return
        horizontal=bool(getattr(self.viewer,'multipage_double_page',False) or (not self.viewer.multipage_info and self.viewer.is_sequence_reader_mode() and self.viewer.is_horizontal_reader_mode()))
        if horizontal:self.container_scrollbar.setGeometry(0,max(0,self.height()-12),max(1,self.width()),12)
        else:self.container_scrollbar.setGeometry(max(0,self.width()-14),0,12,self.height())
        self.zoom_slider.setGeometry(max(16,(self.width()-360)//2),max(0,self.height()-92),min(360,max(80,self.width()-32)),20);self.counter_editor.setGeometry(self.page_counter_rect().adjusted(12,5,-12,-5));search_width=min(320,max(180,self.width()-32));self.pdf_search_edit.setGeometry(QRect((self.width()-search_width)//2,12,search_width,58));self.sync_pdf_search_visibility();self.prepare_frame();self.rebuild_page_layout()
    def rebuild_page_layout(self):
        info=self.viewer.multipage_info
        if not info and self.viewer.is_sequence_reader_mode():return self.rebuild_sequence_layout()
        if not info:
            self.page_layout=[];self.page_tops=[];self.page_content_height=0;self.page_content_width=0;self.double_spreads=[];self.double_spread_starts=[];self.double_spread_widths=[];self.horizontal_pages=[];self.sequence_pages=[];self.visible_sequence_paths=set();self.container_scrollbar.setOrientation(Qt.Vertical);self.container_scrollbar.setGeometry(max(0,self.width()-12),0,12,max(1,self.height()));self.container_scrollbar.setRange(0,0);self.container_scrollbar.hide();return
        ratios=info.get('ratios') or [];count=min(int(info.get('count',0)),100000);width=max(1,self.width());anchor=self.viewer.current_multipage_page() if self.page_tops else 0
        if self.viewer.multipage_mode in ('normal','double_page'):
            self.page_layout=[(0,0) for _ in range(count)];self.page_tops=[0 for _ in range(count)];self.page_content_height=0;self.double_spreads=[];self.double_spread_starts=[];self.double_spread_widths=[];self.horizontal_pages=[];self.container_scrollbar.setRange(0,0);self.container_scrollbar.hide();self.request_visible_container_pages();self.update();return
        if getattr(self.viewer,'multipage_double_page',False):
            old_offset=self.container_scrollbar.value();old_page=max(0,bisect.bisect_right(self.page_tops,old_offset)-1) if self.page_tops else anchor
            zoom=max(.1,min(8.0,float(getattr(self.viewer,'reader_zoom',1.0))));gap=18;cursor=0;layout=[];tops=[];pages=[]
            for index in range(count):
                ratio=max(.15,min(4.0,float(ratios[index] if index<len(ratios) else .72)))
                if abs(int(getattr(self.viewer,'rotation',0)))%180==90:ratio=1.0/ratio
                page_width=max(1,round(min(width,max(1,self.height()*ratio))*zoom));page_height=max(1,round(page_width/ratio));y=(self.height()-page_height)//2+self.pan_offset.y();rect=QRect(cursor,y,page_width,page_height)
                pages.append((index,rect));layout.append((cursor,page_width));tops.append(cursor);cursor+=page_width+gap
            self.page_layout=layout;self.page_tops=tops;self.horizontal_pages=pages;self.page_content_height=self.height();self.page_content_width=max(0,cursor-gap)
            self.container_scrollbar.setOrientation(Qt.Horizontal);self.container_scrollbar.setGeometry(0,max(0,self.height()-12),max(1,self.width()),12);self.container_scrollbar.setRange(0,max(0,self.page_content_width-width));self.container_scrollbar.setPageStep(width)
            self.container_scrollbar.setVisible(self.container_scrollbar.maximum()>self.container_scrollbar.minimum())
            if layout and old_page<len(layout):self.container_scrollbar.setValue(layout[old_page][0]+max(0,old_offset-(self.page_tops[old_page] if old_page<len(self.page_tops) else 0)))
            self._layout_double_page=True;self.request_visible_container_pages();self.update();return
        self._layout_double_page=False;self.double_spreads=[];self.double_spread_starts=[];self.double_spread_widths=[];self.horizontal_pages=[]
        self.container_scrollbar.setOrientation(Qt.Vertical);self.container_scrollbar.setGeometry(max(0,self.width()-12),0,12,self.height())
        # The scrollbar is an overlay. Do not subtract a scrollbar gutter or
        # decorative margins from the image's fit-to-screen area.
        width=max(1,self.width());gap=18;top=0;layout=[];tops=[];anchor_offset=self.container_scrollbar.value()-self.page_tops[anchor] if self.page_tops and anchor>=0 else 0
        zoom=max(.1,min(8.0,float(getattr(self.viewer,'reader_zoom',1.0))))
        rotated=abs(int(getattr(self.viewer,'rotation',0)))%180==90
        for index in range(count):
            ratio=max(.15,min(4.0,float(ratios[index] if index<len(ratios) else .72)))
            if rotated:ratio=1.0/ratio
            # At the default reader zoom, fit the whole page inside the
            # viewport. Higher zoom levels scale relative to that fit.
            page_width=min(width,max(1,self.height()*ratio))*zoom
            height=max(1,round(page_width/ratio));tops.append(top);layout.append((top,height));top+=height+gap
        self.page_layout=layout;self.page_tops=tops;self.page_content_height=max(0,top-gap);self.container_scrollbar.setRange(0,max(0,self.page_content_height-self.height()));self.container_scrollbar.setPageStep(max(1,self.height()//2));self.container_scrollbar.setVisible(self.container_scrollbar.maximum()>self.container_scrollbar.minimum())
        if anchor>=0 and anchor<len(layout):self.container_scrollbar.setValue(layout[anchor][0]+min(anchor_offset,layout[anchor][1]-1))
        self.request_visible_container_pages();self.update()
    def rebuild_sequence_layout(self):
        viewer=self.viewer;count=len(viewer.paths);width=max(1,self.width());height=max(1,self.height())
        if not count:self.sequence_pages=[];self.page_layout=[];self.page_tops=[];self.container_scrollbar.setRange(0,0);self.container_scrollbar.hide();return
        horizontal=viewer.is_horizontal_reader_mode();old_value=self.container_scrollbar.value();old_tops=self.page_tops;old_page=max(0,min(count-1,bisect.bisect_right(old_tops,old_value)-1)) if old_tops else max(0,min(viewer.index,count-1));old_start=old_tops[old_page] if old_tops and old_page<len(old_tops) else 0;old_size=self.page_layout[old_page][1] if self.page_layout and old_page<len(self.page_layout) else 1;local=max(0,old_value-old_start);gap=18;cursor=0;pages=[];tops=[];layout=[];zoom=max(.1,min(8.0,float(viewer.reader_zoom)));rotated=abs(int(viewer.rotation))%180==90
        if horizontal:self.container_scrollbar.setOrientation(Qt.Horizontal);self.container_scrollbar.setGeometry(0,max(0,height-12),width,12)
        else:self.container_scrollbar.setOrientation(Qt.Vertical);self.container_scrollbar.setGeometry(max(0,width-12),0,12,height)
        for index,path in enumerate(viewer.paths):
            ratio=viewer.sequence_aspect_ratios.get(path);entry=viewer.image_cache.get(path)
            if not ratio and entry is not None and entry[0] is not None and not entry[0].isNull():ratio=entry[0].width()/max(1,entry[0].height())
            ratio=max(.15,min(4.0,float(ratio or .72)))
            if rotated:ratio=1.0/ratio
            if horizontal:page_width=max(1,round(min(width,max(1,height*ratio))*zoom));page_height=max(1,round(page_width/ratio));rect=QRect(cursor,(height-page_height)//2,page_width,page_height);tops.append(cursor);layout.append((cursor,page_width));cursor+=page_width+gap
            else:page_width=max(1,round(min(width,max(1,height*ratio))*zoom));page_height=max(1,round(page_width/ratio));rect=QRect((width-page_width)//2,cursor,page_width,page_height);tops.append(cursor);layout.append((cursor,page_height));cursor+=page_height+gap
            pages.append((index,rect))
        self.sequence_pages=pages;self.horizontal_pages=pages;self.page_layout=layout;self.page_tops=tops;self._layout_double_page=horizontal
        if horizontal:self.page_content_width=max(0,cursor-gap);self.page_content_height=height;self.container_scrollbar.setRange(0,max(0,self.page_content_width-width));self.container_scrollbar.setPageStep(width)
        else:self.page_content_height=max(0,cursor-gap);self.page_content_width=width;self.container_scrollbar.setRange(0,max(0,self.page_content_height-height));self.container_scrollbar.setPageStep(max(1,height//2))
        if old_page<count:self.container_scrollbar.setValue(tops[old_page]+min(round(local*layout[old_page][1]/max(1,old_size)),max(0,layout[old_page][1]-1)))
        self.container_scrollbar.setVisible(self.container_scrollbar.maximum()>0);self.request_visible_container_pages();self.update()
    def container_scroll_changed(self,value):
        if self.viewer.multipage_info:self.viewer.update_multipage_current(value)
        elif self.viewer.is_sequence_reader_mode():self.viewer.update_sequence_current(value)
        self.request_visible_container_pages();self.update()
    def request_visible_container_pages(self):
        if not self.viewer.multipage_info:
            viewer=self.viewer
            if not viewer.is_sequence_reader_mode() or not self.sequence_pages:return
            horizontal=viewer.is_horizontal_reader_mode();offset=self.container_scrollbar.value();limit=offset+(self.width() if horizontal else self.height());first=max(0,bisect.bisect_right(self.page_tops,offset)-1);visible=set()
            for index in range(first,min(len(self.sequence_pages),first+16)):
                start,size=self.page_layout[index]
                if start>limit+size:break
                visible.add(viewer.paths[index])
            self.visible_sequence_paths=visible
            for index in range(max(0,first-3),min(len(viewer.paths),first+18)):
                path=viewer.paths[index]
                if path not in viewer.image_cache and path not in viewer.pending_image_paths:viewer.request_image(path,preload=index!=viewer.index)
            return
        if not self.page_layout:return
        if self.viewer.multipage_mode in ('normal','double_page'):
            current=max(0,min(len(self.page_layout)-1,self.viewer.multipage_normal_page));end=min(len(self.page_layout),current+(2 if self.viewer.multipage_mode=='double_page' else 1));needed=[i for i in range(max(0,current-1),min(len(self.page_layout),end+2)) if i not in self.viewer.multipage_images]
            if needed:self.viewer.request_multipage_pages(needed)
            return
        if getattr(self.viewer,'multipage_double_page',False):
            offset=self.container_scrollbar.value();first=max(0,bisect.bisect_right(self.page_tops,offset)-1);needed=[]
            for index in range(max(0,first-2),min(len(self.page_layout),first+5)):
                if index not in self.viewer.multipage_images:needed.append(index)
            if needed:self.viewer.request_multipage_pages(needed)
            return
        top=self.container_scrollbar.value();bottom=top+self.height();first=max(0,bisect.bisect_right(self.page_tops,top)-2)
        needed=[]
        for index in range(first,min(len(self.page_layout),first+12)):
            page_top,page_height=self.page_layout[index]
            if page_top>bottom+page_height:break
            if index not in self.viewer.multipage_images:needed.append(index)
        if needed:self.viewer.request_multipage_pages(needed)
    def scroll_to_page(self,index):
        if 0<=index<len(self.page_layout):
            if not self.viewer.multipage_info and self.viewer.is_sequence_reader_mode():self.container_scrollbar.setValue(self.page_layout[index][0]);return
            if self.viewer.multipage_mode in ('normal','double_page'):self.viewer.multipage_normal_page=index;self.viewer.update_multipage_current(0);self.request_visible_container_pages();self.update()
            elif getattr(self.viewer,'multipage_double_page',False):self.container_scrollbar.setValue(self.page_layout[index][0])
            else:self.container_scrollbar.setValue(self.page_layout[index][0])
    def paint_pdf_search_highlights(self,p,index,rect):
        info=self.viewer.multipage_info or {};sizes=info.get('page_sizes',[])
        grouped=getattr(self.viewer,'pdf_search_results_by_page',{}).get(index,[])
        if not grouped or not 0<=index<len(sizes):return
        try:page_width,page_height=sizes[index];sx=rect.width()/max(1,page_width);sy=rect.height()/max(1,page_height)
        except (TypeError,ValueError,IndexError):return
        p.save()
        for result_index,rectangles in grouped:
            p.setPen(QPen(QColor(255,216,55,205),1));p.setBrush(QColor(255,210,25,130) if result_index==self.viewer.pdf_search_result_index else QColor(255,235,75,65))
            for found in rectangles:
                try:p.drawRect(QRectF(rect.x()+found.x()*sx,rect.y()+found.y()*sy,found.width()*sx,found.height()*sy))
                except (AttributeError,TypeError):continue
        p.restore()
    def clear_page_render_cache(self):
        self.page_render_cache.clear();self.page_render_cache_bytes=0
    def page_image_geometry(self,source,bounds,zoom=1.0,pan=None):
        size=source.size();source_width=max(1,size.width());source_height=max(1,size.height());rotation=int(self.viewer.rotation)%360;quarter_turn=rotation%180==90
        oriented_width,oriented_height=(source_height,source_width) if quarter_turn else (source_width,source_height)
        scale=min(bounds.width()/max(1,oriented_width),bounds.height()/max(1,oriented_height))*max(.1,min(8.0,float(zoom)))
        draw_width=max(1,round(source_width*scale));draw_height=max(1,round(source_height*scale));frame_width,frame_height=(draw_height,draw_width) if quarter_turn else (draw_width,draw_height)
        pan=pan if pan is not None else QPoint();center=QPoint(bounds.center().x()+pan.x(),bounds.center().y()+pan.y())
        return draw_width,draw_height,QRect(center.x()-frame_width//2,center.y()-frame_height//2,frame_width,frame_height),center
    @staticmethod
    def pan_after_zoom(bounds,position,pan,ratio):
        center=QPointF(bounds.center());anchor=QPointF(position);previous=QPointF(pan);vector=anchor-center-previous;result=anchor-center-vector*ratio
        return QPoint(round(result.x()),round(result.y()))
    def paint_page_image(self,p,source,bounds,zoom=1.0,pan=None,clip=None,cache=True):
        """Draw every image/page through the same fit, zoom, pan, and transform path."""
        if source is None or source.isNull() or bounds.isEmpty():return False
        draw_width,draw_height,target,center=self.page_image_geometry(source,bounds,zoom,pan);frame_width,frame_height=target.width(),target.height();rotation=int(self.viewer.rotation)%360;visible=(bounds if clip is None else clip).intersected(self.rect())
        if visible.isEmpty():return False
        p.save();p.setClipRect(visible);p.setRenderHint(QPainter.SmoothPixmapTransform,True)
        pixels=frame_width*frame_height;source_key=source.cacheKey() if hasattr(source,'cacheKey') else id(source);key=(type(source).__name__,source_key,frame_width,frame_height,rotation,self.viewer.flip_h,self.viewer.flip_v)
        frame=self.page_render_cache.get(key) if cache and pixels<=self.page_render_cache_max_pixels else None
        if frame is not None:self.page_render_cache.move_to_end(key);p.drawPixmap(target.topLeft(),frame)
        elif cache and pixels<=self.page_render_cache_max_pixels:
            frame=QPixmap(frame_width,frame_height);frame.fill(Qt.transparent);fp=QPainter(frame);fp.setRenderHint(QPainter.SmoothPixmapTransform,True);fp.translate(frame_width/2,frame_height/2);fp.rotate(rotation);fp.scale(-1 if self.viewer.flip_h else 1,-1 if self.viewer.flip_v else 1);source_rect=QRectF(source.rect());destination=QRectF(-draw_width/2,-draw_height/2,draw_width,draw_height)
            if isinstance(source,QImage):fp.drawImage(destination,source,source_rect)
            else:fp.drawPixmap(destination,source,source_rect)
            fp.end();self.page_render_cache[key]=frame;self.page_render_cache_bytes+=frame.width()*frame.height()*max(1,(frame.depth()+7)//8);p.drawPixmap(target.topLeft(),frame)
            while self.page_render_cache_bytes>self.page_render_cache_limit and len(self.page_render_cache)>1:
                _old_key,old_frame=self.page_render_cache.popitem(last=False);self.page_render_cache_bytes=max(0,self.page_render_cache_bytes-old_frame.width()*old_frame.height()*max(1,(old_frame.depth()+7)//8))
        else:
            p.translate(center);p.rotate(rotation);p.scale(-1 if self.viewer.flip_h else 1,-1 if self.viewer.flip_v else 1);source_rect=QRectF(source.rect());destination=QRectF(-draw_width/2,-draw_height/2,draw_width,draw_height)
            if isinstance(source,QImage):p.drawImage(destination,source,source_rect)
            else:p.drawPixmap(destination,source,source_rect)
        p.restore();return True
    def paint_multipage_page(self,p,index,bounds,clip=None,zoom=1.0,pan=None):
        viewer=self.viewer;image=viewer.multipage_images.get(index);visible=(bounds if clip is None else clip).intersected(self.rect());p.save();p.setClipRect(visible);p.fillRect(bounds,QColor('#05060a'))
        if image is not None and not image.isNull():
            _draw_width,_draw_height,image_rect,_center=self.page_image_geometry(image,bounds,zoom,pan);self.paint_page_image(p,image,bounds,zoom=zoom,pan=pan,clip=visible);self.paint_pdf_search_highlights(p,index,image_rect)
        elif viewer.multipage_error:p.setPen(QColor('#d0a0a0'));p.drawText(bounds,Qt.AlignCenter|Qt.TextWordWrap,viewer.multipage_error)
        elif index in viewer.multipage_page_errors:p.setPen(QColor('#d0a0a0'));p.drawText(bounds,Qt.AlignCenter|Qt.TextWordWrap,viewer.multipage_page_error_messages.get(index,'Could not decode this page'))
        else:p.setPen(QColor('#9da2aa'));p.drawText(bounds,Qt.AlignCenter,f'Loading page {index+1}…')
        p.restore()
    def paint_multipage(self,p):
        viewer=self.viewer
        if viewer.multipage_mode in ('normal','double_page'):
            if not self.page_layout:return
            start=max(0,min(len(self.page_layout)-1,viewer.multipage_normal_page));slots=2 if viewer.multipage_mode=='double_page' else 1
            p.save();p.setRenderHint(QPainter.SmoothPixmapTransform,True)
            for slot in range(slots):
                index=start+slot
                if index>=len(self.page_layout):break
                area=QRect(slot*self.width()//slots,0,(slot+1)*self.width()//slots-slot*self.width()//slots,self.height())
                zoom_key=viewer.multipage_zoom_key(index) if viewer.multipage_mode=='double_page' else None
                zoom=max(.1,min(8.0,float(viewer.double_page_zooms.get(zoom_key,1.0)))) if zoom_key is not None else max(.1,min(8.0,float(viewer.reader_zoom)))
                pan=viewer.double_page_pan.get(zoom_key,QPoint()) if zoom_key is not None else self.pan_offset
                self.paint_multipage_page(p,index,area,zoom=zoom,pan=pan)
            p.restore();return
        if getattr(viewer,'multipage_double_page',False):
            offset=self.container_scrollbar.value();right=offset+self.width();first=max(0,bisect.bisect_right(self.page_tops,offset)-1);p.save();p.setRenderHint(QPainter.SmoothPixmapTransform,True)
            for index,content_rect in self.horizontal_pages[first:min(len(self.horizontal_pages),first+6)]:
                if content_rect.left()>right:break
                rect=QRect(content_rect);rect.translate(-offset+self.pan_offset.x(),self.pan_offset.y());clip=rect.intersected(self.rect())
                if clip.isEmpty():continue
                self.paint_multipage_page(p,index,rect,clip)
            p.restore();return
        offset=self.container_scrollbar.value();bottom=offset+self.height();first=max(0,bisect.bisect_right(self.page_tops,offset)-1);p.save();p.setRenderHint(QPainter.SmoothPixmapTransform,True)
        for index in range(first,len(self.page_layout)):
            top,height=self.page_layout[index]
            if top>bottom:break
            ratios=viewer.multipage_info.get('ratios') or [];ratio=max(.15,min(4.0,float(ratios[index] if index<len(ratios) else .72)))
            if abs(int(viewer.rotation))%180==90:ratio=1.0/ratio
            page_width=min(max(1,self.width()),max(1,self.height()*ratio))*max(.1,min(8.0,float(viewer.reader_zoom)))
            rect=QRect((self.width()-round(page_width))//2+self.pan_offset.x(),top-offset+self.pan_offset.y(),round(page_width),height);self.paint_multipage_page(p,index,rect,self.rect())
        p.restore()
    def prepare_frame(self):
        # The shared bounded render cache keys each source and transform, so
        # panning can reuse its raster while zoom and rotation naturally miss.
        self.update()
    def bottom_buttons(self):
        width=42;count=8;left=(self.width()-count*width)//2;top=self.height()-55
        return [QRect(left+i*width,top,width,width) for i in range(count)]
    def paintEvent(self,e):
        p=QPainter(self);p.fillRect(self.rect(),QColor('#080a10'))
        if self.viewer.compare_enabled:
            self.paint_compare(p)
        elif self.viewer.multipage_info:
            self.paint_multipage(p)
        elif self.viewer.is_sequence_reader_mode():
            self.paint_sequence_reader(p)
        elif self.viewer.double_page:
            self.paint_double_page(p)
        else:
            self.paint_single_page(p)
        if self.hover_zone=='previous':self.paint_swipe_control(p,QRect(12,(self.height()-54)//2,48,54),'‹',True)
        if self.hover_zone=='next':self.paint_swipe_control(p,QRect(self.width()-60,(self.height()-54)//2,48,54),'›',False)
        if self.hover_zone in ('top','metadata'):
            self.paint_metadata(p)
            self.paint_page_counter(p)
        if self.hover_zone=='bottom':
            glyphs=('↶','↷','↕','↔','□','▣','⇆','Ⅱ' if self.viewer.slideshow_active else '▶')
            rects=self.bottom_buttons()
            if rects:
                strip=QRect(rects[0].left(),rects[0].top(),rects[-1].right()-rects[0].left()+1,rects[0].height());gradient=QLinearGradient(strip.left(),0,strip.right(),0);gradient.setColorAt(0,QColor(32,34,38,0));gradient.setColorAt(.08,QColor(8,10,14,170));gradient.setColorAt(.92,QColor(8,10,14,170));gradient.setColorAt(1,QColor(32,34,38,0));p.save();p.setPen(Qt.NoPen);p.setBrush(gradient);p.drawRect(strip);p.restore()
            for index,(rect,glyph) in enumerate(zip(rects,glyphs)):
                if index==5:
                    icon=self.mode_icons.get(self.viewer.visualization_mode,QPixmap())
                    if not icon.isNull():self.paint_control_icon(p,rect,icon)
                    else:self.paint_control_glyph(p,rect,glyph)
                elif index==6:
                    if self.viewer.compare_enabled:p.fillRect(rect.adjusted(3,3,-3,-3),QColor(208,163,45,42))
                    if not self.compare_icon.isNull():self.paint_control_icon(p,rect,self.compare_icon)
                    else:self.paint_control_glyph(p,rect,glyph)
                else:self.paint_control_glyph(p,rect,glyph)
    def paint_control_glyph(self,p,rect,text):
        p.save();p.setPen(QColor(255,255,255,235));font=QFont('Segoe UI Symbol',20);p.setFont(font);bounds=QFontMetricsF(font).boundingRect(text);p.drawText(QPointF(rect.center().x()-bounds.width()/2-bounds.x(),rect.center().y()-bounds.height()/2-bounds.y()),text);p.restore()
    def paint_swipe_control(self,p,rect,text,fade_to_right):
        gradient=QLinearGradient(rect.left(),0,rect.right(),0)
        if fade_to_right:
            gradient.setColorAt(0,QColor(8,10,14,170));gradient.setColorAt(.72,QColor(8,10,14,125));gradient.setColorAt(1,QColor(32,34,38,0))
        else:
            gradient.setColorAt(0,QColor(32,34,38,0));gradient.setColorAt(.28,QColor(8,10,14,125));gradient.setColorAt(1,QColor(8,10,14,170))
        p.save();p.setPen(Qt.NoPen);p.setBrush(gradient);p.drawRect(rect);p.restore();self.paint_control_glyph(p,rect,text)
    def paint_sequence_reader(self,p):
        viewer=self.viewer
        if not self.sequence_pages:return
        horizontal=viewer.is_horizontal_reader_mode();offset=self.container_scrollbar.value();limit=offset+(self.width() if horizontal else self.height());first=max(0,bisect.bisect_right(self.page_tops,offset)-1);p.save();p.setRenderHint(QPainter.SmoothPixmapTransform,True)
        for index,content_rect in self.sequence_pages[first:min(len(self.sequence_pages),first+18)]:
            start,size=self.page_layout[index]
            if start>limit+size:break
            rect=QRect(content_rect)
            if horizontal:rect.translate(-offset+self.pan_offset.x(),self.pan_offset.y())
            else:rect.translate(self.pan_offset.x(),-offset+self.pan_offset.y())
            clip=rect.intersected(self.rect())
            if clip.isEmpty():continue
            p.save();p.setClipRect(clip);p.fillRect(rect,QColor('#05060a'));source=viewer.page_source(index)
            if source.isNull():
                p.setPen(QColor('#9da2aa'));p.setFont(QFont('Segoe UI',10));p.drawText(rect,Qt.AlignCenter,'Loading image…')
                path=viewer.paths[index]
                if path not in viewer.pending_image_paths:viewer.request_image(path,preload=index!=viewer.index)
            else:self.paint_page_image(p,source,rect,clip=clip,cache=viewer.movie is None)
            p.restore()
        p.restore()
    def _sequence_page_at(self,pos):
        if not self.sequence_pages:return None
        horizontal=self.viewer.is_horizontal_reader_mode();offset=self.container_scrollbar.value();point=QPoint(pos.x()+offset-self.pan_offset.x(),pos.y()-self.pan_offset.y()) if horizontal else QPoint(pos.x()-self.pan_offset.x(),pos.y()+offset-self.pan_offset.y());axis=point.x() if horizontal else point.y();index=max(0,min(len(self.sequence_pages)-1,bisect.bisect_right(self.page_tops,axis)-1))
        for candidate in range(max(0,index-1),min(len(self.sequence_pages),index+2)):
            if self.sequence_pages[candidate][1].contains(point):return candidate
        return index
    def _reader_page_at(self,pos):
        if self.viewer.multipage_info:
            if self.viewer.multipage_mode in ('normal','double_page'):
                return min(len(self.page_layout)-1,self.viewer.multipage_normal_page+(1 if self.viewer.multipage_mode=='double_page' and pos.x()>=self.width()//2 else 0)) if self.page_layout else None
            horizontal=self.viewer.is_horizontal_reader_mode();offset=self.container_scrollbar.value();point=QPoint(pos.x()+offset-self.pan_offset.x(),pos.y()-self.pan_offset.y()) if horizontal else QPoint(pos.x()-self.pan_offset.x(),pos.y()+offset-self.pan_offset.y());axis=point.x() if horizontal else point.y();return max(0,min(len(self.page_tops)-1,bisect.bisect_right(self.page_tops,axis)-1)) if self.page_tops else None
        return self._sequence_page_at(pos)
    def _reader_content_page_rect(self,page):
        viewer=self.viewer
        if not 0<=page<len(self.page_layout):return None
        if not viewer.multipage_info:
            return QRect(self.sequence_pages[page][1]) if page<len(self.sequence_pages) else None
        if viewer.is_horizontal_reader_mode():return QRect(self.horizontal_pages[page][1]) if page<len(self.horizontal_pages) else None
        top,height=self.page_layout[page];ratios=viewer.multipage_info.get('ratios') or [];ratio=max(.15,min(4.0,float(ratios[page] if page<len(ratios) else .72)))
        if abs(int(viewer.rotation))%180==90:ratio=1.0/ratio
        width=max(1,round(min(max(1,self.width()),max(1,self.height()*ratio))*max(.1,min(8.0,float(viewer.zoom)))))
        return QRect((self.width()-width)//2,top,width,height)
    def most_visible_reader_page(self):
        viewer=self.viewer
        if not (viewer.multipage_info or viewer.is_sequence_reader_mode()):return None
        count=len(self.page_layout)
        if not count:return None
        horizontal=viewer.is_horizontal_reader_mode();offset=self.container_scrollbar.value();first=max(0,bisect.bisect_right(self.page_tops,offset)-2);best_page=None;best_area=-1
        for page in range(first,min(count,first+64)):
            rect=self._reader_content_page_rect(page)
            if rect is None:continue
            rect.translate(-offset+self.pan_offset.x(),self.pan_offset.y()) if horizontal else rect.translate(self.pan_offset.x(),-offset+self.pan_offset.y())
            visible=rect.intersected(self.rect());area=visible.width()*visible.height()
            if area>best_area:best_page=page;best_area=area
            start=rect.left() if horizontal else rect.top()
            if start>(self.width() if horizontal else self.height()) and page>first+2:break
        if best_page is not None:return best_page
        return viewer.current_multipage_page() if viewer.multipage_info else max(0,min(count-1,viewer.index))
    def center_reader_page(self,page,align_start=False):
        rect=self._reader_content_page_rect(page)
        if rect is None:return
        if self.viewer.is_horizontal_reader_mode():target=rect.center().x()-self.width()//2
        elif align_start:target=rect.top()
        else:target=rect.center().y()-self.height()//2
        self.container_scrollbar.setValue(max(0,min(self.container_scrollbar.maximum(),target)))
    def _double_page_key(self,slot=None):
        viewer=self.viewer;slot=viewer.double_page_active_slot if slot is None else max(0,min(1,int(slot)))
        if viewer.multipage_info:
            page=viewer.multipage_normal_page+slot
            if page<0 or page>=int(viewer.multipage_info.get('count',0)):return None
            return viewer.multipage_zoom_key(max(0,page))
        if viewer.paths:return viewer.paths[(viewer.index+slot)%len(viewer.paths)]
        return None
    def zoom_reader_at(self,pos,factor):
        viewer=self.viewer
        if (viewer.multipage_info and viewer.multipage_mode=='normal') or (not viewer.multipage_info and not viewer.is_sequence_reader_mode()):return self.zoom_fitted_page_at(pos,factor)
        page=self._reader_page_at(pos)
        if page is None:return
        horizontal=viewer.is_horizontal_reader_mode();rect=self._reader_content_page_rect(page)
        if rect is None:return
        previous_pan=QPoint(self.pan_offset)
        if not viewer.multipage_info and page!=viewer.index:
            path=viewer.paths[page]
            if Path(viewer.paths[viewer.index]).suffix.casefold() in IMAGE_EXTS and Path(path).suffix.casefold() in IMAGE_EXTS:viewer.remember_file_visualization_mode(path)
            viewer.index=page;entry=viewer.image_cache.get(path)
            if entry is not None:viewer.full_image,viewer.fit_pixmap=entry[:2];viewer.static_pixmap=QPixmap();viewer.original_resolution=entry[2] if len(entry)>2 else viewer.full_image.size();viewer.current_file_size=entry[3] if len(entry)>3 else None;viewer.loading=False
            else:viewer.full_image=QImage();viewer.fit_pixmap=QPixmap();viewer.static_pixmap=QPixmap();viewer.loading=True;viewer.request_image(path,preload=False)
            viewer.begin_pdf_search(path);self.pan_offset=previous_pan
        scroll=self.container_scrollbar.value();ax=pos.x()+scroll-self.pan_offset.x() if horizontal else pos.x()-self.pan_offset.x();ay=pos.y()-self.pan_offset.y() if horizontal else pos.y()+scroll-self.pan_offset.y();fx=max(0.0,min(1.0,(ax-rect.left())/max(1,rect.width())));fy=max(0.0,min(1.0,(ay-rect.top())/max(1,rect.height())));old=max(.1,float(viewer.zoom));new=max(.1,min(8.0,old*factor))
        if abs(old-new)<1e-8:return
        viewer.reader_zoom_mode_enabled=True;viewer.set_reader_zoom(new,rebuild=False);self.layout_update_timer.stop();self.rebuild_page_layout()
        rect=self._reader_content_page_rect(page)
        if rect is None:return
        if horizontal:self.container_scrollbar.setValue(max(0,min(self.container_scrollbar.maximum(),rect.left()+round(fx*rect.width())+self.pan_offset.x()-pos.x())));self.pan_offset.setY(round(pos.y()-(rect.top()+fy*rect.height())))
        else:self.container_scrollbar.setValue(max(0,min(self.container_scrollbar.maximum(),rect.top()+round(fy*rect.height())+self.pan_offset.y()-pos.y())));self.pan_offset.setX(round(pos.x()-(rect.left()+fx*rect.width())))
        self.sync_zoom_slider();self.update()
    def zoom_fitted_page_at(self,pos,factor):
        viewer=self.viewer;old=max(.1,float(viewer.zoom));new=max(.1,min(8.0,old*factor))
        if abs(old-new)<1e-8:return
        self.pan_offset=self.pan_after_zoom(self.rect(),pos,self.pan_offset,new/old);viewer.reader_zoom_mode_enabled=True;viewer.set_reader_zoom(new,rebuild=False);self.sync_zoom_slider();self.update()
    def toggle_fitted_page_zoom_at(self,pos):
        viewer=self.viewer
        if viewer.reader_zoom_mode_enabled:
            viewer.reader_zoom_mode_enabled=False;viewer.set_reader_zoom(1.0,rebuild=False);self.pan_offset=QPoint();self.sync_zoom_slider();self.update();return
        old=max(.1,float(viewer.zoom));target=max(2.0,old)
        if abs(target-old)<1e-8:return
        self.zoom_fitted_page_at(pos,target/old)
    def toggle_reader_zoom_at(self,pos):
        viewer=self.viewer
        if (viewer.multipage_info and viewer.multipage_mode=='normal') or (not viewer.multipage_info and not viewer.is_sequence_reader_mode()):return self.toggle_fitted_page_zoom_at(pos)
        if viewer.reader_zoom_mode_enabled or getattr(viewer,'zoom_bar_active',False):
            page=self._reader_page_at(pos);page=self.most_visible_reader_page() if page is None else page
            viewer.reader_zoom_mode_enabled=False;viewer.zoom_bar_active=False;self.pan_offset=QPoint();viewer.set_reader_zoom(1.0,rebuild=False);self.layout_update_timer.stop();self.rebuild_page_layout();self.center_reader_page(page,align_start=not viewer.is_horizontal_reader_mode());self.sync_zoom_slider();return
        old=max(.1,float(viewer.reader_zoom));target=max(2.0,old);viewer.zoom_bar_active=False;viewer.reader_zoom_mode_enabled=True
        if abs(target-old)<1e-8:self.sync_zoom_slider();return
        self.zoom_reader_at(pos,target/old)
    def paint_compare(self,p):
        pair=self.viewer.compare_pair()
        if not pair:return
        box=QRect(self.rect()).translated(getattr(self.viewer,'compare_box_offset',QPoint()))
        split=max(box.left()+1,min(box.right(),round(box.left()+box.width()*self.viewer.compare_slider)))
        shared_zoom=max(.1,min(8.0,float(getattr(self.viewer,'compare_zoom',1.0))));shared_pan=getattr(self.viewer,'compare_pan',QPoint())
        for slot,key in enumerate(pair):
            zoom=max(.1,min(8.0,shared_zoom*float(getattr(self.viewer,'compare_image_zooms',{}).get(key,1.0))))
            pan=shared_pan+getattr(self.viewer,'compare_image_pans',{}).get(key,QPoint())
            source=self.viewer.compare_source(key);clip=(QRect(box.left(),box.top(),split-box.left(),box.height()) if slot==0 else QRect(split,box.top(),box.right()-split+1,box.height())).intersected(self.rect())
            if source is None or source.isNull():
                if key[0]=='page':self.viewer.request_multipage_pages([key[1]])
                else:
                    index=next((i for i,path in enumerate(self.viewer.paths) if path==key[1]),-1)
                    if index>=0 and key[1] not in self.viewer.pending_image_paths:self.viewer.request_image(key[1],preload=index!=self.viewer.index)
                p.fillRect(clip,QColor('#080a10'));p.setPen(QColor('#9da2aa'));p.drawText(clip,Qt.AlignCenter,'Loading comparison image…');continue
            self.paint_page_image(p,source,box,zoom=zoom,pan=pan,clip=clip,cache=self.viewer.movie is None)
        p.save();p.setClipRect(self.rect());p.setRenderHint(QPainter.Antialiasing,True);p.setPen(QPen(QColor(0,0,0,190),5));p.drawLine(split,box.top(),split,box.bottom());handle=QRect(split-13,box.center().y()-21,26,42);p.setPen(QPen(QColor(240,240,240,225),1));p.setBrush(QColor(18,20,25,210));p.drawRoundedRect(handle,8,8);p.setPen(QColor(255,255,255,225));p.setFont(QFont('Segoe UI Symbol',13));p.drawText(handle,Qt.AlignCenter,'↔');p.restore()
    def compare_box_rect(self):
        return QRect(self.rect()).translated(getattr(self.viewer,'compare_box_offset',QPoint()))
    def paint_single_page(self,p):
        source=self.viewer.page_source(self.viewer.index)
        if not source.isNull():self.paint_page_image(p,source,self.rect(),zoom=self.viewer.zoom,pan=self.pan_offset,cache=self.viewer.movie is None)
        elif self.viewer.loading:p.setPen(QColor('#9da2aa'));p.setFont(QFont('Segoe UI',12));p.drawText(self.rect(),Qt.AlignCenter,'Loading image…')
    def paint_double_page(self,p):
        viewer=self.viewer
        pages=(viewer.index,(viewer.index+1)%len(viewer.paths)) if viewer.paths else ()
        for slot,index in enumerate(pages):
            left=slot*self.width()//2;right=(slot+1)*self.width()//2;area=QRect(left,0,right-left,self.height());path=viewer.paths[index];zoom=max(.1,min(8.0,float(viewer.double_page_zooms.get(path,1.0))));source=viewer.page_source(index)
            if source.isNull():
                p.setPen(QColor('#9da2aa'));p.setFont(QFont('Segoe UI',11));p.drawText(area,Qt.AlignCenter,'Loading image…')
                if index!=viewer.index:viewer.request_image(viewer.paths[index],preload=True)
                continue
            self.paint_page_image(p,source,area,zoom=zoom,pan=viewer.double_page_pan.get(path,QPoint()),clip=area,cache=viewer.movie is None)
        p.setPen(QPen(QColor(255,255,255,25),1));p.drawLine(self.width()//2,0,self.width()//2,self.height())
    def paint_metadata(self,p):
        viewer=self.viewer
        if not viewer.paths:return
        raw_path=str(viewer.paths[viewer.index]);decoded=decode_archive_path(raw_path);path=Path(decoded[0]) if decoded else Path(raw_path);name=(PurePosixPath(decoded[1]).name if decoded and decoded[1] else path.name) or str(path);suffix=Path(name).suffix[1:].upper() or 'Unknown'
        if viewer.multipage_info:resolution=str(viewer.multipage_info.get('count',0))
        else:resolution=f'{viewer.original_resolution.width()} × {viewer.original_resolution.height()}' if viewer.original_resolution.isValid() else 'Loading resolution…'
        size=sizefmt(viewer.current_file_size) if viewer.current_file_size is not None else 'Size unavailable'
        created=self.metadata_created_cache.get(raw_path)
        if created is None:
            created=datefmt(birth(path));self.metadata_created_cache[raw_path]=created
            if len(self.metadata_created_cache)>128:self.metadata_created_cache.popitem(last=False)
        else:self.metadata_created_cache.move_to_end(raw_path)
        font=QFont('Segoe UI',10);metrics=QFontMetricsF(font)
        first=f'{name}  •  {suffix}';second=(f'Pages: {resolution}  •  {size}  •  Created: {created}' if viewer.multipage_info else f'Resolution: {resolution}  •  {size}  •  Created: {created}')
        right_limit=self.page_counter_rect().left()-12
        if self.pdf_search_edit.isVisible():right_limit=min(right_limit,self.pdf_search_edit.geometry().left()-12)
        available=max(1,min(760,right_limit-16));text_width=min(max(1,available-24),max(180,math.ceil(metrics.horizontalAdvance(second))));wrap_flags=Qt.TextWordWrap|Qt.TextWrapAnywhere;first_height=max(22,math.ceil(metrics.boundingRect(QRectF(0,0,text_width,1200),wrap_flags,first).height()));self.metadata_requires_expand=metrics.horizontalAdvance(first)>text_width+1;fade_width=min(44,max(0,available-text_width-24));box_width=max(1,text_width+24+fade_width);expanded_height=min(max(76,first_height+48),max(58,self.height()-24));height=round(58+(expanded_height-58)*self.metadata_progress);rect=QRect(16,12,box_width,height);self.metadata_rect=rect
        if self.hover_zone=='metadata':self.set_metadata_expanded(self.metadata_requires_expand)
        background=QColor(8,10,14,170);gradient=QLinearGradient(rect.left(),0,rect.right(),0);solid_end=min(1.0,(text_width+24)/max(1,box_width));gradient.setColorAt(0,background);gradient.setColorAt(solid_end,background)
        if fade_width:gradient.setColorAt(1,QColor(background.red(),background.green(),background.blue(),0))
        p.save();p.setPen(Qt.NoPen);p.setBrush(gradient);p.drawRect(rect);p.setFont(font);p.setPen(QColor('#f2f2f2'))
        if self.metadata_progress<.98:p.drawText(QRectF(rect.x()+12,rect.y()+5,text_width,22),Qt.AlignLeft|Qt.AlignVCenter,metrics.elidedText(first,Qt.ElideMiddle,text_width))
        else:p.drawText(QRectF(rect.x()+12,rect.y()+5,text_width,first_height),Qt.AlignLeft|Qt.AlignTop|wrap_flags,first)
        details_y=rect.y()+(first_height+10 if self.metadata_progress>=.98 else 30)
        p.setPen(QColor('#c8c8c8'));p.drawText(QRectF(rect.x()+12,details_y,text_width,22),Qt.AlignLeft|Qt.AlignVCenter,metrics.elidedText(second,Qt.ElideRight,text_width));p.restore()
    def set_metadata_expanded(self,expanded):
        target=1.0 if expanded else 0.0
        if self.metadata_target==target:return
        self.metadata_target=target;self.metadata_animation_timer.start()
    def advance_metadata_animation(self):
        delta=self.metadata_target-self.metadata_progress
        if abs(delta)<.035:self.metadata_progress=self.metadata_target;self.metadata_animation_timer.stop()
        else:self.metadata_progress+=delta*.28
        self.update()
    def page_counter_rect(self):return QRect(max(8,self.width()-162),12,146,58)
    def sync_pdf_search_visibility(self):
        query_active=bool(self.pdf_search_edit.text().strip())
        show=bool(self.viewer.paths and Path(self.viewer.paths[self.viewer.index]).suffix.casefold()=='.pdf' and (query_active or self.hover_zone in ('top','metadata') or self.pdf_search_edit.hasFocus()))
        self.pdf_search_edit.setVisible(show)
        bar=self.container_scrollbar
        bar.setProperty('magellan_search_keep_visible',query_active)
        if query_active:
            if bar.maximum()>bar.minimum():bar.show()
        elif hasattr(self.viewer.application_owner(),'set_scrollbar_visibility'):
            owner=self.viewer.application_owner();owner.set_scrollbar_visibility(bar,not owner.scrollbars_auto_hide or bar.underMouse() or owner.scrollbar_pointer_in_reveal_zone(bar))
    def paint_page_counter(self,p):
        if not self.viewer.paths:return
        current,total=self.viewer.counter_position();rect=self.page_counter_rect();background=QColor(8,10,14,170);gradient=QLinearGradient(rect.left(),0,rect.right(),0);gradient.setColorAt(0,QColor(background.red(),background.green(),background.blue(),0));gradient.setColorAt(.18,background);gradient.setColorAt(1,background)
        p.save();p.setPen(Qt.NoPen);p.setBrush(gradient);p.drawRect(rect);p.setPen(QColor('#f2f2f2'));p.setFont(QFont('Segoe UI',11));p.drawText(rect.adjusted(8,0,0,0),Qt.AlignCenter,f'{current} of {total}');p.restore()
    def submit_page_counter(self):
        value=self.counter_editor.text().strip();self.counter_editor.hide()
        try:
            if value:self.viewer.go_to_position(max(0,int(value)-1))
        except (TypeError,ValueError,OverflowError):pass
    def eventFilter(self,watched,event):
        if watched is getattr(self,'counter_editor',None) and event.type()==QEvent.FocusOut:self.counter_editor.hide()
        if watched is getattr(self,'pdf_search_edit',None) and event.type()==QEvent.FocusOut:QTimer.singleShot(0,self.sync_pdf_search_visibility)
        return super().eventFilter(watched,event)
    def sync_zoom_slider(self):
        if self.viewer.compare_enabled:
            value=getattr(self.viewer,'compare_zoom',1.0)
        elif self.viewer.double_page and self.viewer.paths:
            key=self._double_page_key();value=self.viewer.double_page_zooms.get(key,1.0)
        else:value=self.viewer.reader_zoom if self.viewer.multipage_info or self.viewer.is_sequence_reader_mode() else self.viewer.zoom
        self.zoom_slider.blockSignals(True);self.zoom_slider.setValue(max(self.zoom_slider.minimum(),min(self.zoom_slider.maximum(),round(value*100))));self.zoom_slider.blockSignals(False)
    def _capture_reader_screen_center_anchor(self):
        center=QPoint(self.width()//2,self.height()//2);page=self._reader_page_at(center)
        if page is None:page=self.most_visible_reader_page()
        if page is None:return None
        rect=self._reader_content_page_rect(page)
        if rect is None:return None
        horizontal=self.viewer.is_horizontal_reader_mode();visible=QRect(rect);offset=self.container_scrollbar.value()
        if horizontal:visible.translate(-offset+self.pan_offset.x(),self.pan_offset.y())
        else:visible.translate(self.pan_offset.x(),-offset+self.pan_offset.y())
        fx=max(0.0,min(1.0,(center.x()-visible.left())/max(1,visible.width())))
        fy=max(0.0,min(1.0,(center.y()-visible.top())/max(1,visible.height())))
        return page,fx,fy,center,horizontal
    def _restore_reader_screen_center_anchor(self,anchor):
        if not anchor:return
        page,fx,fy,center,horizontal=anchor;rect=self._reader_content_page_rect(page)
        if rect is None:return
        if horizontal:
            self.container_scrollbar.setValue(max(0,min(self.container_scrollbar.maximum(),rect.left()+round(fx*rect.width())+self.pan_offset.x()-center.x())))
            self.pan_offset.setY(round(center.y()-(rect.top()+round(fy*rect.height()))))
        else:
            self.container_scrollbar.setValue(max(0,min(self.container_scrollbar.maximum(),rect.top()+round(fy*rect.height())+self.pan_offset.y()-center.y())))
            self.pan_offset.setX(round(center.x()-(rect.left()+round(fx*rect.width()))))
        self.update()
    def zoom_slider_changed(self,value):
        viewer=self.viewer;zoom=max(.1,min(8.0,value/100.0));center=QPoint(self.width()//2,self.height()//2)
        if viewer.compare_enabled:
            old=max(.1,float(getattr(viewer,'compare_zoom',1.0)));viewer.compare_zoom=zoom;box=self.compare_box_rect();center=box.center()
            viewer.compare_pan=self.pan_after_zoom(box,center,getattr(viewer,'compare_pan',QPoint()),zoom/old)
            self.update()
        elif viewer.double_page and viewer.paths:
            key=self._double_page_key()
            if key is None:return
            old=max(.1,float(viewer.double_page_zooms.get(key,1.0)));viewer.double_page_zooms[key]=zoom
            if abs(zoom-1.0)>.01:viewer.double_page_zoom_modes.add(key)
            else:viewer.double_page_zoom_modes.discard(key)
            slot=viewer.double_page_active_slot;area=QRect(slot*self.width()//2,0,(slot+1)*self.width()//2-slot*self.width()//2,self.height())
            viewer.double_page_pan[key]=self.pan_after_zoom(area,center,viewer.double_page_pan.get(key,QPoint()),zoom/old)
            self.update()
        elif viewer.is_sequence_reader_mode() or (viewer.multipage_info and viewer.multipage_mode in ('reader','double_reader')):
            anchor=self._capture_reader_screen_center_anchor();vertical_reader=not viewer.is_horizontal_reader_mode();viewer.zoom_bar_active=abs(zoom-1.0)>.01
            if vertical_reader:viewer.reader_zoom_mode_enabled=False
            else:viewer.reader_zoom_mode_enabled=abs(zoom-1.0)>.01
            viewer.set_reader_zoom(zoom)
            self._zoom_anchor_generation=getattr(self,'_zoom_anchor_generation',0)+1;generation=self._zoom_anchor_generation
            if anchor is not None:QTimer.singleShot(65,lambda token=generation,state=anchor:self._restore_delayed_zoom_anchor(token,state))
        else:
            old=max(.1,float(viewer.zoom));self.pan_offset=self.pan_after_zoom(self.rect(),center,self.pan_offset,zoom/old)
            viewer.reader_zoom_mode_enabled=abs(zoom-1.0)>.01;viewer.set_reader_zoom(zoom,rebuild=False);self.prepare_frame()
    def _restore_delayed_zoom_anchor(self,generation,anchor):
        if generation!=getattr(self,'_zoom_anchor_generation',0):return
        self._restore_reader_screen_center_anchor(anchor)
    @staticmethod
    def paint_control(p,rect,text):
        p.setPen(QPen(QColor(255,255,255,75),1));p.setBrush(QColor(0,0,0,150));p.drawRoundedRect(rect,7,7);p.setPen(QColor(255,255,255,230));font=QFont('Segoe UI' if len(text)>1 else 'Segoe UI Symbol',10 if len(text)>1 else 20);p.setFont(font);bounds=QFontMetricsF(font).boundingRect(text);p.drawText(QPointF(rect.center().x()-bounds.width()/2-bounds.x(),rect.center().y()-bounds.height()/2-bounds.y()),text)
    def paint_control_icon(self,p,rect,icon):
        if icon.isNull():return
        side=max(16,min(24,rect.height()-14));p.drawPixmap(QRect(rect.center().x()-side//2,rect.center().y()-side//2,side,side),icon)
    def mouseMoveEvent(self,e):
        pos=e.position().toPoint()
        self.viewer.note_pointer_activity()
        compare_box=self.compare_box_rect()
        if self.viewer.compare_enabled and not self.compare_gesture and 82<pos.y()<self.height()-72:
            pair=self.viewer.compare_pair()
            if pair:
                active=pair[0] if pos.x()<compare_box.left()+compare_box.width()*self.viewer.compare_slider else pair[1]
                if active!=self.viewer.compare_active_key:self.viewer.compare_active_key=active;self.sync_zoom_slider()
        if self.viewer.compare_enabled and self.compare_gesture and self.press_pos is not None and e.buttons()&Qt.LeftButton:
            if not self.compare_dragging:
                if (pos-self.press_pos).manhattanLength()<QApplication.startDragDistance():e.accept();return
                self.compare_dragging=True;self.setCursor(Qt.ClosedHandCursor)
            delta=pos-self.last_pos
            if self.compare_divider_drag:
                box=self.compare_box_rect();self.viewer.compare_slider=max(.02,min(.98,(pos.x()-box.left())/max(1,box.width())))
            elif self.compare_box_drag:
                offset=getattr(self.viewer,'compare_box_offset',QPoint())+delta;limit_x=max(0,self.width()-100);limit_y=max(0,self.height()-100)
                self.viewer.compare_box_offset=QPoint(max(-limit_x,min(limit_x,offset.x())),max(-limit_y,min(limit_y,offset.y())))
            else:
                key=getattr(self.viewer,'compare_active_key',None)
                if key is not None:
                    pans=getattr(self.viewer,'compare_image_pans',None)
                    if not isinstance(pans,dict):pans={};self.viewer.compare_image_pans=pans
                    pans[key]=pans.get(key,QPoint())+delta
            self.last_pos=pos;self.update();e.accept();return
        if self.press_pos is not None and e.buttons() & Qt.LeftButton:
            if not self.dragging and (pos-self.press_pos).manhattanLength()>=QApplication.startDragDistance():self.dragging=True;self.setCursor(Qt.ClosedHandCursor)
            if self.dragging:
                delta=pos-self.last_pos
                if self.viewer.double_page and self.viewer.paths and self.double_page_drag_path is not None:
                    path=self.double_page_drag_path;self.viewer.double_page_pan[path]=self.viewer.double_page_pan.get(path,QPoint())+delta
                elif self.viewer.is_horizontal_reader_mode():
                    if self.viewer.reader_zoom_mode_enabled:self.pan_offset+=delta
                    else:self.container_scrollbar.setValue(self.container_scrollbar.value()-delta.x())
                elif self.viewer.multipage_info or self.viewer.is_sequence_reader_mode():
                    if self.viewer.reader_zoom_mode_enabled:self.pan_offset+=delta
                    else:self.container_scrollbar.setValue(self.container_scrollbar.value()-delta.y())
                else:self.pan_offset+=delta
                self.last_pos=pos;self.update();e.accept();return
        center_left=self.width()*.25<=pos.x()<=self.width()*.75
        in_metadata=self.metadata_rect.adjusted(-2,-2,2,2).contains(pos)
        zone='metadata' if in_metadata else ('top' if pos.y()<=82 else ('bottom' if pos.y()>=self.height()-112 and center_left else ('previous' if pos.x()<80 else ('next' if pos.x()>self.width()-80 else ''))))
        if zone!=self.hover_zone:
            self.hover_zone=zone
            self.set_metadata_expanded(zone=='metadata' and getattr(self,'metadata_requires_expand',False))
            if zone=='bottom':self.sync_zoom_slider();self.zoom_slider.show()
            else:self.zoom_slider.hide()
            self.sync_pdf_search_visibility()
            self.update()
        if self.viewer.double_page and self.viewer.paths and zone not in ('bottom','top','metadata'):
            self.viewer.double_page_active_slot=0 if pos.x()<self.width()//2 else 1
            self.sync_zoom_slider()
    def leaveEvent(self,e):
        self.hover_zone='';self.set_metadata_expanded(False);self.zoom_slider.hide();self.sync_pdf_search_visibility();self.update();super().leaveEvent(e)
    def contextMenuEvent(self,e):e.accept()
    def keyPressEvent(self,e):
        if self.viewer.handle_viewer_key(e):e.accept();return
        amount=max(24,round(min(self.width(),self.height())*.06))
        offsets={Qt.Key_W:QPoint(0,amount),Qt.Key_A:QPoint(amount,0),Qt.Key_S:QPoint(0,-amount),Qt.Key_D:QPoint(-amount,0)}
        if e.key() in offsets:self.pan_offset+=offsets[e.key()];self.update();e.accept();return
        super().keyPressEvent(e)
    def keyReleaseEvent(self,e):
        self.viewer.handle_viewer_key_release(e);super().keyReleaseEvent(e)
    def mousePressEvent(self,e):
        if e.button()==Qt.RightButton:
            app=QApplication.instance()
            if app is not None:app._magellan_suppress_context_until=time.monotonic()+1.0
            self.right_close_pending=True;e.accept();return
        if e.button()!=Qt.LeftButton:return
        pos=e.position().toPoint()
        controls=self.page_counter_rect().contains(pos) or (self.zoom_slider.isVisible() and self.zoom_slider.geometry().contains(pos))
        if self.hover_zone=='bottom':controls=controls or any(rect.contains(pos) for rect in self.bottom_buttons())
        if self.hover_zone in ('top','metadata') and self.pdf_search_edit.isVisible() and self.pdf_search_edit.geometry().contains(pos):controls=True
        if self.viewer.compare_enabled and not controls:
            pair=self.viewer.compare_pair()
            if not pair:return
            self.compare_gesture=True;self.compare_dragging=False;self.press_pos=pos;self.last_pos=pos;self.dragging=False
            box=self.compare_box_rect();self.viewer.compare_active_key=pair[0] if pos.x()<box.left()+box.width()*self.viewer.compare_slider else pair[1]
            divider_x=round(box.left()+box.width()*self.viewer.compare_slider)
            self.compare_divider_drag=not bool(e.modifiers()&Qt.ShiftModifier) and abs(pos.x()-divider_x)<=12
            self.compare_box_drag=bool(e.modifiers()&Qt.ShiftModifier)
            self.compare_drag_path=None
            self.setCursor(Qt.SplitHCursor if self.compare_divider_drag else Qt.OpenHandCursor)
            self.update();e.accept();return
        is_control=controls
        self.press_pos=pos if not is_control else None;self.last_pos=pos;self.dragging=False
        self.double_page_drag_path=None
        if not is_control and self.viewer.double_page and self.viewer.paths:
            self.viewer.double_page_active_slot=0 if pos.x()<self.width()//2 else 1
            self.double_page_drag_path=self._double_page_key()
    def dispatch_click(self):
        pos=self.pending_click;self.pending_click=None
        if pos is None:return
        if pos.y()>=self.height()-72 and self.width()*.25<=pos.x()<=self.width()*.75:
            for i,rect in enumerate(self.bottom_buttons()):
                if rect.contains(pos):
                    if i==4:self.viewer.fit_to_screen()
                    elif i==5:self.viewer.cycle_visualization_mode()
                    elif i==6:self.viewer.toggle_compare_mode()
                    elif i==7:self.viewer.toggle_slideshow()
                    else:self.viewer.transform_image(i)
                    return
        if pos.x()<80:self.viewer.step(-1)
        elif pos.x()>self.width()-80:self.viewer.step(1)
    def mouseReleaseEvent(self,e):
        if e.button()==Qt.RightButton and self.right_close_pending:
            self.right_close_pending=False;e.accept();QTimer.singleShot(120,self.viewer.close);return
        if e.button()!=Qt.LeftButton:return
        pos=e.position().toPoint();was_dragging=self.dragging;self.press_pos=None;self.dragging=False;self.setCursor(Qt.ArrowCursor)
        if self.compare_gesture:self.compare_gesture=False;self.compare_dragging=False;self.compare_drag_path=None;self.compare_divider_drag=False;self.compare_box_drag=False;self.setCursor(Qt.ArrowCursor);self.press_pos=None;e.accept();return
        if was_dragging:return
        if getattr(self,'suppress_control_release',False):self.suppress_control_release=False;return
        if self.hover_zone in ('top','metadata') and self.page_counter_rect().contains(pos):
            current,total=self.viewer.counter_position();self.counter_editor.setText(str(current));self.counter_editor.setValidator(QIntValidator(1,max(1,total),self.counter_editor));self.counter_editor.setGeometry(self.page_counter_rect().adjusted(12,5,-12,-5));self.counter_editor.show();self.counter_editor.selectAll();self.counter_editor.setFocus(Qt.MouseFocusReason)
            return
        self.pending_click=pos;self.dispatch_click()
    def mouseDoubleClickEvent(self,e):
        if e.button()!=Qt.LeftButton:return
        pos=e.position().toPoint()
        control=self.page_counter_rect().contains(pos) or (pos.y()>=self.height()-72 and any(r.contains(pos) for r in self.bottom_buttons())) or (not self.viewer.compare_enabled and (pos.x()<80 or pos.x()>self.width()-80)) or (self.zoom_slider.isVisible() and self.zoom_slider.geometry().contains(pos))
        self.click_timer.stop();self.pending_click=None
        if control:
            # Qt delivers a double-click event instead of the second press.
            # Treat it as another button activation, then consume its release.
            self.suppress_control_release=True
            if (pos.y()>=self.height()-72 and any(r.contains(pos) for r in self.bottom_buttons())) or (not self.viewer.compare_enabled and not self.page_counter_rect().contains(pos) and (pos.x()<80 or pos.x()>self.width()-80)):
                self.pending_click=pos;self.dispatch_click()
            return
        if self.viewer.compare_enabled:
            key=getattr(self.viewer,'compare_active_key',None)
            if key is None:
                pair=self.viewer.compare_pair()
                if not pair:return
                box=self.compare_box_rect();key=pair[0] if pos.x()<box.left()+box.width()*self.viewer.compare_slider else pair[1];self.viewer.compare_active_key=key
            zooms=getattr(self.viewer,'compare_image_zooms',None)
            if not isinstance(zooms,dict):zooms={};self.viewer.compare_image_zooms=zooms
            pans=getattr(self.viewer,'compare_image_pans',None)
            if not isinstance(pans,dict):pans={};self.viewer.compare_image_pans=pans
            old=max(.1,float(zooms.get(key,1.0)))
            if old>1.01:zooms[key]=1.0;pans[key]=QPoint()
            else:
                new=max(2.0,old);zooms[key]=new;pans[key]=self.pan_after_zoom(self.compare_box_rect(),pos,pans.get(key,QPoint()),new/old)
            self.suppress_control_release=True;self.update()
            return
        if self.viewer.double_page and self.viewer.paths:
            slot=0 if pos.x()<self.width()//2 else 1;self.viewer.double_page_active_slot=slot;key=self._double_page_key(slot)
            if key is None:return
            if key in self.viewer.double_page_zoom_modes:self.viewer.double_page_zoom_modes.discard(key);self.viewer.double_page_zooms[key]=1.0;self.viewer.double_page_pan[key]=QPoint()
            else:
                old=max(.1,float(self.viewer.double_page_zooms.get(key,1.0)));new=max(2.0,old);self.viewer.double_page_zoom_modes.add(key);self.viewer.double_page_zooms[key]=new
                area=QRect(slot*self.width()//2,0,(slot+1)*self.width()//2-slot*self.width()//2,self.height())
                self.viewer.double_page_pan[key]=self.pan_after_zoom(area,pos,self.viewer.double_page_pan.get(key,QPoint()),new/old)
            self.sync_zoom_slider();self.update();return
        self.toggle_reader_zoom_at(pos)
    def wheelEvent(self,e):
        viewer=self.viewer;delta=e.angleDelta().y() or e.pixelDelta().y()
        if viewer.compare_enabled:
            if not delta:return
            pos=e.position().toPoint();pair=viewer.compare_pair();box=self.compare_box_rect()
            if pair:
                key=pair[0] if pos.x()<box.left()+box.width()*viewer.compare_slider else pair[1];viewer.compare_active_key=key
                zooms=getattr(viewer,'compare_image_zooms',None)
                if not isinstance(zooms,dict):zooms={};viewer.compare_image_zooms=zooms
                pans=getattr(viewer,'compare_image_pans',None)
                if not isinstance(pans,dict):pans={};viewer.compare_image_pans=pans
                old=max(.1,min(8.0,float(zooms.get(key,1.0))));new_zoom=max(.1,min(8.0,old*(1.15**(delta/120.0))));pans[key]=self.pan_after_zoom(box,pos,pans.get(key,QPoint()),new_zoom/old);zooms[key]=new_zoom
                self.update();e.accept();return
            e.accept();return
        if viewer.double_page and viewer.paths:
            slot=0 if e.position().x()<self.width()//2 else 1;viewer.double_page_active_slot=slot;path=self._double_page_key(slot);amount=delta or e.angleDelta().x() or e.pixelDelta().x()
            zoom_by_default=getattr(viewer.application_owner(),'viewer_wheel_behavior','swipe')=='zoom'
            if path is None:
                if amount:
                    for _ in range(max(1,min(12,round(abs(amount)/120.0)))):viewer.step(1 if amount<0 else -1)
                    e.accept()
                return
            if path not in viewer.double_page_zoom_modes and not zoom_by_default:
                if amount:
                    for _ in range(max(1,min(12,round(abs(amount)/120.0)))):viewer.step(1 if amount<0 else -1)
                    e.accept();return
            if not amount:return
            old=max(.1,float(viewer.double_page_zooms.get(path,1.0)));new=max(.1,min(8.0,old*(1.15**(amount/120.0))));viewer.double_page_zoom_modes.add(path);viewer.double_page_zooms[path]=new
            area=QRect(slot*self.width()//2,0,(slot+1)*self.width()//2-slot*self.width()//2,self.height());viewer.double_page_pan[path]=self.pan_after_zoom(area,e.position().toPoint(),viewer.double_page_pan.get(path,QPoint()),new/old);self.sync_zoom_slider();self.update();e.accept();return
        if viewer.multipage_info or viewer.is_sequence_reader_mode():
            horizontal=viewer.is_horizontal_reader_mode();amount=(e.pixelDelta().x() or e.angleDelta().x()) if horizontal else delta
            if not amount:amount=e.pixelDelta().y() or e.angleDelta().y()
            if e.modifiers() & Qt.ControlModifier and (viewer.is_sequence_reader_mode() or viewer.multipage_mode in ('reader','double_reader')):viewer.reader_zoom_mode_enabled=True
            zoom_by_default=getattr(viewer.application_owner(),'viewer_wheel_behavior','swipe')=='zoom'
            if viewer.reader_zoom_mode_enabled or zoom_by_default:
                zoom_amount=delta or e.pixelDelta().y() or e.angleDelta().x() or e.pixelDelta().x()
                if zoom_amount:
                    viewer.reader_zoom_mode_enabled=True
                    self.zoom_reader_at(e.position().toPoint(),1.15**(zoom_amount/120.0));e.accept();return
            if viewer.multipage_info and viewer.multipage_mode in ('normal','double_page'):
                if amount:
                    for _ in range(max(1,min(12,round(abs(amount)/120.0)))):viewer.step(1 if amount<0 else -1)
                    e.accept();return
            if amount:
                speed=max(.1,min(10.0,float(getattr(viewer,'reader_scroll_speed',1.0))));bar=self.container_scrollbar;old_value=bar.value();maximum=bar.maximum();continuous=bool(getattr(viewer.application_owner(),'continuous_viewer_swipe',False))
                if continuous and viewer.multipage_info and viewer.multipage_mode in ('reader','double_reader') and ((amount<0 and old_value>=maximum) or (amount>0 and old_value<=0)):
                    viewer.step(1 if amount<0 else -1,force_container=True);e.accept();return
                bar.setValue(old_value-round(amount*speed));e.accept();return
            return
        if not delta:return
        app=viewer.application_owner();zoom_by_default=getattr(app,'viewer_wheel_behavior','swipe')=='zoom'
        if not viewer.wheel_zoom_enabled and not zoom_by_default:
            for _ in range(max(1,min(12,round(abs(delta)/120.0)))):viewer.step(1 if delta<0 else -1)
            e.accept();return
        self.zoom_fitted_page_at(e.position().toPoint(),1.15**(delta/120.0));e.accept()


class MediaViewer(QMainWindow):
    def __init__(self,paths,index=0,parent=None,slideshow_interval_ms=5000,detached=False):
        super().__init__(None if detached else parent,Qt.Window);self._application_owner=parent;self.paths=list(paths);self.farview_sequence=[];self.farview_page_cache=OrderedDict();self.farview_prefetch_future=None;self.farview_prefetch_pending=None;self.farview_prefetch_failed=set();self.farview_active=False;self.farview_index=0;self.forced_single_page_mode=False;self.index=max(0,min(int(index),len(self.paths)-1)) if self.paths else -1;self.rotation=0;self.flip_h=False;self.flip_v=False;self.zoom=1.0;self.wheel_zoom_enabled=False;self.zoom_bar_active=False;self.reader_zoom=max(.1,min(8.0,float(getattr(parent,'reader_zoom',1.0))));self.reader_zoom_mode_enabled=abs(self.reader_zoom-1.0)>0.01;self.reader_scroll_speed=max(.1,min(10.0,float(getattr(parent,'reader_scroll_speed',1.0))));self.movie=None;self.full_image=QImage();self.static_pixmap=QPixmap();self.fit_pixmap=QPixmap();self.original_resolution=QSize();self.double_page=False;self.double_page_zooms={};self.double_page_zoom_modes=set();self.double_page_pan={};self.double_page_active_slot=0;self.compare_enabled=False;self.compare_slider=.5;self.compare_active_key=None;self.compare_zoom=1.0;self.compare_pan=QPoint();self.compare_box_offset=QPoint();self.double_page_path=None;self.double_page_frame=QPixmap();self.loading=False;self.current_file_size=None;self.image_cache=OrderedDict();self.image_cache_bytes=0;self.image_cache_limit=256*1024*1024;self.cache_max_dimension=8000;self.cache_max_file_bytes=100*1024*1024;self.pending_image_paths=set();self.image_load_futures={};self.pending_delete_path=None;self.navigation_key=None;self.navigation_started=0.0;self.slideshow_active=False;self.multipage_info=None;self.multipage_info_pending=False;self.multipage_images=OrderedDict();self.multipage_page_errors=set();self.multipage_page_error_messages={};self.visualization_mode='reader' if self.paths and Path(self.paths[self.index]).suffix.casefold() in MULTIPAGE_EXTS else 'normal';self.multipage_mode=self.visualization_mode;self.double_page=self.visualization_mode=='double_page';self.multipage_double_page=self.visualization_mode=='double_reader';self.multipage_normal_page=0;self.sequence_aspect_ratios={};self.multipage_image_bytes=0;self.multipage_image_cache_limit=256*1024*1024;self.multipage_pending=set();self.multipage_preload_queue=deque();self.multipage_page_queue=deque();self.multipage_page_active_pages=set();self.multipage_page_job_active=False;self.multipage_page_job_id=0;self.multipage_page_job_future=None;self.multipage_error=None;self.multipage_generation=0;self._closed=False;self.slideshow_timer=QTimer(self);self.slideshow_timer.setInterval(max(250,int(slideshow_interval_ms)));self.slideshow_timer.timeout.connect(lambda:self.step(1));app=QApplication.instance();self.load_bridge=getattr(app,'_viewer_image_load_bridge',None)
        if self.load_bridge is None:self.load_bridge=ViewerImageLoadBridge(app);app._viewer_image_load_bridge=self.load_bridge
        self.apply_file_visualization_mode(self.paths[self.index] if self.paths else '')
        self.pointer_hide_timer=QTimer(self);self.pointer_hide_timer.setSingleShot(True);self.pointer_hide_timer.setInterval(1000);self.pointer_hide_timer.timeout.connect(self.hide_slideshow_cursor)
        self.load_bridge.loaded.connect(self.on_image_loaded);self.setWindowTitle('Magellan Viewer');self.canvas=MediaCanvas(self);self.pdf_search_document=None;self.pdf_search_path=None;self.pdf_search_model=QPdfSearchModel(self);self.pdf_search_results=[];self.pdf_search_results_by_page={};self.pdf_search_result_index=-1;self.pdf_search_active_key=None;self.pdf_search_scan_generation=0;self.pdf_search_scan_cursor=0;self.pdf_search_scan_count=0;self.pdf_search_scan_buffer=[];self.pdf_search_seen=set();self.pdf_search_scan_complete=True;self.pdf_search_pending_advances=0;self._pdf_search_advancing=False;self._pdf_search_last_advance=0.0;self.pdf_search_scan_timer=QTimer(self);self.pdf_search_scan_timer.setSingleShot(True);self.pdf_search_scan_timer.setInterval(100);self.pdf_search_scan_timer.timeout.connect(self._scan_pdf_search_results);self.pdf_search_model.countChanged.connect(self.pdf_search_results_changed);self.pdf_search_shortcut=QShortcut(QKeySequence('Ctrl+F'),self);self.pdf_search_shortcut.activated.connect(self.focus_pdf_search);self.reader_zoom_save_timer=QTimer(self);self.reader_zoom_save_timer.setSingleShot(True);self.reader_zoom_save_timer.setInterval(300);self.reader_zoom_save_timer.timeout.connect(lambda:parent.save_settings() if parent is not None else None);self.setContextMenuPolicy(Qt.PreventContextMenu);self.canvas.setContextMenuPolicy(Qt.PreventContextMenu);self.setAttribute(Qt.WA_NoMousePropagation,True);self.canvas.setAttribute(Qt.WA_NoMousePropagation,True);self.canvas.setContentsMargins(0,0,0,0);self.setContentsMargins(0,0,0,0);self.setCentralWidget(self.canvas);self.setStyleSheet('QMainWindow{background:#080a10;border:0px}');
        if parent is not None and hasattr(parent,'register_scrollbars'):parent.register_scrollbars(self.canvas)
    def application_owner(self):
        owner=getattr(self,'_application_owner',None)
        return owner if owner is not None else self.parent()
    def _mode_key_for_path(self,path):
        return os.path.normcase(os.path.abspath(os.fspath(path)))
    def remember_file_visualization_mode(self,path,mode=None):
        if not path:return
        parent=self.application_owner()
        if parent is None:return
        modes=getattr(parent,'viewer_file_modes',None)
        if not isinstance(modes,dict):modes={};parent.viewer_file_modes=modes
        modes[self._mode_key_for_path(path)]=mode or self.visualization_mode
    def apply_file_visualization_mode(self,path):
        parent=self.application_owner();modes=getattr(parent,'viewer_file_modes',None)
        if not isinstance(modes,dict):
            modes={}
            if parent is not None:parent.viewer_file_modes=modes
        key=self._mode_key_for_path(path) if path else ''
        mode=modes.get(key) if key else None
        suffix=Path(path).suffix.casefold() if path else ''
        pending_member=getattr(self,'pending_archive_member',None)
        if suffix in ('.zip','.rar'):
            mode='normal'
            if key:modes[key]='normal'
        if pending_member and Path(path).suffix.casefold() in ('.zip','.rar') and Path(str(pending_member)).suffix.casefold() in IMAGE_EXTS:
            mode='normal'
        if getattr(self,'forced_single_page_mode',False):mode='normal'
        if mode not in ('normal','double_page','reader','double_reader'):
            mode='reader' if path and Path(path).suffix.casefold() in MULTIPAGE_EXTS else 'normal'
            if key:modes[key]=mode
        self.visualization_mode=mode;self.multipage_mode=mode;self.double_page=mode=='double_page';self.multipage_double_page=mode=='double_reader';self.wheel_zoom_enabled=False;self.zoom_bar_active=mode in ('reader','double_reader') and abs(self.zoom-1.0)>0.01
    @property
    def reader_zoom(self):return self.zoom
    @reader_zoom.setter
    def reader_zoom(self,value):self.zoom=max(.1,min(8.0,float(value)))
    @property
    def reader_zoom_mode_enabled(self):return self.wheel_zoom_enabled
    @reader_zoom_mode_enabled.setter
    def reader_zoom_mode_enabled(self,value):self.wheel_zoom_enabled=bool(value)
    def current_frame(self):
        if self.movie is not None:return self.movie.currentPixmap()
        if self.is_sequence_reader_mode():
            if self.reader_zoom<=1.0 and not self.fit_pixmap.isNull():return self.fit_pixmap
            if self.static_pixmap.isNull() and not self.full_image.isNull():self.static_pixmap=QPixmap.fromImage(self.full_image)
            return self.static_pixmap
        if self.zoom<=1.0 and not self.fit_pixmap.isNull():return self.fit_pixmap
        if self.static_pixmap.isNull() and not self.full_image.isNull():self.static_pixmap=QPixmap.fromImage(self.full_image)
        return self.static_pixmap
    def page_source(self,index):
        """Return the best decoded source for the shared canvas renderer."""
        if self.multipage_info:return self.multipage_images.get(index,QImage())
        if not self.paths or not 0<=index<len(self.paths):return QPixmap()
        path=self.paths[index]
        if index==self.index:
            if self.movie is not None:
                frame=self.movie.currentPixmap()
                if not frame.isNull():return frame
            if not self.full_image.isNull():return self.full_image
            frame=self.current_frame()
            if not frame.isNull():return frame
        if self.double_page_path==path and not self.double_page_frame.isNull():return self.double_page_frame
        entry=self.image_cache.get(path)
        if entry is None:return QPixmap()
        self.image_cache.move_to_end(path)
        if entry[0] is not None and not entry[0].isNull():return entry[0]
        return entry[1]
    @staticmethod
    def pixmap_bytes(pixmap):return pixmap.width()*pixmap.height()*max(1,(pixmap.depth()+7)//8)
    def cache_entry_bytes(self,entry):
        full,fit=entry[:2]
        return full.sizeInBytes()+self.pixmap_bytes(fit)
    def cache_window_contains(self,path):
        if self.is_sequence_reader_mode() and path in self.canvas.visible_sequence_paths:return True
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
        self.double_page_path=None;self.double_page_frame=QPixmap();self.canvas.clear_page_render_cache()
    def multipage_position_key(self,path):return os.path.normcase(os.path.abspath(path))
    def multipage_zoom_key(self,page):
        path=self.paths[self.index] if self.paths and 0<=self.index<len(self.paths) else ''
        return ('multipage',self.multipage_position_key(path) if path else '',int(page))
    def current_multipage_page(self):
        if not self.multipage_info or not self.canvas.page_layout:return 0
        return max(0,min(len(self.canvas.page_layout)-1,self.multipage_normal_page))
    def update_multipage_current(self,value):
        if not self.multipage_info or not self.canvas.page_tops:return
        if self.multipage_mode in ('normal','double_page'):page=self.multipage_normal_page
        elif self.multipage_double_page:
            page=max(0,min(len(self.canvas.page_tops)-1,bisect.bisect_right(self.canvas.page_tops,value)-1))
        else:page=max(0,min(len(self.canvas.page_tops)-1,bisect.bisect_right(self.canvas.page_tops,value)-1))
        self.multipage_normal_page=page
        parent=self.application_owner()
        if parent is not None and self.paths:parent.multipage_page_positions[self.multipage_position_key(self.paths[self.index])]=page
        self.canvas.update()
    def update_sequence_current(self,value):
        if not self.canvas.sequence_pages or not self.paths:return
        index=max(0,min(len(self.paths)-1,bisect.bisect_right(self.canvas.page_tops,value)-1))
        if index==self.index:return
        self.index=index;path=self.paths[index]
        if Path(path).suffix.casefold() in IMAGE_EXTS:self.remember_file_visualization_mode(path)
        entry=self.image_cache.get(path)
        if entry is not None:
            self.image_cache.move_to_end(path);self.full_image,self.fit_pixmap=entry[:2];self.static_pixmap=QPixmap();self.original_resolution=entry[2] if len(entry)>2 and entry[2].isValid() else self.full_image.size();self.current_file_size=entry[3] if len(entry)>3 else None;self.loading=False
        else:
            self.full_image=QImage();self.fit_pixmap=QPixmap();self.static_pixmap=QPixmap();self.original_resolution=QSize();self.current_file_size=None;self.loading=True;self.request_image(path,preload=False)
        self.begin_pdf_search(path);self.canvas.sync_pdf_search_visibility();self.canvas.update()
    def counter_position(self):
        if self.farview_active:return self.farview_index+1,len(self.farview_sequence)
        if self.multipage_info:return self.current_multipage_page()+1,int(self.multipage_info.get('count',1))
        return (self.index+1 if self.index>=0 else 0),len(self.paths)
    def begin_pdf_search(self,path):
        if self.pdf_search_path==path:return
        self.pdf_search_scan_timer.stop();self.pdf_search_scan_generation+=1;self.pdf_search_scan_cursor=0;self.pdf_search_scan_count=0;self.pdf_search_scan_buffer=[];self.pdf_search_seen=set();self.pdf_search_scan_complete=True;self.pdf_search_pending_advances=0;target_path=path if Path(path).suffix.casefold()=='.pdf' else None;self.pdf_search_path=target_path;self.pdf_search_results=[];self.pdf_search_results_by_page={};self.pdf_search_result_index=-1;self.pdf_search_active_key=None;self.canvas.container_scrollbar.set_search_markers([]);self.canvas.pdf_search_edit.clear();self.pdf_search_model.setSearchString('')
        old=self.pdf_search_document;self.pdf_search_document=None;self.pdf_search_model.setDocument(None)
        if old is not None:
            try:old.close();old.deleteLater()
            except RuntimeError:logger.debug('Prior PDF search document was already released')
        if self.pdf_search_path is None:return
        document=QPdfDocument(self);self.pdf_search_document=document;document.statusChanged.connect(lambda status,d=document,p=path:self.attach_pdf_search_document(d,p) if status==QPdfDocument.Status.Ready else None);document.load(path)
        if document.status()==QPdfDocument.Status.Ready:self.attach_pdf_search_document(document,path)
        elif document.status()==QPdfDocument.Status.Error:logger.warning('PDF text search could not load document: %s',path)
    def focus_pdf_search(self):
        if self.pdf_search_path is None:return
        self.canvas.hover_zone='metadata';self.canvas.sync_pdf_search_visibility();self.canvas.pdf_search_edit.show();self.canvas.pdf_search_edit.setFocus(Qt.ShortcutFocusReason);self.canvas.update()
    def attach_pdf_search_document(self,document,path):
        if document is not self.pdf_search_document or path!=self.pdf_search_path:return
        self.pdf_search_model.setDocument(document);query=self.canvas.pdf_search_edit.text().strip();self.pdf_search_model.setSearchString(query)
    def set_pdf_search_query(self,query):
        if self.pdf_search_path is None:return
        self.pdf_search_scan_timer.stop();self.pdf_search_scan_generation+=1;self.pdf_search_scan_cursor=0;self.pdf_search_scan_count=0;self.pdf_search_results=[];self.pdf_search_results_by_page={};self.pdf_search_result_index=-1;self.pdf_search_active_key=None;self.pdf_search_pending_advances=0;self.pdf_search_scan_buffer=[];self.pdf_search_seen=set();self.pdf_search_scan_complete=not bool(query.strip());self.canvas.container_scrollbar.set_search_markers([]);self.pdf_search_model.setSearchString(query.strip());self.pdf_search_results_changed();self.canvas.sync_pdf_search_visibility();self.canvas.update()
    def _pdf_result_key(self,result):
        try:
            page,rectangles=result
            rect_key=tuple(tuple(round(float(getattr(rect,field)()),3) for field in ('x','y','width','height')) for rect in rectangles)
            return int(page),rect_key
        except (RuntimeError,AttributeError,TypeError,ValueError,OverflowError):return repr(result)
    def _pdf_active_result_key(self):
        if 0<=self.pdf_search_result_index<len(self.pdf_search_results):return self._pdf_result_key(self.pdf_search_results[self.pdf_search_result_index])
        return self.pdf_search_active_key
    def _publish_pdf_search_batch(self,batch,final=False):
        added=False
        for result in batch:
            try:
                page,rectangles=result;page=int(page);rectangles=tuple(rectangles)
                if page<0 or not rectangles:continue
                key=self._pdf_result_key((page,rectangles))
                if key in self.pdf_search_seen:continue
                self.pdf_search_seen.add(key);result_index=len(self.pdf_search_results);entry=(page,rectangles)
                self.pdf_search_results.append(entry);self.pdf_search_results_by_page.setdefault(page,[]).append((result_index,rectangles));added=True
            except (TypeError,ValueError,IndexError,RuntimeError,OverflowError):continue
        if final:
            self.pdf_search_scan_complete=True
            self.update_pdf_search_markers()
        if self.pdf_search_pending_advances and self.pdf_search_results:
            index=self.pdf_search_result_index;pending=self.pdf_search_pending_advances;self.pdf_search_pending_advances=0
            if index<0:
                consumed=min(pending,len(self.pdf_search_results));index=consumed-1;pending-=consumed
            else:
                consumed=min(pending,max(0,len(self.pdf_search_results)-index-1));index+=consumed;pending-=consumed
            if final and pending:index=(max(-1,index)+pending)%len(self.pdf_search_results);pending=0
            self.pdf_search_pending_advances=pending
            if index>=0:self._show_pdf_search_result(index)
        elif final:self.pdf_search_pending_advances=0
        if added or final:self.canvas.update()
    def pdf_search_results_changed(self,*_):
        if self.pdf_search_path is None:return
        try:self.pdf_search_scan_count=min(10000,max(0,int(self.pdf_search_model.count())))
        except (RuntimeError,TypeError,ValueError,OverflowError):self.pdf_search_scan_count=0
        if self.pdf_search_scan_count==0:
            if not self.canvas.pdf_search_edit.text().strip():
                self.pdf_search_scan_complete=True;self.pdf_search_pending_advances=0;self.canvas.container_scrollbar.set_search_markers([]);self.canvas.update()
            else:
                self.pdf_search_scan_complete=False
                if not self.pdf_search_scan_timer.isActive():self.pdf_search_scan_timer.start(80)
            return
        if self.pdf_search_scan_cursor>self.pdf_search_scan_count:
            self.pdf_search_scan_cursor=0;self.pdf_search_results=[];self.pdf_search_results_by_page={};self.pdf_search_seen=set();self.pdf_search_result_index=-1;self.pdf_search_scan_buffer=[]
        self.pdf_search_scan_complete=self.pdf_search_scan_cursor>=self.pdf_search_scan_count
        if not self.pdf_search_scan_complete and not self.pdf_search_scan_timer.isActive():self.pdf_search_scan_timer.start(8)
    def _scan_pdf_search_results(self):
        if self.pdf_search_path is None:return
        generation=self.pdf_search_scan_generation;model=self.pdf_search_model;start=self.pdf_search_scan_cursor;end=min(self.pdf_search_scan_count,start+48)
        batch=[]
        for index in range(start,end):
            try:
                link=model.resultAtIndex(index)
                if not link.isValid():continue
                page=int(link.page());rectangles=tuple(link.rectangles())
                batch.append((page,rectangles))
            except (RuntimeError,AttributeError,IndexError,TypeError,ValueError,OverflowError):continue
        if generation!=self.pdf_search_scan_generation or self.pdf_search_path is None:return
        self.pdf_search_scan_cursor=end;final=end>=self.pdf_search_scan_count;self._publish_pdf_search_batch(batch,final=final)
        if end<self.pdf_search_scan_count:
            self.pdf_search_scan_timer.start(8);return
    def next_pdf_search_result(self):
        if self._pdf_search_advancing:return
        if not self.pdf_search_results:
            if not self.pdf_search_scan_complete:self.pdf_search_pending_advances=min(100,self.pdf_search_pending_advances+1)
            return
        if self.pdf_search_result_index<0:self.pdf_search_result_index=0
        elif self.pdf_search_result_index+1<len(self.pdf_search_results):self.pdf_search_result_index+=1
        elif not self.pdf_search_scan_complete:
            self.pdf_search_pending_advances=min(100,self.pdf_search_pending_advances+1);return
        else:self.pdf_search_result_index=0
        self._show_pdf_search_result(self.pdf_search_result_index)
    def _show_pdf_search_result(self,index):
        if not 0<=index<len(self.pdf_search_results):return
        self._pdf_search_advancing=True
        try:
            page,rectangles=self.pdf_search_results[index];page=int(page)
            if page<0:return
            self.pdf_search_active_key=self._pdf_result_key((page,rectangles));page_sizes=(self.multipage_info or {}).get('page_sizes',[])
            if self.multipage_mode=='reader' and not self.multipage_double_page and rectangles and 0<=page<len(page_sizes) and page<len(self.canvas.page_layout) and page<len(self.canvas.page_tops):
                page_height=float(page_sizes[page][1]);rendered_height=self.canvas.page_layout[page][1]
                if page_height>0 and rendered_height>0:
                    match=rectangles[0];center_y=float(match.y()+match.height()/2);target=self.canvas.page_tops[page]+round(center_y*rendered_height/page_height)-self.canvas.height()//3
                    self.canvas.container_scrollbar.setValue(max(0,min(self.canvas.container_scrollbar.maximum(),target)))
                    self.canvas.request_visible_container_pages()
                else:self.canvas.scroll_to_page(page)
            else:self.canvas.scroll_to_page(page)
            self.canvas.update()
        except (RuntimeError,AttributeError,TypeError,ValueError,IndexError,OverflowError):
            logger.warning('Could not display PDF search result %s',index,exc_info=True)
        finally:self._pdf_search_advancing=False
    def update_pdf_search_markers(self):
        info=self.multipage_info or {};page_count=int(info.get('count',0));content=max(1,self.canvas.page_content_width if self.multipage_double_page else self.canvas.page_content_height);markers=[];seen_pages=set()
        for page,_rectangles in self.pdf_search_results:
            if page in seen_pages or not 0<=page<page_count or page>=len(self.canvas.page_tops) or page>=len(self.canvas.page_layout):continue
            seen_pages.add(page);markers.append((self.canvas.page_tops[page]+self.canvas.page_layout[page][1]//2)/content)
        self.canvas.container_scrollbar.set_search_markers(markers)
    def go_to_position(self,position):
        if self.farview_active:
            self.set_farview_index(max(0,min(int(position),len(self.farview_sequence)-1)));return
        if self.multipage_info:
            self.canvas.scroll_to_page(max(0,min(int(position),int(self.multipage_info.get('count',1))-1)));return
        if self.is_sequence_reader_mode():
            self.canvas.scroll_to_page(max(0,min(int(position),len(self.paths)-1)));return
        if self.paths:self.index=max(0,min(int(position),len(self.paths)-1));self.load_current()
    def request_multipage_info(self,path):
        self.multipage_generation+=1;generation=self.multipage_generation;self.multipage_info_pending=True
        future=VIEWER_IMAGE_POOL.submit(load_multipage_info,path)
        def completed(result,p=path,g=generation):
            try:data={'stage':'multipage_info','generation':g,'info':result.result()}
            except Exception as error:logger.warning('Could not read multipage file %s',p,exc_info=True);data={'stage':'multipage_info','generation':g,'error':str(error)}
            self.load_bridge.loaded.emit(p,data)
        future.add_done_callback(completed)
    def cancel_multipage_page_requests(self):
        future=getattr(self,'multipage_page_job_future',None)
        self.multipage_page_job_id=getattr(self,'multipage_page_job_id',0)+1
        if future is not None:
            try:future.cancel()
            except (RuntimeError,AttributeError):logger.debug('Could not cancel a queued page render',exc_info=True)
        self.multipage_page_job_future=None;self.multipage_page_job_active=False;self.multipage_page_active_pages.clear();self.multipage_page_queue.clear();self.multipage_preload_queue.clear();self.multipage_pending.clear()
    def request_multipage_pages(self,indices,priority=True):
        if not self.multipage_info or not self.paths or self.pending_delete_path==self.paths[self.index]:return
        count=int(self.multipage_info.get('count',0));active=set(self.multipage_page_active_pages);normalized=[];seen=set()
        for value in indices:
            try:index=int(value)
            except (TypeError,ValueError,OverflowError):continue
            if index in seen or not 0<=index<count or index in self.multipage_images or index in self.multipage_page_errors:continue
            seen.add(index);normalized.append(index)
        if priority:
            old_view=list(self.multipage_page_queue);requested=set(normalized)
            self.multipage_page_queue=deque(index for index in normalized if index not in active)
            if requested:self.multipage_preload_queue=deque(index for index in self.multipage_preload_queue if index not in requested)
            remaining_preload=set(self.multipage_preload_queue)
            for index in old_view:
                if index not in active and index not in requested and index not in remaining_preload:self.multipage_pending.discard(index)
            self.multipage_pending.update(index for index in normalized if index not in active)
        else:
            queued=set(self.multipage_page_queue)|set(self.multipage_preload_queue)|active
            for index in normalized:
                if index in queued:continue
                self.multipage_preload_queue.append(index);self.multipage_pending.add(index)
        self._start_next_multipage_decode()
    def _start_next_multipage_decode(self):
        if self._closed or self.multipage_page_job_active or not self.multipage_info or not self.paths:return
        queue=self.multipage_page_queue if self.multipage_page_queue else self.multipage_preload_queue
        batch=[]
        while queue and len(batch)<2:
            index=queue.popleft()
            if index in self.multipage_images or index in self.multipage_page_errors:
                self.multipage_pending.discard(index);continue
            if index in self.multipage_page_active_pages:continue
            batch.append(index)
        if not batch:return
        path=self.paths[self.index];members=self.multipage_info.get('members');requests=[(index,members[index] if members else None) for index in batch];generation=self.multipage_generation;target_width=max(800,min(2400,self.canvas.width()-64));job_id=self.multipage_page_job_id+1;self.multipage_page_job_id=job_id;self.multipage_page_active_pages=set(batch);self.multipage_pending.update(batch);self.multipage_page_job_active=True
        future=VIEWER_IMAGE_POOL.submit(decode_multipage_pages,path,requests,target_width);self.multipage_page_job_future=future;bridge=self.load_bridge
        def completed(result,p=path,requested=tuple(batch),g=generation,j=job_id,signal=bridge):
            try:pages=result.result()
            except Exception as error:
                if not isinstance(error,CancelledError):logger.warning('Could not decode pages from %s',p,exc_info=True)
                pages=[(page,QImage(),.72,str(error)) for page in requested]
            signal.loaded.emit(p,{'stage':'multipage_pages','generation':g,'job_id':j,'requested':requested,'pages':pages})
        future.add_done_callback(completed)
    def preload_all_multipage_pages(self):
        parent=self.application_owner()
        if not getattr(parent,'preload_all_readable_pages',False) or not self.multipage_info:return
        queued=set(self.multipage_page_active_pages)|set(self.multipage_page_queue)|set(self.multipage_images)|self.multipage_page_errors
        self.multipage_preload_queue=deque(index for index in range(int(self.multipage_info.get('count',0))) if index not in queued)
        self.multipage_pending.update(self.multipage_preload_queue)
        self._request_multipage_preload_batch()
    def _request_multipage_preload_batch(self):
        parent=self.application_owner()
        if not getattr(parent,'preload_all_readable_pages',False) or not self.multipage_info:return
        self._start_next_multipage_decode()
    def trim_multipage_cache(self):
        parent=self.application_owner()
        if getattr(parent,'preload_all_readable_pages',False):return
        while self.multipage_image_bytes>self.multipage_image_cache_limit and len(self.multipage_images)>1:
            _,old=self.multipage_images.popitem(last=False);self.multipage_image_bytes=max(0,self.multipage_image_bytes-old.sizeInBytes())
    def set_preload_all_multipage_pages(self,enabled):
        if not enabled:
            for page in self.multipage_preload_queue:
                if page not in self.multipage_page_queue and page not in self.multipage_page_active_pages:self.multipage_pending.discard(page)
            self.multipage_preload_queue.clear();self.trim_multipage_cache();self.canvas.update();return
        self.preload_all_multipage_pages()
    def load_current(self):
        if self.multipage_info and self.paths and 0<=self.index<len(self.paths):
            parent=self.application_owner();positions=getattr(parent,'multipage_page_positions',None)
            if positions is not None:positions[self.multipage_position_key(self.paths[self.index])]=self.current_multipage_page()
        self.cancel_multipage_page_requests();self.multipage_generation+=1;self.multipage_info=None;self.multipage_info_pending=False;self.multipage_images.clear();self.multipage_page_errors.clear();self.multipage_page_error_messages.clear();self.multipage_image_bytes=0;self.multipage_error=None
        self.stop_movie()
        if not self.paths:
            self.index=-1;self.full_image=QImage();self.static_pixmap=QPixmap();self.fit_pixmap=QPixmap();self.original_resolution=QSize();self.current_file_size=None;self.loading=False;self.canvas.rebuild_page_layout();self.canvas.prepare_frame();return
        self.index=max(0,min(self.index,len(self.paths)-1));path=self.paths[self.index]
        self.apply_file_visualization_mode(path)
        keep_reader_zoom=self.visualization_mode in ('reader','double_reader')
        parent=self.application_owner();next_zoom=max(.1,min(8.0,float(getattr(parent,'reader_zoom',1.0)))) if keep_reader_zoom else 1.0
        self.full_image=QImage();self.static_pixmap=QPixmap();self.fit_pixmap=QPixmap();self.original_resolution=QSize();self.double_page_path=None;self.double_page_frame=QPixmap();self.loading=False;self.rotation=0;self.flip_h=False;self.flip_v=False;self.zoom=next_zoom;self.wheel_zoom_enabled=False;self.zoom_bar_active=bool(keep_reader_zoom and next_zoom>1.01);self.canvas.pan_offset=QPoint();self.canvas.sync_zoom_slider();self.canvas.rebuild_page_layout()
        self.current_file_size=None
        self.begin_pdf_search(path);self.canvas.sync_pdf_search_visibility()
        if Path(path).suffix.casefold() in MULTIPAGE_EXTS:
            cached=self.farview_page_cache.get(self.farview_page_key(self.farview_sequence[self.farview_index])) if self.farview_active and self.farview_sequence else None
            if cached:
                info,page,frame=cached;self.multipage_images[page]=frame;self.multipage_image_bytes=frame.sizeInBytes()
                self.on_image_loaded(path,{'stage':'multipage_info','generation':self.multipage_generation,'info':copy.deepcopy(info)})
                self.preload_farview_pages();self.request_file_size(path);self.canvas.prepare_frame();return
            self.loading=True;self.request_multipage_info(path);self.preload_farview_pages();self.request_file_size(path);self.setWindowTitle('Magellan Viewer');self.canvas.prepare_frame();return
        if Path(path).suffix.casefold() in ('.gif','.webp'):
            movie=QMovie(str(path));movie.setCacheMode(QMovie.CacheNone)
            if movie.isValid():
                self.movie=movie;movie.frameChanged.connect(self.canvas.prepare_frame);movie.start()
                if not self.original_resolution.isValid() and not movie.currentPixmap().isNull():self.original_resolution=movie.currentPixmap().size()
                self.request_file_size(path)
            else:
                entry=self.image_cache.get(path)
                if entry is not None:self.image_cache.move_to_end(path);self.full_image,self.fit_pixmap=entry[:2];self.static_pixmap=QPixmap();self.original_resolution=entry[2] if len(entry)>2 and entry[2].isValid() else self.full_image.size();self.current_file_size=entry[3] if len(entry)>3 else None
                else:self.loading=True;self.request_image(path,fast_preview=path==getattr(self,'external_open_path',None))
        else:
            entry=self.image_cache.get(path)
            if entry is not None:self.image_cache.move_to_end(path);self.full_image,self.fit_pixmap=entry[:2];self.static_pixmap=QPixmap();self.original_resolution=entry[2] if len(entry)>2 and entry[2].isValid() else self.full_image.size();self.current_file_size=entry[3] if len(entry)>3 else None
            else:self.loading=True;self.request_image(path,fast_preview=path==getattr(self,'external_open_path',None))
        self.setWindowTitle('Magellan Viewer');self.canvas.prepare_frame()
        if not getattr(self,'external_open_path',None):self.preload_neighbours()
    def viewer_preview_size(self):
        size=QSize(self.canvas.size())
        if size.width()<2 or size.height()<2:
            screen=QApplication.primaryScreen();size=screen.size() if screen else QSize(1920,1080)
        return size
    def submit_full_image_decode(self,path,preload=False,fast_preview=False,known_source_size=None,known_file_size=None):
        preview_size=self.viewer_preview_size()
        if fast_preview:future=VIEWER_IMAGE_POOL.submit(decode_viewer_preview,path,preview_size)
        else:future=VIEWER_IMAGE_POOL.submit(decode_viewer_image,path,preview_size,self.cache_max_dimension,self.cache_max_file_bytes,preload,known_source_size,known_file_size)
        self.image_load_futures[path]=future
        def completed(result,p=path):
            try:data=result.result()
            except CancelledError:return
            except Exception:
                logger.exception('Viewer image load failed for %s',p)
                data=(QImage(),QImage(),False,QSize())
            if fast_preview:data={'stage':'viewer_preview','image':data}
            self.load_bridge.loaded.emit(p,data)
        future.add_done_callback(completed)
    def request_image(self,path,preload=False,fast_preview=False,known_source_size=None,known_file_size=None):
        if path in self.pending_image_paths:return
        self.pending_image_paths.add(path);self.submit_full_image_decode(path,preload=preload,fast_preview=fast_preview,known_source_size=known_source_size,known_file_size=known_file_size)
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
        if self.farview_active:self.preload_farview_pages()
        if not self.paths or not 0<=self.index<len(self.paths):return
        current=self.paths[self.index]
        # Preserve the proven 1.3 behavior: prepare both adjacent images so a
        # swipe in either direction normally uses the in-memory cache.
        for offset in (1,-1):
            path=self.paths[(self.index+offset)%len(self.paths)]
            if path!=current and path not in self.image_cache and path not in self.pending_image_paths:self.request_image(path,preload=True)
    def start_external_sibling_scan(self):
        scan=getattr(self,'external_sibling_scan',None)
        if callable(scan):
            self.external_sibling_scan=None;scan()
    def on_image_loaded(self,path,image):
        if self._closed or not self.paths:return
        if isinstance(image,dict) and image.get('stage')=='farview_prefetch':self.receive_farview_prefetch(image);return
        self.pending_image_paths.discard(path);self.image_load_futures.pop(path,None)
        if isinstance(image,dict) and image.get('stage')=='viewer_preview':
            if path==self.paths[self.index]:
                preview=image.get('image',());frame=preview[0] if isinstance(preview,tuple) and preview else QImage()
                fit=preview[1] if len(preview)>1 and not preview[1].isNull() else frame
                known_size=preview[3] if len(preview)>3 and isinstance(preview[3],QSize) else None;known_file_size=preview[4] if len(preview)>4 and isinstance(preview[4],int) else None
                # If the quick preview was already decoded at native resolution,
                # it is the final image. Re-reading it immediately adds a second
                # complete decode before first paint (common for screen-sized PNGs).
                if not frame.isNull() and known_size is not None:
                    native_size=(frame.size()==known_size or QSize(frame.height(),frame.width())==known_size)
                    within_decode_limit=frame.width()*frame.height()<=VIEWER_MAX_DECODE_PIXELS
                    cacheable=(known_file_size is not None and known_file_size<=self.cache_max_file_bytes and max(frame.width(),frame.height())<=self.cache_max_dimension and within_decode_limit)
                    if native_size and within_decode_limit:
                        self.on_image_loaded(path,(frame,fit,cacheable,known_size,known_file_size))
                        return
                if not frame.isNull():
                    self.full_image=frame;self.fit_pixmap=QPixmap.fromImage(fit);self.static_pixmap=QPixmap();self.original_resolution=known_size if known_size is not None and known_size.isValid() else frame.size();self.current_file_size=known_file_size;self.loading=True;self.canvas.prepare_frame()
                self.request_image(path,known_source_size=known_size,known_file_size=known_file_size)
            return
        if isinstance(image,dict) and image.get('stage')=='external_paths':
            if path==getattr(self,'external_open_path','') and path in image.get('paths',[]):
                self.paths=list(image['paths']);self.index=self.paths.index(path);self.canvas.rebuild_page_layout();self.canvas.update()
                if not self.loading:self.preload_neighbours()
            return
        if isinstance(image,dict) and image.get('stage')=='metadata':
            if path==self.paths[self.index]:
                self.current_file_size=image.get('size');self.canvas.update()
                if Path(path).suffix.casefold() not in MULTIPAGE_EXTS:self.start_external_sibling_scan()
            return
        if isinstance(image,dict) and image.get('stage')=='multipage_info':
            if path!=self.paths[self.index] or image.get('generation')!=self.multipage_generation:return
            self.multipage_info_pending=False
            if image.get('error'):
                self.multipage_error=image['error'];self.loading=False;self.canvas.update();QMessageBox.warning(self,'Could not open file',self.multipage_error);return
            self.multipage_info=image['info'];self.loading=False;self.canvas.sync_zoom_slider();self.canvas.rebuild_page_layout();self.update_pdf_search_markers();self.pdf_search_results_changed();self.canvas.update()
            positions=getattr(self.application_owner(),'multipage_page_positions',{});page=positions.get(self.multipage_position_key(path),0);requested_member=getattr(self,'pending_archive_member',None);requested_page=getattr(self,'pending_multipage_page',None)
            if requested_page is not None:
                self.pending_multipage_page=None;page=max(0,min(int(requested_page),int(self.multipage_info.get('count',1))-1));positions[self.multipage_position_key(path)]=page
            if requested_member:
                self.pending_archive_member=None;members=self.multipage_info.get('members') or []
                page=next((i for i,name in enumerate(members) if name.replace('\\','/')==requested_member.replace('\\','/')),page)
                positions[self.multipage_position_key(path)]=page
            def restore_page(p=page,source=path,generation=self.multipage_generation):
                if not self._closed and generation==self.multipage_generation and self.paths and self.paths[self.index]==source:self.canvas.scroll_to_page(p)
            QTimer.singleShot(0,restore_page);self.canvas.request_visible_container_pages();self.preload_all_multipage_pages();self.canvas.update();self.start_external_sibling_scan();return
        if isinstance(image,dict) and image.get('stage')=='multipage_pages':
            if image.get('job_id')!=self.multipage_page_job_id:return
            requested=set(image.get('requested',()));self.multipage_pending.difference_update(requested);self.multipage_page_active_pages.clear();self.multipage_page_job_active=False;self.multipage_page_job_future=None
            if path!=self.paths[self.index] or image.get('generation')!=self.multipage_generation:
                self._start_next_multipage_decode();return
            ratio_changed=False
            for page_result in image.get('pages',[]):
                try:page,frame,ratio=page_result[:3];error=page_result[3] if len(page_result)>3 else ''
                except (TypeError,ValueError,IndexError):continue
                if frame and not frame.isNull() and 0<=page<len(self.multipage_info.get('ratios',[])):
                    self.multipage_images[page]=frame;self.multipage_images.move_to_end(page);self.multipage_image_bytes+=frame.sizeInBytes();ratios=self.multipage_info['ratios']
                    if abs(ratios[page]-ratio)>.01:ratios[page]=ratio;ratio_changed=True
                else:self.multipage_page_errors.add(page);self.multipage_page_error_messages[page]=str(error or 'Could not decode this page')
            self.trim_multipage_cache();self._request_multipage_preload_batch()
            if ratio_changed:self.canvas.layout_update_timer.start()
            self.canvas.update();self._start_next_multipage_decode();self.preload_farview_pages();return
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
            if self.is_sequence_reader_mode():
                ratio=source_size.width()/max(1,source_size.height()) if source_size.isValid() else full_image.width()/max(1,full_image.height())
                if math.isfinite(ratio) and ratio>0:self.sequence_aspect_ratios[path]=max(.15,min(4.0,ratio));self.canvas.layout_update_timer.start()
            if path==self.paths[self.index]:
                self.full_image,self.fit_pixmap=entry[:2];self.static_pixmap=QPixmap();self.original_resolution=source_size;self.current_file_size=file_size;self.loading=False;self.canvas.prepare_frame();self.preload_neighbours();self.start_external_sibling_scan()
            elif self.double_page and self.paths and path==self.paths[(self.index+1)%len(self.paths)]:self.double_page_path=path;self.double_page_frame=fit_pixmap;self.canvas.update()
        elif path==self.paths[self.index]:
            self.loading=False;self.canvas.prepare_frame();self.preload_neighbours();self.start_external_sibling_scan()
    def showEvent(self,e):super().showEvent(e);self.load_current()
    def farview_page_key(self,entry):
        return (str(entry['source']),entry.get('member'),entry.get('page'))
    def preload_farview_pages(self):
        if self._closed or not self.farview_active or not self.farview_sequence:return
        wanted=[self.farview_sequence[(self.farview_index+offset)%len(self.farview_sequence)] for offset in (0,1,-1,2,-2)]
        keys={self.farview_page_key(entry) for entry in wanted}
        for key in list(self.farview_page_cache):
            if key not in keys:self.farview_page_cache.pop(key,None)
        if self.farview_prefetch_future is not None:return
        for entry in wanted:
            key=self.farview_page_key(entry)
            if key in self.farview_page_cache or key in self.farview_prefetch_failed or Path(key[0]).suffix.casefold() not in MULTIPAGE_EXTS:continue
            width=max(800,min(2400,self.canvas.width()-64));self.farview_prefetch_pending=key
            def decode(e=dict(entry),w=width):
                info=load_multipage_info(e['source']);page=e.get('page')
                if page is None:page=next((i for i,m in enumerate(info.get('members') or []) if m.replace('\\','/')==str(e.get('member','')).replace('\\','/')),0)
                members=info.get('members');frame=decode_multipage_pages(e['source'],[(page,members[page] if members else None)],w)[0][1]
                return info,page,frame
            future=VIEWER_IMAGE_POOL.submit(decode);self.farview_prefetch_future=future;bridge=self.load_bridge
            def completed(result,k=key,signal=bridge):
                try:data=result.result()
                except CancelledError:return
                except Exception:logger.debug('Could not preload Farview page %s',k,exc_info=True);data=None
                signal.loaded.emit(k[0],{'stage':'farview_prefetch','key':k,'data':data})
            future.add_done_callback(completed);break
    def receive_farview_prefetch(self,message):
        self.farview_prefetch_future=None;self.farview_prefetch_pending=None;key=message['key'];data=message.get('data')
        if data and not data[2].isNull() and sum(value[2].sizeInBytes() for value in self.farview_page_cache.values())+data[2].sizeInBytes()<=self.multipage_image_cache_limit:
            self.farview_page_cache[key]=data
        else:
            self.farview_prefetch_failed.add(key)
            if len(self.farview_prefetch_failed)>64:self.farview_prefetch_failed.clear()
        self.preload_farview_pages()

    def set_farview_index(self,index):
        if self._closed or not self.farview_sequence:return
        index=max(0,min(int(index),len(self.farview_sequence)-1));entry=self.farview_sequence[index]
        if self.multipage_info and self.paths:
            positions=getattr(self.application_owner(),'multipage_page_positions',None)
            if positions is not None:positions[self.multipage_position_key(self.paths[self.index])]=self.current_multipage_page()
        self.multipage_info=None
        self.farview_index=index;self.paths=[str(entry['source'])];self.index=0
        self.pending_archive_member=entry.get('member');self.pending_multipage_page=entry.get('page')
        self.load_current()

    def step(self,amount,force_container=False):
        if not self.paths:return
        direction=1 if int(amount)>0 else -1
        if self.farview_active and self.farview_sequence:
            self.set_farview_index((self.farview_index+direction)%len(self.farview_sequence));return
        continuous=bool(getattr(self.application_owner(),'continuous_viewer_swipe',False))
        if force_container and not continuous:force_container=False
        if not force_container and self.multipage_info:
            page=self.current_multipage_page()+int(amount);count=int(self.multipage_info.get('count',0))
            if 0<=page<count:self.canvas.scroll_to_page(page);return
        elif not force_container and self.is_sequence_reader_mode():
            page=self.index+int(amount)
            if 0<=page<len(self.paths):self.canvas.scroll_to_page(page);return
        next_index=(self.index+direction)%len(self.paths)
        if not continuous:
            for _ in range(len(self.paths)):
                if Path(self.paths[next_index]).suffix.casefold() in IMAGE_EXTS:break
                next_index=(next_index+direction)%len(self.paths)
            else:return
        current_path=self.paths[self.index];next_path=self.paths[next_index]
        if Path(current_path).suffix.casefold() in IMAGE_EXTS and Path(next_path).suffix.casefold() in IMAGE_EXTS:self.remember_file_visualization_mode(next_path)
        if continuous:
            if Path(next_path).suffix.casefold() in MULTIPAGE_EXTS:
                self.pending_multipage_page=0;self.pending_archive_member=None
                parent=self.application_owner();positions=getattr(parent,'multipage_page_positions',None)
                if positions is not None:positions[self.multipage_position_key(next_path)]=0
            else:
                self.pending_multipage_page=None;self.pending_archive_member=None
        self.index=next_index;self.double_page_active_slot=0;self.load_current()

    def transform_image(self,button):
        if button==0:self.rotation=(self.rotation-90)%360
        elif button==1:self.rotation=(self.rotation+90)%360
        elif button==2:self.flip_v=not self.flip_v
        else:self.flip_h=not self.flip_h
        if self.multipage_info or self.is_sequence_reader_mode():self.canvas.rebuild_page_layout()
        else:self.canvas.prepare_frame()
    def fit_to_screen(self):
        if self.compare_enabled:
            self.compare_zoom=1.0;self.compare_pan=QPoint();self.compare_image_zooms={};self.compare_image_pans={};self.compare_box_offset=QPoint();self.canvas.sync_zoom_slider();self.canvas.update();return
        continuous=bool(self.multipage_info and self.multipage_mode in ('reader','double_reader')) or self.is_sequence_reader_mode()
        page=self.canvas.most_visible_reader_page() if continuous else (self.current_multipage_page() if self.multipage_info else None)
        self.reader_zoom_mode_enabled=False;self.zoom_bar_active=False;self.set_reader_zoom(1.0,rebuild=False);self.canvas.pan_offset=QPoint()
        if self.double_page and self.paths:
            key=self.canvas._double_page_key()
            if key is not None:self.double_page_zooms[key]=1.0;self.double_page_zoom_modes.discard(key);self.double_page_pan[key]=QPoint()
        if (self.multipage_info or self.is_sequence_reader_mode()) and not self.double_page:
            self.canvas.rebuild_page_layout()
            if page is not None:
                if continuous:self.canvas.center_reader_page(page,align_start=not self.is_horizontal_reader_mode())
                else:self.canvas.scroll_to_page(page)
        else:self.canvas.sync_zoom_slider()
        self.canvas.prepare_frame()
    def set_reader_zoom(self,value,rebuild=True):
        self.reader_zoom=max(.1,min(8.0,float(value)));self.canvas.sync_zoom_slider()
        if rebuild and self.multipage_info:self.canvas.rebuild_page_layout()
        elif rebuild and self.is_sequence_reader_mode():self.canvas.layout_update_timer.start()
        parent=self.application_owner()
        if parent is not None and self.visualization_mode in ('reader','double_reader'):
            parent.reader_zoom=self.reader_zoom
            timer=getattr(self,'reader_zoom_save_timer',None)
            if timer is not None:timer.start(300)
    def is_sequence_reader_mode(self):
        if self.multipage_info or not self.paths or not 0<=self.index<len(self.paths):return False
        return self.visualization_mode in ('reader','double_reader') and Path(self.paths[self.index]).suffix.casefold() in IMAGE_EXTS
    def is_horizontal_reader_mode(self):
        return self.visualization_mode=='double_reader' and (bool(self.multipage_info) or self.is_sequence_reader_mode())
    def compare_pair(self):
        if self.multipage_info:
            page=self.current_multipage_page() if self.multipage_mode in ('reader','double_reader') else self.multipage_normal_page
            if page<0 or page+1>=int(self.multipage_info.get('count',0)):return ()
            return (('page',page),('page',page+1))
        if not self.paths or not 0<=self.index<len(self.paths) or Path(self.paths[self.index]).suffix.casefold() not in IMAGE_EXTS:return ()
        current=self.paths[self.index];next_index=next((i for i in range(self.index+1,len(self.paths)) if Path(self.paths[i]).suffix.casefold() in IMAGE_EXTS),None)
        if next_index is None:next_index=next((i for i in range(0,self.index) if Path(self.paths[i]).suffix.casefold() in IMAGE_EXTS),None)
        if next_index is None:return ()
        return (('file',current),('file',self.paths[next_index]))
    def compare_source(self,key):
        if not key:return None
        if key[0]=='page':return self.multipage_images.get(key[1])
        index=next((i for i,path in enumerate(self.paths) if path==key[1]),-1)
        return self.page_source(index) if index>=0 else None
    def toggle_compare_mode(self):
        if self.compare_enabled:self.compare_enabled=False;self.canvas.update();return
        pair=self.compare_pair()
        if not pair:
            pending_multipage=bool(self.paths and Path(self.paths[self.index]).suffix.casefold() in MULTIPAGE_EXTS and not self.multipage_info)
            if not pending_multipage:
                parent=self.application_owner()
                if parent is not None and hasattr(parent,'status'):parent.status.setText('Compare mode needs two available images or pages')
                return
        self.compare_enabled=True;self.compare_slider=.5;self.compare_active_key=pair[0] if pair else None;self.compare_zoom=1.0;self.compare_pan=QPoint();self.compare_image_zooms={};self.compare_image_pans={};self.compare_box_offset=QPoint()
        for key in pair:
            if key[0]=='page' and key[1] not in self.multipage_images:self.request_multipage_pages([key[1]])
            elif key[0]=='file' and key[1] not in self.image_cache and key[1] not in self.pending_image_paths:self.request_image(key[1],preload=key[1]!=self.paths[self.index])
        self.canvas.update()
    def cycle_visualization_mode(self):
        modes=('normal','double_page','reader','double_reader')
        try:next_index=(modes.index(self.visualization_mode)+1)%len(modes)
        except ValueError:next_index=0
        self.set_visualization_mode(modes[next_index])
    def set_visualization_mode(self,mode):
        modes=('normal','double_page','reader','double_reader')
        if mode not in modes:return
        page=self.current_multipage_page() if self.multipage_info else self.index
        self.visualization_mode=mode;self.multipage_mode=mode
        self.double_page=mode=='double_page';self.multipage_double_page=mode=='double_reader';self.wheel_zoom_enabled=False;self.zoom_bar_active=mode in ('reader','double_reader') and abs(self.zoom-1.0)>0.01
        parent=self.application_owner()
        if parent is not None and self.paths and 0<=self.index<len(self.paths):
            self.remember_file_visualization_mode(self.paths[self.index],mode)
        self.canvas.pan_offset=QPoint();self.canvas.rebuild_page_layout();self.canvas.scroll_to_page(page)
        if self.double_page and self.paths and not self.multipage_info:self.request_image(self.paths[(self.index+1)%len(self.paths)],preload=True)
        self.canvas.sync_zoom_slider();self.canvas.update()
    def toggle_double_page(self):
        self.cycle_visualization_mode()
    def delete_current_to_trash(self):
        if not self.paths or self.index<0:return
        path=self.paths[self.index];future=self.image_load_futures.get(path)
        if (future is not None and not future.done()) or self.multipage_info_pending or self.multipage_pending:
            if self.pending_delete_path==path:return
            self.pending_delete_path=path;self.slideshow_timer.stop();QTimer.singleShot(40,lambda p=path:self.finish_pending_delete(p));return
        self.delete_path_to_trash(path)
    def finish_pending_delete(self,path):
        if self.pending_delete_path!=path:return
        future=self.image_load_futures.get(path)
        if future is not None and not future.done():QTimer.singleShot(40,lambda p=path:self.finish_pending_delete(p));return
        if self.multipage_info_pending or self.multipage_pending:
            QTimer.singleShot(40,lambda p=path:self.finish_pending_delete(p));return
        self.pending_delete_path=None;self.delete_path_to_trash(path)
    def delete_path_to_trash(self,path):
        if path not in self.paths:return
        index=self.paths.index(path);was_current=index==self.index;parent=self.application_owner()
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
        if e.key()==Qt.Key_S and e.modifiers()&Qt.ControlModifier:
            parent=self.application_owner()
            if parent is not None and hasattr(parent,'grid'):parent.grid.undo_deselection()
            return True
        if (self.multipage_info or self.is_sequence_reader_mode()) and e.key() in (Qt.Key_Home,Qt.Key_End,Qt.Key_PageUp,Qt.Key_PageDown):
            count=int(self.multipage_info.get('count',1)) if self.multipage_info else len(self.paths);current=self.current_multipage_page() if self.multipage_info else self.index
            if e.key()==Qt.Key_Home:self.canvas.scroll_to_page(0)
            elif e.key()==Qt.Key_End:self.canvas.scroll_to_page(count-1)
            elif e.key()==Qt.Key_PageUp:self.canvas.scroll_to_page(max(0,current-(2 if self.multipage_double_page else 1)))
            else:self.canvas.scroll_to_page(min(count-1,current+(2 if self.multipage_double_page else 1)))
            return True
        if e.key()==Qt.Key_F and e.modifiers()&Qt.ControlModifier and self.paths and Path(self.paths[self.index]).suffix.casefold()=='.pdf':
            self.canvas.hover_zone='metadata';self.canvas.sync_pdf_search_visibility();self.canvas.pdf_search_edit.show();self.canvas.pdf_search_edit.setFocus(Qt.ShortcutFocusReason);self.canvas.update();return True
        if e.key()==Qt.Key_Z and e.modifiers()&Qt.ControlModifier:
            parent=self.application_owner()
            if parent is not None and hasattr(parent,'undo_last_action'):parent.undo_last_action()
        elif e.key() in (Qt.Key_Escape,Qt.Key_Up):self.close()
        elif e.key()==Qt.Key_F11:
            self.showNormal() if self.isFullScreen() else self.showFullScreen()
        elif (self.multipage_info or self.is_sequence_reader_mode()) and self.visualization_mode=='reader' and e.key() in (Qt.Key_Left,Qt.Key_Right):
            delta=max(1,self.canvas.height()//3)*(1 if e.key()==Qt.Key_Right else -1);self.canvas.container_scrollbar.setValue(max(0,min(self.canvas.container_scrollbar.maximum(),self.canvas.container_scrollbar.value()+delta)))
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
    def contextMenuEvent(self,e):e.accept()
    def closeEvent(self,e):
        if self._closed:e.accept();return
        self._closed=True;self.multipage_generation+=1
        if self.farview_prefetch_future is not None:self.farview_prefetch_future.cancel()
        self.farview_page_cache.clear();self.farview_prefetch_failed.clear()
        if self.multipage_info and self.paths and 0<=self.index<len(self.paths):
            positions=getattr(self.application_owner(),'multipage_page_positions',None)
            if positions is not None:positions[self.multipage_position_key(self.paths[self.index])]=self.current_multipage_page()
        self.slideshow_timer.stop();self.pointer_hide_timer.stop();self.release_current_image()
        try:
            self.pdf_search_scan_timer.stop();self.pdf_search_scan_generation+=1;self.pdf_search_model.setDocument(None)
            if self.pdf_search_document is not None:self.pdf_search_document.close();self.pdf_search_document.deleteLater();self.pdf_search_document=None
        except RuntimeError:logger.debug('PDF search document was already released during viewer close')
        self.canvas.clear_page_render_cache();self.multipage_images.clear();self.multipage_image_bytes=0;self.cancel_multipage_page_requests();self.multipage_info=None;self.sequence_aspect_ratios.clear();self.multipage_page_errors.clear();self.multipage_page_error_messages.clear();self.image_cache.clear();self.image_cache_bytes=0
        pending_futures=tuple(self.image_load_futures.values());self.image_load_futures.clear();self.pending_image_paths.clear()
        for future in pending_futures:future.cancel()
        self.paths.clear();self.index=-1;self.pending_delete_path=None
        parent=self.application_owner()
        if parent is not None and hasattr(parent,'viewer_closed'):
            try:parent.viewer_closed(self)
            except RuntimeError:logger.debug('Viewer closed after its parent was deleted')
        super().closeEvent(e)


class Explorer(QMainWindow):
    def __init__(self,external_file=None):
        super().__init__();self._defer_initial_tab_load=bool(external_file);self._deferred_startup_maintenance=bool(external_file);self._pending_external_tab_restore=False;self._pending_external_tab_index=-1;self._pending_external_folder_path=None;self._external_folder_tab_opened=False;self._external_folder_highlight_path=None;self._replacing_external_viewer=False;self._closing=False;self.viewer=None;self.viewer_file_modes={};self.file_clipboard=[];self.file_clipboard_cut=False;self.file_operation_task=None;self.file_operation_bridge=FileOperationCompletionBridge(self);self.file_operation_bridge.completed.connect(self._complete_file_operation,Qt.QueuedConnection);self.file_operation_bridge.task_finished.connect(self.release_file_operation_task,Qt.QueuedConnection);self.multipage_page_positions={};self.setWindowTitle('Magellan');icon_root=Path(sys._MEIPASS) if getattr(sys,'frozen',False) else Path(__file__).resolve().parent.parent;self.icon_root=icon_root;self.icon_dir=icon_root/'Alternative Icons';icon_path=self.icon_dir/'MainIconNew.ico';legacy_icon_path=icon_root/'MainIcon.ico';self.app_icon=QIcon(str(icon_path if icon_path.is_file() else legacy_icon_path)) if icon_path.is_file() or legacy_icon_path.is_file() else make_boat_icon();self.setWindowIcon(self.app_icon);QApplication.setWindowIcon(self.app_icon);self.resize(1900,1000);self.window_cycle=0;self.in_favorites_view=False;self.setStyleSheet(QSS)
        self.file_clipboard_set=set();self.preload_all_readable_pages=False
        self._ui_ready=False;self._app_event_filter_installed=False
        self.hover_details_enabled=True;self.hover_details_delay=500;self.hover_details_placement='top';self.appearance_transparency=0;self.appearance_blur=0;self.appearance_color='';self.appearance_gradient_area=55;self.appearance_gradient_distance=50;self.appearance_gradient_position_y=50;self.appearance_gradient_hue=0
        for _surface in ('main','buttons','tabs','tree','menus'):
            _surface_background={'main':'#202124','buttons':'#303236','tabs':'#2b2d31','tree':'#15171c','menus':'#25272b'}[_surface];setattr(self,f'appearance_{_surface}_transparency',0);setattr(self,f'appearance_{_surface}_blur',0);setattr(self,f'appearance_{_surface}_background_color',_surface_background);setattr(self,f'appearance_{_surface}_background_hue',max(0,QColor(_surface_background).hue()));setattr(self,f'appearance_{_surface}_background_brightness',100);setattr(self,f'appearance_{_surface}_color','');setattr(self,f'appearance_{_surface}_accent_color','');setattr(self,f'appearance_{_surface}_accent_hue',QColor('#d0a32d').hue());setattr(self,f'appearance_{_surface}_accent_brightness',100);setattr(self,f'appearance_{_surface}_gradient_area',55);setattr(self,f'appearance_{_surface}_gradient_transparency',100);setattr(self,f'appearance_{_surface}_gradient_brightness',100);setattr(self,f'appearance_{_surface}_gradient_distance',50);setattr(self,f'appearance_{_surface}_gradient_position_y',50);setattr(self,f'appearance_{_surface}_gradient_rotation',0);setattr(self,f'appearance_{_surface}_gradient_hue',0)
        self.appearance_bottom_gradient_enabled=False;self.appearance_bottom_gradient_area=55;self.appearance_bottom_gradient_distance=50;self.appearance_bottom_gradient_position_y=50;self.appearance_bottom_gradient_transparency=35;self.appearance_bottom_gradient_brightness=100;self.appearance_bottom_gradient_rotation=0;self.appearance_bottom_gradient_hue=0;self.appearance_bottom_gradient_color='';self.appearance_custom_colors=[];self.appearance_font_color='#ffffff';self._appearance_preview_active=False
        self.slideshow_interval_ms=5000;self.continuous_viewer_swipe=False;self.wheel_scroll_rows=1;self.scroll_glide_duration_ms=0;self.reader_scroll_speed=1.0;self.reader_zoom=1.0;self.viewer_mode='reader';self.viewer_wheel_behavior='swipe';self.scrollbars_auto_hide=True;self.scrollbars_hide_delay=700;self.smooth_wheel_scroll=False;self.gpu_thumbnail_rendering_enabled=False;self.gpu_blur_enabled=False;self.gpu_composition_enabled=False;self.scroll_test_logging_enabled=False;self.auto_refresh_folders=False;self.folder_refresh_interval_s=30;self._tab_drag_active=False;self.auto_hide_main=False;self.auto_hide_tabs=False;self.auto_hide_tree=False;self.main_hide_delay=900;self.tree_hide_delay=900;self.right_controls_auto_hide=True;self.right_controls_hide_delay=500;self.favorite_organize_mode=False;self.favorites_navigation_root=None;self.favorite_return_group=None;self.recursive_search_enabled=False;self.recursive_search_results=None;self.recursive_search_result_query='';self.recursive_search_generation=0;self.recursive_search_jobs=[];self.google_lens_query='';self.disk_thumbnail_cache_limit_enabled=False;self.disk_thumbnail_cache_limit_mib=4096;self._disk_cache_trim_lock=threading.Lock();self._disk_cache_trim_future=None
        self.settings_dialog_geometries={};self.current=Path.home();self.items=[];self.sort='name';self.desc=False;self.columns=5;self.thumb=260;self.mode='vertical';self.thumbnail_cache_enabled=True;self.file_thumbnail_disk_cache_enabled=True;self.thumbnail_cache_limit_mib=256;self.favorites=set();self.favorite_groups=[];self.active_favorite_group=None;self.tree_favorites=set();self.tree_expanded_paths=set();self.tree_selected_path='';self.tree_scroll_value=0;self.tree_scroll_anchor_path='';self.tree_scroll_anchor_offset=0;self._tree_scroll_restore_pending=False;self.pinned_folders=set();self._pinned_folders_generation=0;self.pinned_thumbs={};self.pinned_folder_sizes={};self.pinned_folder_sizes_checked={};self.custom={};self.startup_folder=None;self.tabs=[];self.active_tab_index=-1;self.saved_tabs=[];self.closed_tabs=[];self.saved_active_tab=0;self.scan_generation=0;self.scan_jobs=[];self.return_to_favorites=False;self.favorites_return_path=None;self.scroll_positions={};self.folder_sort_settings={};self.folder_manual_orders={};self.manual_order_created={};self._suppress_scroll_save=False;self.window_mode='fullscreen';self.last_deletion_undo=None;self.last_move_undo=None;self.cache_dir=Path(QStandardPaths.writableLocation(QStandardPaths.AppDataLocation));self.cache_dir.mkdir(parents=True,exist_ok=True);self.settings_file=self.cache_dir/'settings.json';self.install_dir=Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent;self.persistent_thumb_dir=self.install_dir/'thumbnail_cache';self.persistent_thumb_dir.mkdir(parents=True,exist_ok=True);self.load_settings();self.developer_mode='--developer' in sys.argv[1:];self.scroll_test_logging_enabled=self.developer_mode and self.scroll_test_logging_enabled;self.persistent_thumb_dir=Path(getattr(self,'thumbnail_cache_dir',self.persistent_thumb_dir));self.persistent_thumb_dir.mkdir(parents=True,exist_ok=True);self.back_target=Path(self.startup_folder) if self.startup_folder else Path(self.current);self.image_search_provider='saucenao';self.image_search_mode=False;self.scan=None;self.work=None;self._ui_deferred=bool(external_file)
        self.migrate_legacy_explorer_context_registration()
        if external_file:return
        self._initialize_ui(external_launch=False)

    def _initialize_ui(self,external_launch=False):
        self.build();self.apply_scrollbar_behavior();self.grid.set_cache_limit_mib(self.thumbnail_cache_limit_mib);self.grid.persistent_folder_cache_dir=self.persistent_thumb_dir;self.grid.folder_cache.update(self.pinned_thumbs);self.grid.folder_size_cache.update(self.pinned_folder_sizes);self.grid.folder_size_cache_checked_at.update(self.pinned_folder_sizes_checked)
        self.appearance_capture_timer=QTimer(self);self.appearance_capture_timer.setSingleShot(True);self.appearance_capture_timer.setInterval(0);self.appearance_capture_timer.timeout.connect(self.refresh_appearance_backdrop);self.appearance_preview_timer=QTimer(self);self.appearance_preview_timer.setSingleShot(True);self.appearance_preview_timer.setInterval(45);self.appearance_preview_timer.timeout.connect(lambda:self.apply_appearance_settings(persist=False));self.apply_appearance_settings(persist=False);self.in_archive_view=False;self.archive_path=None;self.archive_prefix='';self.archive_return_folder=None;self.archive_return_favorites=False;self.archive_return_group=None;self.archive_generation=0;self.archive_scroll_positions={};self.archive_navigation_stack=[];self._folder_refresh_scan=None;self._folder_refresh_context=None;self._folder_refresh_pending=None;self._folder_refresh_generation=0
        self.folder_refresh_timer=QTimer(self);self.folder_refresh_timer.timeout.connect(self.auto_refresh_folder_page);self.sync_folder_refresh_timer()
        self.folder_refresh_apply_timer=QTimer(self);self.folder_refresh_apply_timer.setSingleShot(True);self.folder_refresh_apply_timer.setInterval(400);self.folder_refresh_apply_timer.timeout.connect(self.apply_pending_folder_refresh)
        self._folder_thumb_bridge=FolderThumbnailBridge(self);self._folder_thumb_bridge.completed.connect(self.handle_pinned_thumbnail_ready);self._archive_scan_bridge=ArchiveScanBridge(self);self._archive_scan_bridge.completed.connect(self.finish_archive_scan)
        self.restore_tabs(activate=not external_launch)
        if not self._deferred_startup_maintenance:self.run_startup_maintenance()
        self.hover_details_enabled=True;self.grid.set_hover_settings(True,self.hover_details_delay);self.apply_window_state();self.apply_auto_hide();self.apply_scrollbar_behavior()
        QTimer.singleShot(1500,self.welcome_gpu_options)
        self._ui_ready=True;app=QApplication.instance()
        if app is not None and not self._app_event_filter_installed:app.installEventFilter(self);self._app_event_filter_installed=True
        self.migrate_legacy_explorer_context_registration()
    def load_settings(self):
        self._settings_load_failed=False;self.explorer_context_menu_enabled=True
        self.log_storage_mib=8
        configure_logging(self.cache_dir/'logs',self.log_storage_mib)
        try:
            d=self._sanitize_settings(json.loads(self.settings_file.read_text(encoding='utf-8-sig')));self.explorer_context_menu_enabled=bool(d.get('explorer_context_menu_enabled',True));self.favorites=set(d.get('favorites',[]));self.favorite_groups=d.get('favorite_groups',[]);self.tree_favorites=set(d.get('tree_favorites',[]));self.tree_expanded_paths=set(d.get('tree_expanded_paths',[]));self.tree_selected_path=d.get('tree_selected_path','');self.tree_scroll_value=d.get('tree_scroll_value',0);self.tree_scroll_anchor_path=d.get('tree_scroll_anchor_path','');self.tree_scroll_anchor_offset=d.get('tree_scroll_anchor_offset',0);self.custom=d.get('custom',{});self.pinned_folders=set(d.get('pinned_folders',[]));self._pinned_folders_generation+=1;self.pinned_thumbs=d.get('pinned_thumbs',{});self.pinned_folder_sizes=d.get('pinned_folder_sizes',{});self.pinned_folder_sizes_checked=d.get('pinned_folder_sizes_checked',{});self.image_search_provider=d.get('image_search_provider','saucenao');self.saucenao_api_key=d.get('saucenao_api_key','');self.window_mode=d.get('window_mode','fullscreen');self.saved_geometry=d.get('geometry');self.hover_details_enabled=bool(d.get('hover_details_enabled',True));self.hover_details_delay=max(0,min(3000,int(d.get('hover_details_delay',500))));_placement=d.get('hover_details_placement','top');self.hover_details_placement='bottom' if _placement=='below' else (_placement if _placement in ('top','bottom','above') else 'top');self.thumbnail_cache_enabled=bool(d.get('thumbnail_cache_enabled',True));self.thumbnail_cache_limit_mib=max(16,min(8192,int(d.get('thumbnail_cache_limit_mib',256))));self.auto_hide_main=bool(d.get('auto_hide_main',False));self.auto_hide_tabs=bool(d.get('auto_hide_tabs',False));self.auto_hide_tree=bool(d.get('auto_hide_tree',False));self.main_hide_delay=max(0,min(10000,int(d.get('main_hide_delay',900))));self.tree_hide_delay=max(0,min(10000,int(d.get('tree_hide_delay',900))));self.has_saved_tabs='tabs' in d;self.saved_tabs=d.get('tabs',[]);self.closed_tabs=d.get('closed_tabs',[]) if isinstance(d.get('closed_tabs',[]),list) else [];self.saved_active_tab=max(0,int(d.get('active_tab',0)));self.folder_sort_settings=d.get('folder_sort_settings',{});self.folder_manual_orders=d.get('folder_manual_orders',{});self.manual_order_created=d.get('manual_order_created',{});self.thumbnail_cache_dir=d.get('thumbnail_cache_dir') or str(self.persistent_thumb_dir)
            self.file_thumbnail_disk_cache_enabled=bool(d.get('file_thumbnail_disk_cache_enabled',True))
            self.slideshow_interval_ms=max(250,min(60000,int(d.get('slideshow_interval_ms',5000))))
            geometries=d.get('settings_dialog_geometries',{})
            self.settings_dialog_geometries={str(key):value for key,value in geometries.items() if isinstance(key,str) and isinstance(value,str)} if isinstance(geometries,dict) else {}
            self.wheel_scroll_rows=max(.1,min(12.0,float(d.get('wheel_scroll_rows',1))))
            self.reader_scroll_speed=max(.1,min(10.0,float(d.get('reader_scroll_speed',1.0))))
            self.reader_zoom=max(.1,min(8.0,float(d.get('reader_zoom',1.0))))
            self.viewer_mode=d.get('viewer_mode','reader') if d.get('viewer_mode') in ('normal','double_page','reader','double_reader') else 'reader'
            self.viewer_wheel_behavior=d.get('viewer_wheel_behavior','swipe') if d.get('viewer_wheel_behavior') in ('swipe','zoom') else 'swipe'
            self.scrollbars_auto_hide=bool(d.get('scrollbars_auto_hide',True));self.scrollbars_hide_delay=max(0,min(10000,int(d.get('scrollbars_hide_delay',700))))
            self.scroll_glide_duration_ms=d.get('scroll_glide_duration_ms',0)
            self.smooth_wheel_scroll=bool(d.get('smooth_wheel_scroll',False));self.gpu_thumbnail_rendering_enabled=bool(d.get('gpu_thumbnail_rendering_enabled',False));self.gpu_blur_enabled=bool(d.get('gpu_blur_enabled',False));self.gpu_composition_enabled=bool(d.get('gpu_composition_enabled',False))
            self.scroll_test_logging_enabled=bool(d.get('scroll_test_logging_enabled',False))
            self.auto_refresh_folders=bool(d.get('auto_refresh_folders',False))
            self.folder_refresh_interval_s=max(1,min(3600,int(d.get('folder_refresh_interval_s',30))));self.continuous_viewer_swipe=bool(d.get('continuous_viewer_swipe',False))
            self.log_storage_mib=max(1,min(64,int(d.get('log_storage_mib',8))))
            self.recursive_search_enabled=bool(d.get('recursive_search_enabled',False))
            self.preload_all_readable_pages=bool(d.get('preload_all_readable_pages',False))
            self.appearance_transparency=max(0,min(100,int(d.get('appearance_transparency',0))));self.appearance_blur=max(0,min(100,int(d.get('appearance_blur',0))));self.appearance_gradient_area=max(1,min(500,int(d.get('appearance_gradient_area',55))));self.appearance_gradient_distance=max(0,min(100,int(d.get('appearance_gradient_distance',50))));_appearance_color=d.get('appearance_color','');self.appearance_color=_appearance_color if isinstance(_appearance_color,str) and re.fullmatch(r'#[0-9a-fA-F]{6}',_appearance_color) else ''
            for _surface in ('main','buttons','tabs','tree','menus'):
                _valid=_appearance_color if isinstance(_appearance_color,str) and re.fullmatch(r'#[0-9a-fA-F]{6}',_appearance_color) else ''
                setattr(self,f'appearance_{_surface}_transparency',max(0,min(100,int(d.get(f'appearance_{_surface}_transparency',self.appearance_transparency)))))
                setattr(self,f'appearance_{_surface}_blur',max(0,min(100,int(d.get(f'appearance_{_surface}_blur',self.appearance_blur)))))
                _background=d.get(f'appearance_{_surface}_background_color',{'main':'#202124','buttons':'#303236','tabs':'#2b2d31','tree':'#15171c','menus':'#25272b'}[_surface]);setattr(self,f'appearance_{_surface}_background_color',_background if isinstance(_background,str) and re.fullmatch(r'#[0-9a-fA-F]{6}',_background) else {'main':'#202124','buttons':'#303236','tabs':'#2b2d31','tree':'#15171c','menus':'#25272b'}[_surface])
                _background_hue=QColor(getattr(self,f'appearance_{_surface}_background_color')).hue();setattr(self,f'appearance_{_surface}_background_hue',max(0,min(360,int(d.get(f'appearance_{_surface}_background_hue',_background_hue)))));setattr(self,f'appearance_{_surface}_background_brightness',max(0,min(200,int(d.get(f'appearance_{_surface}_background_brightness',100)))))
                _color=d.get(f'appearance_{_surface}_color',_valid);setattr(self,f'appearance_{_surface}_color',_color if isinstance(_color,str) and re.fullmatch(r'#[0-9a-fA-F]{6}',_color) else '')
                _accent=d.get(f'appearance_{_surface}_accent_color',_color if isinstance(_color,str) else '');setattr(self,f'appearance_{_surface}_accent_color',_accent if isinstance(_accent,str) and re.fullmatch(r'#[0-9a-fA-F]{6}',_accent) else '')
                _accent_hue=QColor(getattr(self,f'appearance_{_surface}_accent_color') or '#d0a32d').hue()
                setattr(self,f'appearance_{_surface}_accent_hue',max(0,min(360,int(d.get(f'appearance_{_surface}_accent_hue',_accent_hue)))))
                setattr(self,f'appearance_{_surface}_accent_brightness',max(0,min(200,int(d.get(f'appearance_{_surface}_accent_brightness',100)))))
                setattr(self,f'appearance_{_surface}_gradient_area',max(0,min(500,int(d.get(f'appearance_{_surface}_gradient_area',self.appearance_gradient_area)))))
                setattr(self,f'appearance_{_surface}_gradient_transparency',max(0,min(100,int(d.get(f'appearance_{_surface}_gradient_transparency',100)))))
                setattr(self,f'appearance_{_surface}_gradient_brightness',max(0,min(200,int(d.get(f'appearance_{_surface}_gradient_brightness',100)))))
                setattr(self,f'appearance_{_surface}_gradient_distance',max(-100,min(200,int(d.get(f'appearance_{_surface}_gradient_distance',50)))));setattr(self,f'appearance_{_surface}_gradient_position_y',max(-100,min(200,int(d.get(f'appearance_{_surface}_gradient_position_y',50)))));setattr(self,f'appearance_{_surface}_gradient_rotation',max(0,min(359,int(d.get(f'appearance_{_surface}_gradient_rotation',0)))));setattr(self,f'appearance_{_surface}_gradient_hue',max(0,min(360,int(d.get(f'appearance_{_surface}_gradient_hue',0)))))
            self.appearance_bottom_gradient_area=max(0,min(500,int(d.get('appearance_bottom_gradient_area',55))));self.appearance_bottom_gradient_distance=max(-100,min(200,int(d.get('appearance_bottom_gradient_distance',50))));self.appearance_bottom_gradient_position_y=max(-100,min(200,int(d.get('appearance_bottom_gradient_position_y',50))));self.appearance_bottom_gradient_transparency=max(0,min(100,int(d.get('appearance_bottom_gradient_transparency',35))));self.appearance_bottom_gradient_brightness=max(0,min(200,int(d.get('appearance_bottom_gradient_brightness',100))));self.appearance_bottom_gradient_rotation=max(0,min(359,int(d.get('appearance_bottom_gradient_rotation',0))));self.appearance_bottom_gradient_hue=max(0,min(360,int(d.get('appearance_bottom_gradient_hue',0))));_bottom_color=d.get('appearance_bottom_gradient_color','');self.appearance_bottom_gradient_color=_bottom_color if isinstance(_bottom_color,str) and re.fullmatch(r'#[0-9a-fA-F]{6}',_bottom_color) else '';self.appearance_bottom_gradient_enabled=bool(self.appearance_bottom_gradient_color);_font_color=d.get('appearance_font_color','#ffffff');self.appearance_font_color=_font_color if _font_color in ('#ffffff','#d3d3d3','#808080','#000000') else '#ffffff';_custom_colors=d.get('appearance_custom_colors',[]);self.appearance_custom_colors=list(dict.fromkeys(color.lower() for color in _custom_colors if isinstance(color,str) and re.fullmatch(r'#[0-9a-fA-F]{6}',color)))[:100] if isinstance(_custom_colors,list) else []
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
            self._settings_load_failed=True
            try:
                backup=self.settings_file.with_name('settings.load-error.bak')
                if self.settings_file.exists() and not backup.exists():shutil.copy2(self.settings_file,backup)
            except OSError:logger.exception('Could not preserve the settings file after a load failure')
            self.window_mode='fullscreen';self.saved_geometry=None
    @staticmethod
    def _sanitize_settings(data):
        if not isinstance(data,dict):raise ValueError('Settings root must be a JSON object')
        original_keys=set(data)
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
                tab['kind']=tab.get('kind') if tab.get('kind') in ('folder','farview','archive','empty') else 'folder'
                if tab['kind']=='archive':
                    archive=tab.get('archive','')
                    if not isinstance(archive,str) or not archive.strip() or Path(archive).suffix.casefold() not in COMIC_EXTS:tab['kind']='folder';tab.pop('archive',None);tab.pop('archive_prefix',None)
                    else:tab['archive']=archive;tab['archive_prefix']=str(tab.get('archive_prefix','')).replace('\\','/').strip('/')[:4096]
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
        closed=data.get('closed_tabs',[]);data['closed_tabs']=[{k:v for k,v in tab.items() if k in ('path','kind','archive','archive_prefix','scroll','positions','sort','desc','unsorted','selection','color','source_order','view_id')} for tab in closed[-20:] if isinstance(tab,dict) and isinstance(tab.get('path'),str) and tab.get('kind') in ('folder','farview','archive','empty')]
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
        for key,default in {'active_tab':0,'hover_details_delay':500,'thumbnail_cache_limit_mib':256,'disk_thumbnail_cache_limit_mib':4096,'main_hide_delay':900,'tree_hide_delay':900,'slideshow_interval_ms':5000,'log_storage_mib':8,'right_controls_hide_delay':500,'folder_refresh_interval_s':30,'appearance_transparency':0,'appearance_blur':0,'appearance_gradient_area':55,'appearance_gradient_transparency':100,'appearance_gradient_distance':50,'appearance_gradient_position_y':50,'appearance_gradient_hue':0,'appearance_bottom_gradient_area':55,'appearance_bottom_gradient_distance':50,'appearance_bottom_gradient_position_y':50,'appearance_bottom_gradient_transparency':35,'appearance_bottom_gradient_hue':0}.items():
            try:data[key]=int(data.get(key,default))
            except (TypeError,ValueError,OverflowError):data[key]=default
        for key,default in {'gpu_composition_enabled':False,'gpu_blur_enabled':False,'gpu_thumbnail_rendering_enabled':False,'file_thumbnail_disk_cache_enabled':True,'hover_details_enabled':True,'thumbnail_cache_enabled':True,'disk_thumbnail_cache_limit_enabled':False,'auto_hide_main':False,'auto_hide_tabs':False,'auto_hide_tree':False,'right_controls_auto_hide':True,'recursive_search_enabled':False,'preload_all_readable_pages':False,'smooth_wheel_scroll':False,'scroll_test_logging_enabled':False,'auto_refresh_folders':False,'continuous_viewer_swipe':False,'explorer_context_menu_enabled':True}.items():
            data[key]=data.get(key,default) if isinstance(data.get(key,default),bool) else default
        color=data.get('appearance_color','');data['appearance_color']=color if isinstance(color,str) and re.fullmatch(r'#[0-9a-fA-F]{6}',color) else ''
        for surface in ('main','buttons','tabs','tree','menus'):
            color_key=f'appearance_{surface}_color';raw_color=data.get(color_key,data['appearance_color']);surface_color=raw_color if isinstance(raw_color,str) and re.fullmatch(r'#[0-9a-fA-F]{6}',raw_color) else '';surface_hue=QColor(surface_color).hue() if surface_color else 0;data[color_key]=surface_color
            background_defaults={'main':'#202124','buttons':'#303236','tabs':'#2b2d31','tree':'#15171c','menus':'#25272b'};background_key=f'appearance_{surface}_background_color';raw_background=data.get(background_key,background_defaults[surface]);data[background_key]=raw_background if isinstance(raw_background,str) and re.fullmatch(r'#[0-9a-fA-F]{6}',raw_background) else background_defaults[surface]
            for field,default,low,high in (('transparency',0,0,100),('blur',0,0,100),('gradient_area',55,0,500),('gradient_transparency',100,0,100),('gradient_brightness',100,0,200),('gradient_distance',50,0,100),('gradient_rotation',0,0,359),('gradient_position_y',50,0,100),('gradient_hue',surface_hue,0,360)):
                key=f'appearance_{surface}_{field}'
                legacy_key={'transparency':'appearance_transparency','blur':'appearance_blur','gradient_area':'appearance_gradient_area','gradient_transparency':'appearance_gradient_transparency','gradient_brightness':'appearance_gradient_brightness','gradient_distance':'appearance_gradient_distance','gradient_rotation':'appearance_gradient_rotation','gradient_position_y':'appearance_gradient_position_y','gradient_hue':'appearance_gradient_hue'}[field]
                fallback=surface_hue if field=='gradient_hue' and surface_color else data.get(legacy_key,default)
                if field in ('gradient_distance','gradient_position_y'):low,high=-100,200
                try:data[key]=max(low,min(high,int(data.get(key,fallback))))
                except (TypeError,ValueError,OverflowError):data[key]=default
            try:data[f'appearance_{surface}_background_hue']=max(0,min(360,int(data.get(f'appearance_{surface}_background_hue',QColor(data[background_key]).hue() if QColor(data[background_key]).hue()>=0 else 0))))
            except (TypeError,ValueError,OverflowError):data[f'appearance_{surface}_background_hue']=max(0,QColor(data[background_key]).hue())
            try:data[f'appearance_{surface}_background_brightness']=max(0,min(200,int(data.get(f'appearance_{surface}_background_brightness',100))))
            except (TypeError,ValueError,OverflowError):data[f'appearance_{surface}_background_brightness']=100
            accent_key=f'appearance_{surface}_accent_color';raw_accent=data.get(accent_key,surface_color);accent_color=raw_accent if isinstance(raw_accent,str) and re.fullmatch(r'#[0-9a-fA-F]{6}',raw_accent) else '';data[accent_key]=accent_color
            accent_hue=QColor(accent_color or '#d0a32d').hue()
            try:data[f'appearance_{surface}_accent_hue']=max(0,min(360,int(data.get(f'appearance_{surface}_accent_hue',accent_hue))))
            except (TypeError,ValueError,OverflowError):data[f'appearance_{surface}_accent_hue']=accent_hue
            try:data[f'appearance_{surface}_accent_brightness']=max(0,min(200,int(data.get(f'appearance_{surface}_accent_brightness',100))))
            except (TypeError,ValueError,OverflowError):data[f'appearance_{surface}_accent_brightness']=100
        try:data['appearance_bottom_gradient_area']=max(0,min(500,int(data.get('appearance_bottom_gradient_area',55))))
        except (TypeError,ValueError,OverflowError):data['appearance_bottom_gradient_area']=55
        raw_bottom=data.get('appearance_bottom_gradient_color','');bottom_color=raw_bottom if isinstance(raw_bottom,str) and re.fullmatch(r'#[0-9a-fA-F]{6}',raw_bottom) else '';bottom_hue=QColor(bottom_color).hue() if bottom_color else 0;data['appearance_bottom_gradient_color']=bottom_color
        if 'appearance_bottom_gradient_hue' not in original_keys:data['appearance_bottom_gradient_hue']=bottom_hue
        for key,default,low,high in (('appearance_bottom_gradient_distance',50,-100,200),('appearance_bottom_gradient_position_y',50,-100,200),('appearance_bottom_gradient_transparency',35,0,100),('appearance_bottom_gradient_brightness',100,0,200),('appearance_bottom_gradient_rotation',0,0,359),('appearance_bottom_gradient_hue',bottom_hue,0,360)):
            try:data[key]=max(low,min(high,int(data.get(key,default))))
            except (TypeError,ValueError,OverflowError):data[key]=default
        custom_colors=data.get('appearance_custom_colors',[])
        if not isinstance(custom_colors,list):custom_colors=[]
        data['appearance_custom_colors']=list(dict.fromkeys(color.lower() for color in custom_colors if isinstance(color,str) and re.fullmatch(r'#[0-9a-fA-F]{6}',color)))[:100]
        data['appearance_bottom_gradient_enabled']=data.get('appearance_bottom_gradient_enabled',False) if isinstance(data.get('appearance_bottom_gradient_enabled',False),bool) else False
        data['viewer_wheel_behavior']=data.get('viewer_wheel_behavior','swipe') if data.get('viewer_wheel_behavior') in ('swipe','zoom') else 'swipe'
        _font=data.get('appearance_font_color','#ffffff');data['appearance_font_color']=_font if _font in ('#ffffff','#d3d3d3','#808080','#000000') else '#ffffff'
        _bottom=data.get('appearance_bottom_gradient_color','');data['appearance_bottom_gradient_color']=_bottom if isinstance(_bottom,str) and re.fullmatch(r'#[0-9a-fA-F]{6}',_bottom) else ''
        try:data['wheel_scroll_rows']=max(.1,min(12.0,float(data.get('wheel_scroll_rows',1))))
        except (TypeError,ValueError,OverflowError):data['wheel_scroll_rows']=1.0
        for key,default in (('reader_scroll_speed',1.0),('reader_zoom',1.0)):
            try:data[key]=max(.1,min(10.0 if key=='reader_scroll_speed' else 8.0,float(data.get(key,default))))
            except (TypeError,ValueError,OverflowError):data[key]=default
        try:
            duration=int(data.get('scroll_glide_duration_ms',0));data['scroll_glide_duration_ms']=0 if duration<=0 else max(100,min(3000,duration))
        except (TypeError,ValueError,OverflowError):data['scroll_glide_duration_ms']=0
        if data.get('window_mode') not in ('fullscreen','maximized','normal'):data['window_mode']='fullscreen'
        return data
    def save_settings(self):
        if getattr(self,'_settings_load_failed',False):
            logger.error('Skipping settings save because the existing settings did not load; preserving the original file')
            return
        timer=getattr(self,'_settings_save_timer',None)
        if getattr(self,'_closing',False):
            if timer is not None:timer.stop()
            self._save_settings_snapshot();return
        if timer is None:
            timer=QTimer(self);timer.setSingleShot(True);timer.setInterval(250)
            timer.timeout.connect(self._save_settings_snapshot);self._settings_save_timer=timer
        # Do not restart an active timer: continuous updates must still save.
        if not timer.isActive():timer.start()

    def _save_settings_snapshot(self):
        if getattr(self,'_settings_load_failed',False):
            logger.error('Skipping settings save because the existing settings did not load; preserving the original file')
            return
        deferred=bool(getattr(self,'_ui_deferred',False));geometry=getattr(self,'saved_geometry',None) if deferred else None
        if not deferred:
            try:geometry=bytes(self.saveGeometry().toBase64()).decode('ascii')
            except (RuntimeError,ValueError):logger.debug('Could not save window geometry',exc_info=True)
        tab_source=getattr(self,'saved_tabs',[]) if deferred else getattr(self,'tabs',[]);tabs=[{k:v for k,v in tab.items() if k in ('path','kind','archive','archive_prefix','scroll','positions','sort','desc','unsorted','selection','color','source_order','view_id')} for tab in tab_source]
        active_tab=max(0,int(getattr(self,'saved_active_tab',0))) if deferred else (max(0,self.current_tab_index()) if hasattr(self,'tabbar') else 0)
        payload={'favorites':sorted(self.favorites),'favorite_groups':self.favorite_groups,'tree_favorites':sorted(self.tree_favorites),'pinned_folders':sorted(self.pinned_folders),'pinned_thumbs':self.pinned_thumbs,'pinned_folder_sizes':self.pinned_folder_sizes,'pinned_folder_sizes_checked':self.pinned_folder_sizes_checked,'custom':self.custom,'image_search_provider':self.image_search_provider,'saucenao_api_key':getattr(self,'saucenao_api_key',''),'hover_details_enabled':self.hover_details_enabled,'hover_details_delay':self.hover_details_delay,'hover_details_placement':self.hover_details_placement,'thumbnail_cache_enabled':self.thumbnail_cache_enabled,'thumbnail_cache_limit_mib':self.thumbnail_cache_limit_mib,'auto_hide_main':getattr(self,'auto_hide_main',False),'auto_hide_tabs':getattr(self,'auto_hide_tabs',False),'auto_hide_tree':getattr(self,'auto_hide_tree',False),'main_hide_delay':getattr(self,'main_hide_delay',900),'tree_hide_delay':getattr(self,'tree_hide_delay',900),'last':str(self.current),'startup':self.startup_folder,'window_mode':self.window_mode,'geometry':geometry,'tabs':tabs,'active_tab':active_tab}
        payload.update({'gpu_composition_enabled':self.gpu_composition_enabled,'gpu_blur_enabled':self.gpu_blur_enabled,'gpu_thumbnail_rendering_enabled':self.gpu_thumbnail_rendering_enabled,'file_thumbnail_disk_cache_enabled':self.file_thumbnail_disk_cache_enabled,'explorer_context_menu_enabled':getattr(self,'explorer_context_menu_enabled',True)})
        appearance={f'appearance_{surface}_{field}':getattr(self,f'appearance_{surface}_{field}') for surface in ('main','buttons','tabs','tree','menus') for field in ('transparency','blur','background_color','background_hue','background_brightness','color','accent_color','accent_hue','accent_brightness','gradient_area','gradient_transparency','gradient_brightness','gradient_distance','gradient_position_y','gradient_rotation','gradient_hue')}
        payload.update({'slideshow_interval_ms':self.slideshow_interval_ms,'wheel_scroll_rows':self.wheel_scroll_rows,'reader_scroll_speed':self.reader_scroll_speed,'reader_zoom':self.reader_zoom,'viewer_mode':self.viewer_mode,'viewer_wheel_behavior':self.viewer_wheel_behavior,'scrollbars_auto_hide':self.scrollbars_auto_hide,'scrollbars_hide_delay':self.scrollbars_hide_delay,'smooth_wheel_scroll':self.smooth_wheel_scroll,'scroll_glide_duration_ms':self.scroll_glide_duration_ms,'scroll_test_logging_enabled':self.scroll_test_logging_enabled,'auto_refresh_folders':self.auto_refresh_folders,'folder_refresh_interval_s':self.folder_refresh_interval_s,'continuous_viewer_swipe':self.continuous_viewer_swipe,'right_controls_auto_hide':self.right_controls_auto_hide,'right_controls_hide_delay':self.right_controls_hide_delay,'log_storage_mib':self.log_storage_mib,'recursive_search_enabled':self.recursive_search_enabled,'preload_all_readable_pages':self.preload_all_readable_pages,'appearance_transparency':self.appearance_transparency,'appearance_blur':self.appearance_blur,'appearance_color':self.appearance_color,'appearance_gradient_area':self.appearance_gradient_area,'appearance_gradient_distance':self.appearance_gradient_distance,'appearance_gradient_position_y':self.appearance_gradient_position_y,'appearance_gradient_hue':self.appearance_gradient_hue,'appearance_bottom_gradient_enabled':self.appearance_bottom_gradient_enabled,'appearance_bottom_gradient_area':self.appearance_bottom_gradient_area,'appearance_bottom_gradient_distance':self.appearance_bottom_gradient_distance,'appearance_bottom_gradient_position_y':self.appearance_bottom_gradient_position_y,'appearance_bottom_gradient_transparency':self.appearance_bottom_gradient_transparency,'appearance_bottom_gradient_brightness':self.appearance_bottom_gradient_brightness,'appearance_bottom_gradient_rotation':self.appearance_bottom_gradient_rotation,'appearance_bottom_gradient_hue':self.appearance_bottom_gradient_hue,'appearance_bottom_gradient_color':self.appearance_bottom_gradient_color,'appearance_custom_colors':self.appearance_custom_colors,'appearance_font_color':self.appearance_font_color,**appearance,'folder_sort_settings':self.folder_sort_settings,'folder_manual_orders':self.folder_manual_orders,'manual_order_created':self.manual_order_created,'tree_expanded_paths':sorted(self.tree_expanded_paths),'tree_selected_path':self.tree_selected_path,'tree_scroll_value':self.tree_scroll_value,'tree_scroll_anchor_path':getattr(self,'tree_scroll_anchor_path',''),'tree_scroll_anchor_offset':getattr(self,'tree_scroll_anchor_offset',0),'thumbnail_cache_dir':str(self.persistent_thumb_dir),'google_lens_query':getattr(self,'google_lens_query',''),'disk_thumbnail_cache_limit_enabled':self.disk_thumbnail_cache_limit_enabled,'disk_thumbnail_cache_limit_mib':self.disk_thumbnail_cache_limit_mib})
        closed_tabs=[{k:v for k,v in tab.items() if k in ('path','kind','archive','archive_prefix','scroll','positions','sort','desc','unsorted','selection','color','source_order','view_id')} for tab in getattr(self,'closed_tabs',[])[-20:]]
        payload.update({'closed_tabs':closed_tabs})
        payload.update({'settings_dialog_geometries':self.settings_dialog_geometries})
        try:
            # Snapshot mutable GUI-owned data before handing it to the writer.
            snapshot=copy.deepcopy(payload)
            writer=getattr(self,'_settings_writer',None)
            if writer is None:
                writer=SettingsWriter(self.settings_file,logger);self._settings_writer=writer
            writer.submit(snapshot)
        except (OSError,TypeError,ValueError,RuntimeError):logger.exception('Could not queue application settings')
    @staticmethod
    def settings_backup_signature(path):
        raw=Path(path).read_bytes()
        try:
            data=json.loads(raw.decode('utf-8'))
            # These collections are serialized from sets, so their order can
            # vary between launches without representing a user change.
            for key in ('favorites','tree_favorites','pinned_folders'):
                if isinstance(data,dict) and isinstance(data.get(key),list):data[key]=sorted(data[key])
            canonical=json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')
            return hashlib.sha256(canonical).digest()
        except (UnicodeError,json.JSONDecodeError,TypeError,ValueError):
            return hashlib.sha256(raw).digest()
    def backup_settings_if_changed(self):
        if getattr(self,'_ui_deferred',False) or getattr(self,'_settings_load_failed',False) or not self.settings_file.is_file():return
        backup_dir=self.cache_dir/'Backups'
        try:
            backup_dir.mkdir(parents=True,exist_ok=True)
            current_signature=self.settings_backup_signature(self.settings_file)
            backups=sorted(backup_dir.glob('settings_*.json'),key=lambda path:(path.stat().st_mtime_ns,path.name))
            def trim_old_backups(paths):
                for stale in paths[:-10]:
                    try:stale.unlink()
                    except OSError:logger.warning('Could not remove old settings backup %s',stale,exc_info=True)
            if backups:
                latest=backups[-1]
                if self.settings_backup_signature(latest)==current_signature or time.time()-latest.stat().st_mtime<60:
                    trim_old_backups(backups)
                    return
            stamp=datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            destination=backup_dir/f'settings_{stamp}.json';temporary=destination.with_suffix('.tmp')
            temporary.write_bytes(self.settings_file.read_bytes());os.replace(temporary,destination)
            backups=sorted(backup_dir.glob('settings_*.json'),key=lambda path:(path.stat().st_mtime_ns,path.name))
            trim_old_backups(backups)
            logger.info('Created settings backup %s',destination)
        except OSError:
            logger.exception('Could not create settings backup in %s',backup_dir)
    def prepare_settings_dialog(self,dlg,key,default_size=(560,380),minimum_size=None):
        dlg.resize(*default_size)
        if minimum_size:dlg.setMinimumSize(*minimum_size)
        saved=self.settings_dialog_geometries.get(key)
        if saved:
            try:dlg.restoreGeometry(QByteArray.fromBase64(saved.encode('ascii')))
            except (ValueError,RuntimeError):logger.debug('Could not restore settings dialog geometry for %s',key,exc_info=True)
        def remember_geometry(_result=0,dialog=dlg,dialog_key=key):
            try:
                self.settings_dialog_geometries[dialog_key]=bytes(dialog.saveGeometry().toBase64()).decode('ascii');self.save_settings()
            except RuntimeError:logger.debug('Settings dialog closed before its geometry could be saved',exc_info=True)
        dlg.finished.connect(remember_geometry)
    def configure_hover_hide_settings(self):
        dlg=QDialog(self);dlg.setWindowTitle('Hover and hide settings');self.prepare_settings_dialog(dlg,'hover_hide',(600,420),(580,380));layout=QVBoxLayout(dlg);tabs=QTabWidget();layout.addWidget(tabs)
        hover=QWidget();hl=QVBoxLayout(hover);row=QHBoxLayout();row.addWidget(QLabel('Show thumbnail details after'));delay=QSpinBox();delay.setRange(0,3000);delay.setSingleStep(100);delay.setSuffix(' ms');delay.setValue(self.hover_details_delay);row.addWidget(delay);row.addStretch();hl.addLayout(row)
        place_row=QHBoxLayout();place_row.addWidget(QLabel('Tooltip placement'));placement=QComboBox();placement.addItem('Over the top of the thumbnail','top');placement.addItem('Below; fall back above','below');placement.addItem('Above; fall back below','above');placement.setCurrentIndex(max(0,placement.findData('below' if self.hover_details_placement=='bottom' else self.hover_details_placement)));place_row.addWidget(placement,1);hl.addLayout(place_row);note=QLabel('Folder size includes files in its subfolders and is calculated in the background.');note.setWordWrap(True);hl.addWidget(note);hl.addStretch();tabs.addTab(hover,'Thumbnail hover')
        auto=QWidget();al=QVBoxLayout(auto);main=QCheckBox('Hide the main bar until the pointer reaches the top edge');main.setChecked(self.auto_hide_main);al.addWidget(main);mr=QHBoxLayout();mr.addWidget(QLabel('Main bar stays visible for'));md=QSpinBox();md.setRange(0,10000);md.setSuffix(' ms');md.setValue(self.main_hide_delay);mr.addWidget(md);al.addLayout(mr);tabs_check=QCheckBox('Hide the tabs with the main bar');tabs_check.setChecked(self.auto_hide_tabs);al.addWidget(tabs_check);tree=QCheckBox('Hide the file tree until the pointer reaches the left edge');tree.setChecked(self.auto_hide_tree);al.addWidget(tree);tr=QHBoxLayout();tr.addWidget(QLabel('File tree stays visible for'));td=QSpinBox();td.setRange(0,10000);td.setSuffix(' ms');td.setValue(self.tree_hide_delay);tr.addWidget(td);al.addLayout(tr);al.addStretch();tabs.addTab(auto,'Auto-hide panels')
        right=QWidget();rl=QVBoxLayout(right);hide_right=QCheckBox('Hide right-side buttons until the pointer reaches the right edge');hide_right.setChecked(self.right_controls_auto_hide);rl.addWidget(hide_right);rr=QHBoxLayout();rr.addWidget(QLabel('Keep buttons visible for'));rd=QSpinBox();rd.setRange(0,10000);rd.setSuffix(' ms');rd.setValue(self.right_controls_hide_delay);rr.addWidget(rd);rl.addLayout(rr);rl.addStretch();tabs.addTab(right,'Right-side buttons')
        scroll=QWidget();sl=QVBoxLayout(scroll);hide_scroll=QCheckBox('Hide scroll bars until the pointer reaches them');hide_scroll.setChecked(self.scrollbars_auto_hide);sl.addWidget(hide_scroll);sr=QHBoxLayout();sr.addWidget(QLabel('Keep scroll bars visible for'));sd=QSpinBox();sd.setRange(0,10000);sd.setSuffix(' ms');sd.setValue(self.scrollbars_hide_delay);sr.addWidget(sd);sl.addLayout(sr);sl.addStretch();tabs.addTab(scroll,'Scroll bars')
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel);buttons.accepted.connect(dlg.accept);buttons.rejected.connect(dlg.reject);layout.addWidget(buttons)
        if dlg.exec()==QDialog.Accepted:
            self.hover_details_enabled=True;self.hover_details_delay=delay.value();self.hover_details_placement=placement.currentData();self.grid.set_hover_settings(True,self.hover_details_delay);self.auto_hide_main=main.isChecked();self.auto_hide_tabs=tabs_check.isChecked();self.main_hide_delay=md.value();self.auto_hide_tree=tree.isChecked();self.tree_hide_delay=td.value();self.right_controls_auto_hide=hide_right.isChecked();self.right_controls_hide_delay=rd.value();self.scrollbars_auto_hide=hide_scroll.isChecked();self.scrollbars_hide_delay=sd.value();self.apply_auto_hide();self.apply_scrollbar_behavior();self.favorite_overlay_timer.stop()
            if self.right_controls_auto_hide:self.hide_favorite_controls()
            else:self.show_right_controls()
            self.save_settings();QTimer.singleShot(0,self.grid.refresh_hover_from_cursor)
    def configure_slideshow(self):
        dlg=QDialog(self);dlg.setWindowTitle('Slideshow');self.prepare_settings_dialog(dlg,'slideshow',(430,220));layout=QVBoxLayout(dlg);row=QHBoxLayout();row.addWidget(QLabel('Time per image'));interval=QSpinBox();interval.setRange(250,60000);interval.setSingleStep(250);interval.setSuffix(' ms');interval.setValue(self.slideshow_interval_ms);row.addWidget(interval);layout.addLayout(row)
        note=QLabel('The viewer advances to the next image at this interval while the slideshow button is active.');note.setWordWrap(True);layout.addWidget(note);buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel);buttons.accepted.connect(dlg.accept);buttons.rejected.connect(dlg.reject);layout.addWidget(buttons)
        if dlg.exec()==QDialog.Accepted:self.slideshow_interval_ms=interval.value();self.save_settings()
    def refresh_context_menu_appearance(self):
        for menu in self.findChildren(AppearanceMenu):menu.configure(self)

    def apply_appearance_settings(self,persist=True):
        def css_color(color,alpha):
            c=QColor(color);alpha=max(0,min(255,int(alpha)))
            return c.name() if alpha==255 else f'rgba({c.red()},{c.green()},{c.blue()},{alpha})'
        def centered_gradient(color,alpha,position_x,position_y,area,rotation,base_color,base_alpha=None,center_alpha=None):
            # The fade must blend into the surface color, not into transparent
            # pixels. Otherwise a gradient makes even a 0%-transparent panel
            # look see-through. X/Y define the center; rotation only changes
            # the gradient's axis around that center.
            edge_alpha=alpha if base_alpha is None else max(0,min(255,int(base_alpha)));center_alpha=alpha if center_alpha is None else max(0,min(255,int(center_alpha)))
            if not color or int(area)<=0:return css_color(base_color,edge_alpha)
            c=QColor(color);base=QColor(base_color);angle=math.radians(int(rotation)%360);span=max(.005,min(2.5,float(area)/200.0));cx=int(position_x)/100;cy=(100-int(position_y))/100;dx=math.cos(angle)*span;dy=math.sin(angle)*span
            return f'qlineargradient(x1:{cx-dx:.4f},y1:{cy-dy:.4f},x2:{cx+dx:.4f},y2:{cy+dy:.4f},stop:0 {css_color(base.name(),edge_alpha)},stop:0.5 {css_color(c.name(),center_alpha)},stop:1 {css_color(base.name(),edge_alpha)})'
        def surface(name,base_color):
            transparency=max(0,min(100,int(getattr(self,f'appearance_{name}_transparency',0))));alpha=255-round(255*transparency/100)
            base_qcolor=QColor(base_color)
            background_hue=int(getattr(self,f'appearance_{name}_background_hue',base_qcolor.hue() if base_qcolor.hue()>=0 else 0))%360
            background_brightness=max(0,min(200,int(getattr(self,f'appearance_{name}_background_brightness',100))))
            base_qcolor.setHsv(background_hue,base_qcolor.saturation(),max(0,min(255,round(base_qcolor.value()*background_brightness/100))))
            rendered_base=base_qcolor.name()
            raw_gradient=getattr(self,f'appearance_{name}_color','');gradient_color=QColor(raw_gradient or base_color)
            gradient_hue=int(getattr(self,f'appearance_{name}_gradient_hue',gradient_color.hue() if gradient_color.hue()>=0 else 35))%360
            if raw_gradient:gradient_color.setHsv(gradient_hue,gradient_color.saturation() if gradient_color.saturation()>0 else 210,gradient_color.value())
            gradient_brightness=max(0,min(200,int(getattr(self,f'appearance_{name}_gradient_brightness',100))))
            gradient_color.setHsv(gradient_color.hue() if gradient_color.hue()>=0 else gradient_hue,gradient_color.saturation(),max(0,min(255,round(gradient_color.value()*gradient_brightness/100))))
            overlay_color=QColor(gradient_color)
            gradient_transparency=max(0,min(100,int(getattr(self,f'appearance_{name}_gradient_transparency',100))))
            # Gradient transparency blends the gradient back into the
            # configured background. Only the surface transparency reveals
            # the thumbnails behind buttons and tabs.
            blend=gradient_transparency/100.0
            gradient_color.setRgb(round(gradient_color.red()*(1-blend)+base_qcolor.red()*blend),round(gradient_color.green()*(1-blend)+base_qcolor.green()*blend),round(gradient_color.blue()*(1-blend)+base_qcolor.blue()*blend))
            center_alpha=alpha
            base=css_color(rendered_base,alpha)
            gradient_args=(gradient_color.name(),alpha,getattr(self,f'appearance_{name}_gradient_distance',50),getattr(self,f'appearance_{name}_gradient_position_y',50),getattr(self,f'appearance_{name}_gradient_area',55),getattr(self,f'appearance_{name}_gradient_rotation',0),rendered_base)
            gradient=centered_gradient(*gradient_args,base_alpha=alpha,center_alpha=center_alpha) if raw_gradient and int(getattr(self,f'appearance_{name}_gradient_area',55))>0 else base
            overlay_args=(overlay_color.name(),*gradient_args[1:])
            overlay_gradient=centered_gradient(*overlay_args,base_alpha=0,center_alpha=round(255*(100-gradient_transparency)/100)) if raw_gradient and int(getattr(self,f'appearance_{name}_gradient_area',55))>0 else ''
            raw_accent=getattr(self,f'appearance_{name}_accent_color','');accent=QColor(raw_accent or '#d0a32d')
            accent_hue=int(getattr(self,f'appearance_{name}_accent_hue',accent.hue() if accent.hue()>=0 else 35))%360
            accent_sat=accent.saturation() if accent.saturation()>0 else 210
            accent_brightness=max(0,min(200,int(getattr(self,f'appearance_{name}_accent_brightness',100))))
            accent.setHsv(accent_hue,accent_sat,max(0,min(255,round(accent.value()*accent_brightness/100))))
            return {'alpha':alpha,'base':base,'base_color':rendered_base,'accent':accent,'color':raw_accent,'gradient_color':raw_gradient,'blur':int(getattr(self,f'appearance_{name}_blur',0)),'gradient':gradient,'overlay_gradient':overlay_gradient,'transparency':transparency}
        main=surface('main',getattr(self,'appearance_main_background_color','#202124'));buttons=surface('buttons',getattr(self,'appearance_buttons_background_color','#303236'));tabs=surface('tabs',getattr(self,'appearance_tabs_background_color','#2b2d31'));tree=surface('tree',getattr(self,'appearance_tree_background_color','#15171c'));menus=surface('menus',getattr(self,'appearance_menus_background_color','#25272b'))
        path_color=self.appearance_bottom_gradient_color
        if path_color:
            path_qcolor=QColor(path_color);path_hue=int(self.appearance_bottom_gradient_hue)%360;path_saturation=path_qcolor.saturation() if path_qcolor.saturation()>0 else 210;path_value=max(0,min(255,round(path_qcolor.value()*int(self.appearance_bottom_gradient_brightness)/100)));path_qcolor.setHsv(path_hue,path_saturation,path_value);path_color=path_qcolor.name()
        path_alpha=255-round(255*max(0,min(100,int(self.appearance_bottom_gradient_transparency)))/100)
        # Path contrast is a second color layer over the shared main-panel
        # gradient. Its edges must be transparent so it does not replace the
        # Main Panel gradient or reset the gradient coordinates.
        path_gradient=centered_gradient(path_color,path_alpha,self.appearance_bottom_gradient_distance,self.appearance_bottom_gradient_position_y,self.appearance_bottom_gradient_area,self.appearance_bottom_gradient_rotation,'#202124',0, path_alpha) if path_color and self.appearance_bottom_gradient_area>0 else ''
        font_color=self.appearance_font_color
        if hasattr(self,'menu_bar'):
            self.menu_bar.accent_color=QColor(main['accent']);self.menu_bar.text_color=QColor(font_color);self.menu_bar.update()
        self.refresh_context_menu_appearance()
        # Render the Main Panel once on a shared backdrop spanning the menu,
        # controls, path, tabs and status strip. The Path contrast layer is
        # painted over it by a child overlay with the same geometry.
        main_panel_background=css_color(main['base_color'],255)
        # Qt can retain WA_NoSystemBackground after a widget has previously
        # been translucent. Explicitly paint opaque main surfaces when the
        # user has effects disabled; otherwise the thumbnail grid bleeds
        # through these panels on some Windows/Qt styles.
        main_surface_css='transparent'
        style=QSS+f'''
QFrame#top{{background-color:{main_surface_css};background:{main_surface_css};color:{font_color};}}
QFrame#navigation{{background-color:{main_surface_css};background:{main_surface_css};color:{font_color};}}
QMenuBar{{background-color:{main_surface_css};background:{main_surface_css};color:{font_color};}}
QFrame#line{{background:transparent;max-height:0px;}}
QMenuBar::item,QMenu{{color:{font_color};}}
QMenuBar{{border-bottom:0px;}}
QMenuBar::item:selected,QMenuBar::item:pressed,QMenuBar::item:hover{{background:{main['accent'].name()};}}
QToolButton{{background-color:{buttons['base']};background:{buttons['gradient']};color:#ffffff;}}
QToolButton#gold{{background-color:{buttons['base']};background:{buttons['gradient']};color:#ffffff;}}
QToolButton:hover{{background-color:{buttons['base']};background:{buttons['gradient']};border-color:{buttons['accent'].name() if buttons['color'] else '#60646b'};}}
QToolButton:pressed{{background:{css_color('#292b2f',buttons['alpha'])};}}
QToolButton:checked{{background:{css_color(buttons['accent'].name(),buttons['alpha'])};border-color:{buttons['accent'].name()};}}
QToolButton#minimizeButton,QToolButton#fullscreenToggleButton,QToolButton#fullscreenCloseButton{{min-width:22px;max-width:22px;min-height:19px;max-height:19px;padding:0;border-radius:2px;font-size:13px;}}
QToolButton#fullscreenCloseButton{{font-size:15px;}}
QToolButton#fullscreenToggleButton{{font-size:13px;}}
QToolButton#fullscreenCloseButton:hover,QToolButton#minimizeButton:hover,QToolButton#fullscreenToggleButton:hover{{border-color:{buttons['accent'].name() if buttons['color'] else '#60646b'};}}
QLineEdit#search{{background-color:{buttons['base']};background:{buttons['gradient']};color:{font_color};}}
QSlider#thumbnailSizeSlider::handle:horizontal{{width:10px;height:10px;margin:-3px 0;background:{buttons['gradient']};border:1px solid {buttons['accent'].name() if buttons['color'] else '#858990'};border-radius:0px;}}
QMenu{{background-color:{menus['base']};background:{menus['gradient']};color:{font_color};border:1px solid #4b4e53;}}
QMenu::item:selected{{background:{css_color(menus['accent'].name() if menus['color'] else '#393c41',menus['alpha'])};color:{font_color};}}
QTabBar#navigationTabs{{background-color:transparent;background:transparent;}}
QTabBar#navigationTabs::tab-bar{{background:transparent;}}
QTabBar::tab{{background-color:{tabs['base']};background:{tabs['gradient']};color:{font_color};}}
QTabBar::tab:selected{{background:{css_color(tabs['accent'].name() if tabs['color'] else '#3a3c41',tabs['alpha'])};border-color:{tabs['accent'].name() if tabs['color'] else '#65686d'};color:{font_color};}}
QTabBar::tab:hover{{background:{css_color(tabs['accent'].name(),tabs['alpha']) if tabs['color'] else css_color('#34373c',tabs['alpha'])};}}
QDockWidget#fileTreeDock{{background:transparent;color:{font_color};border-right:1px solid #3b3e44;}}
QDockWidget#fileTreeDock::title{{background:transparent;color:{font_color};padding:7px;font-weight:600;}}
QTreeWidget{{background:transparent;color:{font_color};border:0;padding:8px 5px;font-size:13px;}}
QTreeWidget::item{{padding:5px 4px;}}
QTreeWidget::item:hover{{background:{css_color(tree['accent'].name(),tree['alpha']) if tree['color'] else css_color('#2b2d31',tree['alpha'])};}}
QTreeWidget::item:selected{{background:{css_color(tree['accent'].name(),tree['alpha']) if tree['color'] else css_color('#51441d',tree['alpha'])};color:{font_color};}}
QLabel#crumb,QLabel#status,QLabel#section{{color:{font_color};}}
'''
        if getattr(self,'_appearance_stylesheet_cache',None)!=style:
            self.setStyleSheet(style);self._appearance_stylesheet_cache=style
        eligible=False
        panel_backdrop=getattr(self,'main_panel_backdrop',None)
        if panel_backdrop is not None:
            panel_backdrop_style=f'QLabel#appearancePanelBackdrop{{background:{main_panel_background};}}'
            if panel_backdrop.property('appearance_style')!=panel_backdrop_style:panel_backdrop.setStyleSheet(panel_backdrop_style);panel_backdrop.setProperty('appearance_style',panel_backdrop_style)
            path_overlay=getattr(self,'main_path_gradient_overlay',None)
            if path_overlay is not None:
                path_overlay_style=f'QLabel#appearancePathGradient{{background:{path_gradient or "transparent"};}}'
                if path_overlay.property('appearance_style')!=path_overlay_style:path_overlay.setStyleSheet(path_overlay_style);path_overlay.setProperty('appearance_style',path_overlay_style)
                path_overlay.setGeometry(panel_backdrop.rect());path_overlay.setVisible(bool(path_gradient))
            gradient_overlay=getattr(self,'main_gradient_overlay',None)
            if gradient_overlay is not None:
                gradient_overlay_style=f'QLabel#appearanceMainGradient{{background:{main["overlay_gradient"] or "transparent"};}}'
                if gradient_overlay.property('appearance_style')!=gradient_overlay_style:gradient_overlay.setStyleSheet(gradient_overlay_style);gradient_overlay.setProperty('appearance_style',gradient_overlay_style)
                gradient_overlay.setGeometry(panel_backdrop.rect());gradient_overlay.setVisible(bool(main['overlay_gradient']))
            blur_layer=getattr(self,'main_blur_backdrop',None);effect=blur_layer.graphicsEffect() if blur_layer is not None else None
            if blur_layer is not None:
                blur_layer.setStyleSheet('QLabel#appearanceMainBlur{background:transparent;}');blur_layer.setGeometry(panel_backdrop.rect());blur_layer.lower()
            # Blur is baked into the sampled pixels before painting. A widget
            # graphics effect was intermittently clearing the translucent
            # backdrop on Windows after the first repaint.
            if effect is not None:blur_layer.setGraphicsEffect(None)
            if main['alpha']==255 and blur_layer is not None:blur_layer.setPixmap(QPixmap())
            eligible|=bool(main['transparency']>0)
        for name,backdrop,cfg in (('buttons',getattr(self,'controls_backdrop',None),buttons),('tabs',getattr(self,'tabs_backdrop',None),tabs),('tree',getattr(self,'tree_blur_backdrop',None),tree)):
            if backdrop is None:continue
            reveal=cfg['transparency']>0
            # Backdrops are blur-only image layers. Surface colors and
            # gradients belong to the actual panel widgets, so they are not
            # painted twice underneath the same content.
            backdrop_style='QLabel#appearanceBackdrop{background:transparent;}'
            if backdrop.property('appearance_style')!=backdrop_style:backdrop.setStyleSheet(backdrop_style);backdrop.setProperty('appearance_style',backdrop_style)
            effect=backdrop.graphicsEffect()
            # Every surface uses bounded, pre-blurred samples so the blur
            # cannot expand beyond the requested panel or control area.
            if effect is not None:backdrop.setGraphicsEffect(None)
            if not reveal and name!='tabs':backdrop.setPixmap(QPixmap())
            eligible|=bool(reveal)
        # The tab strip needs a Main Panel backplate even when tab transparency
        # is off, because the spaces between tab shapes are not part of a tab.
        eligible|=getattr(self,'tabbar',None) is not None
        # The tree background is painted once across the title and contents.
        if hasattr(self,'tree_dock'):
            self.tree_dock.setAttribute(Qt.WA_TranslucentBackground,True)
            self.tree_dock.setAttribute(Qt.WA_NoSystemBackground,False)
            self.tree_dock.setAttribute(Qt.WA_OpaquePaintEvent,False)
            self.tree_dock.setAutoFillBackground(False)
            dock_palette=self.tree_dock.palette();dock_palette.setColor(QPalette.Window,QColor(tree['base_color']));dock_palette.setColor(QPalette.Base,QColor(tree['base_color']));self.tree_dock.setPalette(dock_palette)
            tree_gradient=getattr(self,'tree_gradient_overlay',None)
            if tree_gradient is not None:
                tree_style=f'QLabel#appearanceTreeGradient{{background:{tree["overlay_gradient"] or "transparent"};}}'
                if tree_gradient.property('appearance_style')!=tree_style:tree_gradient.setStyleSheet(tree_style);tree_gradient.setProperty('appearance_style',tree_style)
                tree_gradient.setGeometry(self.tree_dock.rect());tree_gradient.setVisible(bool(tree['overlay_gradient']));tree_gradient.raise_()
            titlebar=getattr(self,'tree_titlebar',None)
            if titlebar is not None:
                titlebar.setAttribute(Qt.WA_NoSystemBackground,True);titlebar.setAttribute(Qt.WA_TranslucentBackground,True);titlebar.setAttribute(Qt.WA_OpaquePaintEvent,False);titlebar.setAutoFillBackground(False)
                titlebar.setStyleSheet('QWidget#fileTreeTitleBar{background:transparent;}')
        if hasattr(self,'file_tree'):
            self.file_tree.setAttribute(Qt.WA_TranslucentBackground,True)
            self.file_tree.setAttribute(Qt.WA_NoSystemBackground,False)
            self.file_tree.setAttribute(Qt.WA_OpaquePaintEvent,False)
            self.file_tree.setAutoFillBackground(False)
            viewport=self.file_tree.viewport()
            viewport.setAttribute(Qt.WA_TranslucentBackground,True)
            viewport.setAttribute(Qt.WA_NoSystemBackground,False)
            viewport.setAttribute(Qt.WA_OpaquePaintEvent,False)
            viewport.setAutoFillBackground(False)
            palette=viewport.palette();palette.setColor(QPalette.Window,QColor(tree['base_color']));palette.setColor(QPalette.Base,QColor(tree['base_color']));viewport.setPalette(palette)
            tree_css=f'QTreeWidget{{background:transparent;color:{font_color};border:0;padding:8px 5px;font-size:13px;}}'
            if self.file_tree.styleSheet()!=tree_css:self.file_tree.setStyleSheet(tree_css)
            if viewport.styleSheet()!='background:transparent;':viewport.setStyleSheet('background:transparent;')
        if panel_backdrop is not None and hasattr(self,'main_bar') and hasattr(self,'nav_bar') and hasattr(self,'root_widget'):
            top_pos=self.main_bar.mapTo(self.root_widget,QPoint(0,0));nav_pos=self.nav_bar.mapTo(self.root_widget,QPoint(0,0));top=top_pos.y()+self.main_bar.height();bottom=nav_pos.y()+self.nav_bar.height();y=min(top_pos.y(),nav_pos.y())
            if self.auto_hide_main and not self.main_bar.isVisible():
                if self.auto_hide_tabs or not self.nav_bar.isVisible():panel_backdrop.hide()
                else:
                    nav_pos=self.nav_bar.mapTo(self.root_widget,QPoint(0,0));panel_backdrop.setGeometry(QRect(0,nav_pos.y(),self.root_widget.width(),self.nav_bar.height()));panel_backdrop.show();panel_backdrop.raise_();self.nav_bar.raise_()
            else:
                panel_backdrop.setGeometry(QRect(0,y,self.root_widget.width(),max(top,bottom)-y));panel_backdrop.show();panel_backdrop.raise_()
            if hasattr(self,'main_blur_backdrop'):self.main_blur_backdrop.setGeometry(panel_backdrop.rect());self.main_blur_backdrop.lower()
            if hasattr(self,'main_gradient_overlay'):self.main_gradient_overlay.setGeometry(panel_backdrop.rect());self.main_gradient_overlay.raise_()
            if hasattr(self,'main_path_gradient_overlay'):self.main_path_gradient_overlay.setGeometry(panel_backdrop.rect())
            if hasattr(self,'main_path_gradient_overlay'):self.main_path_gradient_overlay.raise_()
            self.raise_main_panel_contents()
        if hasattr(self,'tree_backdrop') and hasattr(self,'tree_dock'):
            tree_base_style=f'QLabel#appearanceTreeBackdrop{{background:{css_color(tree["base_color"],255)};}}'
            if self.tree_backdrop.property('appearance_style')!=tree_base_style:self.tree_backdrop.setStyleSheet(tree_base_style);self.tree_backdrop.setProperty('appearance_style',tree_base_style)
            self.tree_backdrop.setGeometry(self.tree_dock.rect());self.tree_backdrop.lower()
            if hasattr(self,'tree_blur_backdrop'):self.tree_blur_backdrop.setGeometry(self.tree_backdrop.rect());self.tree_blur_backdrop.lower()
            if hasattr(self,'tree_gradient_overlay'):self.tree_gradient_overlay.setGeometry(self.tree_backdrop.rect());self.tree_gradient_overlay.raise_()
        if hasattr(self,'appearance_capture_timer'):
            if eligible:self.appearance_capture_timer.start()
            else:
                self.appearance_capture_timer.stop()
                for backdrop in (getattr(self,'main_blur_backdrop',None),getattr(self,'controls_backdrop',None),getattr(self,'tree_blur_backdrop',None)):
                    if backdrop is not None:backdrop.setPixmap(QPixmap())
        if getattr(self,'_gpu_composition',None) is not None and hasattr(self,'appearance_capture_timer'):self.appearance_capture_timer.start()
        if persist:self.save_settings()
    def raise_main_panel_contents(self):
        # The shared appearance backplate must stay behind the real controls
        # in both layout-managed and auto-hide modes. Mouse-transparent layers
        # can still cover widgets visually even when hit testing looks correct.
        if self.nav_bar.isVisible():self.nav_bar.raise_()
        if self.main_bar.isVisible():self.main_bar.raise_()
        for name in ('minimize_button','fullscreen_button','fullscreen_close_button'):
            button=getattr(self,name,None)
            if button is not None and button.isVisible():button.raise_()

    def appearance_source_snapshot(self,source):
        # Keep one source snapshot during ordinary use as well as previews.
        # Viewport paints, geometry, scale and scrolling invalidate it.
        enabled=not getattr(self,'_appearance_cache_disabled',False)
        key=(id(source),source.width(),source.height(),source.devicePixelRatioF(),self.grid.verticalScrollBar().value(),getattr(self,'_appearance_source_revision',0))
        cached=getattr(self,'_appearance_source_cache',None)
        if enabled and cached is not None and cached[0]==key:return cached[1]
        # Reuse the same cached card pixels that draw the grid. Reading the
        # entire OpenGL framebuffer back to the CPU stalls presentation and
        # gives the appearance layers an older frame.
        image=self.grid.render_appearance_source() if source is self.grid.viewport() else source.grab()
        # Backdrop rectangles use logical coordinates. Normalize only this
        # blurred/background sample, never the displayed thumbnail pixels.
        if image.devicePixelRatioF()!=1.0:
            image=image.scaled(source.size(),Qt.IgnoreAspectRatio,Qt.SmoothTransformation);image.setDevicePixelRatio(1.0)
        # A stylesheet can request a repaint without changing any thumbnails.
        # Reuse the original pixmap only after an exact pixel comparison.
        if enabled and getattr(self,'_appearance_preview_active',False) and cached is not None and cached[1].size()==image.size() and cached[1].devicePixelRatio()==image.devicePixelRatio() and cached[1].toImage()==image.toImage():image=cached[1]
        key=(id(source),source.width(),source.height(),source.devicePixelRatioF(),self.grid.verticalScrollBar().value(),getattr(self,'_appearance_source_revision',0))
        self._appearance_source_cache=(key,image) if enabled else None
        return image
    def gpu_composition_marker(self):
        return self.cache_dir/'gpu_composition.enabled'
    def close_gpu_composition(self):
        compositor=getattr(self,'_gpu_composition',None);self._gpu_composition=None
        if compositor is not None:
            try:compositor.close()
            except Exception:logger.exception('Could not release GPU composition')
        for name in ('main_blur_backdrop','controls_backdrop','tabs_backdrop','tree_blur_backdrop'):
            widget=getattr(self,name,None)
            if widget is not None:widget.show()
    def disable_gpu_composition(self,reason):
        self.gpu_composition_enabled=False;self.close_gpu_composition()
        try:self.gpu_composition_marker().unlink(missing_ok=True)
        except OSError:logger.warning('Could not remove GPU composition startup marker',exc_info=True)
        action=getattr(self,'gpu_composition_action',None)
        if action is not None:
            blocked=action.blockSignals(True);action.setChecked(False);action.blockSignals(blocked)
        self.apply_appearance_settings(False);self.save_settings()
        if hasattr(self,'status'):self.status.setText('Regular panel rendering restored: '+reason)
    def refresh_gpu_composition(self):
        if not getattr(self,'gpu_composition_enabled',False):return False
        from PySide6.QtGui import QOpenGLContext
        # A viewer-only cold start deliberately avoids initializing shared GL.
        # It can later open the browser using the regular renderer until restart.
        if QOpenGLContext.globalShareContext() is None:return False
        grid=getattr(self,'grid',None)
        if grid is None or not grid.viewport().property('magellan_gpu_viewport') or not getattr(grid.viewport(),'isValid',lambda:False)():return False
        started=time.perf_counter()
        try:
            compositor=getattr(self,'_gpu_composition',None)
            if compositor is None:
                from gpu_composition import Composition
                compositor=Composition(self);self._gpu_composition=compositor
            compositor.refresh()
            self._appearance_capture_started=started;self._appearance_capture_ms=(time.perf_counter()-started)*1000
            self.appearance_capture_timer.setInterval(0)
            return True
        except Exception:
            logger.exception('GPU composition unavailable; restoring regular panels')
            self.disable_gpu_composition('GPU composition unavailable')
            return False
    def set_gpu_composition_enabled(self,enabled):
        self.gpu_composition_enabled=bool(enabled)
        try:
            marker=self.gpu_composition_marker()
            if enabled:marker.touch()
            else:marker.unlink(missing_ok=True)
        except OSError:logger.warning('Could not update GPU composition startup marker',exc_info=True)
        if not enabled:self.close_gpu_composition()
        self.apply_appearance_settings(False);self.save_settings()
        if hasattr(self,'status'):self.status.setText('Restart Magellan to enable experimental GPU composition' if enabled else 'Regular panel rendering restored')

    def close_gpu_blur_backend(self):
        backend=getattr(self,'_gpu_blur_backend',None);self._gpu_blur_backend=None
        if backend is not None:
            try:backend.close()
            except Exception:logger.debug('Could not release GPU blur resources',exc_info=True)
    def gpu_blurred_crop(self,source,rect,radius):
        if not getattr(self,'gpu_blur_enabled',False) or radius<=0:return None
        if not self.grid.viewport().property('magellan_gpu_viewport'):return None
        try:
            backend=getattr(self,'_gpu_blur_backend',None)
            if backend is None:
                from gpu_blur import GpuBlurBackend
                backend=GpuBlurBackend();self._gpu_blur_backend=backend
            cached=getattr(self,'_appearance_source_cache',None)
            tree=getattr(self,'tree_dock',None);primary=cached[1] if cached is not None else None
            tree_size=tree.size() if tree is not None and tree.isVisible() and primary is not None and tree.height()>primary.height() and self.appearance_tree_transparency>0 else None
            return backend.crop(source,rect,radius,primary,tree_size,int(getattr(self,'appearance_tree_blur',0)))
        except Exception:
            logger.warning('Experimental GPU blur failed; using regular blur',exc_info=True)
            self.close_gpu_blur_backend();self.gpu_blur_enabled=False
            action=getattr(self,'gpu_blur_action',None)
            if action is not None:
                blocked=action.blockSignals(True);action.setChecked(False);action.blockSignals(blocked)
            self.save_settings()
            if hasattr(self,'status'):self.status.setText('GPU blur unavailable; regular blur restored')
            return None
    def appearance_blurred_crop(self,source,rect,radius):
        gpu_sample=self.gpu_blurred_crop(source,rect,radius)
        if gpu_sample is not None:return gpu_sample
        enabled=not getattr(self,'_appearance_cache_disabled',False)
        if not enabled:return self.blur_appearance_sample(source.copy(rect),radius)
        source_key=source.cacheKey()
        if getattr(self,'_appearance_crop_source',None)!=source_key:
            self._appearance_crop_source=source_key;self._appearance_crop_cache=OrderedDict();self._appearance_crop_bytes=0
        key=(rect.x(),rect.y(),rect.width(),rect.height(),int(radius));cache=self._appearance_crop_cache
        if key in cache:cache.move_to_end(key);return cache[key]
        image=self.blur_appearance_sample(source.copy(rect),radius);size=image.width()*image.height()*4
        if size<=32*1024*1024:
            while cache and self._appearance_crop_bytes+size>32*1024*1024:
                _,old=cache.popitem(last=False);self._appearance_crop_bytes-=old.width()*old.height()*4
            cache[key]=image;self._appearance_crop_bytes+=size
        return image
    def clear_appearance_preview_cache(self):
        self._appearance_source_cache=None;self._appearance_crop_cache=OrderedDict();self._appearance_crop_bytes=0;self._appearance_crop_source=None

    def schedule_appearance_capture(self,*_):
        timer=getattr(self,'appearance_capture_timer',None)
        if timer is None or timer.isActive():return
        surfaces=(('main',getattr(self,'main_bar',None)),('buttons',getattr(self,'controls_bar',None)),('tabs',getattr(self,'tabbar',None)),('tree',getattr(self,'tree_dock',None)))
        visible=any(widget is not None and widget.isVisible() and getattr(self,f'appearance_{name}_transparency',0)>0 for name,widget in surfaces)
        if not visible and getattr(self,'appearance_buttons_transparency',0)>0:
            visible=any(widget.isVisible() for widget in self.right_control_widgets())
        if visible and getattr(self.grid,'_appearance_frame_scroll',None)==self.grid.verticalScrollBar().value():
            # Completed thumbnail jobs can repaint faster than the display.
            # Coalesce that burst and leave UI time for cold-folder loading.
            busy=bool(self.grid.folder_futures or self.grid.thumb_futures)
            period=1/30 if busy else 1/60
            elapsed=time.perf_counter()-getattr(self,'_appearance_capture_started',0)
            timer.start(max(0,math.ceil((period-elapsed)*1000)))

    def sync_detached_tabs_spacing(self):
        # Tabs stay reachable below a revealed panel when they do not hide
        # with it. Reserve the space only for the duration of the reveal.
        spacer=getattr(self,'main_reveal_spacer',None)
        reserve=self.auto_hide_main and not self.auto_hide_tabs and self.main_bar.isVisible()
        if spacer is None:
            spacer=QWidget(self.root_widget);spacer.setObjectName('mainRevealSpacer')
            spacer.setAttribute(Qt.WA_TransparentForMouseEvents,True)
            self.main_reveal_spacer=spacer
        if self.main_bar_layout.indexOf(spacer)>=0:self.main_bar_layout.removeWidget(spacer)
        if reserve:
            spacer.setFixedHeight(self.main_bar.height());self.main_bar_layout.insertWidget(0,spacer);spacer.show()
        else:spacer.hide()
        self.main_bar_layout.activate()

    def refresh_appearance_backdrop(self):
        if self.refresh_gpu_composition():return
        grid=getattr(self,'grid',None)
        if grid is None:return
        started=time.perf_counter();self._appearance_capture_started=started
        source=grid.viewport();source_global=QRect(source.mapToGlobal(QPoint(0,0)),source.size())
        panels=[]
        for name,panel,backdrop in (('main',getattr(self,'main_panel_backdrop',None),getattr(self,'main_blur_backdrop',None)),('buttons',getattr(self,'controls_bar',None),getattr(self,'controls_backdrop',None)),('tabs',getattr(self,'tabbar',None),getattr(self,'tabs_backdrop',None)),('tree',getattr(self,'tree_dock',None),getattr(self,'tree_blur_backdrop',None))):
            if panel is None or backdrop is None:continue
            blur=int(getattr(self,f'appearance_{name}_blur',0));transparency=int(getattr(self,f'appearance_{name}_transparency',0));global_rect=QRect(panel.mapToGlobal(QPoint(0,0)),panel.size());intersection=global_rect.intersected(source_global)
            if panel.isVisible() and (name=='tabs' or transparency>0):panels.append((name,panel,backdrop,intersection,global_rect,blur))
            elif name!='tabs':backdrop.setPixmap(QPixmap())
        source_image=self.appearance_source_snapshot(source)
        for name,panel,backdrop,intersection,global_rect,blur in panels:
            if name=='tabs':
                # The whole strip gets the exact already-composited Main Panel
                # pixels. Tab-only thumbnail samples are painted over this
                # backplate below and clipped to each tab's rounded shape.
                main_backdrop=getattr(self,'main_panel_backdrop',None)
                main_image=main_backdrop.grab() if main_backdrop is not None and main_backdrop.isVisible() else QPixmap()
                if not main_image.isNull():
                    main_global=QRect(main_backdrop.mapToGlobal(QPoint(0,0)),main_backdrop.size())
                    crop=global_rect.translated(-main_global.topLeft())
                    image=main_image.copy(crop.intersected(main_image.rect()))
                    if image.size()!=panel.size():image=image.scaled(panel.size(),Qt.IgnoreAspectRatio,Qt.SmoothTransformation)
                else:image=QPixmap()
                if image.isNull():image=QPixmap(panel.size());image.fill(Qt.transparent)
            else:
                image=QPixmap(panel.size());image.fill(Qt.transparent)
            painter=QPainter(image)
            clip_region=QRegion()
            # Main and Tree surfaces blend the sampled pixels over a solid
            # base; buttons and tabs use their own translucent widget fills.
            panel_transparency=int(getattr(self,f'appearance_{name}_transparency',0))
            painter.setOpacity(max(0.0,min(1.0,panel_transparency/100.0)) if name in ('main','tree') else 1.0)
            if name in ('buttons','tabs'):
                # Button and tab cutouts reveal the actual thumbnail viewport,
                # even when the Main Panel surface itself is fully opaque.
                widgets=[target for target in [*panel.findChildren(QToolButton),*panel.findChildren(QLineEdit,'search')] if target.isVisibleTo(panel)]
                targets=[QRect(target.mapToGlobal(QPoint(0,0)),target.size()) for target in widgets]
                if name=='buttons' and hasattr(self,'slider') and self.slider.isVisibleTo(panel):
                    option=QStyleOptionSlider();self.slider.initStyleOption(option);handle=self.slider.style().subControlRect(QStyle.CC_Slider,option,QStyle.SC_SliderHandle,self.slider);targets.append(QRect(self.slider.mapToGlobal(handle.topLeft()),handle.size()))
                if name=='tabs':
                    targets=[QRect(panel.mapToGlobal(panel.tabRect(index).topLeft()),panel.tabRect(index).size()) for index in range(panel.count())] if panel_transparency>0 else []
                for target_global in targets:
                    local_rect=target_global.translated(-global_rect.topLeft())
                    painter.save()
                    if name=='tabs':
                        tab_shape=self.tab_backdrop_shape_region(local_rect);clip_region=clip_region.united(tab_shape);painter.setClipRegion(tab_shape,Qt.IntersectClip)
                    else:clip_region=clip_region.united(QRegion(local_rect))
                    sample_origin=target_global.topLeft()-source_global.topLeft()
                    if not source_global.intersects(target_global):
                        main_surface=getattr(self,'main_panel_backdrop',None)
                        if main_surface is not None:sample_origin=target_global.topLeft()-main_surface.mapToGlobal(QPoint(0,0))
                    requested=QRect(sample_origin,target_global.size())
                    source_rect=requested.intersected(QRect(0,0,source_image.width(),source_image.height()))
                    if not source_rect.isEmpty():
                        sample=self.appearance_blurred_crop(source_image,source_rect,blur)
                        destination=QRect(local_rect.topLeft()+(source_rect.topLeft()-requested.topLeft()),source_rect.size())
                        painter.drawPixmap(destination,sample)
                    painter.restore()
            else:
                # Main and Tree surfaces also need a usable backdrop when
                # their layout reserves space instead of placing the panel
                # directly over the grid. Sample the grid from its origin to
                # fill the surface, then blur that bounded image.
                source_rect=QRect(0,0,min(panel.width(),source_image.width()),min(panel.height(),source_image.height()))
                if not source_rect.isEmpty():
                    # A docked tree is taller than the grid below the main
                    # panel. Fill its entire backing image, rather than ending
                    # the sample abruptly at the viewport's shorter height.
                    if name=='tree' and panel.height()>source_image.height():
                        strip=source_image.copy(QRect(0,0,source_rect.width(),source_image.height()))
                        strip=strip.scaled(panel.width(),panel.height(),Qt.IgnoreAspectRatio,Qt.SmoothTransformation)
                        sample=self.appearance_blurred_crop(strip,strip.rect(),blur)
                        painter.drawPixmap(QPoint(0,0),sample)
                    else:
                        sample=self.appearance_blurred_crop(source_image,source_rect,blur)
                        painter.drawPixmap(source_rect.topLeft(),sample)
            painter.end()
            effect=backdrop.graphicsEffect()
            if effect is not None:backdrop.setGraphicsEffect(None)
            if backdrop is not panel:backdrop.setGeometry(QRect(backdrop.parentWidget().mapFromGlobal(global_rect.topLeft()),panel.size()))
            if name=='buttons':backdrop.setMask(clip_region)
            else:backdrop.clearMask()
            backdrop.setPixmap(image)
        self.refresh_right_control_backdrops(source_image,source_global)
        # One coalesced update after the displayed card frame, without a
        # trailing debounce or a GPU framebuffer readback.
        self._appearance_capture_ms=(time.perf_counter()-started)*1000
        self.appearance_capture_timer.setInterval(0)
    def sync_tree_appearance_geometry(self):
        if not hasattr(self,'tree_dock') or not hasattr(self,'tree_backdrop'):return
        rect=self.tree_dock.rect();self.tree_backdrop.setGeometry(rect);self.tree_backdrop.lower()
        # Docking changes native/child stacking; never let an appearance layer
        # obscure the title or the asynchronously populated tree.
        if hasattr(self,'file_tree'):self.file_tree.raise_()
        if hasattr(self,'tree_titlebar'):self.tree_titlebar.raise_()
        if hasattr(self,'tree_blur_backdrop'):self.tree_blur_backdrop.setGeometry(self.tree_backdrop.rect())
        if hasattr(self,'tree_gradient_overlay'):self.tree_gradient_overlay.setGeometry(self.tree_backdrop.rect())
        if hasattr(self,'appearance_capture_timer') and self.tree_dock.isVisible():self.appearance_capture_timer.start()
    def refresh_right_control_backdrops(self,source_image=None,source_global=None):
        if getattr(self,'_gpu_composition',None) is not None:
            self.appearance_capture_timer.start();return
        backdrops=getattr(self,'right_control_backdrops',None)
        if not backdrops or not hasattr(self,'grid'):return
        source=self.grid.viewport()
        if source_image is None:source_image=self.appearance_source_snapshot(source)
        if source_global is None:source_global=QRect(source.mapToGlobal(QPoint(0,0)),source.size())
        reveal=int(getattr(self,'appearance_buttons_transparency',0))>0
        blur=int(getattr(self,'appearance_buttons_blur',0))
        targets=list(backdrops.items())
        targets.extend(getattr(self,'window_control_backdrops',{}).items())
        for button,backdrop in targets:
            if not reveal or not button.isVisible():backdrop.hide();backdrop.setPixmap(QPixmap());continue
            rect=QRect(button.mapToGlobal(QPoint(0,0)),button.size());source_rect=QRect(rect.topLeft()-source_global.topLeft(),rect.size()).intersected(QRect(0,0,source_image.width(),source_image.height()))
            image=QPixmap(button.size());image.fill(Qt.transparent)
            if not source_rect.isEmpty():
                sample=self.appearance_blurred_crop(source_image,source_rect,blur);destination=QRect(source_rect.topLeft()-(rect.topLeft()-source_global.topLeft()),source_rect.size());painter=QPainter(image);painter.drawPixmap(destination,sample);painter.end()
            effect=backdrop.graphicsEffect()
            if effect is not None:backdrop.setGraphicsEffect(None)
            backdrop.setGeometry(button.geometry());backdrop.setMask(QRegion(backdrop.rect()));backdrop.setPixmap(image);backdrop.show();backdrop.raise_();button.raise_()
    @staticmethod
    def blur_appearance_sample(pixmap,radius):
        if radius<=0 or pixmap.isNull():return pixmap
        try:
            from PIL import Image,ImageFilter
            # A format without alpha guarantees opacity; no pixel scan is needed.
            original=pixmap.toImage();opaque=not original.hasAlphaChannel();mode='RGBX' if opaque else 'RGBA';fmt=QImage.Format_RGBX8888 if opaque else QImage.Format_RGBA8888
            image=original.convertToFormat(fmt);width=image.width();height=image.height();stride=image.bytesPerLine();pil=Image.frombuffer(mode,(width,height),image.constBits(),'raw',mode,stride,1)
            # Broad background blur contains little high-frequency detail.
            # Filter a smaller sample at the equivalent radius, then restore
            # its exact bounds. Text, icons and thumbnails are never reduced.
            factor=4 if radius>=24 else (2 if radius>=12 else 1)
            if factor>1:
                size=(max(1,math.ceil(width/factor)),max(1,math.ceil(height/factor)))
                pil=pil.resize(size,Image.Resampling.BILINEAR).filter(ImageFilter.GaussianBlur(float(radius)/factor)).resize((width,height),Image.Resampling.BILINEAR)
            else:pil=pil.filter(ImageFilter.GaussianBlur(float(radius)))
            # Convert directly into Qt-owned native storage while pixels stays alive.
            pixels=pil.tobytes();result=QImage(pixels,width,height,width*4,fmt).convertToFormat(QImage.Format_ARGB32_Premultiplied);return QPixmap.fromImage(result)
        except Exception:logger.debug('Could not blur a cropped appearance backdrop',exc_info=True);return pixmap
    def configure_appearance_settings(self):
        snapshot={key:getattr(self,key) for key in vars(self) if key.startswith('appearance_') and not key.startswith('appearance_capture') and not key.startswith('appearance_preview')};had_tree=hasattr(self,'tree_dock');main_was_visible=self.main_bar.isVisible();tree_was_visible=self.tree_dock.isVisible() if had_tree else False;right_controls_were_visible={widget:widget.isVisible() for widget in self.right_control_widgets()}
        self._appearance_preview_active=True;self.main_hide_timer.stop();self.tree_hide_timer.stop();self.favorite_overlay_timer.stop();self.set_main_panel_visible(True)
        if not had_tree:self.build_file_tree()
        if self.auto_hide_tree:self.show_tree_overlay()
        else:self.tree_dock.show()
        self.tree_dock.raise_();self.refresh_file_tree();self.show_right_controls()
        dlg=QDialog(self);dlg.setWindowTitle('Appearance settings');self.prepare_settings_dialog(dlg,'appearance',(760,660),(700,600));layout=QVBoxLayout(dlg);pages=QTabWidget();layout.addWidget(pages);base_palette=['', '#d64b4b','#e87935','#e7b83f','#a9c83b','#55ad62','#32a88d','#36b9bb','#3d8ec9','#526bd0','#7755c8','#a14ec1','#cf559d','#df788f','#8f6e55','#b6a489','#87909c','#e4e7ec','#555b67','#4c91a6'];palette=base_palette+list(self.appearance_custom_colors);controls_by_surface={};swatch_mosaics=[]
        def slider_row(parent,label,value,low,high,suffix='',trailing=None):
            row=QHBoxLayout();row.setSpacing(9);caption=QLabel(label);caption.setMinimumWidth(145);row.addWidget(caption);slider=AppearanceSlider(Qt.Horizontal);slider.setRange(low,high);slider.setSingleStep(1);slider.setPageStep(5);slider.setValue(max(low,min(high,int(value))));slider.setToolTip(f'{label}: scroll for steps of 5');row.addWidget(slider,1);value_label=QLabel(f'{slider.value()}{suffix}');value_label.setMinimumWidth(55);value_label.setAlignment(Qt.AlignRight|Qt.AlignVCenter);row.addWidget(value_label);slider.valueChanged.connect(lambda number,target=value_label,suffix=suffix:target.setText(f'{number}{suffix}'))
            if trailing is not None:trailing.setFixedSize(30,26);trailing.setToolTip('Add the current hue color to the palette');row.addWidget(trailing)
            parent.addLayout(row);return slider
        def recolor_swatches(swatch_buttons,color):
            for button in swatch_buttons:
                fill=button.property('swatch');border='2px solid #f3d35b' if fill==(color or '#303236') else '1px solid #60646b';button.setStyleSheet(f'QPushButton{{background:{fill};border:{border};}}')
        def remove_hue_color(color):
            color=QColor(color).name().lower()
            if color not in self.appearance_custom_colors:return
            palette[:]=[entry for entry in palette if entry!=color]
            self.appearance_custom_colors=[entry for entry in self.appearance_custom_colors if entry!=color]
            for name,controls in controls_by_surface.items():
                if controls.get('color')==color:controls['color']='';recolor_swatches(next((buttons for _,buttons,chooser in swatch_mosaics if chooser==controls.get('_choose')),[]),'')
                if controls.get('accent_color')==color:controls['accent_color']='';recolor_swatches(next((buttons for _,buttons,chooser in swatch_mosaics if chooser==controls.get('_accent_choose')),[]),'')
                if controls.get('background_color')==color:
                    controls['background_color']=controls['background_default'];default_background=QColor(controls['background_default']);controls['background_hue'].setValue(max(0,default_background.hue()));controls['background_brightness'].setValue(100);recolor_swatches(next((buttons for _,buttons,chooser in swatch_mosaics if chooser==controls.get('_background_choose')),[]),controls['background_color'])
            if bottom_color.get('value')==color:bottom_color['value']=''
            for swatches,buttons,choose_color in swatch_mosaics:
                for button in list(buttons):
                    if button.property('swatch')==color:
                        swatches.removeWidget(button);button.deleteLater();buttons.remove(button)
                for index,button in enumerate(buttons):swatches.addWidget(button,index//10,index%10)
            for name,controls in controls_by_surface.items():
                if controls.get('color')=='':
                    for _,buttons,chooser in swatch_mosaics:
                        if chooser==controls.get('_choose'):recolor_swatches(buttons,'')
                if controls.get('accent_color')=='':
                    for _,buttons,chooser in swatch_mosaics:
                        if chooser==controls.get('_accent_choose'):recolor_swatches(buttons,'')
                if controls.get('_background_choose'):
                    for _,buttons,chooser in swatch_mosaics:
                        if chooser==controls.get('_background_choose'):recolor_swatches(buttons,controls['background_color'])
            if bottom_color.get('value')=='':recolor_swatches(bottom_buttons,'')
            update_preview()
        def add_hue_color(color):
            color=QColor(color).name().lower()
            if color in palette or len(palette)>=120:return
            palette.append(color);self.appearance_custom_colors=palette[len(base_palette):]
            for swatches,buttons,choose_color in swatch_mosaics:
                index=len(buttons);button=AppearancePaletteButton('');button.setFixedSize(34,30);button.setProperty('swatch',color);button.setToolTip(color);button.clicked.connect(lambda checked=False,c=color,fn=choose_color:fn(c));button.doubleClicked.connect(lambda c=color:remove_hue_color(c));buttons.append(button);swatches.addWidget(button,index//10,index%10)
        def hue_color(color,hue):
            chosen=QColor(color or '#d0a32d');chosen.setHsv(int(hue)%360,max(160,chosen.saturation()),chosen.value());return chosen.name()
        def update_preview():
            for name,controls in controls_by_surface.items():
                mapping={'transparency':'transparency','blur':'blur','gradient_area':'gradient_area','gradient_transparency':'gradient_transparency','gradient_brightness':'gradient_brightness','position_x':'gradient_distance','position_y':'gradient_position_y','rotation':'gradient_rotation','hue':'gradient_hue'}
                for control,field in mapping.items():
                    if control in controls:setattr(self,f'appearance_{name}_{field}',controls[control].value())
                setattr(self,f'appearance_{name}_color',controls['color'])
                setattr(self,f'appearance_{name}_accent_color',controls['accent_color'])
                setattr(self,f'appearance_{name}_accent_hue',controls['accent_hue'].value())
                setattr(self,f'appearance_{name}_accent_brightness',controls['accent_brightness'].value())
                setattr(self,f'appearance_{name}_background_color',controls['background_color'])
                setattr(self,f'appearance_{name}_background_hue',controls['background_hue'].value())
                setattr(self,f'appearance_{name}_background_brightness',controls['background_brightness'].value())
            self.appearance_bottom_gradient_color=bottom_color['value'];self.appearance_bottom_gradient_enabled=bool(bottom_color['value']);self.appearance_bottom_gradient_area=bottom_controls['area'].value();self.appearance_bottom_gradient_distance=bottom_controls['position_x'].value();self.appearance_bottom_gradient_position_y=bottom_controls['position_y'].value();self.appearance_bottom_gradient_transparency=bottom_controls['transparency'].value();self.appearance_bottom_gradient_brightness=bottom_controls['brightness'].value();self.appearance_bottom_gradient_rotation=bottom_controls['rotation'].value();self.appearance_bottom_gradient_hue=bottom_controls['hue'].value();self.appearance_font_color=font_color.currentData()
            if pages.tabText(pages.currentIndex())=='Context menus':self.refresh_context_menu_appearance()
            else:self.appearance_preview_timer.start()
        for name,title in (('main','Main panel'),('buttons','Buttons'),('tabs','Tabs'),('tree','File Tree'),('menus','Context menus')):
            page_content=QWidget();form=QVBoxLayout(page_content);form.setSpacing(11);background_default={'main':'#202124','buttons':'#303236','tabs':'#2b2d31','tree':'#15171c','menus':'#25272b'}[name];controls={'color':getattr(self,f'appearance_{name}_color'),'accent_color':getattr(self,f'appearance_{name}_accent_color'),'background_color':getattr(self,f'appearance_{name}_background_color',background_default),'background_default':background_default};fields=(('transparency','Background transparency',0,100,'%'),('blur','Background blur',0,100,' px'),('gradient_area','Gradient area / fade range',1,500,'%'),('gradient_transparency','Gradient transparency',0,100,'%'),('position_x','Position X',-100,200,'%'),('position_y','Position Y',-100,200,'%'),('rotation','Gradient rotation',0,359,'°'),('hue','Gradient hue',0,360,'°'),('gradient_brightness','Gradient brightness (0 = black)',0,200,'%'))
            gradient_hue_add_button=None
            for field,label,low,high,suffix in fields:
                if field=='gradient_area':low=0
                if field in ('position_x','position_y'):low,high=-100,200
                attr={'position_x':'gradient_distance','position_y':'gradient_position_y','rotation':'gradient_rotation','hue':'gradient_hue'}.get(field,field);add_button=QPushButton('+') if field=='hue' else None
                if field=='hue':gradient_hue_add_button=add_button
                slider=slider_row(form,label,getattr(self,f'appearance_{name}_{attr}'),low,high,suffix,add_button);controls[field]=slider
            form.addWidget(QLabel('Gradient color'));swatches=QGridLayout();swatches.setSpacing(7);form.addLayout(swatches);swatch_buttons=[]
            for index,color in enumerate(palette):
                button=AppearancePaletteButton('×' if not color else '');button.setFixedSize(34,30);button.setProperty('swatch',color or '#303236');button.setToolTip('Clear color' if not color else color);swatch_buttons.append(button);swatches.addWidget(button,index//10,index%10)
            def choose(color,swatch_buttons=swatch_buttons,controls=controls):
                controls['color']=color
                chosen=QColor(color or '#d0a32d');hue=max(0,chosen.hue());controls['hue'].blockSignals(True);controls['hue'].setValue(hue);controls['hue'].blockSignals(False)
                recolor_swatches(swatch_buttons,color);update_preview()
            def hue_changed(value,controls=controls,swatch_buttons=swatch_buttons):
                base=QColor(controls['color'] or '#d0a32d');sat=max(160,base.saturation());base.setHsv(value%360,sat,base.value());controls['color']=base.name();recolor_swatches(swatch_buttons,controls['color']);update_preview()
            gradient_hue_add_button.clicked.connect(lambda checked=False,controls=controls:add_hue_color(hue_color(controls['color'],controls['hue'].value())))
            for index,color in enumerate(palette):swatch_buttons[index].clicked.connect(lambda checked=False,c=color,fn=choose:fn(c))
            recolor_swatches(swatch_buttons,controls['color']);controls['hue'].valueChanged.connect(hue_changed);controls_by_surface[name]=controls
            controls['_choose']=choose;swatch_mosaics.append((swatches,swatch_buttons,choose))
            for color,button in zip(palette,swatch_buttons):
                if color:button.doubleClicked.connect(lambda c=color:remove_hue_color(c))
            # The background palette follows the gradient palette. Its hue
            # and brightness controls are independent from gradient color.
            bg=QColor(controls['background_color']);bg_hue=bg.hue() if bg.hue()>=0 else 0
            bg_add_button=QPushButton('+')
            controls['background_hue']=slider_row(form,'Background hue',getattr(self,f'appearance_{name}_background_hue',bg_hue),0,360,'°',bg_add_button)
            controls['background_brightness']=slider_row(form,'Background brightness',getattr(self,f'appearance_{name}_background_brightness',100),0,200,'%')
            form.addWidget(QLabel('Background color'));background_swatches=QGridLayout();background_swatches.setSpacing(7);form.addLayout(background_swatches);background_buttons=[]
            for index,color in enumerate(palette):
                button=AppearancePaletteButton('×' if not color else '');button.setFixedSize(34,30);button.setProperty('swatch',color or background_default);button.setToolTip('Reset background color' if not color else color);background_buttons.append(button);background_swatches.addWidget(button,index//10,index%10)
            def choose_background(color,buttons=background_buttons,controls=controls):
                selected=color or controls['background_default'];controls['background_color']=selected;chosen=QColor(selected);controls['background_hue'].blockSignals(True);controls['background_hue'].setValue(max(0,chosen.hue()));controls['background_hue'].blockSignals(False);controls['background_brightness'].setValue(100);recolor_swatches(buttons,selected);update_preview()
            def background_hue_changed(value,buttons=background_buttons,controls=controls):
                chosen=QColor(controls['background_color']);chosen.setHsv(value%360,max(150,chosen.saturation()),chosen.value());controls['background_color']=chosen.name();recolor_swatches(buttons,controls['background_color']);update_preview()
            for index,color in enumerate(palette):background_buttons[index].clicked.connect(lambda checked=False,c=color,fn=choose_background:fn(c))
            bg_add_button.clicked.connect(lambda checked=False,controls=controls:add_hue_color(hue_color(controls['background_color'],controls['background_hue'].value())))
            controls['background_hue'].valueChanged.connect(background_hue_changed)
            recolor_swatches(background_buttons,controls['background_color']);controls['_background_choose']=choose_background;swatch_mosaics.append((background_swatches,background_buttons,choose_background))
            for field,slider in controls.items():
                if field not in ('color','hue','_choose','accent_color','accent_hue','accent_brightness','_accent_choose','background_color','background_default','background_hue','background_brightness','_background_choose'):slider.valueChanged.connect(update_preview)
            controls['background_brightness'].valueChanged.connect(update_preview)
            # Accent color has its own selection state and palette. Its hue
            # and brightness controls never alter the gradient settings.
            accent_add_button=QPushButton('+')
            controls['accent_hue']=slider_row(form,'Accent hue',getattr(self,f'appearance_{name}_accent_hue',0),0,360,'°',accent_add_button)
            controls['accent_brightness']=slider_row(form,'Accent brightness',getattr(self,f'appearance_{name}_accent_brightness',100),0,200,'%')
            form.addWidget(QLabel('Accent color'));accent_swatches=QGridLayout();accent_swatches.setSpacing(7);form.addLayout(accent_swatches);accent_buttons=[]
            for index,color in enumerate(palette):
                button=AppearancePaletteButton('×' if not color else '');button.setFixedSize(34,30);button.setProperty('swatch',color or '#303236');button.setToolTip('Clear accent color' if not color else color);accent_buttons.append(button);accent_swatches.addWidget(button,index//10,index%10)
            def choose_accent(color,buttons=accent_buttons,controls=controls):
                controls['accent_color']=color;chosen=QColor(color or '#d0a32d');controls['accent_hue'].blockSignals(True);controls['accent_hue'].setValue(max(0,chosen.hue()));controls['accent_hue'].blockSignals(False);recolor_swatches(buttons,color);update_preview()
            def accent_hue_changed(value,buttons=accent_buttons,controls=controls):
                chosen=QColor(controls['accent_color'] or '#d0a32d');chosen.setHsv(value%360,max(160,chosen.saturation()),chosen.value());controls['accent_color']=chosen.name();recolor_swatches(buttons,controls['accent_color']);update_preview()
            for color,button in zip(palette,accent_buttons):
                button.clicked.connect(lambda checked=False,c=color,fn=choose_accent:fn(c))
                if color:button.doubleClicked.connect(lambda c=color:remove_hue_color(c))
            recolor_swatches(accent_buttons,controls['accent_color'])
            controls['accent_hue'].valueChanged.connect(accent_hue_changed);controls['accent_brightness'].valueChanged.connect(update_preview)
            accent_add_button.clicked.connect(lambda checked=False,controls=controls:add_hue_color(hue_color(controls['accent_color'],controls['accent_hue'].value())))
            # Keep each accent mosaic aligned with the shared user palette.
            controls['_accent_choose']=choose_accent;swatch_mosaics.append((accent_swatches,accent_buttons,choose_accent))
            form.addStretch();page=QScrollArea();page.setWidgetResizable(True);page.setFrameShape(QFrame.NoFrame);page.setWidget(page_content);pages.addTab(page,title)
        contrast_page=QWidget();contrast_layout=QVBoxLayout(contrast_page);contrast_layout.setSpacing(11);font_row=QHBoxLayout();font_row.addWidget(QLabel('Interface font color'));font_color=QComboBox()
        for label,color in (('White','#ffffff'),('Light grey','#d3d3d3'),('Grey','#808080'),('Black','#000000')):font_color.addItem(label,color)
        font_index=font_color.findData(self.appearance_font_color);font_color.setCurrentIndex(max(0,font_index));font_color.currentIndexChanged.connect(update_preview);font_row.addWidget(font_color);font_row.addStretch();contrast_layout.addLayout(font_row)
        bottom_color={'value':self.appearance_bottom_gradient_color}
        bottom_add_button=QPushButton('+');bottom_controls={'area':slider_row(contrast_layout,'Gradient area / fade range',self.appearance_bottom_gradient_area,0,500,'%'),'transparency':slider_row(contrast_layout,'Gradient transparency',self.appearance_bottom_gradient_transparency,0,100,'%'),'position_x':slider_row(contrast_layout,'Position X',self.appearance_bottom_gradient_distance,-100,200,'%'),'position_y':slider_row(contrast_layout,'Position Y',self.appearance_bottom_gradient_position_y,-100,200,'%'),'rotation':slider_row(contrast_layout,'Gradient rotation',self.appearance_bottom_gradient_rotation,0,359,'°'),'hue':slider_row(contrast_layout,'Gradient hue',self.appearance_bottom_gradient_hue,0,360,'°',bottom_add_button),'brightness':slider_row(contrast_layout,'Gradient brightness (0 = black)',self.appearance_bottom_gradient_brightness,0,200,'%')}
        def choose_bottom(color):
            bottom_color['value']=color;chosen=QColor(color or '#d0a32d');bottom_controls['hue'].blockSignals(True);bottom_controls['hue'].setValue(max(0,chosen.hue()));bottom_controls['hue'].blockSignals(False);recolor_swatches(bottom_buttons,color);update_preview()
        def bottom_hue_changed(value):
            base=QColor(bottom_color['value'] or '#d0a32d');base.setHsv(value%360,max(160,base.saturation()),base.value());bottom_color['value']=base.name();recolor_swatches(bottom_buttons,bottom_color['value']);update_preview()
        bottom_add_button.clicked.connect(lambda checked=False:add_hue_color(hue_color(bottom_color['value'],bottom_controls['hue'].value())))
        bottom_controls['hue'].valueChanged.connect(bottom_hue_changed)
        for field,slider in bottom_controls.items():
            if field!='hue':slider.valueChanged.connect(update_preview)
        contrast_layout.addWidget(QLabel('Main Panel gradient color'));bottom_swatches=QGridLayout();bottom_swatches.setSpacing(7);contrast_layout.addLayout(bottom_swatches);bottom_buttons=[]
        for index,color in enumerate(palette):
            button=AppearancePaletteButton('×' if not color else '');button.setFixedSize(34,30);button.setProperty('swatch',color or '#303236');button.setToolTip('Clear color' if not color else color);bottom_buttons.append(button);bottom_swatches.addWidget(button,index//10,index%10);button.clicked.connect(lambda checked=False,c=color:choose_bottom(c))
            if color:button.doubleClicked.connect(lambda c=color:remove_hue_color(c))
        swatch_mosaics.append((bottom_swatches,bottom_buttons,choose_bottom))
        recolor_swatches(bottom_buttons,bottom_color['value']);contrast_layout.addStretch();contrast_scroll=QScrollArea();contrast_scroll.setWidgetResizable(True);contrast_scroll.setFrameShape(QFrame.NoFrame);contrast_scroll.setWidget(contrast_page);pages.addTab(contrast_scroll,'Extra gradient')
        note=QLabel('Gradients fade symmetrically around the selected position. Set area to 0% or clear a color to turn a gradient off. Changes preview as you adjust sliders; mouse-wheel steps are 5. Save keeps them; Cancel restores the previous settings.');note.setWordWrap(True);layout.addWidget(note);box=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel);box.accepted.connect(dlg.accept);box.rejected.connect(dlg.reject);layout.addWidget(box)
        preview_menu=AppearanceMenu(self);preview_menu.configure(self);preview_menu.setWindowFlags(Qt.Tool|Qt.FramelessWindowHint|Qt.WindowDoesNotAcceptFocus);preview_menu.setAttribute(Qt.WA_ShowWithoutActivating,True);preview_menu.addAction('Undo');preview_menu.addSeparator();preview_menu.addAction('Cut');preview_menu.addAction('Copy');preview_menu.addAction('Paste');preview_menu.addSeparator();preview_menu.addAction('Rename');preview_menu.hide()
        def preview_current_surface():
            title=pages.tabText(pages.currentIndex());show_main=title in ('Main panel','Buttons','Tabs','Extra gradient');show_tree=title=='File Tree';show_buttons=title=='Buttons'
            self._appearance_preview_active=False
            try:
                if self.auto_hide_main:self.set_main_panel_visible(show_main)
                if self.auto_hide_tree:self.tree_dock.setVisible(show_tree)
                for widget in self.right_control_widgets():widget.setVisible(show_buttons and widget is not getattr(self,'favorite_group_overlay',None))
            finally:self._appearance_preview_active=True
            if title=='Context menus':
                preview_menu.configure(self);preview_menu.adjustSize();preview_menu.move(dlg.frameGeometry().right()+8,dlg.frameGeometry().top()+45);preview_menu.show();preview_menu.setActiveAction(preview_menu.actions()[2])
            else:preview_menu.hide()
        pages.currentChanged.connect(lambda *_:preview_current_surface());QTimer.singleShot(0,preview_current_surface)
        accepted=dlg.exec()==QDialog.Accepted;preview_menu.close();preview_menu.deleteLater();self._appearance_preview_active=False;self.clear_appearance_preview_cache();self.appearance_preview_timer.stop()
        if not accepted:
            for key,value in snapshot.items():setattr(self,key,value)
        if accepted:self.save_settings()
        if not main_was_visible and self.auto_hide_main:self.set_main_panel_visible(False)
        if not tree_was_visible:self.tree_dock.hide()
        elif self.auto_hide_tree:self.show_tree_overlay()
        if hasattr(self,'tree_backdrop'):self.tree_backdrop.setGeometry(self.tree_dock.rect())
        self.apply_appearance_settings(persist=False)
        for widget,was_visible in right_controls_were_visible.items():
            if was_visible:widget.show()
            else:widget.hide()
    def configure_scroll_test(self):
        dlg=QDialog(self);dlg.setWindowTitle('Scroll test');self.prepare_settings_dialog(dlg,'scroll_test',(520,280));layout=QVBoxLayout(dlg)
        renderer='GPU' if self.grid.viewport().property('magellan_gpu_viewport') else 'Standard'
        layout.addWidget(QLabel('Current thumbnail renderer: '+renderer))
        note=QLabel('Smooth scrolling is intended for use with GPU thumbnail rendering enabled. Enable it in PERFORMANCE and restart Magellan before testing. Standard rendering remains supported, but smooth scrolling may show tearing.');note.setWordWrap(True);layout.addWidget(note)
        logging=QCheckBox('Write scroll timing diagnostics to the application log');logging.setChecked(self.scroll_test_logging_enabled);layout.addWidget(logging)
        behavior=QPushButton('Scroll behavior settings…');behavior.clicked.connect(self.configure_scroll_behavior);layout.addWidget(behavior)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel);buttons.accepted.connect(dlg.accept);buttons.rejected.connect(dlg.reject);layout.addWidget(buttons)
        if dlg.exec()==QDialog.Accepted:
            action=self.scroll_test_logging_action;blocked=action.blockSignals(True)
            try:action.setChecked(logging.isChecked())
            finally:action.blockSignals(blocked)
            self.set_scroll_test_logging_enabled(logging.isChecked())
    def configure_scroll_behavior(self):
        dlg=QDialog(self);dlg.setWindowTitle('Scroll behavior');self.prepare_settings_dialog(dlg,'scroll_behavior',(540,440));layout=QVBoxLayout(dlg)
        row=QHBoxLayout();row.addWidget(QLabel('Scroll distance per wheel step'));rows=QDoubleSpinBox();rows.setRange(.1,12);rows.setSingleStep(.1);rows.setDecimals(1);rows.setSuffix(' rows');rows.setValue(self.wheel_scroll_rows);row.addWidget(rows);layout.addLayout(row)
        reader_row=QHBoxLayout();reader_row.addWidget(QLabel('Reader pages per wheel movement'));reader_speed=QDoubleSpinBox();reader_speed.setRange(.1,10);reader_speed.setSingleStep(.1);reader_speed.setDecimals(1);reader_speed.setSuffix('×');reader_speed.setValue(self.reader_scroll_speed);reader_row.addWidget(reader_speed);layout.addLayout(reader_row)
        viewer_row=QHBoxLayout();viewer_row.addWidget(QLabel('Image viewer wheel default'));viewer_wheel=QComboBox();viewer_wheel.addItem('Swipe to previous / next image','swipe');viewer_wheel.addItem('Zoom in / out','zoom');viewer_wheel.setCurrentIndex(max(0,viewer_wheel.findData(self.viewer_wheel_behavior)));viewer_row.addWidget(viewer_wheel,1);layout.addLayout(viewer_row)
        smooth=QCheckBox('Smooth scrolling with gentle momentum');smooth.setChecked(self.smooth_wheel_scroll);layout.addWidget(smooth)
        automatic=QCheckBox('Automatic glide duration (current behavior)');automatic.setChecked(self.scroll_glide_duration_ms==0);layout.addWidget(automatic)
        duration_row=QHBoxLayout();duration_row.addWidget(QLabel('Glide duration'));duration=QSpinBox();duration.setRange(100,3000);duration.setSingleStep(50);duration.setSuffix(' ms');duration.setValue(self.scroll_glide_duration_ms or 600);duration.setEnabled(not automatic.isChecked());automatic.toggled.connect(lambda checked:duration.setEnabled(not checked));duration_row.addWidget(duration);layout.addLayout(duration_row)
        note=QLabel('Smooth scrolling is intended for use with GPU thumbnail rendering enabled (PERFORMANCE menu; restart required). Standard rendering remains available, but smooth scrolling may show tearing. Distance controls travel per wheel step; glide duration controls how gradually scrolling settles.');note.setWordWrap(True);layout.addWidget(note)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel);buttons.accepted.connect(dlg.accept);buttons.rejected.connect(dlg.reject);layout.addWidget(buttons)
        if dlg.exec()==QDialog.Accepted:
            self.wheel_scroll_rows=rows.value();self.reader_scroll_speed=reader_speed.value();self.viewer_wheel_behavior=viewer_wheel.currentData() or 'swipe';self.smooth_wheel_scroll=smooth.isChecked();self.scroll_glide_duration_ms=0 if automatic.isChecked() else duration.value()
            self.grid.stop_smooth_scroll()
            self.save_settings()
    def configure_folder_auto_refresh(self):
        dlg=QDialog(self);dlg.setWindowTitle('Automatic folder refresh');self.prepare_settings_dialog(dlg,'automatic_folder_refresh',(520,320));layout=QVBoxLayout(dlg)
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
        if self._closing or not self.auto_refresh_folders or self.in_favorites_view or getattr(self,'in_archive_view',False) or self.viewer is not None:return
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
        # Selection is intentionally global across folder navigation. A
        # background refresh must not drop items just because they belong to
        # another folder than the page currently being displayed.
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
                self.tree_dock.setFloating(True);self.tree_dock.setWindowFlags(Qt.Tool|Qt.FramelessWindowHint);self.tree_dock.setFixedSize(300,max(200,self.height()-100));self.tree_dock.hide()
            else:
                self.tree_dock.setMinimumHeight(0);self.tree_dock.setMaximumHeight(16777215);self.tree_dock.setFloating(False);self.tree_dock.show()
        if hasattr(self,'selection_clear_overlay'):
            if self.right_controls_auto_hide:self.hide_favorite_controls()
            else:self.show_right_controls()
        if hasattr(self,'titlebar_hover_timer'):
            if self.auto_hide_main or self.auto_hide_tree:self.titlebar_hover_timer.start()
            else:self.titlebar_hover_timer.stop()
        self.sync_detached_tabs_spacing()
        self.apply_appearance_settings(persist=False)
        QTimer.singleShot(0,self.finish_panel_layout_transition)
        self.root_widget.update()
    def finish_panel_layout_transition(self):
        if self._closing:return
        layout=self.root_widget.layout()
        if layout is not None:layout.activate()
        self._appearance_source_cache=None
        self.apply_appearance_settings(False)
        self.refresh_appearance_backdrop()
        self.root_widget.update()
        self.grid.viewport().update()

    def panel_edge_hover(self):
        cursor=QCursor.pos();pos=self.root_widget.mapFromGlobal(cursor)
        bounds=self.frameGeometry().united(QRect(self.root_widget.mapToGlobal(QPoint(0,0)),self.root_widget.size()))
        inside=bounds.adjusted(-1,-1,1,1).contains(cursor)
        return inside and pos.y()<=2,inside and pos.x()<=10
    def check_titlebar_hover(self):
        # Mouse-move events can be coalesced, and native fullscreen borders do
        # not deliver client mouse events. Poll both reveal edges as a fallback.
        if self._closing or getattr(self,'_appearance_preview_active',False):return
        if not (self.auto_hide_main or self.auto_hide_tree) or not hasattr(self,'main_bar') or not self.isVisible() or self.isMinimized():return
        active=QApplication.activeWindow()
        if isinstance(active,MediaViewer):return
        tree=getattr(self,'tree_dock',None)
        if active is None or (active is not self and active is not tree):return
        shift=bool(QApplication.keyboardModifiers()&Qt.ShiftModifier)
        if getattr(self,'shift_selection_held',False) and not shift:self.shift_selection_held=False
        selecting=getattr(self,'shift_selection_held',False) or getattr(self.grid,'selection_dragging',False)
        if selecting:
            if self.auto_hide_main and not self.search_has_focus():self.main_hide_timer.stop();self.set_main_panel_visible(False)
            if self.auto_hide_tree:
                self.tree_hide_timer.stop()
                if tree is not None and tree.isVisible():self.save_tree_navigation_state();tree.hide()
            return
        top,left=self.panel_edge_hover();cursor=QCursor.pos()
        if self.auto_hide_main:
            if self.search_has_focus() or top:
                self.main_hide_timer.stop()
                if not self.main_bar.isVisible():self.set_main_panel_visible(True)
            elif self.main_bar.isVisible():
                if getattr(self,'tab_context_menu_open',False) or getattr(self,'_tab_drag_active',False):self.main_hide_timer.stop()
                else:
                    pos=self.root_widget.mapFromGlobal(cursor)
                    over_panel=self.main_bar.geometry().contains(pos) or (self.nav_bar.isVisible() and self.nav_bar.geometry().contains(pos))
                    over_menu=any(action.menu() and action.menu().isVisible() and action.menu().geometry().contains(cursor) for action in self.menu_bar.actions())
                    if over_panel or over_menu:self.main_hide_timer.stop()
                    elif not self.main_hide_timer.isActive():self.main_hide_timer.start(self.main_hide_delay)
        if self.auto_hide_tree and tree is not None:
            if left:
                self.tree_hide_timer.stop()
                if not tree.isVisible():self.show_tree_overlay()
            elif self.tree_panel_contains_global(cursor) or getattr(self,'tree_scrollbar_interacting',False):self.tree_hide_timer.stop()
            elif tree.isVisible() and not self.tree_hide_timer.isActive():self.tree_hide_timer.start(self.tree_hide_delay)
    def hide_tree_overlay(self):
        if getattr(self,'_appearance_preview_active',False):return
        if self.auto_hide_tree and hasattr(self,'tree_dock'):
            if getattr(self,'tree_scrollbar_interacting',False) or self.tree_panel_contains_global(QCursor.pos()) or self.panel_edge_hover()[1]:
                self.tree_hide_timer.stop();return
            self.save_tree_navigation_state()
            self.tree_dock.hide()
    def hide_main_panel(self):
        if getattr(self,'_appearance_preview_active',False):return
        if getattr(self,'tab_context_menu_open',False) or getattr(self,'_tab_drag_active',False):return
        if getattr(self,'tab_reveal_timer',None) and self.tab_reveal_timer.isActive():return
        if self.search_has_focus():return
        if hasattr(self,'root_widget') and self.panel_edge_hover()[0]:return
        if hasattr(self,'menu_bar') and any(action.menu() and action.menu().isVisible() and action.menu().geometry().contains(QCursor.pos()) for action in self.menu_bar.actions()):return
        if hasattr(self,'root_widget'):
            pos=self.root_widget.mapFromGlobal(QCursor.pos())
            if self.main_bar.isVisible() and self.main_bar.geometry().contains(pos):return
            if self.auto_hide_tabs and self.nav_bar.isVisible() and self.nav_bar.geometry().contains(pos):return
        if hasattr(self,'menu_bar'):
            for action in self.menu_bar.actions():
                menu=action.menu()
                if menu and menu.isVisible():menu.close()
        if self.auto_hide_main and hasattr(self,'main_bar'):self.set_main_panel_visible(False)
    def set_main_panel_visible(self,visible):
        if getattr(self,'_appearance_preview_active',False) and not visible:return
        if not hasattr(self,'main_bar'):return
        if visible and hasattr(self,'main_hide_timer'):self.main_hide_timer.stop()
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
        self.sync_detached_tabs_spacing()
        # The shared surface follows the visible overlay geometry. When only
        # tabs remain visible, keep a backing strip behind those tabs and let
        # the thumbnails show through the unused main-panel area.
        if hasattr(self,'main_panel_backdrop') and hasattr(self,'root_widget'):
            if self.auto_hide_main and not visible:
                if self.auto_hide_tabs or not self.nav_bar.isVisible():self.main_panel_backdrop.hide()
                else:
                    nav_pos=self.nav_bar.mapTo(self.root_widget,QPoint(0,0));self.main_panel_backdrop.setGeometry(QRect(0,nav_pos.y(),self.root_widget.width(),self.nav_bar.height()));self.main_panel_backdrop.show();self.main_panel_backdrop.raise_();self.nav_bar.raise_()
            else:
                top_pos=self.main_bar.mapTo(self.root_widget,QPoint(0,0));nav_pos=self.nav_bar.mapTo(self.root_widget,QPoint(0,0));top=top_pos.y()+self.main_bar.height();bottom=nav_pos.y()+self.nav_bar.height();y=min(top_pos.y(),nav_pos.y());self.main_panel_backdrop.setGeometry(QRect(0,y,self.root_widget.width(),max(top,bottom)-y));self.main_panel_backdrop.show();self.main_panel_backdrop.raise_()
                if hasattr(self,'main_blur_backdrop'):self.main_blur_backdrop.setGeometry(self.main_panel_backdrop.rect());self.main_blur_backdrop.lower()
                if hasattr(self,'main_gradient_overlay'):self.main_gradient_overlay.setGeometry(self.main_panel_backdrop.rect());self.main_gradient_overlay.raise_()
                if hasattr(self,'main_path_gradient_overlay'):self.main_path_gradient_overlay.setGeometry(self.main_panel_backdrop.rect());self.main_path_gradient_overlay.raise_()
                self.raise_main_panel_contents()
        self.update_fullscreen_close_button()
        if visible and hasattr(self,'appearance_capture_timer'):
            if any(getattr(self,f'appearance_{surface}_transparency',0)>0 or getattr(self,f'appearance_{surface}_blur',0)>0 for surface in ('main','buttons','tabs','tree')) or any(getattr(self,f'appearance_{surface}_color','') and getattr(self,f'appearance_{surface}_gradient_transparency',100)>0 for surface in ('buttons','tabs')):
                self.refresh_appearance_backdrop()
    def update_fullscreen_close_button(self):
        controls=(getattr(self,'minimize_button',None),getattr(self,'fullscreen_button',None),getattr(self,'fullscreen_close_button',None))
        if not all(controls) or not hasattr(self,'root_widget'):return
        visible=(self.window_mode=='fullscreen' or self.isFullScreen()) and (not self.auto_hide_main or self.main_bar.isVisible())
        right=self.root_widget.width()-4;y=3
        for button in reversed(controls):
            button.setFixedSize(22,19);width,height=22,19;right-=width;button.setGeometry(max(0,right),y,width,height);button.setVisible(visible)
            backdrop=getattr(self,'window_control_backdrops',{}).get(button)
            if backdrop is not None:
                backdrop.setGeometry(button.geometry())
                if visible and int(getattr(self,'appearance_buttons_transparency',0))>0 and getattr(self,'_gpu_composition',None) is None:backdrop.show();backdrop.raise_()
                else:backdrop.hide()
            if visible:button.raise_()
            right-=2
        if visible and hasattr(self,'appearance_capture_timer') and int(getattr(self,'appearance_buttons_transparency',0))>0:self.appearance_capture_timer.start()
    def open_menu_on_hover(self,action):
        menu=action.menu() if action is not None else None
        if menu is None:return
        for other in self.menu_bar.actions():
            other_menu=other.menu()
            if other_menu is not None and other_menu is not menu and other_menu.isVisible():other_menu.close()
        if not menu.isVisible():menu.popup(self.menu_bar.mapToGlobal(self.menu_bar.actionGeometry(action).bottomLeft()))
    def set_scrollbar_visibility(self,bar,visible):
        if not isinstance(bar,QScrollBar):return
        available=bar.maximum()>bar.minimum();visible=bool(visible and available)
        if bar.property('magellan_search_keep_visible') and available:visible=True
        # Scrollbars share the application's normal styling. Auto-hide for
        # native scroll areas must be controlled through the parent policy;
        # explicitly hiding their child scrollbar can leave it hidden after
        # the range appears (notably for the File Tree on startup).
        bar.setStyleSheet('')
        parent=bar.parentWidget()
        if isinstance(parent,QAbstractScrollArea):
            vertical=bar.orientation()==Qt.Vertical;original_key='magellan_original_vertical_policy' if vertical else 'magellan_original_horizontal_policy'
            original=bar.property(original_key)
            if original is None:
                original=int(parent.verticalScrollBarPolicy() if vertical else parent.horizontalScrollBarPolicy());bar.setProperty(original_key,original)
            if self.scrollbars_auto_hide:policy=Qt.ScrollBarAsNeeded if visible else Qt.ScrollBarAlwaysOff
            else:policy=Qt.ScrollBarPolicy(int(original))
            if vertical:parent.setVerticalScrollBarPolicy(policy)
            else:parent.setHorizontalScrollBarPolicy(policy)
            # QAbstractScrollArea owns native scrollbar visibility. Avoid
            # setVisible() here so a later rangeChanged can make it appear.
        else:
            bar.setVisible(visible or (not self.scrollbars_auto_hide and available))
        bar.setProperty('magellan_overlay_visible',visible)
    def scrollbar_pointer_in_reveal_zone(self,bar,pos=None):
        if not isinstance(bar,QScrollBar) or bar.maximum()<=bar.minimum():return False
        pos=pos or QCursor.pos();parent=bar.parentWidget()
        if parent is None:return False
        # The File Tree can be a floating, auto-hidden dock on startup. Its
        # native scrollbar may have no geometry while hidden, so reveal it
        # whenever the pointer is inside the tree panel and let the shared
        # scrollbar timer hide it after the pointer leaves.
        if hasattr(self,'file_tree') and bar is self.file_tree.verticalScrollBar():
            return self.tree_panel_contains_global(pos)
        if isinstance(parent,QAbstractScrollArea):
            viewport=parent.viewport();rect=QRect(viewport.mapToGlobal(QPoint(0,0)),viewport.size())
            if bar.orientation()==Qt.Vertical:return rect.left()-10<=pos.x()<=rect.right()+18 and rect.top()<=pos.y()<=rect.bottom()
            return rect.top()-10<=pos.y()<=rect.bottom()+18 and rect.left()<=pos.x()<=rect.right()
        rect=QRect(bar.mapToGlobal(QPoint(0,0)),bar.size())
        if bar.orientation()==Qt.Vertical:return abs(pos.x()-rect.center().x())<=18 and rect.top()<=pos.y()<=rect.bottom()
        return abs(pos.y()-rect.center().y())<=18 and rect.left()<=pos.x()<=rect.right()
    def scrollbar_pointer_in_corridor(self,bar,pos=None):
        if not isinstance(bar,QScrollBar) or bar.maximum()<=bar.minimum():return False
        pos=pos or QCursor.pos();rect=QRect(bar.mapToGlobal(QPoint(0,0)),bar.size());screen=QApplication.primaryScreen();bounds=screen.geometry() if screen is not None else QRect(0,0,1920,1080)
        if bar.orientation()==Qt.Vertical:return abs(pos.x()-rect.center().x())<=22 and bounds.top()<=pos.y()<=bounds.bottom()
        return abs(pos.y()-rect.center().y())<=22 and bounds.left()<=pos.x()<=bounds.right()
    def schedule_scrollbar_hide(self,bar,delay=None):
        if not isinstance(bar,QScrollBar) or not self.scrollbars_auto_hide:return
        timer=getattr(bar,'_magellan_hide_timer',None)
        if timer is None:timer=QTimer(bar);timer.setSingleShot(True);bar._magellan_hide_timer=timer
        try:timer.timeout.disconnect()
        except RuntimeError:pass
        timer.timeout.connect(lambda b=bar:self._scrollbar_hide_timeout(b));timer.start(self.scrollbars_hide_delay if delay is None else max(0,int(delay)))
    def _scrollbar_hide_timeout(self,bar):
        if not self.scrollbars_auto_hide:return
        if bar.property('magellan_search_keep_visible'):return
        if bar.underMouse() or self.scrollbar_pointer_in_corridor(bar):self.schedule_scrollbar_hide(bar,120);return
        self.set_scrollbar_visibility(bar,False)
    def register_scrollbars(self,root):
        if not isinstance(root,QWidget):return
        bars=root.findChildren(QScrollBar)
        if isinstance(root,QScrollBar):bars.append(root)
        if not hasattr(self,'_registered_scrollbars'):self._registered_scrollbars=[]
        for bar in bars:
            if bar.property('magellan_internal_scroll_model'):continue
            if bar not in self._registered_scrollbars:self._registered_scrollbars.append(bar)
            if not bar.property('magellan_scroll_filter'):
                bar.installEventFilter(self);bar.setProperty('magellan_scroll_filter',True);bar.destroyed.connect(lambda _=None,b=bar:self._forget_scrollbar(b))
                bar.rangeChanged.connect(lambda _minimum,_maximum,b=bar:self._refresh_scrollbar_range(b))
            keep_visible=bool(bar.property('magellan_search_keep_visible')) or bar.underMouse() or self.scrollbar_pointer_in_reveal_zone(bar) or (bar.property('magellan_overlay_visible') and self.scrollbar_pointer_in_corridor(bar))
            self.set_scrollbar_visibility(bar,not self.scrollbars_auto_hide or keep_visible)
    def _forget_scrollbar(self,bar):
        try:self._registered_scrollbars.remove(bar)
        except (AttributeError,ValueError):pass
    def _refresh_scrollbar_range(self,bar):
        try:
            visible=not self.scrollbars_auto_hide or bar.underMouse() or self.scrollbar_pointer_in_reveal_zone(bar) or (bar.property('magellan_overlay_visible') and self.scrollbar_pointer_in_corridor(bar))
            self.set_scrollbar_visibility(bar,visible)
        except RuntimeError:pass
    def apply_scrollbar_behavior(self):
        self.register_scrollbars(self)
        if self.viewer is not None:
            try:self.register_scrollbars(self.viewer.canvas)
            except RuntimeError:pass
        self.schedule_tree_scrollbar_refresh()
    def schedule_tree_scrollbar_refresh(self):
        if not hasattr(self,'file_tree'):return
        # The tree is populated and resized asynchronously during startup. Run
        # after Qt has laid out the floating/embedded dock so its scrollbar
        # range and reveal zone use the final viewport geometry.
        QTimer.singleShot(0,self.refresh_tree_scrollbar_behavior)
        QTimer.singleShot(100,self.refresh_tree_scrollbar_behavior)
    def refresh_tree_scrollbar_behavior(self):
        if self._closing or not hasattr(self,'file_tree'):return
        bar=self.file_tree.verticalScrollBar()
        if self.scrollbars_auto_hide:
            visible=bool(self.tree_dock.isVisible() and (bar.underMouse() or self.scrollbar_pointer_in_reveal_zone(bar) or (bar.property('magellan_overlay_visible') and self.scrollbar_pointer_in_corridor(bar))))
        else:visible=True
        self.set_scrollbar_visibility(bar,visible)
    def eventFilter(self,obj,event):
        if self._closing or not getattr(self,'_ui_ready',False) or getattr(self,'_app_event_filter_active',False):return False
        self._app_event_filter_active=True
        try:return self._process_app_event_filter(obj,event)
        finally:self._app_event_filter_active=False
    def _process_app_event_filter(self,obj,event):
        if not getattr(self,'_ui_ready',False):return False
        if event.type() in (QEvent.Paint,QEvent.Resize) and hasattr(self,'grid') and obj is self.grid.viewport():
            self._appearance_source_revision=getattr(self,'_appearance_source_revision',0)+1
        if obj is self and event.type() in (QEvent.WinIdChange,QEvent.Show,QEvent.WindowStateChange):
            QTimer.singleShot(0,self.ensure_fullscreen_minimize_style)
            # Qt can finish recreating the native fullscreen style after Show.
            QTimer.singleShot(50,self.ensure_fullscreen_minimize_style)
        if event.type()==QEvent.Show and isinstance(obj,(QMenu,QDockWidget)):
            self.ensure_fullscreen_minimize_style()
        app=QApplication.instance()
        if event.type()==QEvent.ContextMenu and app is not None and time.monotonic()<getattr(app,'_magellan_suppress_context_until',0.0):
            event.accept();return True
        if not hasattr(self,'main_bar'):return False
        if event.type()==QEvent.KeyPress and event.key() in (Qt.Key_Return,Qt.Key_Enter) and hasattr(self,'grid'):
            editor=getattr(self.grid,'inline_rename_editor',None)
            if editor is not None and editor.isVisible() and QApplication.focusWidget() is editor:
                QTimer.singleShot(0,lambda grid=self.grid:grid.commit_inline_rename(restore_focus=True));event.accept();return True
        if event.type()==QEvent.Wheel and hasattr(self,'grid'):
            editor=getattr(self.grid,'inline_rename_editor',None)
            if editor is not None and editor.isVisible() and isinstance(obj,QWidget) and (obj is editor or self.grid.isAncestorOf(obj)):
                QTimer.singleShot(0,lambda grid=self.grid:grid.commit_inline_rename(restore_focus=True))
        if event.type()==QEvent.KeyPress and QApplication.activeWindow() is self and not event.modifiers()&Qt.AltModifier:
            focus=QApplication.focusWidget();editing=isinstance(focus,(QLineEdit,QSpinBox))
            if not editing and event.modifiers()&Qt.ControlModifier:
                operations={Qt.Key_X:lambda:self.copy_selected_files(cut=True),Qt.Key_C:lambda:self.copy_selected_files(cut=False),Qt.Key_V:self.paste_files,Qt.Key_E:self.extract_selected_files}
                action=operations.get(event.key())
                if action is not None:action();event.accept();return True
        tree_viewport=getattr(getattr(self,'file_tree',None),'viewport',lambda:None)()
        if event.type() in (QEvent.MouseButtonPress,QEvent.MouseButtonRelease) and obj is not tree_viewport:
            # A press can start in the tree and release after an auto-hide or
            # panel transition. Never let that stale press activate a row on a
            # later, unrelated click or drag.
            self._tree_click_press=None
        if hasattr(self,'file_tree') and obj is self.file_tree.viewport() and event.type()==QEvent.Wheel:
            # Keep wheel navigation reliable when the native tree scrollbar
            # is hidden by the auto-hide behavior.
            delta=event.angleDelta().y();bar=self.file_tree.verticalScrollBar()
            if delta:
                rows=max(.1,float(getattr(self,'wheel_scroll_rows',1.0)))
                amount=round(delta/120.0*rows*max(1,bar.singleStep())*3)
            else:
                amount=round(-event.pixelDelta().y()*max(.1/3.0,min(4.0,float(getattr(self,'wheel_scroll_rows',1.0))/3.0)))
            bar.setValue(max(bar.minimum(),min(bar.maximum(),bar.value()-amount)))
            event.accept();return True
        if hasattr(self,'file_tree') and obj is self.file_tree.viewport() and event.type()==QEvent.MouseButtonPress and event.button()==Qt.MiddleButton:
            item=self.file_tree.itemAt(event.position().toPoint());path=self._tree_path(item) if item is not None else None
            if path is not None and path.is_dir():self.open_new_tab(path)
            event.accept();return True
        if isinstance(obj,QScrollBar) and obj.property('magellan_scroll_filter') and self.scrollbars_auto_hide:
            if event.type()==QEvent.Enter:
                timer=getattr(obj,'_magellan_hide_timer',None)
                if timer is not None:timer.stop()
                self.set_scrollbar_visibility(obj,True)
            elif event.type()==QEvent.Leave:
                self.schedule_scrollbar_hide(obj)
            elif event.type()==QEvent.Show and not obj.underMouse() and not self.scrollbar_pointer_in_reveal_zone(obj):
                QTimer.singleShot(0,lambda b=obj:self.set_scrollbar_visibility(b,False) if self.scrollbars_auto_hide and not b.underMouse() and not self.scrollbar_pointer_in_reveal_zone(b) else None)
        if self.scrollbars_auto_hide and event.type() in (QEvent.MouseMove,QEvent.HoverMove):
            pos=QCursor.pos()
            for bar in getattr(self,'_registered_scrollbars',()):
                if not isinstance(bar,QScrollBar):continue
                in_zone=self.scrollbar_pointer_in_reveal_zone(bar,pos);in_corridor=self.scrollbar_pointer_in_corridor(bar,pos)
                if in_zone:
                    timer=getattr(bar,'_magellan_hide_timer',None)
                    if timer is not None:timer.stop()
                    self.set_scrollbar_visibility(bar,True)
                elif bar.property('magellan_overlay_visible') and not in_corridor and not bar.underMouse():
                    timer=getattr(bar,'_magellan_hide_timer',None)
                    if timer is None or not timer.isActive():self.schedule_scrollbar_hide(bar)
        viewer_active=isinstance(QApplication.activeWindow(),MediaViewer)
        active=QApplication.activeWindow();main_active=active is self or (active is not None and self.isAncestorOf(active))
        if event.type()==QEvent.MouseButtonPress and event.button()==Qt.LeftButton and isinstance(obj,QWidget) and hasattr(self,'main_bar') and self.main_bar.isAncestorOf(obj) and obj is not getattr(self,'search',None):
            self.grid.setFocus(Qt.MouseFocusReason)
        if hasattr(self,'file_tree') and obj in (self.file_tree,self.file_tree.viewport()) and event.type() in (QEvent.Wheel,QEvent.MouseButtonPress,QEvent.KeyPress):
            if self._tree_scroll_restore_pending and not self._tree_restoring_scroll:
                # Once the user interacts with the visible tree, its saved view
                # is authoritative; a delayed async restore must not jump it.
                self._tree_scroll_restore_pending=False;self._tree_rebuilding=False
        if event.type()==QEvent.ApplicationDeactivate:
            self.shift_selection_held=False
            self.ctrl_key_down=False;self.ctrl_organization_timer.stop()
            if self.ctrl_organization_held:self.ctrl_organization_held=False;self.set_favorite_organization_mode(False)
        if event.type()==QEvent.KeyPress and event.key()==Qt.Key_Control and main_active and QApplication.activeModalWidget() is None and not event.isAutoRepeat():
            self.ctrl_key_down=True;self.ctrl_organization_timer.start()
        elif event.type()==QEvent.KeyRelease and event.key()==Qt.Key_Control:
            self.ctrl_key_down=False
            self.ctrl_organization_timer.stop()
            if self.ctrl_organization_held:self.ctrl_organization_held=False;self.set_favorite_organization_mode(False)
        elif event.type()==QEvent.KeyPress and event.modifiers()&Qt.ControlModifier and main_active and QApplication.activeModalWidget() is None:
            self.ctrl_key_down=True;self.ctrl_organization_timer.stop();self.ctrl_organization_held=True;self.set_favorite_organization_mode(True)
        if event.type()==QEvent.MouseMove and main_active and QApplication.activeModalWidget() is None:
            modifiers=event.modifiers()|QApplication.keyboardModifiers()
            if modifiers&Qt.ControlModifier:
                self.ctrl_key_down=True;self.ctrl_organization_timer.stop();self.ctrl_organization_held=True;self.set_favorite_organization_mode(True)
        if event.type()==QEvent.MouseButtonPress and event.button()==Qt.LeftButton and (self.ctrl_key_down or event.modifiers()&Qt.ControlModifier or QApplication.keyboardModifiers()&Qt.ControlModifier) and obj is getattr(getattr(self,'grid',None),'viewport',lambda:None)():
            self.ctrl_key_down=True;self.ctrl_organization_timer.stop();self.set_favorite_organization_mode(True);self.ctrl_organization_held=True
        if event.type()==QEvent.MouseMove and not viewer_active and hasattr(self,'grid') and self.grid.navigation_index>=0:
            self.grid.navigation_index=-1;self.grid.viewport().update()
        if event.type()==QEvent.KeyPress and QApplication.activeWindow() is self and not viewer_active:
            focus=QApplication.focusWidget();editing=isinstance(focus,(QLineEdit,QSpinBox))
            if not editing and event.key()==Qt.Key_Space and self.grid.selected_paths:self.grid.clear_selection();event.accept();return True
            if event.key()==Qt.Key_T and event.modifiers()&Qt.ControlModifier and not event.modifiers()&Qt.AltModifier:self.reopen_last_closed_tab();event.accept();return True
            if not editing and event.key()==Qt.Key_T and event.modifiers()&Qt.ShiftModifier and not event.modifiers()&(Qt.ControlModifier|Qt.AltModifier):self.add_empty_tab();event.accept();return True
            if not editing and event.key()==Qt.Key_R and event.modifiers()&Qt.ShiftModifier and not event.modifiers()&(Qt.ControlModifier|Qt.AltModifier):
                selected=list(self.grid.selected_paths)
                if len(selected)==1:self.grid.begin_inline_rename(selected[0])
                event.accept();return True
            if not editing and event.key()==Qt.Key_A and event.modifiers()&Qt.ControlModifier:self.grid.select_all_current();event.accept();return True
            if not editing and event.modifiers()&Qt.ControlModifier and event.key()==Qt.Key_S:self.grid.undo_deselection();event.accept();return True
            if not editing and not event.isAutoRepeat() and event.modifiers()&Qt.ControlModifier and event.key()==Qt.Key_N:self.create_new_folder();event.accept();return True
            if not editing and event.key()==Qt.Key_Delete:
                if event.modifiers()&Qt.ShiftModifier:
                    self.send_paths_to_trash(list(self.grid.selected_paths))
                elif self.in_favorites_view:
                    self.remove_favorites_selection()
                else:return super().eventFilter(obj,event)
                event.accept();return True
            if event.key()==Qt.Key_Z and event.modifiers()&Qt.ControlModifier and not editing:self.undo_last_action();event.accept();return True
            if not editing and event.key()==Qt.Key_F11 and not event.modifiers():self.toggle_fullscreen();event.accept();return True
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
        if event.type() in (QEvent.Resize,QEvent.Show) and obj is getattr(self,'tree_dock',None):
            QTimer.singleShot(0,self.sync_tree_appearance_geometry)
            if event.type()==QEvent.Show:self.schedule_tree_scrollbar_refresh()
        if event.type()==QEvent.Resize and obj is getattr(self,'root_widget',None):
            if self.auto_hide_main:self.main_bar.setGeometry(0,0,self.root_widget.width(),self.main_bar.sizeHint().height())
            if self.auto_hide_main and self.auto_hide_tabs:self.nav_bar.setGeometry(0,self.main_bar.sizeHint().height(),self.root_widget.width(),self.nav_bar.sizeHint().height())
            if hasattr(self,'selection_clear_overlay'):self.position_right_controls()
            # Docked panels resize the central widget without resizing the window.
            self.update_fullscreen_close_button()
            if hasattr(self,'grid') and not self._closing:
                timer=getattr(self,'_panel_layout_transition_timer',None)
                if timer is None:
                    timer=QTimer(self);timer.setSingleShot(True);timer.timeout.connect(self.finish_panel_layout_transition);self._panel_layout_transition_timer=timer
                timer.start(0)
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
        if event.type()==QEvent.MouseMove and self._appearance_preview_active:
            self.main_hide_timer.stop();self.tree_hide_timer.stop()
            pass
        if event.type()==QEvent.MouseMove and (self.auto_hide_main or self.auto_hide_tree) and not self._appearance_preview_active and not viewer_active and not getattr(self,'shift_selection_held',False):
            pos=self.root_widget.mapFromGlobal(QCursor.pos())
            if self.auto_hide_main:
                over_tabs=self.nav_bar.isVisible() and self.nav_bar.geometry().contains(pos)
                over_main=self.main_bar.isVisible() and self.main_bar.geometry().contains(pos)
                if self.search_has_focus():
                    self.main_hide_timer.stop()
                    if not self.main_bar.isVisible():self.set_main_panel_visible(True)
                elif over_main or over_tabs or main_menu_hover or getattr(self,'tab_context_menu_open',False):self.main_hide_timer.stop()
                elif pos.y()<=2:
                    self.main_hide_timer.stop();self.set_main_panel_visible(True)
                elif self.main_bar.isVisible() and pos.y()>self.main_bar.height():self.main_hide_timer.start(self.main_hide_delay)
            if self.auto_hide_tree and hasattr(self,'tree_dock'):
                if getattr(self,'tree_scrollbar_interacting',False):
                    self.tree_hide_timer.stop()
                elif self.tree_panel_contains_global(QCursor.pos()):
                    self.tree_hide_timer.stop()
                elif 0<=pos.x()<=10:
                    self.show_tree_overlay()
                elif self.tree_dock.isVisible() and not self.tree_hide_timer.isActive():self.tree_hide_timer.start(self.tree_hide_delay)
        if event.type()==QEvent.MouseMove and hasattr(self,'selection_clear_overlay') and not viewer_active and not self._appearance_preview_active:
            pos=self.root_widget.mapFromGlobal(QCursor.pos());self.position_right_controls()
            rects=[w.geometry().adjusted(-3,-3,3,3) for w in self.right_control_widgets() if w.isVisible()]
            if not self.right_controls_auto_hide:
                self.favorite_overlay_timer.stop();self.show_right_controls()
            elif pos.x()>=self.root_widget.width()-24 and pos.y()>=self.main_bar.height():
                self.favorite_overlay_timer.stop();self.show_right_controls()
            elif any(rect.contains(pos) for rect in rects):self.favorite_overlay_timer.stop()
            elif any(w.isVisible() for w in self.right_control_widgets()):self.favorite_overlay_timer.start(self.right_controls_hide_delay)
        elif self._appearance_preview_active and hasattr(self,'favorite_overlay_timer'):
            self.favorite_overlay_timer.stop()
        return super().eventFilter(obj,event)
    def apply_window_state(self):
        try:
            if self.saved_geometry:
                from PySide6.QtCore import QByteArray
                self.restoreGeometry(QByteArray.fromBase64(self.saved_geometry.encode('ascii')))
        except Exception:
            logger.warning('Could not restore saved window geometry',exc_info=True)
        if self.window_mode=='fullscreen':
            self.showFullScreen();QTimer.singleShot(0,self.ensure_fullscreen_minimize_style)
        elif self.window_mode=='maximized': self.showMaximized()
        else: self.showNormal()
        self.update_window_button();self.sync_taskbar_reveal_timer()
    @staticmethod
    def windows_taskbar_auto_hide_enabled():
        if os.name!='nt':return False
        class APPBARDATA(ctypes.Structure):
            _fields_=[('cbSize',wintypes.DWORD),('hWnd',wintypes.HWND),('uCallbackMessage',wintypes.UINT),('uEdge',wintypes.UINT),('rc',wintypes.RECT),('lParam',wintypes.LPARAM)]
        try:
            data=APPBARDATA();data.cbSize=ctypes.sizeof(APPBARDATA)
            shell32=ctypes.WinDLL('shell32',use_last_error=True);shell32.SHAppBarMessage.argtypes=(wintypes.DWORD,ctypes.POINTER(APPBARDATA));shell32.SHAppBarMessage.restype=ctypes.c_size_t
            return bool(shell32.SHAppBarMessage(0x00000004,ctypes.byref(data))&0x00000001) # ABM_GETSTATE / ABS_AUTOHIDE
        except (AttributeError,OSError,TypeError):
            logger.debug('Could not query the Windows taskbar auto-hide state',exc_info=True);return False
    def sync_taskbar_reveal_timer(self):
        timer=getattr(self,'taskbar_reveal_timer',None)
        if timer is None:return
        timer.stop()
        if self.isMinimized():
            self._taskbar_peek_active=False;return
        self._taskbar_peek_active=False;self._taskbar_restore_geometry=None
        if self.window_mode=='fullscreen' and self.isVisible() and not self.isMinimized() and self.windows_taskbar_auto_hide_enabled():timer.start()
    def reveal_taskbar_from_fullscreen(self):
        if self.window_mode!='fullscreen' or not self.isVisible():
            self.taskbar_reveal_timer.stop();self._taskbar_peek_active=False;self._taskbar_restore_geometry=None;return
        if self.isMinimized():
            self.taskbar_reveal_timer.stop();self._taskbar_peek_active=False;return
        screen=self.screen() or QApplication.primaryScreen()
        if screen is None:return
        bounds=screen.geometry();cursor=QCursor.pos()
        if cursor.x()<bounds.left() or cursor.x()>bounds.right():
            if self._taskbar_peek_active:
                restore=self._taskbar_restore_geometry;self._taskbar_peek_active=False;self._taskbar_restore_geometry=None
                if restore is not None:
                    if self.isFullScreen():self.setGeometry(restore)
                    else:self.showFullScreen()
            return
        if cursor.y()>=bounds.bottom()-1:
            if not self._taskbar_peek_active:
                self._taskbar_restore_geometry=QRect(bounds);self._taskbar_peek_active=True
                # Expose a two-pixel strip at the desktop edge so Windows can
                # receive its native auto-hide taskbar hover trigger.
                self.setGeometry(QRect(bounds.left(),bounds.top(),bounds.width(),max(1,bounds.height()-2)))
        elif self._taskbar_peek_active and cursor.y()<bounds.bottom()-60:
            restore=self._taskbar_restore_geometry
            self._taskbar_peek_active=False;self._taskbar_restore_geometry=None
            if restore is not None:
                if self.isFullScreen():self.setGeometry(restore)
                else:self.showFullScreen()
    def gpu_desktop_fullscreen_available(self):
        return os.name=='nt' and hasattr(self,'grid') and bool(self.grid.viewport().property('magellan_gpu_viewport'))
    def isFullScreen(self):
        return bool(getattr(self,'_gpu_desktop_fullscreen',False)) or super().isFullScreen()
    def showFullScreen(self):
        if self.gpu_desktop_fullscreen_available():
            self.enter_gpu_desktop_fullscreen()
        else:super().showFullScreen()
    def showNormal(self):
        if getattr(self,'_gpu_desktop_fullscreen',False):self.leave_gpu_desktop_fullscreen(False)
        else:super().showNormal()
    def showMaximized(self):
        if getattr(self,'_gpu_desktop_fullscreen',False):self.leave_gpu_desktop_fullscreen(True)
        else:super().showMaximized()
    def gpu_desktop_frame(self,fullscreen):
        user32=ctypes.WinDLL('user32',use_last_error=True)
        get_long=getattr(user32,'GetWindowLongPtrW',user32.GetWindowLongW);set_long=getattr(user32,'SetWindowLongPtrW',user32.SetWindowLongW)
        get_long.argtypes=(wintypes.HWND,ctypes.c_int);get_long.restype=ctypes.c_ssize_t
        set_long.argtypes=(wintypes.HWND,ctypes.c_int,ctypes.c_ssize_t);set_long.restype=ctypes.c_ssize_t
        hwnd=wintypes.HWND(int(self.winId()));style=get_long(hwnd,-16);frame_mask=0x80c40000
        if fullscreen:
            if not hasattr(self,'_gpu_window_frame_style'):self._gpu_window_frame_style=style&frame_mask
            # Keep a native border so Windows continues desktop composition,
            # without changing Qt window flags or recreating the GPU context.
            updated=(style&~frame_mask)|0x00800000|0x00020000|0x00080000
        else:updated=(style&~frame_mask)|getattr(self,'_gpu_window_frame_style',0x00c40000)
        if updated!=style:
            set_long(hwnd,-16,updated)
            user32.SetWindowPos.argtypes=(wintypes.HWND,wintypes.HWND,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,wintypes.UINT);user32.SetWindowPos.restype=wintypes.BOOL
            user32.SetWindowPos(hwnd,None,0,0,0,0,0x0001|0x0002|0x0004|0x0010|0x0020)
    def enter_gpu_desktop_fullscreen(self):
        if getattr(self,'_closing',False) or getattr(self,'_gpu_fullscreen_transition',False):return
        self._gpu_fullscreen_transition=True
        try:
            if not getattr(self,'_gpu_desktop_fullscreen',False):
                self._gpu_desktop_restore_geometry=QRect(self.normalGeometry() if self.isMaximized() else self.geometry())
            screen=self.screen() or QApplication.primaryScreen()
            bounds=QRect(screen.geometry()) if screen is not None else None
            self._gpu_desktop_fullscreen=True
            self.setWindowState(Qt.WindowNoState)
            self.show()
            self.gpu_desktop_frame(True)
            if bounds is not None:self.setGeometry(bounds)
            QTimer.singleShot(0,self.ensure_fullscreen_minimize_style)
        finally:self._gpu_fullscreen_transition=False
    def leave_gpu_desktop_fullscreen(self,maximized):
        self._gpu_fullscreen_transition=True;self._gpu_desktop_fullscreen=False
        try:
            self.gpu_desktop_frame(False)
            if maximized:super().showMaximized()
            else:
                super().showNormal()
                geometry=getattr(self,'_gpu_desktop_restore_geometry',None)
                if geometry is not None and geometry.isValid():self.setGeometry(geometry)
            if hasattr(self,'_gpu_window_frame_style'):del self._gpu_window_frame_style
        finally:self._gpu_fullscreen_transition=False
    def ensure_fullscreen_minimize_style(self):
        if os.name!='nt' or self._closing or not self.isVisible() or self.window_mode!='fullscreen':return
        try:
            user32=ctypes.WinDLL('user32',use_last_error=True);get_long=getattr(user32,'GetWindowLongPtrW',user32.GetWindowLongW);set_long=getattr(user32,'SetWindowLongPtrW',user32.SetWindowLongW)
            get_long.argtypes=(wintypes.HWND,ctypes.c_int);get_long.restype=ctypes.c_ssize_t;set_long.argtypes=(wintypes.HWND,ctypes.c_int,ctypes.c_ssize_t);set_long.restype=ctypes.c_ssize_t
            hwnd=wintypes.HWND(int(self.winId()));gwl_style=-16;style=get_long(hwnd,gwl_style);updated=style|0x00020000|0x00080000
            # Qt documents WS_BORDER as the DWM workaround for invisible
            # top-level menus/tools above fullscreen OpenGL-based windows.
            gpu=hasattr(self,'grid') and bool(self.grid.viewport().property('magellan_gpu_viewport'))
            if gpu:updated|=0x00800000
            # Recalculating the GPU frame makes Qt leave/re-enter fullscreen
            # continuously. WS_BORDER itself is sufficient for DWM composition.
            if updated!=style:
                set_long(hwnd,gwl_style,updated);user32.SetWindowPos.argtypes=(wintypes.HWND,wintypes.HWND,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,wintypes.UINT);user32.SetWindowPos.restype=wintypes.BOOL;user32.SetWindowPos(hwnd,None,0,0,0,0,0x0001|0x0002|0x0004|0x0010|(0 if gpu else 0x0020))
        except (AttributeError,OSError,TypeError,ValueError):logger.debug('Could not enable taskbar minimize controls in fullscreen mode',exc_info=True)
    def minimize_main_window(self):
        timer=getattr(self,'taskbar_reveal_timer',None)
        if timer is not None:timer.stop()
        was_peeking=self._taskbar_peek_active;self._taskbar_peek_active=False
        if not was_peeking:self._taskbar_restore_geometry=None
        self.showMinimized()
    def nativeEvent(self,event_type,message):
        if os.name=='nt' and not getattr(self,'_closing',False) and getattr(self,'window_mode',None)=='fullscreen' and hasattr(self,'grid') and self.grid.viewport().property('magellan_gpu_viewport'):
            try:
                native=ctypes.cast(int(message),ctypes.POINTER(wintypes.MSG)).contents
                if native.message==0x0112 and int(native.wParam)&0xfff0==0xf030: # WM_SYSCOMMAND / SC_MAXIMIZE
                    # Already fullscreen: a second maximize changes the native
                    # presentation mode and can cover floating menus/tools.
                    if self.isMinimized():
                        if getattr(self,'_gpu_desktop_fullscreen',False):self.enter_gpu_desktop_fullscreen()
                        else:self.setWindowState((self.windowState()&~(Qt.WindowMinimized|Qt.WindowMaximized))|Qt.WindowFullScreen)
                    self.ensure_fullscreen_minimize_style()
                    return True,0
            except (TypeError,ValueError,OverflowError):logger.debug('Could not inspect fullscreen maximize command',exc_info=True)
        return super().nativeEvent(event_type,message)
    def changeEvent(self,event):
        super().changeEvent(event)
        if event.type()!=QEvent.WindowStateChange or getattr(self,'_closing',False) or getattr(self,'_gpu_fullscreen_transition',False):return
        timer=getattr(self,'taskbar_reveal_timer',None)
        if self.isMinimized():
            if timer is not None:timer.stop()
            self._taskbar_peek_active=False
        elif self.isVisible():
            restore=self._taskbar_restore_geometry
            self._taskbar_restore_geometry=None;self._taskbar_peek_active=False
            gpu=hasattr(self,'grid') and bool(self.grid.viewport().property('magellan_gpu_viewport'))
            if gpu and self.window_mode=='fullscreen' and getattr(self,'_gpu_desktop_fullscreen',False):
                self.enter_gpu_desktop_fullscreen()
                if restore is not None:self.setGeometry(restore)
                self.update_window_button();self.update_fullscreen_close_button();self.sync_taskbar_reveal_timer()
            elif gpu and self.window_mode=='fullscreen' and (event.oldState()&Qt.WindowMinimized or self.windowState()&Qt.WindowMaximized or not self.isFullScreen()):
                # Repair the restored state before Qt presents an intermediate
                # normal-window frame; do not queue another showFullScreen().
                self.setWindowState((self.windowState()&~(Qt.WindowMinimized|Qt.WindowMaximized))|Qt.WindowFullScreen)
                if restore is not None:self.setGeometry(restore)
                QTimer.singleShot(0,self.ensure_fullscreen_minimize_style)
                self.update_window_button();self.update_fullscreen_close_button();self.sync_taskbar_reveal_timer()
            elif restore is not None and self.window_mode=='fullscreen':QTimer.singleShot(0,lambda rect=QRect(restore):self.restore_fullscreen_after_taskbar_peek(rect))
            elif self.window_mode=='fullscreen' and not self.isFullScreen():QTimer.singleShot(0,self.restore_fullscreen_from_taskbar)
            else:self.sync_taskbar_reveal_timer()
    def restore_fullscreen_from_taskbar(self):
        if self._closing or self.window_mode!='fullscreen' or self.isMinimized() or not self.isVisible():return
        if not self.isFullScreen():self.showFullScreen()
        QTimer.singleShot(0,self.ensure_fullscreen_minimize_style);self.update_window_button();self.update_fullscreen_close_button();self.sync_taskbar_reveal_timer()
    def restore_fullscreen_after_taskbar_peek(self,restore):
        if self._closing or self.window_mode!='fullscreen' or self.isMinimized() or not self.isVisible():return
        if not self.isFullScreen():self.showFullScreen()
        QTimer.singleShot(0,self.ensure_fullscreen_minimize_style);self.setGeometry(restore);self.sync_taskbar_reveal_timer()
    def update_window_button(self):
        fullscreen=self.window_mode=='fullscreen'
        toolbar=getattr(self,'windowbtn',None)
        if toolbar is not None:
            toolbar.setText('□' if fullscreen else '⛶')
            toolbar.setToolTip('Exit full screen' if fullscreen else 'Full screen')
            toolbar.setVisible(not fullscreen)
        button=getattr(self,'fullscreen_button',None)
        if button is not None:
            button.setText('□' if fullscreen else '⛶')
            button.setToolTip('Exit full screen' if fullscreen else 'Full screen')

    def toggle_fullscreen(self):
        if self.window_mode=='fullscreen':
            self.window_mode=getattr(self,'pre_fullscreen_mode','maximized')
            if self.window_mode=='maximized': self.showMaximized()
            else: self.showNormal()
        else:
            self.pre_fullscreen_mode='maximized' if self.isMaximized() else 'normal'
            self.window_mode='fullscreen';self.showFullScreen();QTimer.singleShot(0,self.ensure_fullscreen_minimize_style)
        self.update_window_button();self.update_fullscreen_close_button();self.sync_taskbar_reveal_timer();self.save_settings()
        QTimer.singleShot(0,self.refresh_fullscreen_transition)
        QTimer.singleShot(80,self.refresh_fullscreen_transition)
    def refresh_fullscreen_transition(self):
        if self._closing or not hasattr(self,'root_widget'):return
        layout=getattr(self,'main_bar_layout',None)
        if layout is not None:layout.invalidate();layout.activate()
        controls_layout=getattr(self,'controls_bar',None)
        if controls_layout is not None and controls_layout.layout() is not None:controls_layout.layout().activate()
        self.update_window_button();self.update_fullscreen_close_button()
        self.root_widget.update();self.main_bar.update()
        if hasattr(self,'controls_bar'):self.controls_bar.update()
        for widget in getattr(self,'window_control_backdrops',{}).values():widget.update()
        self.root_widget.repaint()
    def closeEvent(self,e):
        if self._closing:e.accept();return
        file_task=getattr(self,'file_operation_task',None)
        if file_task is not None and file_task.isRunning():
            e.ignore()
            if not getattr(self,'_file_operation_close_pending',False):
                self._file_operation_close_pending=True
                file_task.finished.connect(self.close)
            if hasattr(self,'status'):self.status.setText('Finishing the current file operation before closing…')
            return
        self._closing=True
        app=QApplication.instance()
        if app is not None and getattr(self,'_app_event_filter_installed',False):
            app.removeEventFilter(self);self._app_event_filter_installed=False
        self._ui_ready=False
        for timer_name in ('folder_refresh_timer','folder_refresh_apply_timer','taskbar_reveal_timer','appearance_preview_timer','appearance_capture_timer','selection_size_timer','_panel_layout_transition_timer'):
            timer=getattr(self,timer_name,None)
            if timer is not None:timer.stop()
        self.close_gpu_composition();self.close_gpu_blur_backend()
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
        if not getattr(self,'_ui_deferred',False):
            if self.window_mode=='fullscreen' or self.isFullScreen():self.window_mode='fullscreen'
            elif self.isMaximized():self.window_mode='maximized'
            else:self.window_mode='normal'
        if hasattr(self,'grid'):self.save_current_view()
        self.save_settings()
        writer=getattr(self,'_settings_writer',None)
        settings_saved=writer is None or writer.finish(timeout=5.0)
        if not settings_saved:logger.error('The final settings save did not complete successfully; keeping existing backups')
        if settings_saved and not getattr(self,'_ui_deferred',False):self.backup_settings_if_changed()
        e.accept()
        app=QApplication.instance()
        if app is not None:QTimer.singleShot(0,app.quit)
    def resizeEvent(self,e):
        super().resizeEvent(e)
        self.position_activity_overlay()
        self.update_fullscreen_close_button()
        if hasattr(self,'top_backdrop'):
            self.top_backdrop.setGeometry(self.main_bar.rect());self.nav_backdrop.setGeometry(self.nav_bar.rect())
            if hasattr(self,'controls_backdrop'):self.controls_backdrop.setGeometry(self.controls_bar.rect())
            top_pos=self.main_bar.mapTo(self.root_widget,QPoint(0,0));nav_pos=self.nav_bar.mapTo(self.root_widget,QPoint(0,0));top=top_pos.y()+self.main_bar.height();bottom=nav_pos.y()+self.nav_bar.height();y=min(top_pos.y(),nav_pos.y())
            if self.auto_hide_main and not self.main_bar.isVisible():
                if self.auto_hide_tabs or not self.nav_bar.isVisible():self.main_panel_backdrop.hide()
                else:self.main_panel_backdrop.setGeometry(QRect(0,nav_pos.y(),self.root_widget.width(),self.nav_bar.height()));self.main_panel_backdrop.show();self.main_panel_backdrop.raise_();self.nav_bar.raise_()
            else:self.main_panel_backdrop.setGeometry(QRect(0,y,self.root_widget.width(),max(top,bottom)-y));self.main_panel_backdrop.show();self.main_panel_backdrop.raise_()
            if hasattr(self,'main_blur_backdrop'):self.main_blur_backdrop.setGeometry(self.main_panel_backdrop.rect());self.main_blur_backdrop.lower()
            if hasattr(self,'main_gradient_overlay'):self.main_gradient_overlay.setGeometry(self.main_panel_backdrop.rect());self.main_gradient_overlay.raise_()
            if hasattr(self,'main_path_gradient_overlay'):self.main_path_gradient_overlay.setGeometry(self.main_panel_backdrop.rect());self.main_path_gradient_overlay.raise_()
            self.raise_main_panel_contents()
        if hasattr(self,'tabs_backdrop') and hasattr(self,'tabbar'):self.tabs_backdrop.setGeometry(self.tabbar.geometry())
        if hasattr(self,'tree_backdrop') and hasattr(self,'tree_dock'):self.tree_backdrop.setGeometry(self.tree_dock.rect())
        if hasattr(self,'appearance_capture_timer'):self.appearance_capture_timer.start()
        if hasattr(self,'slider') and hasattr(self,'grid') and self.grid.viewport().width()>200:
            self.slider.setMaximum(max(140,(self.grid.viewport().width()-28)//2))
        if self.auto_hide_main and hasattr(self,'root_widget') and hasattr(self,'main_bar'):
            self.set_main_panel_visible(self.main_bar.isVisible())
    def show_performance_options(self,gpu=False):
        dlg=QDialog(self);dlg.setWindowTitle('GPU settings' if gpu else 'Cache settings');layout=QVBoxLayout(dlg)
        text=QLabel('GPU thumbnail rendering: helps smooth scrolling and reduces tearing. Requires a restart.\n\nGPU panel blur: uses the GPU to blur the background behind panels. It needs GPU thumbnail rendering.\n\nGPU panel composition: can help when visible panels use strong blur. With transparency alone or little blur, it may be slower—leave it off unless it helps on your computer. It affects the main panel, buttons, tabs and File Tree, not dropdown or right-click menus. Requires GPU thumbnail rendering and a restart.\n\nWhen composition is on, it handles panel blur too. These options are experimental; Magellan falls back to regular rendering if they are unavailable.' if gpu else 'Memory caching keeps decoded thumbnails in RAM. Disk caching reuses generated previews across sessions. Memory and disk limits are independent. Disk limiting is off by default. Keep folder thumbnails preserves previews for the chosen folders and their children.');text.setWordWrap(True);layout.addWidget(text)
        menu=self.gpu_options_menu if gpu else self.cache_options_menu
        for action in menu.actions():
            if action.isCheckable():
                control=QCheckBox(action.text());control.setChecked(action.isChecked());control.setToolTip(action.toolTip());control.toggled.connect(lambda checked,a=action:a.setChecked(checked));layout.addWidget(control)
            else:
                button=QPushButton(action.text());button.clicked.connect(lambda checked=False,a=action:a.trigger());layout.addWidget(button)
        close=QDialogButtonBox(QDialogButtonBox.Close);close.rejected.connect(dlg.reject);layout.addWidget(close);dlg.resize(540,400);dlg.exec()
    def welcome_gpu_options(self,preview=False):
        marker=self.cache_dir/'gpu_welcome_seen'
        if not preview and (QApplication.applicationName()!='Magellan' or marker.exists() or not self.isVisible()):return
        dlg=QDialog(self);dlg.setWindowTitle('Welcome to Magellan');layout=QVBoxLayout(dlg)
        illustration=QLabel();pixmap=QPixmap(str(self.icon_dir/'MainIconWelcome.png'))
        if not pixmap.isNull():illustration.setPixmap(pixmap.scaled(220,180,Qt.KeepAspectRatio,Qt.SmoothTransformation))
        illustration.setAlignment(Qt.AlignCenter);layout.addWidget(illustration)
        message=QLabel('Welcome to Magellan!<br><br>The main panel provides search, sorting and layout controls. Tabs keep your folders and Farviews handy. The File Tree on the left helps you navigate drives and favorite locations.<br><br>Visit <b>HELP → Keyboard and mouse controls</b> for controls and available features.<br><br>Enable GPU thumbnail rendering and smooth scrolling for the <b>recommended scrolling experience</b>? Magellan will restart automatically. For more performance options, visit <b>PERFORMANCE → GPU settings</b>.');message.setWordWrap(True);layout.addWidget(message)
        buttons=QDialogButtonBox(QDialogButtonBox.Yes|QDialogButtonBox.No);buttons.accepted.connect(dlg.accept);buttons.rejected.connect(dlg.reject);layout.addWidget(buttons);dlg.resize(520,470);answer=dlg.exec()
        if preview:return
        try:marker.touch()
        except OSError:logger.warning('Could not save welcome state',exc_info=True)
        if answer==QDialog.Accepted:
            self.smooth_wheel_scroll=True
            if self.scroll_glide_duration_ms<=0:self.scroll_glide_duration_ms=600
            self.set_gpu_thumbnail_rendering_enabled(True)
            command=[sys.executable] if getattr(sys,'frozen',False) else [sys.executable,str(Path(__file__).resolve())]
            def restart():
                lock=globals().get('_INSTANCE_LOCK')
                if lock is not None:lock.unlock()
                subprocess.Popen(command,cwd=str(Path(sys.executable).parent))
            QApplication.instance().aboutToQuit.connect(lambda:atexit.register(restart))
            self.close()

    def build(self):
        self._taskbar_peek_active=False;self._taskbar_restore_geometry=None;self.taskbar_reveal_timer=QTimer(self);self.taskbar_reveal_timer.setInterval(50);self.taskbar_reveal_timer.timeout.connect(self.reveal_taskbar_from_fullscreen)
        root=QWidget();root.setObjectName('root');self.setCentralWidget(root);main=QVBoxLayout(root);main.setContentsMargins(0,0,0,0);main.setSpacing(0)
        self.main_panel_backdrop=QLabel(root);self.main_panel_backdrop.setObjectName('appearancePanelBackdrop');self.main_panel_backdrop.setAttribute(Qt.WA_StyledBackground,True);self.main_panel_backdrop.setAttribute(Qt.WA_TransparentForMouseEvents,True);self.main_panel_backdrop.lower()
        self.main_blur_backdrop=QLabel(self.main_panel_backdrop);self.main_blur_backdrop.setObjectName('appearanceMainBlur');self.main_blur_backdrop.setAttribute(Qt.WA_StyledBackground,True);self.main_blur_backdrop.setAttribute(Qt.WA_TransparentForMouseEvents,True);self.main_blur_backdrop.lower()
        self.main_gradient_overlay=QLabel(self.main_panel_backdrop);self.main_gradient_overlay.setObjectName('appearanceMainGradient');self.main_gradient_overlay.setAttribute(Qt.WA_StyledBackground,True);self.main_gradient_overlay.setAttribute(Qt.WA_TransparentForMouseEvents,True);self.main_gradient_overlay.hide()
        self.main_path_gradient_overlay=QLabel(self.main_panel_backdrop);self.main_path_gradient_overlay.setObjectName('appearancePathGradient');self.main_path_gradient_overlay.setAttribute(Qt.WA_StyledBackground,True);self.main_path_gradient_overlay.setAttribute(Qt.WA_TransparentForMouseEvents,True);self.main_path_gradient_overlay.hide()
        mb=AppearanceMenuBar();mb.setMouseTracking(True);mb.hovered.connect(self.open_menu_on_hover);self.menu_bar=mb
        for title in ('FILES','VIEW','PERFORMANCE','TOOLS','HELP'):
            m=AppearanceMenu(title,mb);mb.addMenu(m)
            if title=='FILES':m.addAction('Open folder…',self.choose);m.addAction('Refresh',self.refresh_current_view);m.addSeparator();m.addAction('Exit',self.close)
            if title=='VIEW':
                m.addAction('Appearance settings…',self.configure_appearance_settings);m.addAction('Hover and hide settings…',self.configure_hover_hide_settings);m.addAction('Slideshow timer…',self.configure_slideshow)
                self.continuous_viewer_swipe_action=m.addAction('Continuous viewer swipe');self.continuous_viewer_swipe_action.setCheckable(True);self.continuous_viewer_swipe_action.setChecked(self.continuous_viewer_swipe);self.continuous_viewer_swipe_action.setToolTip('Include PDF, comic and ZIP/RAR pages when swiping through files in a folder. Farview always follows its own listed sequence.');self.continuous_viewer_swipe_action.toggled.connect(self.set_continuous_viewer_swipe)
                m.addAction('Manage personal orders…',self.manage_personal_orders);m.addAction('Automatically refresh folder pages…',self.configure_folder_auto_refresh);m.addAction('Keep folder thumbnails…',self.manage_pinned_folders)
            if title=='PERFORMANCE':
                recursive_action=m.addAction('Recursive search in subfolders');recursive_action.setCheckable(True);recursive_action.setChecked(self.recursive_search_enabled);recursive_action.toggled.connect(self.set_recursive_search_enabled)
                preload_action=m.addAction('Load all readable pages into memory');preload_action.setCheckable(True);preload_action.setChecked(self.preload_all_readable_pages);preload_action.setToolTip('Preload every PDF or archive page while it is open. This can use substantial memory.');preload_action.toggled.connect(self.set_preload_all_readable_pages)
                m.addSeparator()
                self.gpu_options_menu=AppearanceMenu(self);self.cache_options_menu=AppearanceMenu(self)
                gpu_action=self.gpu_options_menu.addAction('Experimental GPU thumbnail rendering (restart required)');gpu_action.setCheckable(True);gpu_action.setChecked(self.gpu_thumbnail_rendering_enabled);gpu_action.setToolTip('GPU-composited thumbnail frames with requested VSync. Restart Magellan after changing. Off uses the standard software renderer.');gpu_action.toggled.connect(self.set_gpu_thumbnail_rendering_enabled)
                self.gpu_blur_action=self.gpu_options_menu.addAction('Experimental GPU panel blur');self.gpu_blur_action.setCheckable(True);self.gpu_blur_action.setChecked(self.gpu_blur_enabled);self.gpu_blur_action.setToolTip('Requires active GPU thumbnail rendering. Uses a slightly different blur; menus and unsupported samples keep regular blur. Falls back automatically if unavailable.');self.gpu_blur_action.setEnabled(bool(self.gpu_thumbnail_rendering_enabled));self.gpu_blur_action.toggled.connect(self.set_gpu_blur_enabled)
                self.gpu_composition_action=self.gpu_options_menu.addAction('Experimental GPU panel composition');self.gpu_composition_action.setCheckable(True);self.gpu_composition_action.setChecked(self.gpu_composition_enabled);self.gpu_composition_action.setToolTip('Requires GPU thumbnail rendering and a full restart. Keeps panel effects on the GPU while retaining normal controls. Falls back to regular panels if unavailable.');self.gpu_composition_action.setEnabled(bool(self.gpu_thumbnail_rendering_enabled));self.gpu_composition_action.toggled.connect(self.set_gpu_composition_enabled)
                cache_action=self.cache_options_menu.addAction('Cache visible thumbnails in memory');cache_action.setCheckable(True);cache_action.setChecked(self.thumbnail_cache_enabled);cache_action.toggled.connect(self.set_thumbnail_cache_enabled)
                file_cache_action=self.cache_options_menu.addAction('Cache file thumbnails on disk');file_cache_action.setCheckable(True);file_cache_action.setChecked(self.file_thumbnail_disk_cache_enabled);file_cache_action.setToolTip('Reuse generated file thumbnails across sessions. Disabling stops reading and writing this cache; existing cache files are retained.');file_cache_action.toggled.connect(self.set_file_thumbnail_disk_cache_enabled)
                self.thumbnail_cache_limit_action=self.cache_options_menu.addAction(f'Memory thumbnail cache limit… ({self.thumbnail_cache_limit_mib} MiB)');self.thumbnail_cache_limit_action.triggered.connect(self.configure_thumbnail_cache_limit)
                self.disk_thumbnail_cache_action=self.cache_options_menu.addAction('Limit on-disk thumbnail cache');self.disk_thumbnail_cache_action.setCheckable(True);self.disk_thumbnail_cache_action.setChecked(self.disk_thumbnail_cache_limit_enabled);self.disk_thumbnail_cache_action.toggled.connect(self.set_disk_thumbnail_cache_limit_enabled)
                self.disk_thumbnail_cache_limit_action=self.cache_options_menu.addAction(f'On-disk thumbnail cache limit… ({self.disk_thumbnail_cache_limit_mib} MiB)');self.disk_thumbnail_cache_limit_action.triggered.connect(self.configure_disk_thumbnail_cache_limit)
                self.cache_options_menu.addAction('Thumbnail cache location…',self.configure_thumbnail_cache_location)
                self.cache_options_menu.addAction('Keep folder thumbnails…',self.manage_pinned_folders)
                m.addAction('GPU settings…',lambda:self.show_performance_options(True));m.addAction('Cache settings…',lambda:self.show_performance_options(False))
                self.log_storage_action=m.addAction(f'Maximum log storage… ({self.log_storage_mib} MiB)');self.log_storage_action.triggered.connect(self.configure_log_storage_limit)
            if title=='TOOLS':
                m.addAction('Set custom folder thumbnail…',self.set_custom);m.addSeparator();m.addAction('Scroll behavior…',self.configure_scroll_behavior);m.addSeparator();m.addAction('Set startup folder…',self.set_startup);m.addAction('Use last folder on startup',self.clear_startup);m.addSeparator();m.addAction('Set Magellan as default viewer…',self.register_default_viewer)
                self.explorer_context_action=m.addAction('Enable “Open in Magellan” in Explorer');self.explorer_context_action.setCheckable(True);self.explorer_context_action.setToolTip('Add or remove “Open in Magellan” from folder and supported archive context menus in Explorer');self.explorer_context_action.setEnabled(os.name=='nt');self.explorer_context_action.setChecked(self.is_explorer_context_menu_enabled());self.explorer_context_action.toggled.connect(self.set_explorer_context_menu_enabled)
                m.addSeparator();m.addAction('Reverse image search…',self.choose_search_provider);m.addAction('Google Lens text query…',self.configure_google_lens_query);m.addAction('Set SauceNAO API key…',self.set_saucenao_api_key)
            if title=='HELP':
                if self.developer_mode:
                    m.addAction('Preview welcome screen…',lambda:self.welcome_gpu_options(preview=True))
                    self.scroll_test_logging_action=m.addAction('Scroll test logging');self.scroll_test_logging_action.setCheckable(True);self.scroll_test_logging_action.setChecked(self.scroll_test_logging_enabled);self.scroll_test_logging_action.setToolTip('Write detailed scroll timing diagnostics to the application log');self.scroll_test_logging_action.toggled.connect(self.set_scroll_test_logging_enabled)
                    m.addAction('Scroll test settings…',self.configure_scroll_test)
                m.addAction('Keyboard and mouse controls…',self.show_controls_help)
        top=QFrame();top.setObjectName('top');self.top_backdrop=QLabel(top);self.top_backdrop.setObjectName('appearanceBackdrop');self.top_backdrop.setAttribute(Qt.WA_TransparentForMouseEvents,True);self.top_backdrop.lower();top_layout=QVBoxLayout(top);top_layout.setContentsMargins(0,0,0,0);top_layout.setSpacing(0);top_layout.addWidget(mb);controls=QWidget(top);self.controls_bar=controls;self.controls_backdrop=QLabel(controls);self.controls_backdrop.setObjectName('appearanceBackdrop');self.controls_backdrop.setAttribute(Qt.WA_TransparentForMouseEvents,True);self.controls_backdrop.lower();tl=QHBoxLayout(controls);tl.setContentsMargins(12,5,12,7);tl.setSpacing(7);top_layout.addWidget(controls)
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
        sz=QHBoxLayout();lab=QLabel('Size');lab.setObjectName('section');lab.setAlignment(Qt.AlignVCenter|Qt.AlignRight);sz.addWidget(lab);self.slider=QSlider(Qt.Horizontal);self.slider.setObjectName('thumbnailSizeSlider');self.slider.setRange(140,max(430,(self.width()-64)//2));self.slider.setValue(self.thumb);self.slider.valueChanged.connect(self.set_thumb);sz.addWidget(self.slider);tl.addLayout(sz);self.windowbtn=self.addbtn(tl,'⛶','Full screen',self.toggle_fullscreen)
        self.main_bar=top
        self.minimize_button=QToolButton(root);self.minimize_button.setObjectName('minimizeButton');self.minimize_button.setText('—');self.minimize_button.setToolTip('Minimize Magellan');self.minimize_button.setFixedSize(22,19);self.minimize_button.setFocusPolicy(Qt.NoFocus);self.minimize_button.clicked.connect(self.minimize_main_window)
        self.fullscreen_button=QToolButton(root);self.fullscreen_button.setObjectName('fullscreenToggleButton');self.fullscreen_button.setText('⛶');self.fullscreen_button.setToolTip('Full screen');self.fullscreen_button.setFixedSize(22,19);self.fullscreen_button.setFocusPolicy(Qt.NoFocus);self.fullscreen_button.clicked.connect(self.toggle_fullscreen)
        self.fullscreen_close_button=QToolButton(root);self.fullscreen_close_button.setObjectName('fullscreenCloseButton');self.fullscreen_close_button.setText('×');self.fullscreen_close_button.setToolTip('Close Magellan');self.fullscreen_close_button.setFixedSize(22,19);self.fullscreen_close_button.setFocusPolicy(Qt.NoFocus);self.fullscreen_close_button.clicked.connect(self.close)
        self.window_control_backdrops={}
        for _control in (self.minimize_button,self.fullscreen_button,self.fullscreen_close_button):
            _backdrop=QLabel(root);_backdrop.setObjectName('appearanceBackdrop');_backdrop.setAttribute(Qt.WA_StyledBackground,True);_backdrop.setAttribute(Qt.WA_TransparentForMouseEvents,True);_backdrop.hide();self.window_control_backdrops[_control]=_backdrop
        self.fullscreen_close_backdrop=self.window_control_backdrops[self.fullscreen_close_button]
        self.update_window_button();self.update_fullscreen_close_button();main.addWidget(top);line=QFrame();line.setObjectName('line');main.addWidget(line);self.main_bar_line=line;self.main_bar_layout=main
        nav=QFrame();nav.setObjectName('navigation');nav.setAttribute(Qt.WA_StyledBackground,True);self.nav_bar=nav;self.nav_backdrop=QLabel(nav);self.nav_backdrop.setObjectName('appearanceBackdrop');self.nav_backdrop.setAttribute(Qt.WA_TransparentForMouseEvents,True);self.nav_backdrop.lower();nl=QHBoxLayout(nav);nl.setContentsMargins(14,4,14,4);nl.setSpacing(8)
        self.crumb=QLabel();self.crumb.setObjectName('crumb');self.crumb.setFixedWidth(360);self.crumb.setTextInteractionFlags(Qt.TextSelectableByMouse);self.crumb.setCursor(Qt.PointingHandCursor);self.crumb.mousePressEvent=lambda e:self.reveal(self.current) if e.button()==Qt.LeftButton else None;nl.addWidget(self.crumb)
        self.tabbar=SafeTabBar();self.tabbar.setObjectName('navigationTabs');self.tabbar.setAttribute(Qt.WA_TranslucentBackground,True);self.tabbar.setAttribute(Qt.WA_NoSystemBackground,False);self.tabbar.setAttribute(Qt.WA_StyledBackground,True);self.tabbar.setAttribute(Qt.WA_OpaquePaintEvent,False);self.tabbar.setAutoFillBackground(False);self.tabbar.setDrawBase(False);self.tabbar.setExpanding(False);self.tabbar.setMovable(False);self.tabbar.setTabsClosable(False);self.tabbar.closeRequested.connect(self.close_tab);self.tabbar.dragStarted.connect(self.begin_tab_drag);self.tabbar.dragFinished.connect(self.end_tab_drag);self.tabbar.tabMoved.connect(self.tab_reordered);self.tabbar.setDocumentMode(True);self.tabbar.setElideMode(Qt.ElideMiddle);self.tabbar.currentChanged.connect(self.switch_tab);self.tabbar.scrollChanged.connect(self.refresh_tab_bar_appearance);self.tabbar.setMinimumWidth(300);self.tabs_backdrop=QLabel(nav);self.tabs_backdrop.setObjectName('appearanceBackdrop');self.tabs_backdrop.setAttribute(Qt.WA_TransparentForMouseEvents,True);self.tabs_backdrop.lower();nl.addWidget(self.tabbar,1)
        self.status=MarqueeLabel();self.status.setObjectName('status');self.status.setFixedWidth(190);self.status.setAlignment(Qt.AlignRight|Qt.AlignVCenter);nl.addWidget(self.status)
        main.addWidget(nav)
        self.grid=VirtualGrid(self);self.grid.openItem.connect(self.open);self.grid.toggleItemFavorite.connect(self.toggle_item_favorite)
        self.grid.imageSearchRequested.connect(self.perform_image_search);self.grid.middleOpenItem.connect(self.open_new_tab);self.grid.goBack.connect(self.up);self.grid.verticalScrollBar().valueChanged.connect(self.remember_scroll);self.grid.verticalScrollBar().valueChanged.connect(self.schedule_appearance_capture);main.addWidget(self.grid,1)
        self.root_widget=root;root.setMouseTracking(True);self.main_hide_timer=QTimer(self);self.main_hide_timer.setSingleShot(True);self.main_hide_timer.timeout.connect(self.hide_main_panel);self.ctrl_organization_timer=QTimer(self);self.ctrl_organization_timer.setSingleShot(True);self.ctrl_organization_timer.setInterval(50);self.ctrl_organization_timer.timeout.connect(self.enable_ctrl_organization_mode);self.ctrl_organization_held=False;self.ctrl_key_down=False;self.tab_reveal_timer=QTimer(self);self.tab_reveal_timer.setSingleShot(True);self.tab_reveal_timer.setInterval(2000);self.tab_reveal_timer.timeout.connect(self.finish_tab_reveal);self.titlebar_hover_timer=QTimer(self);self.titlebar_hover_timer.setInterval(30);self.titlebar_hover_timer.timeout.connect(self.check_titlebar_hover);self.tree_hide_timer=QTimer(self);self.tree_hide_timer.setSingleShot(True);self.tree_hide_timer.timeout.connect(self.hide_tree_overlay);self.tab_context_menu_open=False;self.tab_context_menu_timer=QTimer(self);self.tab_context_menu_timer.setInterval(50);self.tab_context_menu_timer.timeout.connect(self.close_tab_context_menu_if_cursor_left)
        self.favorite_overlay_timer=QTimer(self);self.favorite_overlay_timer.setSingleShot(True);self.favorite_overlay_timer.setInterval(500);self.favorite_overlay_timer.timeout.connect(self.hide_favorite_controls)
        self.favorite_picker_close_timer=QTimer(self);self.favorite_picker_close_timer.setInterval(20);self.favorite_picker_close_timer.timeout.connect(self.check_favorite_picker_dismiss);self.favorite_picker_leave_since=None
        self.favorite_group_overlay=QToolButton(root);self.favorite_group_overlay.setText('＋');self.favorite_group_overlay.setToolTip('Create a favorites group');self.favorite_group_overlay.setFixedSize(58,36);self.favorite_group_overlay.clicked.connect(self.create_favorite_group);self.favorite_group_overlay.hide()
        self.favorite_organize_overlay=QToolButton(root);self.favorite_organize_overlay.setText('↔');self.favorite_organize_overlay.setToolTip('Organization mode (Ctrl)');self.favorite_organize_overlay.setCheckable(True);self.favorite_organize_overlay.setFixedSize(58,36);self.favorite_organize_overlay.toggled.connect(self.set_favorite_organization_mode);self.favorite_organize_overlay.hide()
        self.selection_clear_overlay=QToolButton(root);self.selection_clear_overlay.setText('×');self.selection_clear_overlay.setToolTip('Clear selection');self.selection_clear_overlay.setFixedSize(58,36);self.selection_clear_overlay.clicked.connect(lambda:self.grid.clear_selection());self.selection_clear_overlay.hide()
        self.selection_all_overlay=QToolButton(root);self.selection_all_overlay.setText('☑');self.selection_all_overlay.setToolTip('Select all items on this page (Ctrl+A)');self.selection_all_overlay.setFixedSize(58,36);self.selection_all_overlay.clicked.connect(self.grid.select_all_current);self.selection_all_overlay.hide()
        self.selection_undo_overlay=QToolButton(root);self.selection_undo_overlay.setText('↶');self.selection_undo_overlay.setToolTip('Undo last deselection (Ctrl+S)');self.selection_undo_overlay.setFixedSize(58,36);self.selection_undo_overlay.clicked.connect(self.grid.undo_deselection);self.selection_undo_overlay.hide()
        self.right_control_backdrops={}
        for _control in (self.favorite_group_overlay,self.favorite_organize_overlay,self.selection_clear_overlay,self.selection_all_overlay,self.selection_undo_overlay):
            _backdrop=QLabel(root);_backdrop.setObjectName('appearanceBackdrop');_backdrop.setAttribute(Qt.WA_StyledBackground,True);_backdrop.setAttribute(Qt.WA_TransparentForMouseEvents,True);_backdrop.hide();self.right_control_backdrops[_control]=_backdrop
        self.set_button_asset(self.favorite_group_overlay,'createfavoritegroup.png');self.set_button_asset(self.favorite_organize_overlay,'organizationmode.png');self.set_button_asset(self.selection_clear_overlay,'clearselect.png');self.set_button_asset(self.selection_undo_overlay,'undo.png')
        self.grid.verticalScrollBar().rangeChanged.connect(lambda *_:self.position_right_controls())
        self.activity_overlay=ActivityOverlay(root);self.activity_overlay.cancel_requested.connect(self.cancel_file_operation);self.activity_overlay.raise_()
        self.operation_show_timer=QTimer(self);self.operation_show_timer.setSingleShot(True);self.operation_show_timer.setInterval(220);self.operation_show_timer.timeout.connect(self.show_pending_operation)
        self._pending_operation_text=''
        self.apply_appearance_settings(persist=False)
        self.update_selection_button()
    def toggle_selection(self):
        self.grid.clear_selection()
        self.grid.setFocus()
    def update_selection_button(self):
        if not hasattr(self,'selection_clear_overlay'):return
        count=len(getattr(self.grid,'selected_paths',())) if hasattr(self,'grid') else 0
        if count==0:self.clear_clipboard_feedback()
        self.selection_clear_overlay.setToolTip(f'Clear selection ({count} selected)' if count else 'Clear selection')
        if hasattr(self,'farviewbtn'):self.farviewbtn.setToolTip('Create a Farview tab from the selected files and folders' if count else f'Create a Farview tab from {self.current} and its image subfolders')
        if hasattr(self,'activity_overlay'):
            files=folders=total=unknown=0;pending_sizes=[]
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
                        if str(key) not in self.grid.folder_size_queued and str(key) not in self.grid.folder_size_futures and str(key) not in self.grid.hover_folder_size_futures:pending_sizes.append(str(key))
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
            self._pending_selection_sizes=deque(pending_sizes)
            timer=getattr(self,'selection_size_timer',None)
            if timer is None:
                timer=QTimer(self);timer.setSingleShot(True);timer.timeout.connect(self.start_selected_size_requests);self.selection_size_timer=timer
            if self.grid.selection_dragging or not pending_sizes:timer.stop()
            elif not timer.isActive():timer.start(75)

    def start_selected_size_requests(self):
        if self._closing or self.grid.selection_dragging:return
        pending=getattr(self,'_pending_selection_sizes',deque());deadline=time.perf_counter()+.002;count=0
        while pending and count<32 and time.perf_counter()<deadline:
            key=pending.popleft();count+=1
            if key not in self.grid.selected_paths:continue
            item=next(iter(self.grid.items_by_path.get(key,())),None)
            if item is None:
                metadata=self.grid.selected_metadata.get(key,{})
                if metadata.get('is_dir'):item={'path':key,'is_dir':True,'size':metadata.get('size')}
            if item is not None and item.get('is_dir'):self.grid.request_folder_size(item)
        if pending:self.selection_size_timer.start(16)

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
    def clear_clipboard_feedback(self):
        overlay=getattr(self,'activity_overlay',None)
        if overlay is not None and getattr(overlay,'clipboard_text',''):
            overlay.set_clipboard('');self.position_activity_overlay()
    def selected_files_for_operation(self):
        selected=set(getattr(getattr(self,'grid',None),'selected_paths',set()));paths=[];seen=set()
        for item in getattr(self,'items',[]):
            raw=str(item.get('path',''))
            if raw not in selected:continue
            if item.get('archive_virtual'):
                decoded=decode_archive_path(raw)
                if not decoded or decoded[2] or decode_pdf_page_member(decoded[1]) is not None:continue
                key='archive:'+raw
                if key not in seen:paths.append(raw);seen.add(key)
                continue
            path=Path(raw);key=os.path.normcase(os.path.abspath(str(path)))
            if key not in seen and path.exists():paths.append(str(path));seen.add(key)
        return paths
    def copy_selected_files(self,cut=False):
        return self.copy_paths(self.selected_files_for_operation(),cut=cut)
    def copy_paths(self,paths,cut=False):
        paths=list(dict.fromkeys(str(path) for path in paths if str(path)))
        if cut:paths=[path for path in paths if decode_archive_path(path) is None]
        if not paths:
            archive_selected=any(decode_archive_path(str(path)) for path in getattr(self.grid,'selected_paths',set()))
            self.status.setText('Files inside archives can be copied, not cut' if cut and archive_selected else 'Select files or folders first');return
        self.file_clipboard=paths;self.file_clipboard_cut=bool(cut);self.file_clipboard_set={os.path.normcase(os.path.abspath(path)) for path in paths} if cut else set()
        noun='file' if len(paths)==1 else 'files';message=f'{len(paths)} {noun} being cut' if cut else f'{len(paths)} {noun} copied'
        clipboard_feedback=f'{len(paths)} {noun} being cut' if cut else f'{len(paths)} {noun} being copied'
        self.status.setText(message)
        if hasattr(self,'activity_overlay'):self.activity_overlay.set_clipboard(clipboard_feedback);self.position_activity_overlay()
        self.grid.viewport().update()
    def paste_files(self,destination=None):
        if not self.file_clipboard:self.status.setText('Magellan clipboard is empty');return
        if self.in_favorites_view or getattr(self,'in_archive_view',False):self.status.setText('Paste is available in a normal folder view');return
        if self.file_operation_task is not None and self.file_operation_task.isRunning():self.status.setText('Another file operation is still running');return
        target=Path(destination) if destination is not None else self.current
        if not target.is_dir():self.status.setText('The current destination folder is unavailable');return
        self.start_file_operation('move' if self.file_clipboard_cut else 'copy',self.file_clipboard,target)
    def extract_selected_files(self):
        paths=self.selected_files_for_operation();archives=[path for path in paths if Path(path).suffix.casefold() in COMIC_EXTS]
        if not archives:self.status.setText('Select one or more ZIP or RAR files to extract');return
        if self.in_favorites_view or getattr(self,'in_archive_view',False):self.status.setText('Extraction is available in a normal folder view');return
        if self.file_operation_task is not None and self.file_operation_task.isRunning():self.status.setText('Another file operation is still running');return
        dialog=ExtractionDialog(self,archives);dialog.confirmed.connect(lambda:self.start_file_operation('extract',archives,self.current));dialog.adjustSize();dialog.move(self.mapToGlobal(self.rect().center())-dialog.rect().center());dialog.exec()
    def start_file_operation(self,operation,sources,destination,dialog=None):
        if self.file_operation_task is not None and self.file_operation_task.isRunning():self.status.setText('Another file operation is still running');return
        message={'copy':'Copying selected items…','move':'Moving selected items…','extract':'Extracting selected archives…','undo_move':'Undoing the last move…'}.get(operation,'Working…')
        self.begin_operation(message);task=FileOperationTask(operation,sources,destination);self.file_operation_task=task
        task.signals.progress.connect(self.update_file_operation_progress)
        if dialog is not None:
            task.signals.progress.connect(dialog.set_progress);dialog.cancel_requested.connect(task.requestInterruption)
        def completed(completed_items,errors,op=operation,job=task,progress_dialog=dialog):
            # A plain Python callback may run in the emitting worker thread.
            # Forward completion through a QObject signal so all widget and
            # view-state updates happen on the GUI thread.
            self.file_operation_bridge.completed.emit(completed_items,errors,op,job,progress_dialog)
        task.signals.done.connect(completed);task.finished.connect(lambda job=task:self.file_operation_bridge.task_finished.emit(job));task.start()
    def _complete_file_operation(self,completed,errors,operation,task,progress_dialog):
        self.finish_file_operation(completed,errors,operation,bool(getattr(task,'cancelled',False)))
        if progress_dialog is not None:
            try:progress_dialog.finish(completed,errors,bool(getattr(task,'cancelled',False)))
            except RuntimeError:logger.debug('Extraction progress dialog was already closed')
    def update_file_operation_progress(self,message):
        task=self.file_operation_task
        self._pending_operation_text='Cancelling after the current item…' if task is not None and task.isInterruptionRequested() else str(message)
        if hasattr(self,'activity_overlay') and self.activity_overlay.operation_text:self.activity_overlay.set_operation(self._pending_operation_text);self.position_activity_overlay()
    def release_file_operation_task(self,task=None):
        if task is None or self.file_operation_task is task:self.file_operation_task=None
    def cancel_file_operation(self):
        task=self.file_operation_task
        self.ctrl_organization_timer.stop();self.ctrl_key_down=False;self.ctrl_organization_held=False
        if getattr(self,'favorite_organize_mode',False):self.set_favorite_organization_mode(False)
        if task is None or not task.isRunning():return
        task.requestInterruption()
        if hasattr(self,'activity_overlay') and self.activity_overlay.operation_text:
            self.activity_overlay.set_operation('Cancelling after the current item…');self.position_activity_overlay()
    def finish_file_operation(self,completed,errors,operation=None,cancelled=False):
        if operation is None:
            task=self.file_operation_task;operation=task.operation if task is not None else 'file operation'
        self.finish_operation()
        if operation=='move' and completed:self.push_undo_record({'kind':'move','items':[(str(source),str(target)) for source,target in completed]})
        elif operation=='undo_move' and completed:
            restored={str(source) for source,_target in completed};record=getattr(self,'_undo_record_pending',None)
            if record:
                record['items']=[(source,target) for source,target in record.get('items',[]) if target not in restored]
                if not record['items'] and record in getattr(self,'undo_history',[]):self.undo_history.remove(record)
            self.invalidate_folder_sizes([path for pair in completed for path in pair])
        if operation=='undo_move':self._undo_record_pending=None
        if operation in ('move','copy','extract') and completed:self.grid.deselect_paths([source for source,_target in completed])
        if operation=='move' and self.file_clipboard_cut:
            failed={source for source,_message in errors};self.file_clipboard=[path for path in self.file_clipboard if path in failed]
            if not self.file_clipboard:self.file_clipboard_cut=False
            self.file_clipboard_set={os.path.normcase(os.path.abspath(path)) for path in self.file_clipboard} if self.file_clipboard_cut else set()
            self.grid.viewport().update()
        if operation in ('copy','move'):self.clear_clipboard_feedback()
        for source,message in errors:logger.warning('File operation %s could not process %s: %s',operation,source,message)
        operation_label={'copy':'Copy','move':'Move','extract':'Extraction','undo_move':'Undo move'}.get(operation,str(operation).title())
        if completed:self.status.setText(f'{operation_label} completed for {len(completed)} item(s)'+(f'; {len(errors)} failed' if errors else ''))
        elif cancelled:self.status.setText(f'{operation_label} cancelled')
        elif errors:self.status.setText(f'{operation_label} failed: {errors[0][1]}')
        if completed and not self.in_favorites_view and not getattr(self,'in_archive_view',False) and self.current.is_dir():self.load(self.current,restore_scroll=True,save_previous=False)
    def push_undo_record(self,record):
        history=getattr(self,'undo_history',None)
        if not isinstance(history,list):history=[];self.undo_history=history
        record['time']=time.monotonic();history.append(record)
        if len(history)>5:del history[:-5]
        return record
    def rename_path(self,raw_path,select_name=False):
        self.grid.begin_inline_rename(str(raw_path))
    def apply_inline_rename(self,raw_path,name,new_folder=False):
        path=Path(raw_path)
        if not path.exists():self.status.setText('The selected item is no longer available');return False
        name=str(name).strip()
        if not name or name in ('.','..') or re.search(r'[<>:"/\\|?*]',name) or name.endswith(('.',' ')):
            self.status.setText('Enter a valid Windows file or folder name');return False
        target=path.with_name(name);old_key=self._normalized_path_key(path);target_key=self._normalized_path_key(target)
        if target.exists() and target_key!=old_key:
            self.status.setText(f'{target.name} already exists in this folder');return False
        if target.name!=path.name:
            try:path.rename(target)
            except OSError as error:
                logger.warning('Could not rename %s: %s',path,error,exc_info=True);self.status.setText(f'Could not rename item: {error}');return False
        for collection_name in ('favorites','tree_favorites','pinned_folders'):
            collection=getattr(self,collection_name,set())
            setattr(self,collection_name,{str(target) if self._normalized_path_key(value)==old_key else value for value in collection})
        for group in self.favorite_groups:group['folders']=[str(target) if self._normalized_path_key(value)==old_key else value for value in group.get('folders',[])]
        for item in getattr(self,'items',[]):
            if self._normalized_path_key(item.get('path',''))==old_key:item['path']=target;item['name']=target.name
        for mapping_name in ('custom','pinned_thumbs','pinned_folder_sizes','pinned_folder_sizes_checked'):
            mapping=getattr(self,mapping_name,None)
            if isinstance(mapping,dict) and str(path) in mapping:mapping[str(target)]=mapping.pop(str(path))
        if hasattr(self,'grid'):
            for mapping_name in ('folder_cache','folder_size_cache','folder_size_cache_checked_at'):
                mapping=getattr(self.grid,mapping_name,None)
                if isinstance(mapping,dict) and str(path) in mapping:mapping[str(target)]=mapping.pop(str(path))
            self.grid.clear_card_cache()
            if str(path) in self.grid.selected_paths:
                self.grid.selected_paths.discard(str(path));self.grid.selected_paths.add(str(target));self.grid.selection_order=[str(target) if key==str(path) else key for key in self.grid.selection_order]
        self.file_clipboard=[str(target) if self._normalized_path_key(value)==old_key else value for value in self.file_clipboard]
        self.file_clipboard_set={os.path.normcase(os.path.abspath(value)) for value in self.file_clipboard} if self.file_clipboard_cut else set()
        if getattr(self,'pending_new_folder_path',None)==str(path):self.pending_new_folder_path=None
        self.save_settings();self.status.setText(f'Renamed to {target.name}')
        if self.in_favorites_view:self.refresh_favorite_view()
        elif not getattr(self,'in_archive_view',False):self.refresh()
        def reveal_renamed_item(path_value=str(target)):
            index=next((i for i,item in enumerate(self.grid.items) if self._normalized_path_key(item.get('path',''))==self._normalized_path_key(path_value)),-1)
            if index>=0:self.grid.ensure_navigation_visible(index)
        if not self.in_favorites_view and not getattr(self,'in_archive_view',False):QTimer.singleShot(80,reveal_renamed_item)
        return True
    def create_new_folder(self):
        if self.in_favorites_view or getattr(self,'in_archive_view',False):self.status.setText('Create a folder inside a regular folder view');return
        if not self.current.is_dir():self.status.setText('The current folder is unavailable');return
        pending=getattr(self,'pending_new_folder_path',None)
        if pending and Path(pending).exists():
            self.grid.begin_inline_rename(pending,new_folder=True);return
        base='New folder';candidate=self.current/base;index=1
        while candidate.exists():candidate=self.current/f'{base} ({index})';index+=1
        try:candidate.mkdir()
        except OSError as error:
            logger.warning('Could not create folder in %s: %s',self.current,error,exc_info=True);QMessageBox.warning(self,'Create folder failed',str(error));return
        self.pending_new_folder_path=str(candidate)
        if self.current.is_dir():self.load(self.current,restore_scroll=True,save_previous=False)
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
        if getattr(self,'_appearance_preview_active',False):return
        for widget in self.right_control_widgets():widget.hide()
        for backdrop in getattr(self,'right_control_backdrops',{}).values():backdrop.hide()
    def right_control_widgets(self):
        names=('favorite_group_overlay','favorite_organize_overlay','selection_clear_overlay','selection_all_overlay','selection_undo_overlay')
        return [getattr(self,name) for name in names if hasattr(self,name)]
    def position_right_controls(self):
        if not hasattr(self,'selection_clear_overlay'):return
        scrollbar=getattr(self.grid,'overlay_scrollbar',self.grid.verticalScrollBar());button_width=58;gap=4;x=self.root_widget.width()-button_width-gap
        if scrollbar.maximum()>scrollbar.minimum():
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
        self.refresh_right_control_backdrops()
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
        if self._closing or self._replacing_external_viewer:return
        if getattr(self,'_ui_deferred',False):
            QTimer.singleShot(0,lambda:self.close() if self._ui_deferred and self.viewer is None else None)
            return
        if not self.auto_hide_tree and getattr(self,'tree_was_visible_before_viewer',False) and hasattr(self,'tree_dock'):self.tree_dock.show();self.refresh_file_tree()
        self.tree_was_visible_before_viewer=False
        if self._deferred_startup_maintenance:QTimer.singleShot(900,self.resume_deferred_startup_maintenance)
    def resume_deferred_startup_maintenance(self):
        if self._closing or not self._deferred_startup_maintenance:return
        viewer=getattr(self,'viewer',None)
        if viewer is not None:
            try:
                if viewer.isVisible():
                    QTimer.singleShot(900,self.resume_deferred_startup_maintenance);return
            except RuntimeError:pass
        self._deferred_startup_maintenance=False;self.run_startup_maintenance()
    def run_startup_maintenance(self):
        if self._closing:return
        if self.disk_thumbnail_cache_limit_enabled:self.schedule_disk_thumbnail_cache_trim()
        for folder in list(self.pinned_folders):
            cached=self.grid.load_persistent_folder_thumb(folder)
            if cached:
                self.pinned_thumbs[folder]=cached;self.grid.folder_cache[folder]=cached
            elif Path(folder).is_dir():
                future=self.grid.folder_pool.submit(self.grid.resolve_folder_thumbnail,folder)
                def finished(result,folder_path=folder):
                    try:
                        source=result.result()
                        if source:self._folder_thumb_bridge.completed.emit(folder_path,source)
                    except Exception:
                        if not result.cancelled():logger.exception('Could not warm pinned folder thumbnail for %s',folder_path)
                future.add_done_callback(finished)
    def set_favorite_organization_mode(self,enabled):
        self.favorite_organize_mode=bool(enabled);self.favorite_organize_overlay.setToolTip('Organization mode (Ctrl)')
        if self.favorite_organize_overlay.isChecked()!=self.favorite_organize_mode:self.favorite_organize_overlay.setChecked(self.favorite_organize_mode)
        self.grid.viewport().update()

    def enable_ctrl_organization_mode(self):
        active=QApplication.activeWindow()
        active_main=active is self or (active is not None and self.isAncestorOf(active))
        if self.ctrl_key_down and active_main and QApplication.activeModalWidget() is None:
            self.ctrl_organization_held=True;self.set_favorite_organization_mode(True)
        elif not self.ctrl_key_down:
            self.ctrl_key_down=False;self.ctrl_organization_held=False;self.set_favorite_organization_mode(False)

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
        dlg=QDialog(self);dlg.setWindowTitle('Thumbnail memory cache');self.prepare_settings_dialog(dlg,'thumbnail_memory_cache',(520,300));layout=QVBoxLayout(dlg)
        row=QHBoxLayout();row.addWidget(QLabel('Maximum thumbnail memory'));limit=QSpinBox();limit.setRange(16,8192);limit.setSingleStep(256);limit.setSuffix(' MiB');limit.setValue(self.thumbnail_cache_limit_mib);row.addWidget(limit);layout.addLayout(row)
        note=QLabel('The cache grows only as thumbnails are used. Lowering the limit immediately removes the least recently used thumbnails. The SSD cache for pinned folder previews is separate.');note.setWordWrap(True);layout.addWidget(note)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel);buttons.accepted.connect(dlg.accept);buttons.rejected.connect(dlg.reject);layout.addWidget(buttons)
        if dlg.exec()==QDialog.Accepted:
            self.thumbnail_cache_limit_mib=limit.value();self.grid.set_cache_limit_mib(self.thumbnail_cache_limit_mib);self.thumbnail_cache_limit_action.setText(f'Memory thumbnail cache limit… ({self.thumbnail_cache_limit_mib} MiB)');self.save_settings()
    def configure_disk_thumbnail_cache_limit(self):
        dlg=QDialog(self);dlg.setWindowTitle('On-disk thumbnail cache limit');self.prepare_settings_dialog(dlg,'disk_thumbnail_cache',(520,300));layout=QVBoxLayout(dlg)
        row=QHBoxLayout();row.addWidget(QLabel('Maximum cached thumbnails'));limit=QSpinBox();limit.setRange(256,262144);limit.setSingleStep(1024);limit.setSuffix(' MiB');limit.setValue(self.disk_thumbnail_cache_limit_mib);row.addWidget(limit);layout.addLayout(row)
        note=QLabel('The limiter is off by default. When enabled, only generated folder previews and file thumbnails in Magellan’s thumbnail cache are eligible for removal, starting with the oldest files. Removed previews are regenerated when needed.');note.setWordWrap(True);layout.addWidget(note)
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
                            if not re.fullmatch(r'(?:[0-9a-f]{40}\.jpg|file-[0-9a-f]{64}\.png)',entry.name,re.IGNORECASE):continue
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
        dlg=QDialog(self);dlg.setWindowTitle('Application log storage');self.prepare_settings_dialog(dlg,'log_storage',(520,280));layout=QVBoxLayout(dlg)
        row=QHBoxLayout();row.addWidget(QLabel('Maximum total log storage'));limit=QSpinBox();limit.setRange(1,64);limit.setSingleStep(1);limit.setSuffix(' MiB');limit.setValue(self.log_storage_mib);row.addWidget(limit);layout.addLayout(row)
        note=QLabel('Magellan keeps the current log and one rotated log. Repeated identical messages are coalesced, and older log data is trimmed when you lower this limit.');note.setWordWrap(True);layout.addWidget(note)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel);buttons.accepted.connect(dlg.accept);buttons.rejected.connect(dlg.reject);layout.addWidget(buttons)
        if dlg.exec()==QDialog.Accepted:
            self.log_storage_mib=limit.value();configure_logging(self.cache_dir/'logs',self.log_storage_mib);self.log_storage_action.setText(f'Maximum log storage… ({self.log_storage_mib} MiB)');self.save_settings()
    def set_gpu_thumbnail_rendering_enabled(self,enabled):
        self.gpu_thumbnail_rendering_enabled=bool(enabled);self.save_settings()
        if hasattr(self,'status'):self.status.setText('Restart Magellan to apply the thumbnail rendering change')

    def set_gpu_blur_enabled(self,enabled):
        self.close_gpu_blur_backend();self.gpu_blur_enabled=bool(enabled)
        self.clear_appearance_preview_cache();self.save_settings()
        if hasattr(self,'appearance_capture_timer'):self.appearance_capture_timer.start(0)
        if hasattr(self,'status'):self.status.setText('Experimental GPU panel blur on' if enabled else 'Regular panel blur restored')

    def set_file_thumbnail_disk_cache_enabled(self,enabled):
        self.file_thumbnail_disk_cache_enabled=bool(enabled);self.save_settings()

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
        self.ensure_fullscreen_minimize_style()
        origin=self.root_widget.mapToGlobal(QPoint(0,0))
        self.tree_dock.setFixedSize(300,self.root_widget.height())
        self.tree_dock.setGeometry(origin.x(),origin.y(),300,self.root_widget.height())
        self.tree_dock.show();self.tree_dock.raise_();self.tree_hide_timer.stop()
        self.schedule_tree_scrollbar_refresh()
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
        self.tree_dock.visibilityChanged.connect(lambda visible:(QTimer.singleShot(0,self.sync_tree_appearance_geometry),self.schedule_tree_scrollbar_refresh()) if visible else None)
        self.tree_dock.setAttribute(Qt.WA_StyledBackground,True);self.tree_dock.setAttribute(Qt.WA_TranslucentBackground,True);self.tree_dock.setAutoFillBackground(False)
        self.tree_dock.setAllowedAreas(Qt.LeftDockWidgetArea)
        self.tree_dock.setFeatures(QDockWidget.DockWidgetClosable)
        self.tree_dock.setFixedWidth(300)
        self.tree_backdrop=QLabel(self.tree_dock);self.tree_backdrop.setObjectName('appearanceTreeBackdrop');self.tree_backdrop.setAttribute(Qt.WA_StyledBackground,True);self.tree_backdrop.setAttribute(Qt.WA_TransparentForMouseEvents,True);self.tree_backdrop.lower()
        self.tree_blur_backdrop=QLabel(self.tree_backdrop);self.tree_blur_backdrop.setObjectName('appearanceBackdrop');self.tree_blur_backdrop.setAttribute(Qt.WA_StyledBackground,True);self.tree_blur_backdrop.setAttribute(Qt.WA_TransparentForMouseEvents,True);self.tree_blur_backdrop.lower()
        self.tree_gradient_overlay=QLabel(self.tree_backdrop);self.tree_gradient_overlay.setObjectName('appearanceTreeGradient');self.tree_gradient_overlay.setAttribute(Qt.WA_StyledBackground,True);self.tree_gradient_overlay.setAttribute(Qt.WA_TransparentForMouseEvents,True);self.tree_gradient_overlay.hide()
        title=FixedTreeTitleBar(self.tree_dock);title_row=QHBoxLayout(title);title_row.setContentsMargins(8,2,4,2);title_row.setSpacing(4)
        title_label=QLabel('File Tree',title);title_label.setStyleSheet('color:#eee;font-weight:600;');title_row.addWidget(title_label);title_row.addStretch(1)
        title.setObjectName('fileTreeTitleBar');self.tree_titlebar=title
        close_button=QToolButton(title);close_button.setObjectName('treeCloseButton');close_button.setText('×');close_button.setToolTip('Close file tree');close_button.setFixedSize(28,26);close_button.setStyleSheet('QToolButton#treeCloseButton{min-width:0;min-height:0;padding:0;}');close_button.clicked.connect(self.close_file_tree_panel);title_row.addWidget(close_button);self.tree_dock.setTitleBarWidget(title)
        self.file_tree=QTreeWidget()
        self.file_tree.setAttribute(Qt.WA_StyledBackground,True);self.file_tree.setAttribute(Qt.WA_TranslucentBackground,True);self.file_tree.setAutoFillBackground(False)
        # Mouse tracking is required to reveal the hidden scrollbar when the
        # pointer reaches its edge; otherwise Qt sends no move events over an
        # idle QTreeWidget until a button is pressed.
        self.file_tree.setMouseTracking(True)
        self.file_tree.viewport().setAttribute(Qt.WA_StyledBackground,True);self.file_tree.viewport().setAttribute(Qt.WA_TranslucentBackground,True);self.file_tree.viewport().setAutoFillBackground(False)
        self.file_tree.viewport().setMouseTracking(True)
        self._tree_generation=0;self._tree_scan_active=set();self._tree_bridge=TreeScanBridge(self);self._tree_bridge.completed.connect(self.finish_tree_scan)
        self.file_tree.setHeaderHidden(True)
        self.file_tree.setIndentation(16)
        # Directory scans can complete out of order; keep the tree response
        # immediate and avoid long expand/collapse transitions on large roots.
        self.file_tree.setAnimated(False)
        self.file_tree.itemExpanded.connect(self.expand_tree_item)
        self.file_tree.itemExpanded.connect(self.remember_tree_expanded)
        self.file_tree.itemCollapsed.connect(self.remember_tree_collapsed)
        self.file_tree.currentItemChanged.connect(self.remember_tree_selection)
        self.file_tree.verticalScrollBar().valueChanged.connect(self.remember_tree_scroll)
        self.file_tree.verticalScrollBar().sliderPressed.connect(self.begin_tree_scrollbar_interaction)
        self.file_tree.verticalScrollBar().sliderReleased.connect(self.finish_tree_scrollbar_interaction)
        self._tree_click_press=None;self.file_tree.itemClicked.connect(self.file_tree_clicked);self.file_tree.itemDoubleClicked.connect(self.file_tree_double_clicked)
        self.file_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.file_tree.customContextMenuRequested.connect(self.file_tree_context_menu)
        self.tree_dock.setWidget(self.file_tree)
        self.addDockWidget(Qt.LeftDockWidgetArea,self.tree_dock)
        self.register_scrollbars(self.tree_dock)
        self.apply_appearance_settings(persist=False)
        QTimer.singleShot(0,self.sync_tree_appearance_geometry)
        self.refresh_file_tree()

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
        if not hasattr(self,'file_tree'): return
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
        self.file_tree.setStyleSheet('')
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
            # Restoring the selected row must not expand it. Selection records
            # where the user last navigated in the tree; only the separately
            # persisted expanded-path set records which folders they opened.
            should_expand=path in self.tree_expanded_paths
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
        self.schedule_tree_scrollbar_refresh()

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
            except PermissionError:
                # Protected/system folders are common in drive roots. Treat
                # access denial as an expected empty branch, not an app error.
                logger.info('Skipping inaccessible File Tree folder %s',path_text)
                error='Access denied'
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

    def _tree_label_hit(self,item,point):
        if item is None:return False
        row=self.file_tree.visualItemRect(item)
        if not row.contains(point):return False
        depth=0;parent=item.parent()
        while parent is not None:depth+=1;parent=parent.parent()
        label_left=(depth+1)*self.file_tree.indentation()+8;label_right=label_left+self.file_tree.fontMetrics().horizontalAdvance(item.text(0))
        return label_left<=point.x()<=label_right

    def file_tree_clicked(self,item,column):
        if item is None or item.treeWidget() is not self.file_tree:return
        if item.data(0,TREE_PATH_ROLE)=='__more__':
            parent=item.parent()
            if parent is not None:
                parent.removeChild(item);self.append_tree_page(parent)
            return
        path=self._tree_path(item)
        if not path or not path.is_dir():return
        if self._normalized_path_key(path)==self._normalized_path_key(self.current):return
        self.tree_selected_path=str(path);self.save_settings();self.load(path)

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

    def tab_backdrop_shape_region(self,rect):
        """Return the visible tab fill, excluding its rounded corners and CSS margin."""
        if rect.isEmpty():return QRegion()
        # Qt's tabRect includes QTabBar::tab's 2 px right margin. That strip
        # belongs to the Main Panel and must not receive the Tabs backdrop.
        margin_right=2.0
        bounds=QRectF(rect);left=bounds.left();right=bounds.left()+max(0.0,bounds.width()-margin_right);top=bounds.top();bottom=bounds.top()+bounds.height()
        radius=min(5.0,bounds.width()/2.0,bounds.height())
        path=QPainterPath();path.moveTo(left,bottom);path.lineTo(left,top+radius);path.quadTo(left,top,left+radius,top);path.lineTo(right-radius,top);path.quadTo(right,top,right,top+radius);path.lineTo(right,bottom);path.closeSubpath()
        return QRegion(path.toFillPolygon().toPolygon())

    def refresh_tab_bar_appearance(self):
        """Refresh the Main Panel backplate after tab geometry changes."""
        tabbar=getattr(self,'tabbar',None)
        backdrop=getattr(self,'tabs_backdrop',None)
        if tabbar is None or backdrop is None:return
        # Re-seed from the shared Main Panel layer immediately. The next
        # debounced capture adds tab-only blurred thumbnail samples, clipped
        # to each tab shape, without leaving an old tab image in a gap.
        backdrop.setPixmap(QPixmap())
        main_backdrop=getattr(self,'main_panel_backdrop',None)
        if main_backdrop is not None and main_backdrop.isVisible() and tabbar.width()>0 and tabbar.height()>0:
            main_image=main_backdrop.grab();main_global=QRect(main_backdrop.mapToGlobal(QPoint(0,0)),main_backdrop.size())
            tab_global=QRect(tabbar.mapToGlobal(QPoint(0,0)),tabbar.size());crop=tab_global.translated(-main_global.topLeft()).intersected(main_image.rect())
            if crop.size()==tabbar.size():backdrop.setPixmap(main_image.copy(crop))
        backdrop.clearMask()
        backdrop.update()
        tabbar.updateGeometry();tabbar.update()
        nav=getattr(self,'nav_bar',None)
        if nav is not None:nav.update()
        timer=getattr(self,'appearance_capture_timer',None)
        if timer is not None:timer.start()

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
        tab_name='New tab' if data['kind']=='empty' else (Path(data['archive']).name if data['kind']=='archive' and data.get('archive') else (folder.name or str(folder)))
        idx=self.tabbar.addTab(prefix+tab_name)
        self.tabbar.setTabData(idx,data.get('color',''))
        self.tabbar.setTabToolTip(idx,('Farview selection from chosen items' if data.get('selection') else ('Farview of ' if data['kind']=='farview' else ''))+(str(data['archive']) if data['kind']=='archive' and data.get('archive') else str(folder)))
        if select:
            self.tabbar.setCurrentIndex(idx)
        self.refresh_tab_bar_appearance()

    def restore_tabs(self,activate=True):
        records=[]
        for value in self.saved_tabs:
            if not isinstance(value,dict):continue
            path=Path(value.get('path',''))
            if path.is_dir() and (value.get('kind')!='archive' or (Path(value.get('archive','')).is_file() and Path(value.get('archive','')).suffix.casefold() in COMIC_EXTS)):records.append((path,value))
        self.tabbar.blockSignals(True)
        try:
            for path,value in records:self.add_tab(path,tab=value)
            if not self.tabs and not getattr(self,'has_saved_tabs',False):self.add_tab(self.current)
            active=max(0,min(self.saved_active_tab,len(self.tabs)-1)) if self.tabs else -1
            self.tabbar.setCurrentIndex(active)
        finally:self.tabbar.blockSignals(False)
        if self.tabs and activate:self.activate_tab(self.tabbar.currentIndex(),save_previous=False)
        elif self.tabs:self.active_tab_index=self.tabbar.currentIndex();self.grid.setItems([]);self.crumb.setText('Magellan');self.status.setText('Opening image…')
        else:self.grid.setItems([]);self.crumb.setText('Magellan');self.status.setText('No open tabs')

    def activate_tab(self, idx, save_previous=True):
        if idx<0 or idx>=len(self.tabs): return
        if save_previous:self.save_current_view()
        self.return_to_favorites=False;self.favorites_navigation_root=None;self.favorite_return_group=None
        self.active_tab_index=idx
        tab=self.tabs[idx];target=Path(tab['path']);self.current=target
        if tab.get('kind')=='empty':
            self.current=target;self.in_archive_view=False;self.archive_path=None;self.archive_prefix='';self.in_favorites_view=False;self.items=[];self.grid.setItems([]);self.crumb.setText('Magellan');self.status.setText('Empty tab');self.update_favorite_button();self.update_sort_buttons();self.update_tab_labels();return
        if tab.get('kind')=='archive':
            archive=Path(tab.get('archive',''))
            if target.is_dir() and archive.is_file() and archive.suffix.casefold() in COMIC_EXTS:
                self.sort=tab.get('sort',self.sort);self.desc=bool(tab.get('desc',self.desc));self.load(target,restore_scroll=True,save_previous=False)
                tab=self.tabs[idx];tab['kind']='archive';tab['archive']=str(archive);self.archive_return_folder=target;self.archive_return_favorites=False;self.archive_return_group=None;self.archive_navigation_stack=[];self._archive_auto_flatten=False;self.update_tab_labels();self.browse_archive(archive,tab.get('archive_prefix',''),remember_return=False)
            else:
                tab['kind']='folder';tab.pop('archive',None);tab.pop('archive_prefix',None);self.load(target,restore_scroll=True,save_previous=False);self.update_sort_buttons();self.update_tab_labels()
        elif tab.get('kind')=='farview':
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
        self.active_tab_index=self.tabbar.currentIndex();self.save_settings();self.refresh_tab_bar_appearance()

    def close_tab(self, idx):
        if idx is None or idx<0 or idx>=len(self.tabs): return
        now=time.monotonic()
        if getattr(self,'_last_tab_close',None) and self._last_tab_close[0]==idx and now-self._last_tab_close[1]<.18:return
        self._last_tab_close=(idx,now)
        self.save_current_view()
        if not hasattr(self,'closed_tabs'):self.closed_tabs=[]
        self.closed_tabs.append(copy.deepcopy(self.tabs[idx]));self.closed_tabs=self.closed_tabs[-20:]
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
        self.refresh_tab_bar_appearance()

    def reopen_last_closed_tab(self):
        while getattr(self,'closed_tabs',[]):
            record=self.closed_tabs.pop()
            path=Path(str(record.get('path','')))
            kind=record.get('kind','folder')
            if not path.is_dir():continue
            if kind=='archive' and (not Path(record.get('archive','')).is_file() or Path(record.get('archive','')).suffix.casefold() not in COMIC_EXTS):continue
            if kind not in ('folder','farview','archive','empty'):kind='folder'
            record=dict(record);record['kind']=kind
            self.save_current_view();self.add_tab(path,select=True,tab=record);self.activate_tab(len(self.tabs)-1,save_previous=False);self.save_settings();return True
        return False

    def add_empty_tab(self):
        self.save_current_view()
        folder=Path(self.current) if Path(self.current).is_dir() else Path.home()
        self.add_tab(folder,select=True,tab={'kind':'empty','path':str(folder),'sort':self.sort,'desc':self.desc})
        if self.tabs:self.activate_tab(len(self.tabs)-1,save_previous=False)
        self.save_settings()

    def remember_scroll(self, value):
        # Archive pages have their own (archive, member-prefix) position map.
        # Never let their scroll events overwrite the containing folder tab.
        if self._suppress_scroll_save or self.in_archive_view or self.in_favorites_view or self.return_to_favorites:return
        path=str(self.current)
        self.scroll_positions[path]=int(value)
        idx=self.active_tab_index
        if 0<=idx<len(self.tabs) and self.tabs[idx].get('path')==path:self.tabs[idx]['scroll']=int(value);self.tabs[idx].setdefault('positions',{})[path]=int(value)

    def save_current_view(self):
        if not hasattr(self,'grid') or not hasattr(self,'tabbar'):return
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
            p=Path(t['path']);archive=Path(t.get('archive','')) if t.get('kind')=='archive' else None;prefix=('Farview selection · ' if t.get('kind')=='farview' and t.get('selection') else ('Farview · ' if t.get('kind')=='farview' else ''));label='New tab' if t.get('kind')=='empty' else (archive.name if archive is not None and t.get('archive') else (p.name or str(p)));tooltip=str(archive) if archive is not None and t.get('archive') else str(p);self.tabbar.setTabText(i,prefix+label);self.tabbar.setTabToolTip(i,('Farview selection from chosen items' if t.get('selection') else ('Farview of ' if t.get('kind')=='farview' else ''))+tooltip)

    def load(self,folder,restore_scroll=False,save_previous=True):
        if getattr(self,'in_archive_view',False):self.archive_generation+=1;self.finish_operation()
        self.in_archive_view=False;self.archive_path=None;self.archive_prefix=''
        self.in_favorites_view=False;self.active_favorite_group=None
        if hasattr(self,'selection_clear_overlay'):
            if self.right_controls_auto_hide:self.hide_favorite_controls()
            else:self.show_right_controls()
        folder=Path(folder)
        pending_new=getattr(self,'pending_new_folder_path',None)
        if pending_new and self._normalized_path_key(Path(pending_new).parent)==self._normalized_path_key(self.current) and self._normalized_path_key(folder)!=self._normalized_path_key(self.current):
            if getattr(self.grid,'inline_rename_path',None):self.grid.commit_inline_rename()
            else:self.pending_new_folder_path=None
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
        if getattr(self,'in_archive_view',False):self.archive_generation+=1;self.finish_operation()
        self.in_archive_view=False;self.archive_path=None;self.archive_prefix=''
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
        if (generation is not None and generation!=self.scan_generation) or self.in_archive_view:return
        self.finish_operation()
        if error:self.status.setText('Unable to read folder');QMessageBox.warning(self,'Magellan',error);return
        idx=self.current_tab_index()
        if 0<=idx<len(self.tabs) and self.tabs[idx].get('kind')=='farview':
            tab=self.tabs[idx];source_order=[self._manual_item_key(item) for item in items if self._manual_item_key(item)][:100000]
            tab['source_order']=source_order
        self.items=items;self.refresh();self.save_settings()
        pending_new=getattr(self,'pending_new_folder_path',None)
        creating_here=bool(pending_new and Path(pending_new).exists() and self._normalized_path_key(Path(pending_new).parent)==self._normalized_path_key(self.current))
        if 0<=idx<len(self.tabs):
            target=int(self.tabs[idx].setdefault('positions',{}).get(str(self.current),self.tabs[idx].get('scroll',0)))
        else:
            target=int(self.scroll_positions.get(str(self.current),0))
        if creating_here:target=0
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
        if creating_here:QTimer.singleShot(90,lambda path=pending_new:self.grid.begin_inline_rename(path,new_folder=True) if getattr(self,'pending_new_folder_path',None)==path and self._normalized_path_key(Path(path).parent)==self._normalized_path_key(self.current) else None)

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
        labels={'name':('↥','Name'),'mtime':('↧','Last modified'),'ctime':('◷','Last created'),'type':('◇','Type'),'size':('⇵','Size'),'random':('⤨','Random order'),'manual':('↕','Personal order')}
        idx=self.current_tab_index();folder_order=0<=idx<len(self.tabs) and self.tabs[idx].get('kind')=='farview' and self.tabs[idx].get('unsorted',True)
        for key,b in self.sortBtns.items():
            icon,title=labels[key]
            active=key==self.sort and not folder_order
            composed=QPixmap(46,24);composed.fill(Qt.transparent);painter=QPainter(composed);painter.setRenderHint(QPainter.SmoothPixmapTransform,True)
            asset={'name':'list.png','mtime':'lastmod.png','type':'types.png','size':'size.png','manual':'personalorder.png'}.get(key)
            asset_path=self.icon_dir/asset if asset else None
            source=QPixmap(str(asset_path)) if asset_path and asset_path.is_file() else QPixmap()
            if key=='random':
                painter.setPen(QColor('#e8e8e8'));painter.setFont(QFont('Segoe UI Symbol',16));painter.drawText(QRect(0,0,46,24),Qt.AlignCenter,icon)
            elif source.isNull():
                painter.setPen(QColor('#e8e8e8'));painter.setFont(QFont('Segoe UI Symbol',16));painter.drawText(QRect(0,0,23,24),Qt.AlignCenter,icon)
            else:
                if key=='manual':
                    scaled=source.scaled(34,24,Qt.KeepAspectRatio,Qt.SmoothTransformation);painter.drawPixmap((46-scaled.width())//2,(24-scaled.height())//2,scaled)
                else:
                    scaled=source.scaled(22,22,Qt.KeepAspectRatio,Qt.SmoothTransformation);painter.drawPixmap((23-scaled.width())//2,1+(22-scaled.height())//2,scaled)
            if active and key not in ('manual','random'):
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
        key=self._manual_view_key();idx=self.current_tab_index();tab=self.tabs[idx] if 0<=idx<len(self.tabs) else {}
        source=tab.get('source_order',[]) if tab.get('kind')=='farview' else []
        order=list(self.folder_manual_orders.get(key) or source or [])
        known=set(order)
        for item in self.grid.items:
            item_key=self._manual_item_key(item)
            if item_key and item_key not in known:order.append(item_key);known.add(item_key)
        if order!=self.folder_manual_orders.get(key):self.folder_manual_orders[key]=order
        self._record_manual_order_created(key)

    def _apply_manual_order(self,items):
        key=self._manual_view_key();idx=self.current_tab_index();tab=self.tabs[idx] if 0<=idx<len(self.tabs) else {}
        source=tab.get('source_order',[]) if tab.get('kind')=='farview' else []
        order=list(self.folder_manual_orders.get(key) or source or []);known=set(order)
        for item in items:
            item_key=self._manual_item_key(item)
            if item_key and item_key not in known:order.append(item_key);known.add(item_key)
        self.folder_manual_orders[key]=order;self._record_manual_order_created(key)
        ranks={value:index for index,value in enumerate(order)}
        # Stable fallback keeps newly discovered/unrecorded entries visible after existing entries.
        if self.in_favorites_view and self.active_favorite_group is None:
            items.sort(key=lambda item:(0 if item.get('kind')=='favorite_group' else 1,ranks.get(self._manual_item_key(item),len(ranks))))
        else:items.sort(key=lambda item:ranks.get(self._manual_item_key(item),len(ranks)))

    def _reorder_manual_items(self,paths,target_index,before):
        if not (0<=target_index<len(self.grid.items)):return False
        view_key=self._manual_view_key();visible_order=[self._manual_item_key(item) for item in self.grid.items]
        visible_order=[value for value in visible_order if value]
        visible_keys=set(visible_order)
        moved={os.path.normcase(os.path.normpath(os.path.abspath(path))) for path in paths}&visible_keys
        if not moved:return False
        target=self._manual_item_key(self.grid.items[target_index])
        if target in moved:return False
        if self.sort!='manual':
            # The drag gesture reflects the order the user currently sees.
            # Use it as the starting personal sequence before applying the drop.
            order=list(visible_order)
        else:
            order=list(self.folder_manual_orders.get(view_key) or visible_order)
            known=set(order)
            order.extend(value for value in visible_order if value not in known)
        moving=[value for value in order if value in moved]
        if not moving:return False
        order=[value for value in order if value not in moved]
        try:insert_at=order.index(target)+(0 if before else 1)
        except ValueError:return False
        order[insert_at:insert_at]=moving
        self.folder_manual_orders[view_key]=order;self._record_manual_order_created(view_key)
        idx=self.current_tab_index();self.sort='manual';self.desc=False
        if not self.in_favorites_view and not self.in_archive_view:
            if 0<=idx<len(self.tabs):self.tabs[idx].update({'sort':'manual','desc':False,'unsorted':False})
            self._remember_folder_sort(self.current)
        self.update_sort_buttons();self.refresh();self.save_settings();return True

    def set_mode(self,m):self.mode=m;self.vbtn.setChecked(m=='vertical');self.hbtn.setChecked(m=='horizontal');self.grid.setMode(m);self.refresh()
    def set_thumb(self,n):self.thumb=int(n);self.grid.setThumbSize(n);self.refresh()
    def preset(self,n):self.slider.setValue({1:150,2:200,3:250,4:310,5:390}[n])
    def set_continuous_viewer_swipe(self,enabled):
        self.continuous_viewer_swipe=bool(enabled);self.save_settings()
    def set_recursive_search_enabled(self,enabled):
        self.recursive_search_enabled=bool(enabled);self.save_settings();self.search_text_changed(self.search.text())
    def set_preload_all_readable_pages(self,enabled):
        self.preload_all_readable_pages=bool(enabled)
        if self.viewer is not None and not self.viewer._closed:self.viewer.set_preload_all_multipage_pages(self.preload_all_readable_pages)
        self.save_settings()
    def search_text_changed(self,text):
        self.recursive_search_generation+=1
        if hasattr(self,'recursive_search_timer'):self.recursive_search_timer.stop()
        for job in list(getattr(self,'recursive_search_jobs',[])):
            if job.isRunning():job.requestInterruption()
        self.recursive_search_results=None;self.recursive_search_result_query=''
        if getattr(self,'_pending_operation_text','').startswith('Searching'):self.finish_operation()
        self.refresh()
        if self.recursive_search_enabled and text.strip() and not self.in_favorites_view and not self.in_archive_view:
            self.status.setText('Searching subfolders…');self.begin_operation('Searching subfolders…');self.recursive_search_timer.start()
    def start_recursive_search(self):
        query=self.search.text().casefold().strip()
        if not self.recursive_search_enabled or not query or self.in_favorites_view or self.in_archive_view:return
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
        if generation!=self.recursive_search_generation or folder!=str(self.current) or not self.recursive_search_enabled or self.in_favorites_view or self.in_archive_view:return
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
        active_idx=self.current_tab_index();is_farview=not self.in_archive_view and active_idx>=0 and self.tabs[active_idx].get('kind')=='farview'
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
        pending_new=getattr(self,'pending_new_folder_path',None)
        if pending_new and self._normalized_path_key(Path(pending_new).parent)==self._normalized_path_key(self.current):
            pending_key=self._normalized_path_key(pending_new);pending_item=next((item for item in self.items if self._normalized_path_key(item.get('path',''))==pending_key),None)
            if pending_item is not None:
                items=[item for item in items if self._normalized_path_key(item.get('path',''))!=pending_key];items.insert(0,pending_item)
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
        if self.in_archive_view:
            self.browse_archive(self.archive_path,self.archive_prefix,remember_return=False);return
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
            self.save_current_view();self.add_tab(p,select=False);self.save_settings();self.reveal_panel_for_new_tab()
        elif p.is_file() and p.suffix.casefold() in COMIC_EXTS:
            self.save_current_view();self.add_tab(p.parent,select=False,tab={'kind':'archive','archive':str(p.resolve()),'archive_prefix':''});self.save_settings();self.reveal_panel_for_new_tab()
        elif p.is_file() and p.suffix.casefold() in PREVIEW_EXTS:
            self.save_current_view();self.add_tab(p.parent,select=False);self.save_settings();self.reveal_panel_for_new_tab();self.open(str(p))

    def reveal_panel_for_new_tab(self):
        if not (self.auto_hide_main and self.auto_hide_tabs):return
        self.main_hide_timer.stop();self.set_main_panel_visible(True);self.tab_reveal_timer.start()

    def finish_tab_reveal(self):
        if not (self.auto_hide_main and self.auto_hide_tabs) or not self.main_bar.isVisible():return
        pos=self.root_widget.mapFromGlobal(QCursor.pos())
        if self.main_bar.geometry().contains(pos) or self.nav_bar.geometry().contains(pos):return
        self.set_main_panel_visible(False)

    def create_farview(self):
        selected_keys={str(path) for path in self.grid.selected_paths}
        ordered=[str(path) for path in getattr(self.grid,'selection_order',()) if str(path) in selected_keys]
        ordered.extend(sorted(selected_keys-set(ordered),key=natural_key))
        selected=[];seen_selected=set()
        def add_candidate(raw):
            raw=str(raw);decoded=decode_archive_path(raw)
            if decoded:
                archive,member,is_dir,nested=decoded;page=decode_pdf_page_member(member);suffix=Path(member).suffix.casefold()
                if is_dir or page is not None or suffix in PREVIEW_EXTS:
                    key='archive:'+raw
                    if key not in seen_selected:seen_selected.add(key);selected.append(raw)
                return
            try:
                candidate=Path(raw)
                if candidate.is_dir() or (candidate.is_file() and candidate.suffix.casefold() in PREVIEW_EXTS):
                    key=self._normalized_path_key(candidate)
                    if key not in seen_selected:seen_selected.add(key);selected.append(str(candidate))
            except (OSError,TypeError,ValueError):pass
        for raw in ordered:
            if not raw.startswith('favgroup://'):add_candidate(raw)
        selected_group_ids=list(dict.fromkeys(path.split('://',1)[1] for path in ordered if path.startswith('favgroup://')))
        if self.in_favorites_view and self.active_favorite_group is None and selected_group_ids:
            for group_id in selected_group_ids:
                group=next((entry for entry in self.favorite_groups if entry.get('id')==group_id),None)
                for raw in (group or {}).get('folders',[]):add_candidate(raw)
        if self.in_favorites_view and self.active_favorite_group is not None and not selected:
            group=next((entry for entry in self.favorite_groups if entry.get('id')==self.active_favorite_group),None)
            for raw in (group or {}).get('folders',[]):add_candidate(raw)
            if not selected:self.status.setText('This Favorites group has no available folders or previewable files');return
        archive_view=bool(getattr(self,'in_archive_view',False))
        root=Path(self.archive_path).parent if archive_view else Path(self.current)
        if archive_view and not selected:selected=[encode_archive_path(self.archive_path,self.archive_prefix,True)]
        if selected:
            first=decode_archive_path(selected[0])
            if first:root=Path(first[0]).parent
            else:
                try:
                    first_path=Path(selected[0]);root=first_path if first_path.is_dir() else first_path.parent
                except (OSError,TypeError,ValueError):pass
        if not root.is_dir():
            fallback=None
            for raw in selected:
                decoded=decode_archive_path(raw)
                try:candidate=Path(decoded[0]).parent if decoded else Path(raw)
                except (OSError,TypeError,ValueError):continue
                if candidate.is_dir():fallback=candidate;break
            if fallback is None:return
            root=fallback
        self.save_current_view();tab={'kind':'farview','view_id':hashlib.sha1(os.urandom(16)).hexdigest()[:16],'sort':self.sort,'desc':self.desc,'unsorted':True,'source_order':[],'scroll':0,'positions':{}}
        if selected:tab['selection']=selected
        self.add_tab(root,select=True,tab=tab);self.save_settings();self.reveal_panel_for_new_tab()

    def toggle_item_favorite(self,path):
        key=str(path)
        if key.startswith('favgroup://'):
            group_id=key.split('://',1)[1];self.favorite_groups=[g for g in self.favorite_groups if g.get('id')!=group_id];self.save_settings();self.show_favorites(self.active_favorite_group);return
        if getattr(self,'favorite_picker_key',None)==key and getattr(self,'favorite_picker_menu',None) and self.favorite_picker_menu.isVisible():return
        selected=list(self.grid.selected_paths) if key in self.grid.selected_paths else [key]
        self.show_favorites_picker(key,selection=selected)
    @staticmethod
    def _normalized_path_key(path):
        raw=str(path);decoded=decode_archive_path(raw)
        if decoded:
            archive,member,is_dir,nested=decoded
            archive_key=os.path.normcase(os.path.normpath(os.path.abspath(archive)))
            return f'archive::{archive_key}::{member}::{int(is_dir)}::{int(nested)}'
        return os.path.normcase(os.path.normpath(os.path.abspath(raw)))
    def resolve_archive_favorite(self,raw,cache=None):
        decoded=decode_archive_path(str(raw))
        if not decoded:return None
        archive,member,is_dir,nested=decoded
        if decode_pdf_page_member(member) is not None:return None
        try:
            if not Path(archive).is_file():return None
            normalized=member.replace('\\','/').strip('/')
            parent=normalized.rsplit('/',1)[0] if '/' in normalized else ''
            cache_key=(os.path.normcase(os.path.normpath(os.path.abspath(archive))),parent)
            listing=cache.get(cache_key) if cache is not None else None
            if listing is None:
                listing=list_archive_folder(archive,parent)
                if cache is not None:cache[cache_key]=listing
            for item in listing:
                child=decode_archive_path(str(item.get('path','')))
                if child and child[1].rstrip('/')==normalized and child[2]==is_dir:
                    result=dict(item);result['path']=str(raw);return result
        except (OSError,RuntimeError,ValueError,TypeError):
            logger.debug('Could not restore archive favorite %s',raw,exc_info=True)
        return None
    def favorite_path_available(self,raw):
        if decode_archive_path(str(raw)):return self.resolve_archive_favorite(str(raw)) is not None
        try:return Path(raw).exists()
        except (OSError,TypeError,ValueError):return False
    def is_folder_in_favorite_group(self,path):
        key=self._normalized_path_key(path)
        return any(key==self._normalized_path_key(folder) for group in self.favorite_groups for folder in group.get('folders',[]))
    def show_favorites_picker(self,key,anchor=None,tooltip_rect=None,selection=None):
        old=getattr(self,'favorite_picker_menu',None)
        if old:old.close();old.deleteLater()
        paths=[]
        for value in (selection or [key]):
            raw=str(value)
            if decode_archive_path(raw):
                if self.favorite_path_available(raw):paths.append(raw)
            else:
                try:
                    if Path(raw).exists():paths.append(str(Path(raw)))
                except (OSError,TypeError,ValueError):continue
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
        snapshot=(set(self.favorites),copy.deepcopy(self.favorite_groups))
        keys={self._normalized_path_key(path) for path in paths}
        self.favorites={path for path in self.favorites if self._normalized_path_key(path) not in keys}
        if enabled:self.favorites.update(paths)
        self.grid.deselect_paths(paths)
        self.save_settings();self.refresh_favorite_view();self.push_undo_record({'kind':'favorites','favorites':snapshot})
    def remove_favorites_membership(self,paths):
        snapshot=(set(self.favorites),copy.deepcopy(self.favorite_groups));keys={self._normalized_path_key(path) for path in paths}
        self.favorites={path for path in self.favorites if self._normalized_path_key(path) not in keys}
        for group in self.favorite_groups:group['folders']=[path for path in group.get('folders',[]) if self._normalized_path_key(path) not in keys]
        self.grid.deselect_paths(paths);self.save_settings();self.refresh_favorite_view()
        self.push_undo_record({'kind':'favorites','favorites':snapshot})
    def set_favorite_group_membership(self,paths,group_id,enabled):
        group=next((g for g in self.favorite_groups if g.get('id')==group_id),None)
        if not group:return
        snapshot=(set(self.favorites),copy.deepcopy(self.favorite_groups))
        entries=group.setdefault('folders',[]);keys={self._normalized_path_key(path) for path in paths}
        entries[:]=[path for path in entries if self._normalized_path_key(path) not in keys]
        if enabled:entries.extend(paths)
        self.grid.deselect_paths(paths)
        self.favorites={path for path in self.favorites if self._normalized_path_key(path) not in keys}
        self.save_settings();self.refresh_favorite_view()
        self.push_undo_record({'kind':'favorites','favorites':snapshot})
    def toggle_folder_favorite_group(self,path,group_id):
        group=next((g for g in self.favorite_groups if g.get('id')==group_id),None)
        if not group:return
        snapshot=(set(self.favorites),copy.deepcopy(self.favorite_groups))
        folders=group.setdefault('folders',[])
        path_key=self._normalized_path_key(path);existing=next((entry for entry in folders if self._normalized_path_key(entry)==path_key),None)
        if existing is not None:folders.remove(existing)
        else:folders.append(path)
        self.grid.deselect_paths([path])
        self.favorites={entry for entry in self.favorites if self._normalized_path_key(entry)!=path_key}
        self.save_settings();self.refresh_favorite_view();self.push_undo_record({'kind':'favorites','favorites':snapshot})
    def _remove_favorite(self,key):
        snapshot=(set(self.favorites),copy.deepcopy(self.favorite_groups))
        path_key=self._normalized_path_key(key);self.favorites={entry for entry in self.favorites if self._normalized_path_key(entry)!=path_key}
        self.grid.deselect_paths([key])
        for group in self.favorite_groups:group['folders']=[p for p in group.get('folders',[]) if self._normalized_path_key(p)!=path_key]
        self.save_settings();self.refresh_favorite_view();self.push_undo_record({'kind':'favorites','favorites':snapshot})
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
    def handle_favorite_drop(self,payload,target_index,before=True,inside=False):
        if not (0<=target_index<len(self.grid.items)):return
        target=self.grid.items[target_index]
        if inside and target.get('is_dir') and not self.in_favorites_view and not getattr(self,'in_archive_view',False) and payload.startswith('folder-list://'):
            try:paths=[str(Path(path)) for path in json.loads(payload[len('folder-list://'):]) if Path(path).exists()]
            except (TypeError,ValueError):return
            destination=Path(str(target.get('path','')))
            target_key=self._normalized_path_key(destination)
            paths=[path for path in paths if self._normalized_path_key(path)!=target_key]
            if paths and destination.is_dir():self.start_file_operation('move',paths,destination)
            return
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
        if payload.startswith('folder-list://'):
            try:paths=[str(Path(path)) for path in json.loads(payload[len('folder-list://'):]) if Path(path).exists()]
            except (TypeError,ValueError):return
            self._reorder_manual_items(paths,target_index,before)
    def create_favorite_group(self):
        name,ok=QInputDialog.getText(self,'New favorite group','Group name:')
        name=name.strip()
        if not ok or not name:return
        group={'id':hashlib.sha1(os.urandom(16)).hexdigest()[:12],'name':name,'folders':[]}
        self.favorite_groups.append(group);self.active_favorite_group=None;self.in_favorites_view=True;self.save_settings();self.show_favorites()

    def browse_archive(self,path,prefix='',remember_return=True):
        archive=Path(path)
        if not archive.is_file() or archive.suffix.casefold() not in COMIC_EXTS:return
        if not self.in_archive_view and remember_return:
            self.archive_navigation_stack.clear();self.save_current_view();self.archive_return_folder=self.current;self.archive_return_favorites=self.in_favorites_view;self.archive_return_group=self.active_favorite_group
        if self.scan is not None and self.scan.isRunning():self.scan.requestInterruption()
        self.scan_generation+=1
        if not hasattr(self,'archive_scroll_positions'):self.archive_scroll_positions={}
        if self.in_archive_view and getattr(self,'archive_path',None):self.archive_scroll_positions[(self.archive_path,self.archive_prefix)]=self.grid.verticalScrollBar().value()
        self.archive_path=str(archive.resolve());self.archive_prefix=str(prefix or '').replace('\\','/').strip('/');idx=self.current_tab_index()
        if 0<=idx<len(self.tabs) and self.tabs[idx].get('kind')=='archive' and Path(self.tabs[idx].get('archive','')).resolve()==archive.resolve():self.tabs[idx]['archive_prefix']=self.archive_prefix
        self.in_archive_view=True;self.in_favorites_view=False;self.active_favorite_group=None;self.recursive_search_results=None;self.recursive_search_result_query='';self.recursive_search_generation+=1;self.recursive_search_timer.stop();self.archive_generation+=1;generation=self.archive_generation
        self.grid.setItems([]);self.update_favorite_button();self.update_sort_buttons();self.crumb.setText(f'Archive · {archive.name}'+(f' / {self.archive_prefix}' if self.archive_prefix else ''));self.status.setText('Reading archive contents…');self.begin_operation('Reading archive contents…')
        archive_path=self.archive_path;member_prefix=self.archive_prefix
        future=VIEWER_IMAGE_POOL.submit(list_archive_folder,archive_path,member_prefix)
        def completed(result,p=archive_path,prefix=member_prefix,g=generation):
            try:items=result.result();error=''
            except Exception as exc:logger.warning('Could not list archive contents in %s: %s',p,exc,exc_info=True);items=[];error=str(exc)
            self._archive_scan_bridge.completed.emit(p,prefix,items,error,g)
        future.add_done_callback(completed)

    def finish_archive_scan(self,path,prefix,items,error,generation):
        if generation!=self.archive_generation or not self.in_archive_view or path!=self.archive_path or prefix!=self.archive_prefix:return
        self.finish_operation()
        if error:
            self._archive_auto_flatten=False;self._archive_flatten_depth=0;self.archive_flattened_root_prefix=''
            self.items=[];self.grid.setItems([]);self.status.setText('Could not read archive contents');QMessageBox.warning(self,'Could not browse archive',error);return
        self.items=items;self.refresh()
        scroll=getattr(self,'archive_scroll_positions',{}).get((path,prefix),0);QTimer.singleShot(0,lambda v=scroll:self.grid.verticalScrollBar().setValue(v) if self.in_archive_view and path==self.archive_path and prefix==self.archive_prefix else None)
        if getattr(self,'_archive_auto_flatten',False):
            decoded_single=decode_archive_path(str(items[0].get('path',''))) if len(items)==1 and items[0].get('is_dir') else None
            if decoded_single:
                depth=int(getattr(self,'_archive_flatten_depth',0))
                if depth<16:
                    self._archive_flatten_depth=depth+1
                    if decoded_single[3]:
                        try:
                            nested_path=materialize_nested_archive(path,decoded_single[1])
                            self.archive_navigation_stack.append((path,prefix));self.archive_flattened_root_prefix=''
                            self.browse_archive(nested_path,'',remember_return=False);return
                        except Exception:logger.warning('Could not flatten nested archive wrapper %s in %s',decoded_single[1],path,exc_info=True)
                    else:
                        self.archive_flattened_root_prefix=decoded_single[1].strip('/')
                        self.browse_archive(path,decoded_single[1],remember_return=False);return
            self._archive_auto_flatten=False;self._archive_flatten_depth=0
        if not items:self.status.setText('No files or folders were found in this archive location')

    def farview_viewer_entries(self):
        entries=[]
        for item in self.grid.items:
            if item.get('is_dir') or item.get('kind') not in ('image','document'):continue
            raw=str(item.get('path',''));decoded=decode_archive_path(raw)
            if decoded:
                archive,member,is_dir,nested=decoded
                if is_dir:continue
                page=decode_pdf_page_member(member)
                if page is not None:entries.append({'source':archive,'page':page,'member':None,'name':item.get('name',Path(archive).name)})
                elif Path(member).suffix.casefold() in IMAGE_EXTS:entries.append({'source':archive,'page':None,'member':member,'name':item.get('name',Path(member).name)})
                continue
            path=Path(raw)
            if path.suffix.casefold() in IMAGE_EXTS:entries.append({'source':str(path),'page':None,'member':None,'name':item.get('name',path.name)})
        return entries
    def open_farview_entry(self,path):
        entries=self.farview_viewer_entries();target=decode_archive_path(str(path))
        if target:
            archive,member,_is_dir,_nested=target;page=decode_pdf_page_member(member)
            if page is not None:index=next((i for i,item in enumerate(entries) if item['source']==archive and item['page']==page),-1)
            else:index=next((i for i,item in enumerate(entries) if item['source']==archive and item['member']==member),-1)
        else:index=next((i for i,item in enumerate(entries) if os.path.normcase(os.path.abspath(item['source']))==os.path.normcase(os.path.abspath(str(path)))), -1)
        if not entries or index<0:return False
        if hasattr(self,'tree_dock') and self.tree_dock.isVisible():self.save_tree_navigation_state();self.tree_dock.hide()
        if self.viewer is not None:
            try:
                if self.viewer.isVisible():self.viewer.close()
            except RuntimeError:logger.debug('Previous media viewer was already closed')
        entry=entries[index];viewer=MediaViewer([entry['source']],0,self,self.slideshow_interval_ms);self.viewer=viewer;viewer.farview_sequence=entries;viewer.farview_active=True;viewer.farview_index=index;viewer.forced_single_page_mode=True
        viewer.pending_archive_member=entry.get('member');viewer.pending_multipage_page=entry.get('page');viewer.setAttribute(Qt.WA_DeleteOnClose);viewer.showFullScreen();viewer.raise_();viewer.activateWindow()
        return True

    def open_archive_member(self,archive_path,member,page_index=None):
        if hasattr(self,'tree_dock') and self.tree_dock.isVisible():self.save_tree_navigation_state();self.tree_dock.hide()
        if self.viewer is not None:
            try:
                if self.viewer.isVisible():self.viewer.close()
            except RuntimeError:logger.debug('Previous media viewer was already closed')
        self.viewer=MediaViewer([archive_path],0,self,self.slideshow_interval_ms);self.viewer.pending_archive_member=member
        if page_index is not None:self.viewer.pending_multipage_page=max(0,int(page_index))
        self.viewer.setAttribute(Qt.WA_DeleteOnClose);self.viewer.showFullScreen();self.viewer.raise_();self.viewer.activateWindow()

    def open(self,path):
        raw=str(path)
        tab_index=self.current_tab_index()
        if 0<=tab_index<len(self.tabs) and self.tabs[tab_index].get('kind')=='farview' and self.open_farview_entry(raw):return
        archive_virtual=decode_archive_path(raw)
        if archive_virtual:
            archive,member,is_dir,nested=archive_virtual;pdf_page=decode_pdf_page_member(member)
            if is_dir and nested:
                try:
                    nested_path=materialize_nested_archive(archive,member)
                    if self.in_archive_view:self.archive_navigation_stack.append((self.archive_path,self.archive_prefix))
                    self._archive_auto_flatten=True;self._archive_flatten_depth=0;self.archive_flattened_root_prefix=''
                    self.browse_archive(nested_path,'',remember_return=False)
                except Exception as error:logger.warning('Could not open nested archive %s in %s',member,archive,exc_info=True);QMessageBox.warning(self,'Could not open nested archive',str(error))
            elif is_dir:self.browse_archive(archive,member,remember_return=False)
            elif pdf_page is not None and Path(archive).suffix.casefold() in PDF_EXTS:self.open_archive_member(archive,'',page_index=pdf_page)
            elif Path(member).suffix.casefold() in PDF_EXTS:
                try:self.open(str(Path(materialize_nested_archive(archive,member))))
                except Exception as error:logger.warning('Could not open PDF member %s in %s',member,archive,exc_info=True);QMessageBox.warning(self,'Could not open PDF',str(error))
            elif Path(member).suffix.casefold() in IMAGE_EXTS:self.open_archive_member(archive,member)
            else:self.status.setText('Select this archive file and press Ctrl+C to copy it out')
            return
        if raw.startswith('favgroup://'):
            self.show_favorites(raw.split('://',1)[1]);return
        p=Path(path)
        if p.is_dir():
            if self.in_favorites_view:
                self.return_to_favorites=True;self.favorites_navigation_root=p;self.favorite_return_group=self.active_favorite_group
            self.load(p)
        elif p.suffix.casefold() in COMIC_EXTS:
            self._archive_auto_flatten=True;self._archive_flatten_depth=0;self.archive_flattened_root_prefix='';self.browse_archive(p,'')
        elif p.suffix.casefold() in PREVIEW_EXTS:
            self.tree_was_visible_before_viewer=hasattr(self,'tree_dock') and self.tree_dock.isVisible()
            if self.tree_was_visible_before_viewer:self.save_tree_navigation_state();self.tree_dock.hide()
            paths=[str(item['path']) for item in self.grid.items if item.get('kind') in ('image','document') and Path(item['path']).suffix.casefold() in PREVIEW_EXTS]
            if str(p) not in paths:paths.append(str(p))
            if self.viewer is not None:
                try:
                    if self.viewer.isVisible():self.viewer.close()
                except RuntimeError:logger.debug('Previous media viewer was already closed')
            self.viewer=MediaViewer(paths,paths.index(str(p)),self,self.slideshow_interval_ms);self.viewer.setAttribute(Qt.WA_DeleteOnClose);self.viewer.showFullScreen();self.viewer.activateWindow()
        elif p.suffix.casefold() in ('.lnk','.url'):
            target=self.resolve_link(p)
            if target and Path(target).is_dir():self.load(Path(target))
            elif target:self.reveal(Path(target))
            else:self.reveal(p)
        else:self.reveal(p)

    def open_external_file(self,path,open_folder=False,open_in_magellan=False):
        """Route Explorer launches to a viewer preview or a new browse tab."""
        p=Path(path).expanduser()
        explicit_target=bool(open_folder or open_in_magellan)
        if p.is_dir():
            if explicit_target:self.open_external_folder_tab(p)
            return
        if not p.is_file():return
        suffix=p.suffix.casefold()
        if explicit_target:
            if suffix in COMIC_EXTS:self.open_external_folder_tab(p,open_archive=True)
            else:self.open_external_folder_tab(p.parent)
            return
        if suffix in ('.zip','.rar'):
            self.open_external_folder_tab(p,open_archive=True);return
        if suffix not in PREVIEW_EXTS:return
        self.tree_was_visible_before_viewer=hasattr(self,'tree_dock') and self.tree_dock.isVisible()
        if self.tree_was_visible_before_viewer:self.save_tree_navigation_state();self.tree_dock.hide()
        if self.viewer is not None:
            self._replacing_external_viewer=True
            try:
                if self.viewer.isVisible():self.viewer.close()
            except RuntimeError:logger.debug('Previous media viewer was already closed')
            finally:self._replacing_external_viewer=False
        resolved=str(p.resolve());viewer=MediaViewer([resolved],0,self,self.slideshow_interval_ms,detached=True);self.viewer=viewer;viewer.external_open_path=resolved;viewer.setAttribute(Qt.WA_DeleteOnClose);image_bridge=viewer.load_bridge
        def scan_siblings():
            paths=[]
            try:
                with os.scandir(p.parent) as entries:
                    for entry in entries:
                        try:
                            if entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.casefold() in PREVIEW_EXTS:paths.append(str(Path(entry.path).resolve()))
                        except OSError:continue
                paths.sort(key=natural_key)
            except OSError:logger.debug('Could not scan neighboring media for Explorer-opened file %s',resolved,exc_info=True)
            if resolved not in paths:paths.append(resolved);paths.sort(key=natural_key)
            image_bridge.loaded.emit(resolved,{'stage':'external_paths','paths':paths})
        viewer.external_sibling_scan=lambda:VIEWER_IMAGE_POOL.submit(scan_siblings)
        viewer.setAttribute(Qt.WA_ShowWithoutActivating,False)
        if self.isFullScreen() and self.isVisible():self.showMinimized()
        viewer.showFullScreen()
        def focus_preview(target=viewer):
            try:
                if not target.isVisible() or getattr(target,'_closed',False):return
                target.raise_();target.activateWindow();QApplication.setActiveWindow(target)
                if os.name=='nt':ctypes.windll.user32.SetForegroundWindow(int(target.winId()))
            except (AttributeError,OSError,RuntimeError):logger.debug('Could not focus Explorer-opened viewer',exc_info=True)
        # Focus this viewer once, at launch. Repeated delayed foreground
        # requests can steal focus later when the user has returned to Explorer.
        focus_preview()

    def _explorer_context_verbs(self):
        return [(r'Software\Classes\Directory\shell\MagellanOpen',None),(r'Software\Classes\Folder\shell\MagellanOpen',None),(r'Software\Classes\*\shell\MagellanOpen','*')]
    @staticmethod
    def _delete_registry_verb(winreg,verb_key):
        for key_path in (verb_key+r'\command',verb_key):
            try:winreg.DeleteKey(winreg.HKEY_CURRENT_USER,key_path)
            except FileNotFoundError:pass
    def is_explorer_context_menu_enabled(self):
        if os.name!='nt':return False
        try:
            import winreg
            commands=[];new_registration_complete=True
            for verb_key,_suffix in self._explorer_context_verbs():
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,verb_key+r'\command') as key:commands.append(winreg.QueryValueEx(key,'')[0])
                except FileNotFoundError:new_registration_complete=False;break
            if new_registration_complete and commands and all(isinstance(command,str) and '--open-in-magellan' in command for command in commands):return True
            # Recognize the prior opt-in so it can be migrated without losing
            # the user's choice when they install this version.
            for suffix in sorted(PREVIEW_EXTS):
                legacy=rf'Software\Classes\SystemFileAssociations\{suffix}\shell\MagellanOpenFolder\command'
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,legacy):return True
                except FileNotFoundError:continue
            return False
        except FileNotFoundError:return False
        except OSError:
            logger.debug('Could not inspect Magellan Explorer context-menu registration',exc_info=True);return False
    def _write_explorer_context_menu(self,winreg,enabled):
        executable=Path(sys.executable).resolve()
        command_parts=[str(executable)] if getattr(sys,'frozen',False) else [str(executable),str(Path(sys.argv[0]).resolve())]
        open_command=subprocess.list2cmdline(command_parts)+' --open-in-magellan "%1"'
        verb_keys=[key for key,_suffix in self._explorer_context_verbs()]
        for verb_key in verb_keys:
            if enabled:
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER,verb_key) as key:
                    winreg.SetValueEx(key,'',0,winreg.REG_SZ,'Open in Magellan');winreg.SetValueEx(key,'Icon',0,winreg.REG_SZ,f'{executable},0')
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER,verb_key+r'\command') as key:winreg.SetValueEx(key,'',0,winreg.REG_SZ,open_command)
            else:self._delete_registry_verb(winreg,verb_key)
        # Remove older per-extension registrations so each file has one entry.
        for suffix in sorted(PREVIEW_EXTS):
            extension_verb=rf'Software\Classes\SystemFileAssociations\{suffix}\shell\MagellanOpen'
            legacy_verb=rf'Software\Classes\SystemFileAssociations\{suffix}\shell\MagellanOpenFolder'
            for old_verb in (extension_verb,legacy_verb):
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,old_verb+r'\command') as key:
                        value=winreg.QueryValueEx(key,'')[0]
                        if isinstance(value,str) and ('--open-in-magellan' in value or '--open-folder' in value):self._delete_registry_verb(winreg,old_verb)
                except FileNotFoundError:pass
            prog_ids=set()
            try:
                choice_key=rf'Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\{suffix}\UserChoice'
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER,choice_key) as key:
                    prog_id=winreg.QueryValueEx(key,'ProgId')[0]
                    if isinstance(prog_id,str) and prog_id.strip():prog_ids.add(prog_id.strip())
            except (FileNotFoundError,OSError):pass
            try:
                with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,suffix) as key:
                    prog_id=winreg.QueryValueEx(key,'')[0]
                    if isinstance(prog_id,str) and prog_id.strip():prog_ids.add(prog_id.strip())
            except (FileNotFoundError,OSError):pass
            for prog_id in prog_ids:
                if any(part in ('','.', '..') for part in prog_id.split('\\')):continue
                old_verb=rf'Software\Classes\{prog_id}\shell\MagellanOpen'
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,old_verb+r'\command') as key:
                        value=winreg.QueryValueEx(key,'')[0]
                        if isinstance(value,str) and '--open-in-magellan' in value:self._delete_registry_verb(winreg,old_verb)
                except FileNotFoundError:pass
        # Remove the previous all-file image-plus-folder command while
        # preserving the enabled/disabled choice through the new entries.
        for suffix in sorted(PREVIEW_EXTS):self._delete_registry_verb(winreg,rf'Software\Classes\SystemFileAssociations\{suffix}\shell\MagellanOpenFolder')
    def migrate_legacy_explorer_context_registration(self):
        if os.name!='nt':return
        try:
            import winreg
            registered_command=False
            for verb_key,_suffix in self._explorer_context_verbs():
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,verb_key+r'\command') as key:
                        value=winreg.QueryValueEx(key,'')[0]
                        if isinstance(value,str) and '--open-in-magellan' in value:registered_command=True;break
                except FileNotFoundError:continue
            if registered_command:
                # Refresh stale registrations after the fast build moved the
                # executable from the old MagellanFast directory.
                self._write_explorer_context_menu(winreg,self.explorer_context_menu_enabled)
                try:ctypes.WinDLL('shell32',use_last_error=True).SHChangeNotify(0x08000000,0,None,None)
                except (AttributeError,OSError):logger.debug('Explorer will refresh the repaired command later',exc_info=True)
                return
            legacy_present=False
            current_folder_command=r'Software\Classes\Directory\shell\MagellanOpen\command'
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER,current_folder_command) as key:
                    command=winreg.QueryValueEx(key,'')[0]
                    legacy_present=isinstance(command,str) and '--open-in-magellan' in command
            except FileNotFoundError:pass
            for suffix in sorted(PREVIEW_EXTS):
                if legacy_present:break
                key_path=rf'Software\Classes\SystemFileAssociations\{suffix}\shell\MagellanOpenFolder\command'
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,key_path):legacy_present=True;break
                except FileNotFoundError:continue
            if not legacy_present and not self.explorer_context_menu_enabled:return
            self._write_explorer_context_menu(winreg,True)
            shell32=ctypes.WinDLL('shell32',use_last_error=True);shell32.SHChangeNotify(0x08000000,0,None,None)
            if hasattr(self,'explorer_context_action'):
                self.explorer_context_action.blockSignals(True);self.explorer_context_action.setChecked(True);self.explorer_context_action.blockSignals(False)
            logger.info('Migrated the prior Explorer context command to Open in Magellan')
        except Exception:logger.exception('Could not migrate the previous Explorer context command')
    def set_explorer_context_menu_enabled(self,enabled):
        if os.name!='nt':return
        try:
            import winreg
            self._write_explorer_context_menu(winreg,bool(enabled))
            self.explorer_context_menu_enabled=bool(enabled)
            try:
                shell32=ctypes.WinDLL('shell32',use_last_error=True);shell32.SHChangeNotify(0x08000000,0,None,None)
            except (AttributeError,OSError):logger.debug('Explorer will refresh its context menu registration later',exc_info=True)
            checked=self.is_explorer_context_menu_enabled()
            self.explorer_context_menu_enabled=checked
            self.save_settings()
            if hasattr(self,'explorer_context_action'):
                self.explorer_context_action.blockSignals(True);self.explorer_context_action.setChecked(checked);self.explorer_context_action.blockSignals(False)
            logger.info('Explorer Open in Magellan command %s',('enabled' if checked else 'disabled'))
        except Exception as error:
            logger.exception('Could not update the Explorer context command')
            self.explorer_context_menu_enabled=self.is_explorer_context_menu_enabled()
            if hasattr(self,'explorer_context_action'):
                self.explorer_context_action.blockSignals(True);self.explorer_context_action.setChecked(self.is_explorer_context_menu_enabled());self.explorer_context_action.blockSignals(False)
            QMessageBox.warning(self,'Explorer menu update failed',str(error))
    def open_external_folder_tab(self,path,open_archive=False):
        if self._closing:return
        target=Path(path).expanduser()
        archive=None
        if target.is_dir():folder=target
        elif open_archive and target.is_file() and target.suffix.casefold() in COMIC_EXTS:
            folder=target.parent;archive=target
        else:
            logger.warning('Cannot open Explorer target in Magellan: %s',target);return
        folder=folder.resolve()
        if self.viewer is not None:
            self._replacing_external_viewer=True
            try:
                if self.viewer.isVisible():self.viewer.close()
            except RuntimeError:logger.debug('Previous media viewer was already closed')
            finally:self._replacing_external_viewer=False
        if getattr(self,'_ui_deferred',False):
            self._deferred_startup_maintenance=False;self._initialize_ui(external_launch=True);self._ui_deferred=False
        else:self.save_current_view()
        tab={'kind':'archive','archive':str(archive.resolve()),'archive_prefix':''} if archive is not None else None
        self.add_tab(folder,select=True,tab=tab);self.activate_tab(self.current_tab_index(),save_previous=False);self.focus_main_window()
        if archive is not None:self._archive_auto_flatten=True;self._archive_flatten_depth=0;self.archive_flattened_root_prefix=''
        QTimer.singleShot(250,self.save_settings)
        QTimer.singleShot(0,self.focus_main_window);QTimer.singleShot(120,self.focus_main_window)

    def focus_main_window(self):
        if self._closing:return
        if self.isMinimized():
            mode=getattr(self,'window_mode','normal')
            if mode=='fullscreen':self.showFullScreen()
            elif mode=='maximized':self.showMaximized()
            else:self.showNormal()
        else:self.show()
        self.raise_();self.activateWindow();QApplication.setActiveWindow(self)
        if sys.platform.startswith('win'):
            try:ctypes.windll.user32.SetForegroundWindow(int(self.winId()))
            except (AttributeError,OSError,TypeError,ValueError):logger.debug('Could not foreground the Magellan window',exc_info=True)

    def register_default_viewer(self):
        """Register Magellan as a selectable viewer without replacing file-type labels."""
        if not sys.platform.startswith('win'):
            QMessageBox.information(self,'Default viewer','Magellan can be selected as the default viewer from your operating system’s file-association settings. This shortcut is currently available on Windows.')
            return
        try:
            import winreg
            capability_key=r'Software\Magellan\Capabilities'
            executable=Path(sys.executable).resolve()
            command=[str(executable)] if getattr(sys,'frozen',False) else [str(executable),str(Path(sys.argv[0]).resolve())]
            open_command=subprocess.list2cmdline(command)+' "%1"'
            fallback_types={
                '.jpg':'JPEG image','.jpeg':'JPEG image','.jfif':'JPEG image','.png':'PNG image','.gif':'GIF image','.webp':'WEBP image',
                '.bmp':'Bitmap image','.tif':'TIFF image','.tiff':'TIFF image','.avif':'AVIF image','.heic':'HEIC image','.heif':'HEIF image','.jxl':'JPEG XL image',
                '.pdf':'PDF document','.cbr':'Comic book RAR archive','.cbz':'Comic book ZIP archive','.rar':'RAR archive','.zip':'Compressed (zipped) folder'
            }
            class SHFILEINFO(ctypes.Structure):
                _fields_=[('hIcon',wintypes.HANDLE),('iIcon',ctypes.c_int),('dwAttributes',wintypes.DWORD),('szDisplayName',wintypes.WCHAR*260),('szTypeName',wintypes.WCHAR*80)]
            def original_type_name(suffix):
                info=SHFILEINFO();label=''
                try:
                    shell32=ctypes.WinDLL('shell32',use_last_error=True);func=shell32.SHGetFileInfoW
                    func.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,ctypes.POINTER(SHFILEINFO),wintypes.UINT,wintypes.UINT];func.restype=ctypes.c_size_t
                    if func('Magellan registration sample'+suffix,0x80,ctypes.byref(info),ctypes.sizeof(info),0x410):label=info.szTypeName.strip()
                except (AttributeError,OSError,TypeError,ValueError):logger.debug('Could not query the existing Explorer type label for %s',suffix,exc_info=True)
                if not label or 'magellan' in label.casefold():label=fallback_types.get(suffix,suffix.lstrip('.').upper()+' file')
                return label
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER,capability_key) as key:
                winreg.SetValueEx(key,'ApplicationName',0,winreg.REG_SZ,'Magellan')
                winreg.SetValueEx(key,'ApplicationDescription',0,winreg.REG_SZ,'Browse images, PDFs, and comic archives in Magellan.')
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER,capability_key+r'\FileAssociations') as key:
                for suffix in sorted(PREVIEW_EXTS):
                    prog_id='Magellan.MediaViewer.'+suffix.lstrip('.').replace('.','_')
                    label=original_type_name(suffix)
                    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,rf'Software\Classes\{prog_id}') as prog:
                        winreg.SetValueEx(prog,'',0,winreg.REG_SZ,label)
                        winreg.SetValueEx(prog,'FriendlyTypeName',0,winreg.REG_SZ,label)
                    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,rf'Software\Classes\{prog_id}\DefaultIcon') as key_icon:
                        icon_root=Path(sys._MEIPASS) if getattr(sys,'frozen',False) else Path(__file__).resolve().parent.parent
                        icon_name='MainIconNewRAR.ico' if suffix in {'.rar','.zip'} else 'MainIconNewRead.ico' if suffix in {'.pdf','.cbr','.cbz'} else 'MainIconNew.ico'
                        icon=icon_root/'Alternative Icons'/icon_name
                        if not icon.is_file():icon=icon_root/'MainIcon.ico'
                        icon_value=f'{icon},0'
                        winreg.SetValueEx(key_icon,'',0,winreg.REG_SZ,icon_value)
                    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,rf'Software\Classes\{prog_id}\shell\open\command') as key_command:
                        winreg.SetValueEx(key_command,'',0,winreg.REG_SZ,open_command)
                    winreg.SetValueEx(key,suffix,0,winreg.REG_SZ,prog_id)
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER,r'Software\RegisteredApplications') as key:
                winreg.SetValueEx(key,'Magellan',0,winreg.REG_SZ,capability_key)
            for suffix in PREVIEW_EXTS:
                prog_id='Magellan.MediaViewer.'+suffix.lstrip('.').replace('.','_')
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER,rf'Software\Classes\{suffix}\OpenWithProgids') as key:
                    winreg.SetValueEx(key,prog_id,0,winreg.REG_NONE,b'')
            QDesktopServices.openUrl(QUrl('ms-settings:defaultapps?registeredAppUser=Magellan'))
            QMessageBox.information(self,'Choose Magellan as your viewer','Magellan is registered with Windows. In Default apps, select Magellan, then assign it to the image, PDF, and comic formats you want it to open. Explorer will keep each format’s own file-type name.')
        except Exception as error:
            logger.exception('Could not register Magellan as a Windows viewer')
            QMessageBox.warning(self,'Default viewer setup failed',str(error))

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
        if getattr(self,'_ui_deferred',False):self._ui_deferred=False;self._initialize_ui(external_launch=True)
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
            self.push_undo_record({'kind':'recycle','paths':list(unique),'favorites':favorites_snapshot})
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
        self.push_undo_record({'kind':'favorites','favorites':favorites_snapshot})

    def undo_last_action(self):
        if getattr(self,'_ui_deferred',False):self._ui_deferred=False;self._initialize_ui(external_launch=True)
        task=getattr(self,'file_operation_task',None)
        if task is not None and task.isRunning():self.status.setText('Wait for the current file operation to finish');return False
        history=getattr(self,'undo_history',[])
        if not history:self.status.setText('There is no recent move or deletion to undo');return False
        record=history[-1]
        if record.get('kind')=='move':
            items=list(record.get('items',[]))
            if not items:history.pop();return self.undo_last_action()
            self._undo_record_pending=record;self.start_file_operation('undo_move',items,self.current if self.current.is_dir() else Path.home());return True
        return self.undo_recent_deletion(record)

    def undo_recent_deletion(self,record=None):
        history=getattr(self,'undo_history',[])
        if record is None:record=history[-1] if history else None
        if not record:return False
        if record.get('kind')=='favorites':
            self.favorites,self.favorite_groups=copy.deepcopy(record['favorites'])
            if record in history:history.remove(record)
            self.save_settings();self.show_favorites(self.active_favorite_group);return True
        if record.get('kind')!='recycle':return False
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
            if not record['paths'] and record in history:history.remove(record)
            self.save_settings()
            if self.in_favorites_view:self.refresh_favorite_view()
            elif self.current.is_dir():self.load(self.current,restore_scroll=True,save_previous=False)
            if restore_failures:
                details='\n'.join(f'{path}: {error}' for path,error in restore_failures[:5])
                QMessageBox.warning(self,'Some items could not be restored',f'{len(restored)} item(s) were restored. The remaining items are still available to undo later.\n\n{details}')
            return True
        except Exception as e:
            logger.exception('Could not undo the most recent delete action')
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
        if self.in_archive_view:
            self.up();return
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
        if self.in_archive_view:
            if self.archive_prefix:
                current_prefix=self.archive_prefix.strip('/');wrapper_prefix=str(getattr(self,'archive_flattened_root_prefix','')).strip('/')
                parent_prefix=current_prefix.rsplit('/',1)[0] if '/' in current_prefix else ''
                if wrapper_prefix and current_prefix==wrapper_prefix:
                    self.leave_archive_view();return
                self.browse_archive(self.archive_path,parent_prefix,remember_return=False);return
            if self.archive_navigation_stack:
                path,prefix=self.archive_navigation_stack.pop();self.archive_flattened_root_prefix='';self._archive_auto_flatten=False;self._archive_flatten_depth=0;self.browse_archive(path,prefix,remember_return=False);return
            self.leave_archive_view()
            return
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

    def leave_archive_view(self):
        if self.archive_navigation_stack:
            path,prefix=self.archive_navigation_stack.pop();self.archive_flattened_root_prefix='';self._archive_auto_flatten=False;self.browse_archive(path,prefix,remember_return=False);return
        target=self.archive_return_folder or self.current;was_favorites=self.archive_return_favorites;group=self.archive_return_group
        self.in_archive_view=False;self.archive_path=None;self.archive_prefix='';self.archive_flattened_root_prefix='';self._archive_auto_flatten=False;self._archive_flatten_depth=0
        if was_favorites:self.show_favorites(group)
        elif Path(target).is_dir():self.load(Path(target),restore_scroll=True,save_previous=False)

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
        # Calculate missing root/child sizes in a single tree pass rather than
        # recursively rescanning every kept folder independently.
        self.grid.request_pinned_folder_sizes(key,targets)
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
        dlg=QDialog(self);dlg.setWindowTitle('Keep folder thumbnails');self.prepare_settings_dialog(dlg,'keep_folder_thumbnails',(850,540));self.style_manager_dialog(dlg);lay=QVBoxLayout(dlg);lay.setSpacing(10)
        info=QLabel("Keep selected folders' thumbnails in Magellan's local cache. Their immediate child folders are included; deeper subfolders must be added separately.");info.setWordWrap(True);lay.addWidget(info)
        path_row=QHBoxLayout();path_edit=QLineEdit();path_edit.setPlaceholderText(r'Enter a full folder path, e.g. H:\Pictures');browse=QPushButton('Browse…');add_current=QPushButton('Add current');add_current.setEnabled(not self.in_favorites_view and not getattr(self,'in_archive_view',False));add_path=QPushButton('Add path');path_row.addWidget(path_edit,1);path_row.addWidget(browse);path_row.addWidget(add_current);path_row.addWidget(add_path);lay.addLayout(path_row)
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
        add_current.clicked.connect(lambda:(path_edit.setText(str(self.current)),add_path_value()))
        add_path.clicked.connect(add_path_value);browse.clicked.connect(browse_path);path_edit.returnPressed.connect(add_path_value);search.textChanged.connect(populate)
        sort_by.currentIndexChanged.connect(populate);ascending.clicked.connect(lambda:set_direction(False));descending.clicked.connect(lambda:set_direction(True));ascending.setChecked(True)
        actions=QHBoxLayout();remove_btn=QPushButton('Remove selected');remove_btn.clicked.connect(remove);actions.addWidget(remove_btn);actions.addStretch(1);lay.addLayout(actions)
        close=QDialogButtonBox(QDialogButtonBox.Close);close.rejected.connect(dlg.reject);close.accepted.connect(dlg.accept);lay.addWidget(close);populate();dlg.exec()

    def manage_personal_orders(self):
        dlg=QDialog(self);dlg.setWindowTitle('Manage personal orders');self.prepare_settings_dialog(dlg,'manage_personal_orders',(850,540));self.style_manager_dialog(dlg);layout=QVBoxLayout(dlg)
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

    def start_archive_image_search(self,path,archive,member):
        if getattr(self,'_archive_image_search_future',None) is not None:
            self.status.setText('Preparing the archive image for search…');return
        future=VIEWER_IMAGE_POOL.submit(prepare_archive_search_upload,archive,member)
        self._archive_image_search_future=future
        self.image_search_mode=False;self.grid.set_image_search_active(False);self.grid.search_hover=-1;self.grid.viewport().update()
        self.status.setText('Preparing the archive image for search…')
        timer=QTimer(self);timer.setInterval(40)
        def completed():
            if self._closing:
                future.cancel();timer.stop();timer.deleteLater();self._archive_image_search_future=None;return
            if not future.done():return
            timer.stop();timer.deleteLater();self._archive_image_search_future=None
            try:prepared=future.result()
            except Exception as error:
                logger.warning('Could not prepare archive image search',exc_info=True)
                self.status.setText('Image search failed');QMessageBox.warning(self,'Image search failed',str(error));return
            self.perform_image_search(path,prepared_archive=prepared)
        timer.timeout.connect(completed);timer.start()

    def perform_image_search(self,path,prepared_archive=None):
        virtual=decode_archive_path(str(path)) or decode_archive_thumbnail(str(path))
        if virtual and prepared_archive is None:
            if len(virtual)>2 and virtual[2]:return
            self.start_archive_image_search(path,virtual[0],virtual[1]);return
        generated_upload=None
        try:
            original_path=Path(path);upload_path=original_path;upload_name=original_path.name
            archive_bytes=None
            if prepared_archive is not None:archive_bytes,upload_name=prepared_archive
            elif upload_path.suffix.lower() in VIDEO_EXTS:
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
                raw=archive_bytes if archive_bytes is not None else upload_path.read_bytes()
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
                raw=archive_bytes if archive_bytes is not None else upload_path.read_bytes(); b64=base64.b64encode(raw).decode('ascii');b64_js=javascript_literal(b64);name_js=javascript_literal(upload_name);api_key_js=javascript_literal(api_key)
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
        if getattr(self,'in_archive_view',False):self.archive_generation+=1;self.finish_operation()
        self.in_archive_view=False;self.archive_path=None;self.archive_prefix=''
        if not self.in_favorites_view:self.save_current_view();self.favorites_return_path=str(self.current)
        self.in_favorites_view=True;self.return_to_favorites=False;self.active_favorite_group=group_id
        if hasattr(self,'selection_clear_overlay') and not self.right_controls_auto_hide:self.show_right_controls()
        fav=[]
        archive_favorite_cache={}
        if group_id is None:
            for group in self.favorite_groups:
                members=[raw for raw in group.get('folders',[]) if not decode_archive_path(str(raw)) and Path(raw).is_dir()]
                preview=members[0] if members else None
                fav.append({'path':f"favgroup://{group.get('id')}",'name':group.get('name','Group'),'is_dir':False,'kind':'favorite_group','mtime':0,'ctime':0,'size':None,'preview_path':preview})
            favorite_paths=sorted(self.favorites,key=natural_key)
        else:
            group=next((g for g in self.favorite_groups if g.get('id')==group_id),None)
            if group is None:self.active_favorite_group=None;return self.show_favorites()
            favorite_paths=sorted(set(group.get('folders',[])),key=natural_key)
        for raw in favorite_paths:
            decoded=decode_archive_path(str(raw))
            if decoded:
                item=self.resolve_archive_favorite(str(raw),archive_favorite_cache)
                if item is not None:fav.append(item)
                continue
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
<ul><li>Left-click a thumbnail to open a folder or view a file. Click a thumbnail name to open its folder in File Explorer or reveal the file there. <b>Alt+left-click</b> reveals an item at its exact location.</li><li>Right-click or <b>Backspace</b> goes up/back. The Home button returns to the starting folder. Middle-click a folder to open it in a new tab.</li><li><b>Tab / Shift+Tab</b> switches tabs. Drag a tab to reorder it. Hold <b>Ctrl</b> and click tabs to select multiple tabs for coloring; right-click a tab for its outline color menu. Scroll over the tab strip to move sideways.</li><li>When enabled, Hide tabs with the main bar hides the navigation strip, path, and status area with the main panel. Moving to the top reveals the panel; it stays open briefly after opening a tab.</li></ul>
<h2>Keyboard and selection</h2>
<ul><li>Arrow keys move the keyboard highlight; holding an arrow accelerates. <b>Enter or Space</b> opens the highlighted item; <b>Space</b> clears an active selection. Mouse movement returns navigation to the pointer. <b>Q</b> toggles focus between search and keyboard navigation.</li><li><b>Shift+drag</b> draws a selection rectangle and toggles items inside it. Drag near the top or bottom edge to auto-scroll. Selection remains while you navigate between folders.</li><li><b>Ctrl+A</b> selects all items on the current page. If all are already selected it clears them; if only some are selected it selects the rest. Right-side buttons select all, clear selection, or undo the last deselection.</li><li><b>Ctrl+Z</b> undoes one of the five most recent move, delete, or favorites actions. <b>Ctrl+S</b> undoes the last deselection. <b>Shift+Delete</b> sends selected files and folders to the Recycle Bin. <b>Ctrl+X / Ctrl+C / Ctrl+V</b> cut, copy, and paste selected files or folders; <b>Ctrl+E</b> extracts selected ZIP/RAR/CBR/CBZ archives into the current folder.</li><li><b>Ctrl+N</b> creates and names a folder inline. <b>Ctrl+right-click</b> a thumbnail opens file actions; choose Rename to edit its name inline. <b>Shift+R</b> renames one selected item. Middle-click a File Tree folder to open it in a new tab. <b>F11</b> toggles the main window fullscreen.</li><li>Hold <b>Ctrl</b> while dragging to enable organization mode. Release <b>Ctrl</b> to leave it. Drag near the top or bottom to auto-scroll. Drop on the central part of a folder thumbnail to move selected items inside; drop near either edge to reorder. Favorites groups still accept dragged items.</li></ul>
<h2>Favorites and Farview</h2>
<ul><li>The Favorites button opens Favorites; from a group or subfolder it returns to the Favorites root, and from the root it exits Favorites. Use the plus on a thumbnail to add it to Favorites or choose a group.</li><li>In Favorites, <b>Delete</b> removes selected entries. Click a group name to rename it, including outside organization mode. Groups stay at the top of the Favorites list.</li><li>Farview creates a tab that lines up images/files found in subfolders. It can be created from a folder or selection, and multiple Farview tabs can use different sorting. The Personal order sort uses a saved custom sequence when one exists.</li><li>Use Manage personal orders to search, sort, or remove saved sequences. Excluded paths are listed in the personal-order settings.</li></ul>
<h2>Sorting and search</h2>
<ul><li>Keys 1–6 select Name, Last modified, Last created, Type, Size, and Random sorting; 7 selects Personal order. The direction indicator switches ascending and descending. Personal order applies a custom sequence for the current folder or view.</li><li>Use the search field to filter items. Recursive search can be enabled in Performance. The clear button empties the search. Tools → Scroll behavior sets rows per wheel notch (including fractional rows) and optional momentum.</li></ul>
<h2>Image viewer</h2>
<ul><li><b>Left / right arrows</b> or <b>Q / E</b> show the previous / next image. Holding a navigation key accelerates. In vertical reader mode the left and right arrows scroll by a third of a page.</li><li>PDF, CBZ, CBR, ZIP, and RAR archives show readable pages or files. The visualization button cycles through four layouts for images and readable files: single image, double image, vertical reader, and horizontal reader. Page position and reader zoom are remembered until Magellan closes.</li><li><b>Home / End</b> go to the first / last page; <b>Page Up / Page Down</b> move by a page. In reader modes the wheel scrolls unless zoom behavior is selected or double-click zoom is active. The zoom bar keeps vertical page scrolling available. Dragging pans zoomed pages; horizontal reader pages can also be dragged sideways.</li><li>Right-click, Escape, or Up closes the viewer. Double-click toggles zoom. The wheel swipe/zoom default is configurable in Tools → Scroll behavior. The Compare control overlays the current and next image/page with a shared zoom bar and double-click zoom; wheel zoom and ordinary dragging affect individual images. Shift-drag moves the comparison box; drag the divider to adjust the wipe.</li><li><b>R</b> rotates clockwise; <b>F</b> flips horizontally; <b>G</b> fits the image; <b>H</b> toggles slideshow; minus / equals zoom out/in; <b>WASD</b> pans. <b>Delete</b> sends the current image to the Recycle Bin.</li><li>Hover the top-right counter to see the current file or page position. Click it to jump to a number. In PDFs, <b>Ctrl+F</b> opens text search and <b>Enter</b> cycles through matches. Use the bottom controls for rotate, flip, fit, page layout, and slideshow. Hover the top area for image details.</li><li>Tools → Set Magellan as default viewer registers the supported formats with Windows and opens Windows Default apps so you can choose Magellan.</li></ul>
<h2>Thumbnails and panels</h2>
<ul><li>Hover a thumbnail for size/date details and its Favorites control. Hover the right edge for selection and group controls.</li><li>When auto-hide is enabled, move to the top edge for the main panel or the left edge for File Tree. The tree remembers its open paths and selected location; use its × button to close it.</li><li>View → Hover and hide settings contains thumbnail hover, panel, right-side button, and scrollbar controls. Scrollbars fade away when the pointer leaves and return when hovered.</li><li>Performance settings include recursive search, thumbnail cache controls, and other resource limits. Automatic folder refresh is also in View. GPU thumbnail rendering is optional and requires a restart. Experimental GPU panel composition keeps backplates on the GPU beneath normal controls, requires a full restart, and falls back automatically if unavailable. Explorer viewer-only launches keep their lightweight startup. Experimental GPU panel blur is a separate Performance toggle, disabled by default; it requires active GPU rendering and may look slightly different. Menus and unsupported samples keep regular blur. Smooth scrolling is intended for GPU rendering; Tools → Scroll behavior controls distance and glide duration. Memory and on-disk thumbnail limits are separate; the disk limit is disabled by default.</li></ul>
<h2>More controls and settings</h2><ul>
<li><b>Ctrl+T</b> reopens the last closed tab; <b>Shift+T</b> opens an empty tab. <b>Middle-click</b> closes a tab or opens folders, archives and readable files in a new tab.</li>
<li><b>Shift+click</b> toggles individual thumbnail selections. <b>Double-click an empty area</b> clears selection. Opening folders keeps selection, including selections across folders. Favorites commands and Farview can use the whole selection.</li>
<li><b>Ctrl+right-click</b> also opens file actions from empty page space. Rename edits the thumbnail name; <b>Enter</b> confirms. Scrolling closes the editor. Copy/paste can extract selected archive members. Operation messages show progress; their <b>×</b> cancels an ongoing operation.</li>
<li>Continuous viewer swipe in View includes pages from archives and readable files; otherwise navigation skips those containers. Farview follows its displayed sequence across containers.</li>
<li>Slideshow interval is in View; the pointer hides after one idle second during a slideshow. Hover the viewer top for details and position, or the bottom and side edges for controls. Click anywhere on the zoom bar to select a level.</li>
<li>Tools includes image-search options and additional Google Lens search text. Image search also works on images inside ZIP, RAR, CBZ and CBR archives, including nested archives. Right-click first exits image search. Appearance settings independently customize the main panel, buttons, tabs, File Tree, context menus and extra gradient. Changes preview live; Save keeps them and Cancel restores them.</li>
<li>Keep thumbnails includes child folders. Performance controls memory caching, disk caching and its optional size limit, cache location, recursive search, and loading a readable file into memory. Only one PDF is retained in that memory cache and idle documents are released.</li>
<li>Settings backups retain up to ten copies in AppData → Magellan → Backups, after changed full-app sessions and at least one minute apart. Favorites and groups are saved as references; backups do not copy the original files. Viewer-only sessions do not create backups.</li>
<li>Explorer opens supported files directly in the viewer; the optional <b>Open in Magellan</b> context command opens their location in the main app. Tools manages that command and default-viewer registration.</li></ul>''')
        content.setWordWrap(True);content.setTextFormat(Qt.RichText);content.setMargin(14)
        scroll=QScrollArea(dlg);scroll.setWidgetResizable(True);scroll.setWidget(content);layout.addWidget(scroll)
        buttons=QDialogButtonBox(QDialogButtonBox.Close);buttons.rejected.connect(dlg.reject);layout.addWidget(buttons);dlg.exec()

if __name__=='__main__':
    # Only full browser launches request sharing. Keep Explorer's image-only
    # startup free of the global OpenGL context and appearance initialization.
    _startup_args=[arg for arg in sys.argv[1:] if arg!='--developer']
    _browser_launch=not _startup_args or _startup_args[0] in ('--open-in-magellan','--open-folder')
    if _browser_launch and (Path(os.environ.get('APPDATA',str(Path.home()))) / 'Magellan' / 'gpu_composition.enabled').is_file():
        QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app=QApplication(sys.argv);app.setApplicationName('Magellan');app.setStyle('Fusion')
    lock_dir=Path(QStandardPaths.writableLocation(QStandardPaths.AppDataLocation));lock_dir.mkdir(parents=True,exist_ok=True)
    _INSTANCE_LOCK=QLockFile(str(lock_dir/'magellan-instance.lock'))
    _SERVER_NAME='magellan-'+hashlib.sha256(str(lock_dir.resolve()).encode('utf-8')).hexdigest()[:20]
    _OPEN_ARGS=[];_OPEN_IN_MAGELLAN=False;_CLI_ARGS=[arg for arg in sys.argv[1:] if arg!='--developer']
    if len(_CLI_ARGS)>=2 and _CLI_ARGS[0] in ('--open-in-magellan','--open-folder'):
        try:
            _candidate=Path(_CLI_ARGS[1]).expanduser()
            if _candidate.is_dir() or _candidate.is_file():_OPEN_ARGS.append(str(_candidate.resolve()));_OPEN_IN_MAGELLAN=True
        except (OSError,ValueError):pass
    else:
        for _arg in _CLI_ARGS:
            try:
                _candidate=Path(_arg).expanduser()
                if _candidate.is_file() and _candidate.suffix.casefold() in PREVIEW_EXTS:_OPEN_ARGS.append(str(_candidate.resolve()))
            except (OSError,ValueError):pass
    if not _INSTANCE_LOCK.tryLock(0):
        if _INSTANCE_LOCK.error()==QLockFile.LockFailedError and _OPEN_ARGS:
            try:
                lock_info=_INSTANCE_LOCK.getLockInfo();pid_value=lock_info[1] if lock_info and len(lock_info)>1 and isinstance(lock_info[0],bool) else (lock_info[0] if lock_info else 0);existing_pid=int(pid_value)
                if existing_pid:ctypes.windll.user32.AllowSetForegroundWindow(existing_pid)
            except (AttributeError,OSError,TypeError,ValueError):logger.debug('Could not grant foreground permission to the running instance',exc_info=True)
            _socket=QLocalSocket();_socket.connectToServer(_SERVER_NAME)
            if _socket.waitForConnected(1200):
                _payload={'action':'open_in_magellan','path':_OPEN_ARGS[0]} if _OPEN_IN_MAGELLAN else _OPEN_ARGS
                _socket.write(json.dumps(_payload).encode('utf-8'));_socket.waitForBytesWritten(1200);_socket.disconnectFromServer();sys.exit(0)
            QMessageBox.warning(None,'Magellan is already running','The existing Magellan window could not receive the file-open request. Try again in a moment.')
        elif _INSTANCE_LOCK.error()==QLockFile.LockFailedError:
            QMessageBox.warning(None,'Magellan is already running','Another Magellan instance is still running. Close it in Task Manager before starting another copy. If it has already exited, wait a moment and try again.')
        else:
            QMessageBox.warning(None,'Magellan could not start','Magellan could not create its single-instance lock file. Check that the application settings folder is writable.')
        sys.exit(0)
    QLocalServer.removeServer(_SERVER_NAME);_SERVER=QLocalServer(app);_SERVER.listen(_SERVER_NAME);app._magellan_local_server=_SERVER
    w=Explorer(_OPEN_ARGS[0] if _OPEN_ARGS else None)
    if not _OPEN_ARGS:w.show()
    if _OPEN_ARGS:QTimer.singleShot(0,lambda:w.open_external_file(_OPEN_ARGS[0],open_in_magellan=_OPEN_IN_MAGELLAN))
    def _receive_open_requests():
        while _SERVER.hasPendingConnections():
            _client=_SERVER.nextPendingConnection()
            def _read_client(client=_client):
                try:
                    payload=json.loads(bytes(client.readAll()).decode('utf-8'))
                    open_in_magellan=isinstance(payload,dict) and payload.get('action') in ('open_in_magellan','open_folder')
                    requested=[payload.get('path')] if open_in_magellan else payload if isinstance(payload,list) else []
                    paths=[]
                    for raw in requested:
                        if not isinstance(raw,str):continue
                        candidate=Path(raw)
                        if candidate.is_dir() and open_in_magellan:paths.append(str(candidate))
                        elif candidate.is_file() and (open_in_magellan or candidate.suffix.casefold() in PREVIEW_EXTS):paths.append(str(candidate))
                    if paths:QTimer.singleShot(0,lambda p=paths[0],target=open_in_magellan:w.open_external_file(p,open_in_magellan=target))
                except Exception:logger.warning('Invalid local file-open request',exc_info=True)
                client.disconnectFromServer();client.deleteLater()
            _client.readyRead.connect(_read_client)
            if _client.bytesAvailable():_read_client()
    _SERVER.newConnection.connect(_receive_open_requests)
    sys.exit(app.exec())
