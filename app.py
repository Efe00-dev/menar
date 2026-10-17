"""Menar-ı Tedris — deployable LMS starter.

Run with `uvicorn app:app --reload`, configured with SESSION_SECRET in production.
Demo content and prices are examples, not actual offerings.
"""
import os
import re
import json
import hmac
import secrets
import hashlib
import sqlite3
import mimetypes
from urllib.parse import quote
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get('DATABASE_PATH', str(ROOT / 'data' / 'menar.db')))
UPLOADS = Path(os.environ.get('UPLOAD_DIR', str(ROOT / 'uploads')))
UPLOADS.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
SECRET = os.environ.get('SESSION_SECRET', secrets.token_urlsafe(40))
PRODUCTION = os.environ.get('PRODUCTION', '') == '1'
DEMO = os.environ.get('SITE_DEMO', '1') != '0'
MAX_VIDEO_BYTES = 1024 * 1024 * 1024  # 1GB, small-site starter. Use managed storage for production.

app = FastAPI(title='Menar-ı Tedris', docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=SECRET, session_cookie='menar_session',
                   same_site='lax', https_only=PRODUCTION, max_age=60 * 60 * 24 * 14)
app.mount('/static', StaticFiles(directory=str(ROOT / 'static')), name='static')

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA journal_mode=WAL')
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 350_000)
    return 'pbkdf2_sha256$350000$' + salt.hex() + '$' + digest.hex()


def verify_password(password, stored):
    try:
        algo, rounds, salt, digest = stored.split('$')
        if algo != 'pbkdf2_sha256':
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(actual, bytes.fromhex(digest))
    except (ValueError, TypeError):
        return False


