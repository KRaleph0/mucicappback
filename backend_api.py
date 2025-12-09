import os
import requests
import base64
import oracledb
import re
from difflib import SequenceMatcher
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, g, Response
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash

# --- 1. 설정 ---
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
KOBIS_API_KEY = os.getenv("KOBIS_API_KEY")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")

SPOTIFY_auth_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"
KOBIS_BOXOFFICE_URL = "http://www.kobis.or.kr/kobisopenapi/webservice/rest/boxoffice/searchDailyBoxOfficeList.json"
KOBIS_MOVIE_INFO_URL = "http://www.kobis.or.kr/kobisopenapi/webservice/rest/movie/searchMovieInfo.json"
KOBIS_MOVIE_LIST_URL = "http://www.kobis.or.kr/kobisopenapi/webservice/rest/movie/searchMovieList.json"

if not all([SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, KOBIS_API_KEY, TMDB_API_KEY]):
    print("🚨 API 키 설정 누락!")

DB_USER = os.getenv("DB_USER", "admin")
DB_PASSWORD = os.getenv("DB_PASSWORD", "password")
DB_DSN = os.getenv("DB_DSN", "ordb.mirinea.org:1521/XEPDB1")
PITCH_CLASS = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']

app = Flask(__name__)
CORS(app)

# DB 연결
try:
    db_pool = oracledb.create_pool(user=DB_USER, password=DB_PASSWORD, dsn=DB_DSN, min=1, max=5)
    print("[DB] Oracle Pool 생성 완료.")
except Exception as e:
    print(f"[DB 오류] {e}")
    db_pool = None

def get_db_connection():
    if not db_pool: raise Exception("DB 풀 없음")
    if 'db' not in g: g.db = db_pool.acquire()
    return g.db

@app.teardown_appcontext
def close_db(e):
    db = g.pop('db', None)
    if db: db.close()

# --- 2. 헬퍼 함수 ---
def clean_text(text):
    if not text: return ""
    text = text.lower()
    patterns = [r'\(.*?ost.*?\)', r'original motion picture soundtrack', r'soundtrack', r'ost']
    for pat in patterns: text = re.sub(pat, '', text)
    text = re.sub(r'[^a-z0-9가-힣\s]', ' ', text)
    return ' '.join(text.split())

def get_similarity(a, b):
    return SequenceMatcher(None, clean_text(a), clean_text(b)).ratio()

def get_spotify_headers():
    if not SPOTIFY_CLIENT_ID: raise Exception("Spotify Key Missing")
    auth_str = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()
    res = requests.post(SPOTIFY_auth_URL, headers={'Authorization': f'Basic {b64_auth}', 'Content-Type': 'application/x-www-form-urlencoded'}, data={'grant_type': 'client_credentials'})
    if res.status_code != 200: raise Exception(f"Auth Failed: {res.status_code}")
    return {'Authorization': f'Bearer {res.json().get("access_token")}'}

def ms_to_iso_duration(ms):
    if not ms: return "PT0M0S"
    s = int((ms/1000)%60); m = int((ms/(1000*60))%60)
    return f"PT{m}M{s}S"

def extract_spotify_id(url):
    if len(url) == 22 and re.match(r'^[a-zA-Z0-9]+$', url): return url
    match = re.search(r'track/([a-zA-Z0-9]{22})', url)
    return match.group(1) if match else None

# --- 3. KOBIS 조회 ---
def get_kobis_metadata(movie_name):
    params = {'key': KOBIS_API_KEY, 'movieNm': movie_name}
    try:
        res = requests.get(KOBIS_MOVIE_LIST_URL, params=params).json()
        mlist = res.get('movieListResult', {}).get('movieList', [])
        if mlist:
            t = mlist[0]
            return (t.get('genreAlt', '').split(',') if t.get('genreAlt') else []), t.get('movieNmEn', ''), t.get('movieNmOg', '')
        return [], "", ""
    except: return [], "", ""

# --- 4. Spotify 검색 ---
def find_best_track(titles, headers):
    candidates = []
    seen = set()
    for t in titles:
        if t and t not in seen: candidates.append(t); seen.add(t)
    for title in candidates:
        try:
            res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, params={"q": f"{title} ost", "type": "track", "limit": 5, "market": "KR"}).json()
            for track in res.get('tracks', {}).get('items', []):
                sim = max(get_similarity(title, track['name']), get_similarity(title, track['album']['name']))
                if sim >= 0.5: return track
        except: pass
    return None

# --- 5. 트랙 저장 ---
def save_track_details(track_id, cursor, headers, genres=[]):
    if not track_id: return None
    try:
        t_res = requests.get(f"{SPOTIFY_API_BASE}/tracks/{track_id}", headers=headers)
        if t_res.status_code != 200: return None
        t_data = t_res.json()
        a_res = requests.get(f"{SPOTIFY_API_BASE}/audio-features/{track_id}", headers=headers)
        a_data = a_res.json() if a_res.status_code == 200 else {}

        title = t_data.get('name', 'Unknown')
        artist = t_data['artists'][0]['name'] if t_data.get('artists') else 'Unknown'
        prev = t_data.get('preview_url', '')
        aid = t_data.get('album', {}).get('id')
        img = t_data.get('album', {}).get('images', [{}])[0].get('url', '')
        bpm = a_data.get('tempo', 0); k_int = a_data.get('key', -1); dur = ms_to_iso_duration(t_data.get('duration_ms', 0))
        mkey = PITCH_CLASS[k_int] if 0 <= k_int < 12 else 'Unknown'

        if aid:
            cursor.execute("MERGE INTO ALBUMS USING dual ON (album_id=:1) WHEN NOT MATCHED THEN INSERT (album_id, album_cover_url) VALUES (:1, :2)", [aid, img])
        
        cursor.execute("""
            MERGE INTO TRACKS t USING dual ON (t.track_id=:tid)
            WHEN MATCHED THEN UPDATE SET t.bpm=:bpm, t.music_key=:mkey, t.duration=:dur, t.image_url=:img
            WHEN NOT MATCHED THEN INSERT (track_id, track_title, artist_name, album_id, preview_url, image_url, bpm, music_key, duration)
            VALUES (:tid, :title, :artist, :aid, :prev, :img, :bpm, :mkey, :dur)
        """, {'tid':track_id, 'title':title, 'artist':artist, 'aid':aid, 'prev':prev, 'img':img, 'bpm':bpm, 'mkey':mkey, 'dur':dur})

        tags = set(["tag:Spotify"])
        if genres: tags.add("tag:MovieOST")
        e = a_data.get('energy', 0); v = a_data.get('valence', 0)
        if e>0.7: tags.add('tag:Exciting')
        if e<0.4: tags.add('tag:Rest')
        if v<0.3: tags.add('tag:Sentimental')
        if v>0.7: tags.add('tag:Pop')
        
        g_map = {"액션":"tag:Action", "SF":"tag:SF", "코미디":"tag:Exciting", "드라마":"tag:Sentimental", "멜로":"tag:Romance", "로맨스":"tag:Romance", "공포":"tag:Tension", "호러":"tag:Tension", "스릴러":"tag:Tension", "범죄":"tag:Tension", "애니메이션":"tag:Animation", "가족":"tag:Rest", "뮤지컬":"tag:Pop"}
        for g in genres:
            for k,val in g_map.items(): 
                if k in g: tags.add(val)
        
        for t in tags:
            try: cursor.execute("MERGE INTO TRACK_TAGS USING dual ON (track_id=:1 AND tag_id=:2) WHEN NOT MATCHED THEN INSERT (track_id, tag_id) VALUES (:1, :2)", [track_id, t])
            except: pass
        
        cursor.connection.commit()
        return t_data
    except Exception as e:
        print(f"Track Save Error: {e}")
        return None