def init_db():
    with db() as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
          password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'student',
          created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS categories(
          id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
          description TEXT NOT NULL, icon TEXT NOT NULL, accent TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS courses(
          id INTEGER PRIMARY KEY, category_id INTEGER NOT NULL REFERENCES categories(id),
          slug TEXT UNIQUE NOT NULL, title TEXT NOT NULL, subtitle TEXT NOT NULL,
          description TEXT NOT NULL, instructor TEXT NOT NULL, level TEXT NOT NULL,
          cover_style INTEGER NOT NULL DEFAULT 1, price_kurus INTEGER NOT NULL,
          published INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS lessons(
          id INTEGER PRIMARY KEY, course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
          title TEXT NOT NULL, duration_label TEXT NOT NULL DEFAULT 'Video',
          position INTEGER NOT NULL DEFAULT 0, is_preview INTEGER NOT NULL DEFAULT 0,
          video_url TEXT NOT NULL DEFAULT '', stored_filename TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS live_sessions(
          id INTEGER PRIMARY KEY, course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
          title TEXT NOT NULL, starts_at TEXT NOT NULL, meeting_url TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS enrollments(
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
          course_id INTEGER NOT NULL REFERENCES courses(id), source TEXT NOT NULL,
          created_at TEXT NOT NULL, expires_at TEXT NOT NULL DEFAULT '', UNIQUE(user_id,course_id));
        CREATE TABLE IF NOT EXISTS orders(
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
          course_id INTEGER NOT NULL REFERENCES courses(id),
          amount_kurus INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS progress(
          user_id INTEGER NOT NULL REFERENCES users(id),
          lesson_id INTEGER NOT NULL REFERENCES lessons(id), completed_at TEXT NOT NULL,
          PRIMARY KEY(user_id,lesson_id));
        CREATE TABLE IF NOT EXISTS settings(
          key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
        ''')
        # Eski kurulumlarda veritabanını kaybetmeden opsiyonel erişim süresi ekle.
        if 'expires_at' not in [r['name'] for r in conn.execute('PRAGMA table_info(enrollments)')]:
            conn.execute("ALTER TABLE enrollments ADD COLUMN expires_at TEXT NOT NULL DEFAULT ''")
        existing = conn.execute('SELECT COUNT(*) FROM categories').fetchone()[0]
        if existing:
            return
        cats = [
          ('ilitam-arapca','İLİTAM Arapça Dersleri','İLİTAM Arapça konuları ve sınava hazırlık.','book-open','olive'),
          ('aof-arapca','AÖF Arapça Dersleri','Açıköğretim için adım adım Arapça.','graduation-cap','sand'),
          ('pratik-arapca','Pratik Arapça Dersleri','Konuşma, kelime ve günlük iletişim.','messages-square','lavender'),
          ('genel-ilitam','Genel İLİTAM Dersleri','Temel ilahiyat derslerini pekiştirin.','library','blue'),
          ('dhbt','DHBT Hazırlık','Konu anlatımı ve soru çözümü.','clipboard-list','peach'),
        ]
        conn.executemany('INSERT INTO categories(slug,name,description,icon,accent) VALUES(?,?,?,?,?)',cats)
        examples = [
          (1,'ilitam-arapca-temelden-ileri','İLİTAM Arapça: Temelden İleriye','Sağlam bir gramer temeli oluşturun.','İsim, fiil ve cümle yapısından başlayarak İLİTAM müfredatını örneklerle öğrenin.','Menar Eğitim Ekibi','Tüm seviyeler',1,129000),
          (2,'aof-arapca-sinav-kampi','AÖF Arapça Sınav Kampı','Sınavlara planlı, anlaşılır hazırlık.','Ünite özetleri, örnekler ve soru çözümleri ile sınav konularını tekrar edin.','Menar Eğitim Ekibi','Orta seviye',2,89000),
          (3,'pratik-arapca-konusma','Günlük Hayatta Pratik Arapça','Kelimeleri cümlelere dönüştürün.','Günlük konuşma kalıpları, telaffuz alıştırmaları ve iletişim örnekleri.','Menar Eğitim Ekibi','Başlangıç',3,99000),
          (4,'tefsir-tarihi-ve-usulu','Tefsir Tarihi ve Usulü','Temel kavramlar, ekoller ve yöntemler.','Düzenli konu anlatımlarıyla tefsir tarihi ve usulünün temel başlıkları.','Menar Eğitim Ekibi','Tüm seviyeler',4,79000),
          (5,'dhbt-hazirlik-programi','DHBT Hazırlık Programı','Konuları toparlayın, sorularla pekiştirin.','DHBT kapsamındaki başlıklar, tekrar programı ve örnek soru uygulamaları.','Menar Eğitim Ekibi','Orta seviye',5,109000),
          (1,'arapca-nahiv-atolyesi','Arapça Nahiv Atölyesi','Cümle yapılarını kolayca çözümleyin.','Nahiv kurallarını kısa dersler ve bol örnekle çalışın.','Menar Eğitim Ekibi','Orta seviye',6,69000),
        ]
        conn.executemany('''INSERT INTO courses(category_id,slug,title,subtitle,description,instructor,level,cover_style,price_kurus,created_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?)''',[(c,slug,title,sub,desc,ins,lev,style,price,now_iso()) for c,slug,title,sub,desc,ins,lev,style,price in examples])
        for course_id in range(1,7):
            for num,title,duration in [(1,'Hoş geldiniz ve ders planı','04:20'),(2,'Temel kavramlara giriş','18:45'),(3,'Uygulamalı örnekler','24:10'),(4,'Konu tekrarı ve alıştırmalar','16:30')]:
                conn.execute('''INSERT INTO lessons(course_id,title,duration_label,position,is_preview,stored_filename)
                                VALUES(?,?,?,?,?,?)''',(course_id,title,duration,num,int(num==1),
                                  'ornek-tanitim.mp4' if course_id==1 and num==1 and (UPLOADS/'ornek-tanitim.mp4').exists() else ''))
        next_live = (datetime.now(timezone(timedelta(hours=3))) + timedelta(days=2)).replace(hour=20, minute=0, second=0,microsecond=0)
        conn.execute('INSERT INTO live_sessions(course_id,title,starts_at) VALUES(?,?,?)',
                     (1,'Arapça soru-cevap buluşması',next_live.isoformat()))
        conn.execute('INSERT INTO live_sessions(course_id,title,starts_at) VALUES(?,?,?)',
                     (5,'DHBT canlı tekrar dersi',(next_live+timedelta(days=3)).isoformat()))

init_db()


def json_error(msg, status=400):
    return JSONResponse({'error':msg}, status_code=status)


def safe_user(request: Request):
    uid = request.session.get('uid')
    if not uid:
        return None
    with db() as conn:
        row=conn.execute('SELECT id,name,email,role FROM users WHERE id=?',(uid,)).fetchone()
    return dict(row) if row else None


def require_user(request):
    user=safe_user(request)
    if not user:
        raise HTTPException(401,'Oturum açmanız gerekiyor.')
    return user


def require_admin(request):
    user=require_user(request)
    if user['role']!='admin':
        raise HTTPException(403,'Yönetici yetkisi gerekiyor.')
    return user


def csrf(request):
    token = request.session.get('csrf')
    passed = request.headers.get('x-csrf-token','')
    if not token or not hmac.compare_digest(token, passed):
        raise HTTPException(403,'Güvenlik anahtarı geçersiz. Sayfayı yenileyin.')


def body_obj(data):
    if not isinstance(data,dict):
        raise HTTPException(400,'Geçersiz veri.')
    return data


def clean_course(row):
    obj=dict(row)
    obj.pop('price_kurus',None)  # Fiyatlar arayüzde veya genel API'de yayımlanmaz.
    return obj


def enrolled(conn, uid, course_id):
    return bool(conn.execute("SELECT 1 FROM enrollments WHERE user_id=? AND course_id=? AND (expires_at='' OR expires_at>?)",(uid,course_id,now_iso())).fetchone())


def whatsapp_number(conn):
    row=conn.execute("SELECT value FROM settings WHERE key='sales_whatsapp'").fetchone()
    return row['value'] if row else ''


def whatsapp_order_link(order_id, course_title, phone):
    if not phone:
        return None
    # A message is prepared, never auto-sent. The student must explicitly send it.
    message=(f'Merhaba, Menar-ı Tedris üzerinden "{course_title}" eğitimine kayıt olmak istiyorum. '
             f'Başvuru numaram: MT-{order_id:06d}. '
             'IBAN ve ödeme açıklaması bilgilerini paylaşabilir misiniz?')
    return f'https://wa.me/{phone}?text={quote(message, safe="")}'

@app.get('/')
async def home():
    return FileResponse(ROOT/'static'/'index.html')

@app.get('/api/bootstrap')
async def bootstrap(request: Request):
    if 'csrf' not in request.session:
        request.session['csrf']=secrets.token_urlsafe(28)
    user=safe_user(request)
    with db() as conn:
        categories=[dict(r) for r in conn.execute('SELECT * FROM categories ORDER BY id')]
        courses=[clean_course(r) for r in conn.execute('''SELECT c.*,cat.name AS category_name FROM courses c
                              JOIN categories cat ON cat.id=c.category_id WHERE c.published=1 ORDER BY c.id''')]
        lives=[dict(r) for r in conn.execute('''SELECT l.id,l.course_id,l.title,l.starts_at,c.title AS course_title
                    FROM live_sessions l JOIN courses c ON c.id=l.course_id WHERE l.starts_at>=? ORDER BY l.starts_at LIMIT 4''',
                    ((datetime.now(timezone.utc)-timedelta(hours=4)).isoformat(),))]
    return {'categories':categories,'courses':courses,'lives':lives,'user':user,'csrf':request.session['csrf'],'demo':DEMO}

@app.post('/api/auth/register')
async def register(request: Request):
    csrf(request)
    obj=body_obj(await request.json())
    name=str(obj.get('name','')).strip()
    email=str(obj.get('email','')).lower().strip()
    pw=str(obj.get('password',''))
    if len(name)<2 or len(name)>80 or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',email) or len(pw)<8:
        return json_error('Ad (en az 2 karakter), geçerli e-posta ve en az 8 karakter şifre girin.')
    try:
        with db() as conn:
            cur=conn.execute('INSERT INTO users(name,email,password_hash,created_at) VALUES(?,?,?,?)',(name,email,hash_password(pw),now_iso()))
            uid=cur.lastrowid
    except sqlite3.IntegrityError:
        return json_error('Bu e-posta zaten kayıtlı.')
    request.session['uid']=uid
    request.session['csrf']=secrets.token_urlsafe(28)
    return {'ok':True,'csrf':request.session['csrf'],'user':safe_user(request)}

@app.post('/api/auth/login')
async def login(request: Request):
    csrf(request)
    obj=body_obj(await request.json())
    email=str(obj.get('email','')).lower().strip()
    pw=str(obj.get('password',''))
    with db() as conn:
        user=conn.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
    if not user or not verify_password(pw,user['password_hash']):
        return json_error('E-posta veya şifre hatalı.',401)
    request.session['uid']=user['id']
    request.session['csrf']=secrets.token_urlsafe(28)
    return {'ok':True,'csrf':request.session['csrf'],'user':safe_user(request)}

@app.post('/api/auth/logout')
async def logout(request: Request):
    csrf(request)
    request.session.clear()
    request.session['csrf']=secrets.token_urlsafe(28)
    return {'ok':True,'csrf':request.session['csrf']}

@app.get('/api/courses/{slug}')
async def course_detail(slug: str, request: Request):
    user=safe_user(request)
    with db() as conn:
        row=conn.execute('''SELECT c.*,cat.name AS category_name FROM courses c
                JOIN categories cat ON cat.id=c.category_id WHERE c.slug=?''',(slug,)).fetchone()
        if not row or (not row['published'] and not (user and (user['role']=='admin' or enrolled(conn,user['id'],row['id'])))):
            raise HTTPException(404,'Eğitim bulunamadı.')
        course=clean_course(row)
        owns=bool(user and (user['role']=='admin' or enrolled(conn,user['id'],row['id'])))
        course['owned']=owns
        lessons=[]
        for r in conn.execute('SELECT id,course_id,title,duration_label,position,is_preview,video_url,stored_filename FROM lessons WHERE course_id=? ORDER BY position,id',(row['id'],)):
            item=dict(r)
            can_watch=owns or bool(item['is_preview'])
            item['can_watch']=can_watch
            item['has_video']=bool(item['video_url'] or item['stored_filename'])
            item.pop('video_url')
            item.pop('stored_filename')
            lessons.append(item)
        course['lessons']=lessons
        course['sessions']=[]
        for live in conn.execute('SELECT * FROM live_sessions WHERE course_id=? ORDER BY starts_at',(row['id'],)):
            item=dict(live)
            if not owns:
                item.pop('meeting_url')
            course['sessions'].append(item)
        if user:
            course['completed']=[r[0] for r in conn.execute('''SELECT p.lesson_id FROM progress p JOIN lessons l ON l.id=p.lesson_id
                                     WHERE p.user_id=? AND l.course_id=?''',(user['id'],row['id']))]
        else:
            course['completed']=[]
    return course

@app.get('/api/me')
async def my_page(request: Request):
    user=require_user(request)
    with db() as conn:
        courses=[clean_course(r) for r in conn.execute('''SELECT c.*,cat.name AS category_name FROM enrollments e
             JOIN courses c ON e.course_id=c.id JOIN categories cat ON cat.id=c.category_id
             WHERE e.user_id=? AND (e.expires_at='' OR e.expires_at>?) ORDER BY e.created_at DESC''',(user['id'],now_iso()))]
        orders=[dict(r) for r in conn.execute('''SELECT o.id,o.user_id,o.course_id,o.status,o.created_at,c.title AS course_title FROM orders o
                JOIN courses c ON o.course_id=c.id WHERE o.user_id=? ORDER BY o.id DESC''',(user['id'],))]
        phone=whatsapp_number(conn)
        for order in orders:
            order['whatsapp_url']=(whatsapp_order_link(order['id'],order['course_title'],phone)
                                   if order['status']=='pending' else None)
        finished=conn.execute('SELECT COUNT(*) FROM progress WHERE user_id=?',(user['id'],)).fetchone()[0]
    return {'courses':courses,'orders':orders,'completed_count':finished,'user':user}

@app.post('/api/orders')
async def place_order(request: Request):
    csrf(request)
    user=require_user(request)
    obj=body_obj(await request.json())
    try: cid=int(obj.get('course_id',0))
    except (ValueError,TypeError): return json_error('Geçersiz eğitim.')
    with db() as conn:
        course=conn.execute('SELECT id,title,price_kurus FROM courses WHERE id=? AND published=1',(cid,)).fetchone()
        if not course:
            return json_error('Eğitim bulunamadı.',404)
        if enrolled(conn,user['id'],cid):
            return json_error('Bu eğitime zaten erişiminiz var.')
        phone=whatsapp_number(conn)
        if not phone:
            return json_error('WhatsApp kayıt numarası henüz ayarlanmadı. Lütfen daha sonra tekrar deneyin.',409)
        existing=conn.execute("SELECT id FROM orders WHERE user_id=? AND course_id=? AND status='pending'",(user['id'],cid)).fetchone()
        if existing:
            return {'ok':True,'order_id':existing['id'],'pending':True,
                    'whatsapp_url':whatsapp_order_link(existing['id'],course['title'],phone)}
        cur=conn.execute('INSERT INTO orders(user_id,course_id,amount_kurus,created_at) VALUES(?,?,?,?)',
                         (user['id'],cid,course['price_kurus'],now_iso()))
        order_id=cur.lastrowid
    return {'ok':True,'order_id':order_id,'pending':True,
            'whatsapp_url':whatsapp_order_link(order_id,course['title'],phone)}

@app.get('/api/lessons/{lesson_id}/play')
async def play_lesson(lesson_id: int, request: Request):
    user=safe_user(request)
    with db() as conn:
        lesson=conn.execute('SELECT * FROM lessons WHERE id=?',(lesson_id,)).fetchone()
        if not lesson: raise HTTPException(404,'Ders bulunamadı.')
        owns=bool(user and (user['role']=='admin' or enrolled(conn,user['id'],lesson['course_id'])))
        if not owns and not lesson['is_preview']:
            raise HTTPException(403,'Bu video için eğitime kayıt olmanız gerekiyor.')
        if lesson['stored_filename']:
            return {'type':'file','url':f'/api/videos/{lesson_id}'}
        if lesson['video_url']:
            return {'type':'external','url':lesson['video_url']}
    return {'type':'missing'}

@app.get('/api/videos/{lesson_id}')
async def stream_lesson(lesson_id: int, request: Request):
    user=safe_user(request)
    with db() as conn:
        lesson=conn.execute('SELECT * FROM lessons WHERE id=?',(lesson_id,)).fetchone()
        if not lesson or not lesson['stored_filename']:
            raise HTTPException(404,'Video bulunamadı.')
        owns=bool(user and (user['role']=='admin' or enrolled(conn,user['id'],lesson['course_id'])))
        if not owns and not lesson['is_preview']:
            raise HTTPException(403,'Bu videoya erişiminiz yok.')
        path=UPLOADS/Path(lesson['stored_filename']).name
        if not path.exists(): raise HTTPException(404,'Video dosyası bulunamadı.')
    return FileResponse(path,media_type='video/mp4',headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})

@app.post('/api/progress/{lesson_id}')
async def mark_completed(lesson_id:int, request:Request):
    csrf(request)
    user=require_user(request)
    with db() as conn:
        lesson=conn.execute('SELECT * FROM lessons WHERE id=?',(lesson_id,)).fetchone()
        if not lesson or not (user['role']=='admin' or enrolled(conn,user['id'],lesson['course_id'])):
            raise HTTPException(403,'Bu derse erişiminiz yok.')
        conn.execute('INSERT OR IGNORE INTO progress(user_id,lesson_id,completed_at) VALUES(?,?,?)',(user['id'],lesson_id,now_iso()))
    return {'ok':True}

@app.get('/api/admin/overview')
async def admin_overview(request:Request):
    require_admin(request)
    with db() as conn:
        counts={k:conn.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for k,name in
                [('students','users'),('courses','courses'),('orders','orders'),('lessons','lessons')]}
        orders=[dict(r) for r in conn.execute('''SELECT o.id,o.user_id,o.course_id,o.status,o.created_at,c.title AS course_title,u.name AS user_name,u.email AS user_email
                             FROM orders o JOIN courses c ON c.id=o.course_id JOIN users u ON u.id=o.user_id
                             ORDER BY o.id DESC LIMIT 100''')]
        courses=[dict(r) for r in conn.execute('SELECT id,title,slug,category_id,subtitle,description,instructor,level,published,cover_style FROM courses ORDER BY id DESC')]
        lives=[dict(r) for r in conn.execute('''SELECT l.*,c.title AS course_title FROM live_sessions l
                       JOIN courses c ON c.id=l.course_id ORDER BY l.starts_at DESC LIMIT 30''')]
        lessons=[dict(r) for r in conn.execute('''SELECT l.id,l.course_id,l.title,l.duration_label,l.position,l.is_preview,
                       CASE WHEN l.stored_filename != '' OR l.video_url != '' THEN 1 ELSE 0 END AS has_video,c.title AS course_title
                       FROM lessons l JOIN courses c ON c.id=l.course_id ORDER BY c.id,l.position,l.id''')]
        access=[dict(r) for r in conn.execute("""SELECT e.id,e.user_id,e.course_id,e.created_at,e.expires_at,
            u.name AS user_name,u.email AS user_email,c.title AS course_title
            FROM enrollments e JOIN users u ON u.id=e.user_id
            JOIN courses c ON c.id=e.course_id ORDER BY e.id DESC LIMIT 200""")]
        phone=whatsapp_number(conn)
    return {'counts':counts,'orders':orders,'courses':courses,'lives':lives,'lessons':lessons,'access':access,'whatsapp_phone':phone}

@app.post('/api/admin/settings/whatsapp')
async def set_whatsapp(request: Request):
    csrf(request)
    require_admin(request)
    obj=body_obj(await request.json())
    raw=str(obj.get('phone','')).strip()
    digits=re.sub(r'[^0-9]', '', raw)
    # Turkish mobile phone shorthand and full international number are supported.
    if len(digits)==10 and digits.startswith('5'):
        digits='90'+digits
    if len(digits)==11 and digits.startswith('05'):
        digits='90'+digits[1:]
    if not 8 <= len(digits) <= 15 or digits.startswith('0') or (raw and not re.fullmatch(r'[+0-9 ()-]+',raw)):
        return json_error('Ülke koduyla geçerli bir WhatsApp numarası girin. Örn: 905xxxxxxxxx.')
    with db() as conn:
        conn.execute("INSERT INTO settings(key,value) VALUES('sales_whatsapp',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(digits,))
    return {'ok':True,'phone':digits}

@app.post('/api/admin/orders/{order_id}/approve')
async def approve_order(order_id:int, request:Request):
    csrf(request)
    require_admin(request)
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload,dict):
        return json_error('Geçersiz kayıt seçeneği.')
    try:
        days=int(payload.get('access_days',0))
    except (ValueError, TypeError):
        return json_error('Erişim süresi geçersiz.')
    if days not in (0,30,60,90,180,365):
        return json_error('Desteklenmeyen erişim süresi.')
    expires=(datetime.now(timezone.utc)+timedelta(days=days)).isoformat() if days else ''
    with db() as conn:
        order=conn.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone()
        if not order: return json_error('Talep bulunamadı.',404)
        if order['status']=='pending':
            conn.execute("UPDATE orders SET status='approved' WHERE id=?",(order_id,))
            conn.execute('INSERT OR IGNORE INTO enrollments(user_id,course_id,source,created_at,expires_at) VALUES(?,?,?,?,?)',
                         (order['user_id'],order['course_id'],'manual-approval',now_iso(),expires))
    return {'ok':True}

@app.post('/api/admin/courses')
async def add_course(request:Request):
    csrf(request)
    require_admin(request)
    obj=body_obj(await request.json())
    title=str(obj.get('title','')).strip()
    subtitle=str(obj.get('subtitle','')).strip()
    desc=str(obj.get('description','')).strip()
    instructor=str(obj.get('instructor','Menar Eğitim Ekibi')).strip()
    slug=re.sub(r'[^a-z0-9-]','-',str(obj.get('slug','')).lower().strip()).strip('-')
    try: category_id=int(obj.get('category_id'))
    except (ValueError,TypeError): return json_error('Kategori geçersiz.')
    if len(title)<4 or len(slug)<4 or len(desc)<8:
        return json_error('Başlık, adres ve açıklama alanlarını kontrol edin.')
    try:
        with db() as conn:
            cur=conn.execute('''INSERT INTO courses(category_id,slug,title,subtitle,description,instructor,level,cover_style,price_kurus,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)''',(category_id,slug,title,subtitle,desc,instructor,'Tüm seviyeler',6,0,now_iso()))
    except sqlite3.IntegrityError:
        return json_error('Eğitim adresi kullanımda ya da kategori hatalı.')
    return {'ok':True,'course_id':cur.lastrowid}

@app.post('/api/admin/lessons')
async def add_lesson(request:Request, course_id:int=Form(...), title:str=Form(...),
                     duration_label:str=Form('Video'), is_preview:str=Form('false'),
                     video_url:str=Form(''), video:Optional[UploadFile]=File(None)):
    csrf(request)
    require_admin(request)
    if not title.strip() or len(title)>150:
        return json_error('Ders başlığı gerekli.')
    if video_url and not re.match(r'^https://',video_url):
        return json_error('Dış bağlantı HTTPS olmalı.')
    stored=''
    if video and video.filename:
        ext=Path(video.filename).suffix.lower()
        if ext!='.mp4': return json_error('Şimdilik yalnızca MP4 yüklenebilir.')
        stored=secrets.token_hex(24)+'.mp4'
        dest=UPLOADS/stored
        size=0
        try:
            with open(dest,'wb') as out:
                while chunk:=await video.read(1024*1024):
                    size+=len(chunk)
                    if size>MAX_VIDEO_BYTES:
                        raise ValueError('Dosya 1 GB sınırını aşıyor.')
                    out.write(chunk)
        except Exception as e:
            dest.unlink(missing_ok=True)
            return json_error(str(e))
        if size<16:
            dest.unlink(missing_ok=True)
            return json_error('Video dosyası boş veya geçersiz.')
    try:
        with db() as conn:
            course=conn.execute('SELECT id FROM courses WHERE id=?',(course_id,)).fetchone()
            if not course:
                if stored:(UPLOADS/stored).unlink(missing_ok=True)
                return json_error('Eğitim bulunamadı.')
            pos=conn.execute('SELECT COALESCE(MAX(position),0)+1 FROM lessons WHERE course_id=?',(course_id,)).fetchone()[0]
            conn.execute('''INSERT INTO lessons(course_id,title,duration_label,position,is_preview,video_url,stored_filename)
                            VALUES(?,?,?,?,?,?,?)''',(course_id,title.strip(),duration_label.strip()[:30],pos,int(is_preview=='true'),video_url,stored))
    except Exception:
        if stored:(UPLOADS/stored).unlink(missing_ok=True)
        raise
    return {'ok':True}

@app.post('/api/admin/live')
async def add_live(request:Request):
    csrf(request)
    require_admin(request)
    obj=body_obj(await request.json())
    title=str(obj.get('title','')).strip()
    starts=str(obj.get('starts_at','')).strip()
    url=str(obj.get('meeting_url','')).strip()
    try:
        course_id=int(obj.get('course_id'))
        date=datetime.fromisoformat(starts)
    except (ValueError,TypeError): return json_error('Tarih veya kurs geçersiz.')
    if not title or not url.startswith('https://') or date.tzinfo is None:
        return json_error('Başlık, saat dilimli tarih ve HTTPS toplantı bağlantısı gerekiyor.')
    with db() as conn:
        if not conn.execute('SELECT 1 FROM courses WHERE id=?',(course_id,)).fetchone():
            return json_error('Eğitim bulunamadı.')
        conn.execute('INSERT INTO live_sessions(course_id,title,starts_at,meeting_url) VALUES(?,?,?,?)',
                     (course_id,title,starts,url))
    return {'ok':True}

@app.exception_handler(HTTPException)
async def http_exception_handler(request,exc):
    if request.url.path.startswith('/api/'):
        return json_error(exc.detail,exc.status_code)
    return JSONResponse({'error':exc.detail},status_code=exc.status_code)

@app.put('/api/admin/courses/{course_id}')
async def update_course(course_id:int,request:Request):
    csrf(request)
    require_admin(request)
    obj=body_obj(await request.json())
    title=str(obj.get('title','')).strip()
    subtitle=str(obj.get('subtitle','')).strip()
    description=str(obj.get('description','')).strip()
    instructor=str(obj.get('instructor','')).strip()
    slug=re.sub(r'[^a-z0-9-]','-',str(obj.get('slug','')).lower().strip()).strip('-')
    try:
        category_id=int(obj.get('category_id'))
    except (ValueError,TypeError):
        return json_error('Kategori geçersiz.')
    if len(title)<4 or len(slug)<4 or len(description)<8:
        return json_error('Başlık, adres veya açıklama geçersiz.')
    with db() as conn:
        row=conn.execute('SELECT id FROM courses WHERE id=?',(course_id,)).fetchone()
        if not row: return json_error('Eğitim bulunamadı.',404)
        try:
            conn.execute('UPDATE courses SET category_id=?,slug=?,title=?,subtitle=?,description=?,instructor=? WHERE id=?',
                         (category_id,slug,title,subtitle,description,instructor,course_id))
        except sqlite3.IntegrityError:
            return json_error('Eğitim adresi kullanımda veya kategori geçersiz.')
    return {'ok':True}

@app.post('/api/admin/courses/{course_id}/visibility')
async def toggle_visibility(course_id:int,request:Request):
    csrf(request)
    require_admin(request)
    with db() as conn:
        row=conn.execute('SELECT published FROM courses WHERE id=?',(course_id,)).fetchone()
        if not row: return json_error('Eğitim bulunamadı.',404)
        new_value=0 if row['published'] else 1
        conn.execute('UPDATE courses SET published=? WHERE id=?',(new_value,course_id))
    return {'ok':True,'published':bool(new_value)}


@app.post('/api/admin/orders/{order_id}/reject')
async def reject_order(order_id:int, request:Request):
    csrf(request)
    require_admin(request)
    with db() as conn:
        row=conn.execute('SELECT status FROM orders WHERE id=?',(order_id,)).fetchone()
        if not row:
            return json_error('Başvuru bulunamadı.',404)
        if row['status']!='pending':
            return json_error('Yalnızca bekleyen başvurular iptal edilebilir.')
        conn.execute("UPDATE orders SET status='rejected' WHERE id=?",(order_id,))
    return {'ok':True}

@app.delete('/api/admin/lessons/{lesson_id}')
async def delete_lesson(lesson_id:int,request:Request):
    csrf(request)
    require_admin(request)
    with db() as conn:
        lesson=conn.execute('SELECT stored_filename FROM lessons WHERE id=?',(lesson_id,)).fetchone()
        if not lesson: return json_error('Ders bulunamadı.',404)
        conn.execute('DELETE FROM progress WHERE lesson_id=?',(lesson_id,))
        conn.execute('DELETE FROM lessons WHERE id=?',(lesson_id,))
        filename=lesson['stored_filename']
    if filename:
        with db() as conn:
            used=conn.execute('SELECT 1 FROM lessons WHERE stored_filename=?',(filename,)).fetchone()
        if not used: (UPLOADS/Path(filename).name).unlink(missing_ok=True)
    return {'ok':True}


@app.post('/api/admin/enrollments/{enrollment_id}/revoke')
async def revoke_enrollment(enrollment_id:int,request:Request):
    csrf(request)
    require_admin(request)
    with db() as conn:
        cur=conn.execute('DELETE FROM enrollments WHERE id=?',(enrollment_id,))
        if not cur.rowcount: return json_error('Erişim kaydı bulunamadı.',404)
    return {'ok':True}