# --- 6. Auth API (회원가입/로그인) ---
@app.route('/api/auth/signup', methods=['POST'])
def api_signup():
    d = request.json
    uid, pw, nick = d.get('id'), d.get('password'), d.get('nickname', 'User')
    if not uid or not pw: return jsonify({"error": "Missing fields"}), 400
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT user_id FROM USERS WHERE user_id=:1", [uid])
        if cur.fetchone(): return jsonify({"error": "ID already exists"}), 409
        
        # 기본 role은 'user'
        cur.execute("INSERT INTO USERS (user_id, password, nickname, role) VALUES (:1, :2, :3, 'user')", [uid, generate_password_hash(pw), nick])
        conn.commit()
        return jsonify({"message": "Signup successful"})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    d = request.json
    uid, pw = d.get('id'), d.get('password')
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT user_id, password, nickname, profile_img, role FROM USERS WHERE user_id=:1", [uid])
        u = cur.fetchone()
        if u and check_password_hash(u[1], pw):
            return jsonify({"message": "Login success", "user": {"id": u[0], "nickname": u[2], "profile_img": u[3], "role": u[4]}})
        return jsonify({"error": "Invalid credentials"}), 401
    except Exception as e: return jsonify({"error": str(e)}), 500

# [NEW] 관리자 로그 조회 API
@app.route('/api/admin/logs', methods=['POST'])
def api_get_admin_logs():
    d = request.json
    uid = d.get('user_id') # 현재 로그인한 유저 ID
    try:
        conn = get_db_connection(); cur = conn.cursor()
        # 관리자 여부 확인
        cur.execute("SELECT role FROM USERS WHERE user_id=:1", [uid])
        row = cur.fetchone()
        
        if not row or row[0] != 'admin':
            return jsonify({"error": "관리자 권한이 필요합니다."}), 403
            
        cur.execute("SELECT target_id, previous_value, new_value, user_id, created_at, user_ip FROM MODIFICATION_LOGS ORDER BY created_at DESC FETCH FIRST 50 ROWS ONLY")
        logs = []
        for r in cur.fetchall():
            logs.append({
                "movie": r[0], "old": r[1], "new": r[2], 
                "user": r[3], "date": r[4].strftime("%Y-%m-%d %H:%M"), "ip": r[5]
            })
        return jsonify(logs)
    except Exception as e: return jsonify({"error": str(e)}), 500

# --- 7. 영화 업데이트 (OST 수정) ---
@app.route('/api/movie/<movie_id>/update-ost', methods=['POST'])
def api_update_movie_ost(movie_id):
    d = request.json
    link = d.get('spotifyUrl')
    uid = d.get('user_id', 'Guest') # 로그인한 유저 ID (없으면 Guest)
    ip = request.remote_addr
    
    if not link: return jsonify({"error": "Link required"}), 400

    try:
        conn = get_db_connection(); cur = conn.cursor(); headers = get_spotify_headers()
        
        # ID 디코딩
        real_mid = movie_id
        try:
            if movie_id.endswith('_ost'): movie_id = movie_id[:-4]
            pad = len(movie_id)%4; 
            if pad: movie_id += '='*(4-pad)
            dec = base64.urlsafe_b64decode(movie_id).decode('utf-8')
            cur.execute("SELECT count(*) FROM MOVIES WHERE movie_id=:1", [dec])
            if cur.fetchone()[0]>0: real_mid = dec
        except: pass

        tid = extract_spotify_id(link)
        if not tid: return jsonify({"error": "Invalid Link"}), 400
        
        res = save_track_details(tid, cur, headers, [])
        if not res: return jsonify({"error": "Track not found"}), 404

        cur.execute("SELECT track_id FROM MOVIE_OSTS WHERE movie_id=:1", [real_mid])
        prev = cur.fetchone(); prev_id = prev[0] if prev else "NONE"

        cur.execute("DELETE FROM MOVIE_OSTS WHERE movie_id=:1", [real_mid])
        cur.execute("INSERT INTO MOVIE_OSTS (movie_id, track_id) VALUES (:1, :2)", [real_mid, tid])
        
        # 로그 저장 (user_id 포함)
        cur.execute("INSERT INTO MODIFICATION_LOGS (target_type, target_id, action_type, previous_value, new_value, user_ip, user_id) VALUES ('MOVIE_OST', :1, 'UPDATE', :2, :3, :4, :5)", [real_mid, prev_id, tid, ip, uid])
        
        conn.commit()
        return jsonify({"message": "Updated", "new_track": res['name']})
    except Exception as e: return jsonify({"error": str(e)}), 500

# --- 기타 API (기존 유지) ---
@app.route('/api/spotify-token', methods=['GET'])
def api_get_token():
    try: return jsonify({"access_token": get_spotify_headers()['Authorization'].split(' ')[1]})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/search', methods=['GET'])
def api_search():
    try: 
        res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=get_spotify_headers(), params={"q": request.args.get('q'), "type": request.args.get('type', 'track'), "limit": 20, "market": "KR"})
        return jsonify(res.json()), res.status_code
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/track/<tid>', methods=['GET'])
def api_track(tid):
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT track_title, artist_name, image_url, bpm, music_key, duration FROM TRACKS WHERE track_id=:1", [tid])
        r = cur.fetchone()
        if r and r[3]: return jsonify({"id":tid, "title":r[0], "artist":r[1], "image":r[2], "bpm":r[3], "key":r[4], "duration":r[5], "source":"DB"})
        
        save_track_details(tid, cur, get_spotify_headers(), [])
        cur.execute("SELECT track_title, artist_name, image_url, bpm, music_key, duration FROM TRACKS WHERE track_id=:1", [tid])
        r = cur.fetchone()
        return jsonify({"id":tid, "title":r[0], "artist":r[1], "image":r[2], "bpm":r[3], "key":r[4], "duration":r[5], "source":"API"})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/admin/update-movies', methods=['POST'])
def api_adm_update(): return jsonify({"message": update_box_office_data()})

@app.route('/api/data/box-office.ttl', methods=['GET'])
def get_ttl():
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("""
            SELECT m.movie_id, m.title, m.rank, m.poster_url, t.track_id, t.track_title, t.artist_name, t.preview_url, a.album_cover_url
            FROM MOVIES m LEFT JOIN MOVIE_OSTS mo ON m.movie_id=mo.movie_id LEFT JOIN TRACKS t ON mo.track_id=t.track_id LEFT JOIN ALBUMS a ON t.album_id=a.album_id
            WHERE m.rank<=10 ORDER BY m.rank ASC
        """)
        rows = cur.fetchall()
        ttl = "@prefix schema: <http://schema.org/> .\n@prefix komc: <https://knowledgemap.kr/komc/def/> .\n@prefix tag: <https://knowledgemap.kr/komc/def/tag/> .\n"
        tcur = conn.cursor()
        for r in rows:
            mid, mt, rk, mp, tid, tt, ar, pr, cov = r
            m_uri = base64.urlsafe_b64encode(mid.encode()).decode().rstrip("=")
            mp = mp or "img/playlist-placeholder.png"; cov = cov or "img/playlist-placeholder.png"
            tt = tt or "OST 정보 없음"; ar = ar or "-"
            
            tags = ""
            if tid:
                tcur.execute("SELECT tag_id FROM TRACK_TAGS WHERE track_id=:1", [tid])
                tl = [x[0].replace('tag:', '') for x in tcur.fetchall()]
                if tl: tags = f"    komc:relatedTag tag:{', tag:'.join(tl)} ;"
            
            t_uri = tid if tid else f"{m_uri}_ost"
            ttl += f"""
<https://knowledgemap.kr/komc/resource/movie/{m_uri}> a schema:Movie ; schema:name "{mt}" ; schema:image "{mp}" ; komc:rank {rk} .
<https://knowledgemap.kr/komc/resource/track/{t_uri}> a schema:MusicRecording ; schema:name "{tt}" ; schema:byArtist "{ar}" ; schema:image "{cov}" ; schema:audio "{pr or ''}" ; komc:featuredIn <https://knowledgemap.kr/komc/resource/movie/{m_uri}> ;
{tags}
    schema:genre "Movie Soundtrack" .
""" 
        return Response(ttl, mimetype='text/turtle')
    except Exception as e: return f"# Error: {e}", 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)